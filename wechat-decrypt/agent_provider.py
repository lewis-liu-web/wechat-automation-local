#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent provider protocol and Hermes CLI implementation.

This module is the provider boundary for deep WeChat jobs. It does not enqueue
or send WeChat messages; callers pass a job payload and receive a structured
result that can be post-checked and sent by the product layer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple


_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


try:
    from reply_engine import postcheck  # type: ignore
except Exception:  # pragma: no cover - keeps provider importable in isolation
    def postcheck(text: str) -> str:
        return (text or "").strip()

try:
    from skills import load_skill, render_skill
except ImportError:
    try:
        from skills import load_skill
        render_skill = None  # type: ignore
    except ImportError:
        load_skill = None  # type: ignore
        render_skill = None  # type: ignore
try:
    from reliable_pipeline import (  # type: ignore
        AgentResultContract,
        AgentResultContractError,
        parse_agent_result,
    )
except Exception:  # pragma: no cover - keep provider importable in isolation
    AgentResultContract = None  # type: ignore
    AgentResultContractError = None  # type: ignore
    parse_agent_result = None  # type: ignore

logger = logging.getLogger(__name__)


def _normalize_hermes_profile_id(profile: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(profile or "").strip()).strip("-._")
    return "hermes-%s" % (safe or "default")

DEFAULT_MAX_REPLY_CHARS = 600


def _resolve_max_reply_chars(payload: Dict[str, Any] | None, config: Dict[str, Any] | None = None) -> int:
    """Return the configured max reply length, with safe fallback.

    The value is sourced (in priority order) from:
      1. ``payload['max_reply_chars']`` - the value attached when reply_engine
         enqueued the job, so the agent prompt and the monitor-side truncation
         stay in sync.
      2. ``config['reply_engine']['max_reply_chars']`` - the runtime config
         loaded by the worker, useful when the payload key is missing.
      3. :data:`DEFAULT_MAX_REPLY_CHARS` (600) for any missing/invalid value.
    """
    candidates: List[Any] = []
    if isinstance(payload, dict):
        candidates.append(payload.get("max_reply_chars"))
    if isinstance(config, dict):
        cfg_re = config.get("reply_engine")
        if isinstance(cfg_re, dict):
            candidates.append(cfg_re.get("max_reply_chars"))
    for raw in candidates:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return DEFAULT_MAX_REPLY_CHARS


def parse_hermes_profile_list(output: str) -> List[Dict[str, Any]]:
    """Parse `hermes profile list` table output into provider instance dicts."""
    profiles: List[Dict[str, Any]] = []
    for raw in str(output or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Profile") or line.startswith("─"):
            continue
        active = line.startswith("◆")
        if active:
            line = line[1:].strip()
        parts = line.split()
        if len(parts) < 2:
            continue
        profile = parts[0].strip()
        model = parts[1].strip()
        gateway_status = parts[2].strip() if len(parts) >= 3 else ""
        alias = parts[3].strip() if len(parts) >= 4 else ""
        if alias == "—":
            alias = ""
        label = alias or profile
        profiles.append({
            "id": _normalize_hermes_profile_id(profile),
            "label": label,
            "provider": "hermes",
            "profile": profile,
            "worker_id": label,
            "model": model,
            "gateway_status": "" if gateway_status == "—" else gateway_status,
            "alias": alias,
            "active": active,
            "source": "discovered",
        })
    return profiles


def discover_hermes_profiles(cli_path: str = "hermes", timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Discover local Hermes profiles without mutating project config."""
    try:
        proc = subprocess.run(
            [str(cli_path or "hermes"), "profile", "list"],
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout or 5.0)),
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW_FLAGS,
        )
    except Exception as exc:
        logger.warning("failed to discover Hermes profiles: %s", exc)
        return []
    if proc.returncode != 0:
        logger.warning("hermes profile list failed rc=%s stderr=%s", proc.returncode, (proc.stderr or "")[:200])
        return []
    return parse_hermes_profile_list(proc.stdout or "")




def _strict_mode_available() -> bool:
    return parse_agent_result is not None and AgentResultContractError is not None


def _job_wants_strict(job: Dict[str, Any]) -> bool:
    """Return True if the job's payload opts in to the strict AgentResult contract."""
    if not isinstance(job, dict):
        return False
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return False
    flag = payload.get("reliable_result_contract")
    if isinstance(flag, str):
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return bool(flag)


_STRICT_AGENT_PROMPT_SUFFIX = (
    "\n\n[严格输出协议]\n"
    "本任务受可靠 AgentResult 协议约束。你必须只输出唯一一个 JSON 对象，且严格满足以下结构：\n"
    "  {\n"
    '    "schema_version": 1,\n'
    '    "action": "reply" | "silent" | "escalate",\n'
    '    "reply_text": "string (reply 时必填，其他 action 必须为空字符串)",\n'
    '    "reason_code": "string (机器可读原因码，silent/escalate 必填)",\n'
    '    "risk_level": "low" | "medium" | "high"\n'
    "  }\n"
    "硬规则：\n"
    "1. 不允许输出任何其它文本（不要 Markdown、不要 Box 边框、不要 ANSI、不要解释、不要 JSON 之外的自然语言）。\n"
    "2. schema_version 必须为 1；action 只接受 reply/silent/escalate 之一；仅允许上述 5 个字段。\n"
    "3. 任何不符合上述契约的输出（盒子、纯文本、工具调用提示、错误日志、Thinking 片段等）均视为失败。\n"
    "4. reply_text 在 silent/escalate 时必须留空字符串；reason_code 在 silent/escalate 时建议填写稳定短码。\n"
    "5. 在生成回复前，如有需要请使用工具 mcp__wechat_kb_search__search_knowledge 查询已授权知识库；\n"
    "   没有授权知识库或工具返回空时，基于已有上下文直接作答。\n"
    "请直接以 `{` 起首、JSON 对象结尾输出，不要包含前后叙述。"
)


def _strict_prompt_suffix() -> str:
    """Return the strict-mode tail appended to the legacy prompt. Pure additive."""
    return _STRICT_AGENT_PROMPT_SUFFIX




def _decode_captured_stream(value: Any) -> str:
    """Decode subprocess-captured stream chunks that may be bytes or str.

    ``subprocess.TimeoutExpired.stdout/stderr`` is documented as bytes
    even when ``text=True``; downstream JSON serialization would crash if
    we did not coerce first.  Empty/None collapses to ``""``.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("utf-8", errors="replace")
    return str(value)


def _strict_parse_stdout(stdout: str):
    """Parse the entire stdout as an AgentResult document.

    Strict mode does NOT extract, clean, or fallback-search embedded JSON
    objects.  The model is required to emit exactly one JSON object, so
    ``json.loads`` on the whole stdout is the only legitimate validator.
    Leading/trailing whitespace is tolerated by ``json.loads``; any box
    frame, ANSI sequence, log line, or extra prose makes the document
    unparseable and the run fails.  Re-raises
    :class:`AgentResultContractError` when the parse succeeds but the
    document does not satisfy the AgentResult contract.
    """
    return parse_agent_result(str(stdout or ""))


def _strict_status_for_action(action: str) -> str:
    """Map a validated contract action to its explicit terminal state."""
    if action == "silent":
        return "silent"
    if action == "escalate":
        return "escalated"
    return "done"


def _strict_failed_result(error: str, *, latency: float, raw: Dict[str, Any] | None,
                          provider: str, worker_id: str,
                          status: str = "failed") -> "AgentResult":
    """Build a deterministic failure AgentResult for the strict path."""
    return AgentResult(False, status, error=error, latency=latency,
                       provider=provider, worker_id=worker_id, raw=raw)


@dataclass
class ProviderHealth:
    ok: bool
    provider: str
    ready: bool = False
    error: str = ""
    workers: int = 0
    raw: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerStatus:
    worker_id: str
    provider: str
    status: str = "unknown"
    current_job_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentResult:
    ok: bool
    status: str
    reply_text: str = ""
    error: str = ""
    latency: float = 0.0
    provider: str = ""
    worker_id: str = ""
    raw: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AgentProvider(Protocol):
    name: str

    def health(self) -> ProviderHealth:
        ...

    def list_workers(self) -> List[WorkerStatus]:
        ...

    def run(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        ...

    def submit(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        """M5 async: submit job to external agent and return session info immediately."""
        ...

    def poll(self, external_session_id: str, external_user_msg_id: int,
             timeout: float | None = None) -> AgentResult:
        """M5 async: poll external agent for completion."""
        ...

    def cancel(self, job_id: str) -> bool:
        ...


def _json_safe(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes {len(value)}>"
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _http_json(method: str, url: str, body: Dict[str, Any] | None = None,
               timeout: float = 30) -> Dict[str, Any] | None:
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp_body = resp.read().decode("utf-8", errors="replace")
        if not resp_body:
            return {}
        parsed = json.loads(resp_body)
        return parsed if isinstance(parsed, dict) else {"value": parsed}


def _last_assistant_message(messages: List[Dict[str, Any]], min_id: int = 0) -> Dict[str, Any] | None:
    for msg in reversed(messages or []):
        try:
            msg_id = int(msg.get("id") or 0)
        except Exception:
            msg_id = 0
        if msg.get("role") == "assistant" and msg_id > min_id:
            return msg
    return None


def _build_wechat_deep_prompt(job: Dict[str, Any]) -> str:
    raw_payload = job.get("payload")
    payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    max_reply_chars = _resolve_max_reply_chars(payload)
    agent_mode = str(payload.get("agent_mode") or "").lower()
    # Prefer clean_text only in tool_agent mode; for raw_agent/standard keep prompt
    # first so aggregator context and monitor-side retrieval are preserved.
    if agent_mode == "tool_agent":
        prompt = str(payload.get("clean_text") or payload.get("prompt") or job.get("prompt") or payload.get("raw_text") or "").strip()
    else:
        prompt = str(payload.get("prompt") or payload.get("clean_text") or payload.get("raw_text") or job.get("prompt") or "").strip()
    sender = str(job.get("sender") or payload.get("mention_name") or "").strip().lstrip("@")
    mention_rule = f"最终回复建议以 @{sender} + 空格 开头。" if sender else "最终回复直接输出适合微信群发送的一段话。"
    image_paths = [str(p) for p in (payload.get("image_paths") or []) if p]
    image_descriptions = payload.get("image_descriptions") or []
    image_hint = ""
    if image_descriptions:
        parts = []
        for item in image_descriptions:
            if isinstance(item, dict):
                path = str(item.get("path") or "")
                desc = str(item.get("description") or "").strip()
                if desc:
                    parts.append(f"- {path}: {desc}" if path else f"- {desc}")
        if parts:
            image_hint = "\n\n微信侧已调用视觉工具识别图片，结果如下：\n" + "\n".join(parts)
    if image_paths and not image_descriptions:
        # Only expose raw local paths when no description is available.  In thin
        # monitor mode the WeChat side does not pre-describe images; the agent is
        # expected to use its native multimodal provider or the decode_image MCP
        # tool to understand the picture.
        path_hint = (
            "\n\n用户随消息发送了图片，本地解密路径如下：\n"
            + "\n".join(f"- {p}" for p in image_paths)
            + "\n如果当前 agent 配置有多模态 provider，请直接读取上述图片文件理解内容。"
            "decode_image(chat_name, local_id) 只用于从历史消息重新解密图片，它会返回本地路径而非图片内容，不能替代视觉理解。"
            "若无多模态能力且路径不可读，不要编造图片内容，请说明无法判断图片。"
        )
        image_hint = f"{image_hint}{path_hint}"

    # --- Knowledge hits / tool injection (legacy tool_agent) and MCP search_knowledge (strict) ---
    _KB_MAX_HITS = 5
    _KB_DEFAULT_HIT_MAX_CHARS = 4000
    _KB_TOTAL_BUDGET_CHARS = 12000
    available_tools = payload.get("available_tools") or []
    is_tool_agent = agent_mode == "tool_agent"
    knowledge_hits = payload.get("knowledge_hits") or []
    enable_wechat_mcp_tool = bool(payload.get("enable_wechat_mcp_tool") or False)
    knowledge_section = ""
    kb_tool_instruction = ""
    if _job_wants_strict(job):
        # Strict contract path: rely on the MCP ``search_knowledge`` tool the agent invokes.
        allowed_kb_ids = payload.get("allowed_kb_ids") or []
        if allowed_kb_ids:
            kb_tool_instruction = (
                f"\n\n[知识库工具]\n"
                f"当前已授权知识库：{', '.join(str(k) for k in allowed_kb_ids)}。\n"
                "当需要基于知识库回答时，请调用工具 mcp__wechat_kb_search__search_knowledge(query, limit)。\n"
                "没有授权知识库或工具返回空时，基于已有上下文直接作答，不要编造。"
            )
    elif is_tool_agent:
        max_tool_rounds = max(0, int(payload.get("max_tool_rounds") or 2))
        tools_desc = []
        for tool in available_tools or [{"name": "leann_search", "description": "本地 LEANN 语义搜索工具"}]:
            name = str(tool.get("name") or "leann_search")
            desc = str(tool.get("description") or "本地 LEANN 语义搜索工具")
            tools_desc.append(f"- {name}: {desc}")
        tool_schema_parts: List[str] = []
        for tool in available_tools or [{"name": "leann_search", "description": "本地 LEANN 语义搜索工具"}]:
            name = str(tool.get("name") or "")
            if name == "leann_search":
                allowed = tool.get("allowed_index_names") or []
                default = str(tool.get("default_index_name") or "").strip()
                index_hint = ""
                if allowed:
                    index_hint = "\n  当前目标允许使用的 index_name: %s。" % ", ".join(allowed)
                    if default:
                        index_hint += "\n  如果省略 index_name，默认使用: %s。" % default
                    index_hint += "\n  禁止搜索未列出的索引。"
                tool_schema_parts.append(
                    "leann_search(index_name, query, top_k)\n"
                    "  参数:\n"
                    "    index_name: 知识库索引名称（字符串）\n"
                    "    query: 搜索查询（字符串）\n"
                    "    top_k: 返回结果数量（整数，默认 3）\n"
                    "  输出: JSON 数组，每个元素包含 title/content 等字段。\n"
                    "  调用方式: 在任意一行单独输出 JSON 对象:\n"
                    '    {"tool": "leann_search", "index_name": "your_index", "query": "your query", "top_k": 3}\n'
                    "  系统会执行该工具并把结果追加到对话中，你可以继续推理。"
                    f"{index_hint}"
                )
        tool_schema = "\n\n".join(tool_schema_parts) + f"\n  最多允许 {max_tool_rounds} 轮工具调用。"
        if enable_wechat_mcp_tool:
            tool_schema += (
                "\n\n提示：如果 Hermes 已配置 WeChat MCP server，"
                "你还可以直接调用其中的消息/联系人查询工具。"
            )
        knowledge_section = (
            "\n\n[可用工具]\n"
            + ("\n".join(tools_desc) + "\n\n" if tools_desc else "")
            + tool_schema
            + "\n"
        )
    elif knowledge_hits:
        parts = []
        total_chars = 0
        for idx, hit in enumerate(knowledge_hits[:_KB_MAX_HITS], start=1):
            if isinstance(hit, dict):
                source = str(hit.get("source") or hit.get("kb_id") or "unknown")
                rel = str(hit.get("rel_path") or hit.get("path") or "").strip()
                origin = f"{source} {rel}".strip() if rel else source
                content = str(hit.get("content") or hit.get("body") or "").strip()
            elif isinstance(hit, (list, tuple)) and len(hit) >= 2:
                origin = "unknown"
                content = str(hit[1]).strip()
            else:
                origin = "unknown"
                content = str(hit).strip()
            per_hit_max = int(
                (hit.get("hit_max_chars") if isinstance(hit, dict) else None)
                or _KB_DEFAULT_HIT_MAX_CHARS
            )
            remaining = max(0, _KB_TOTAL_BUDGET_CHARS - total_chars)
            if remaining <= 0:
                break
            truncated = content[:min(per_hit_max, remaining)]
            total_chars += len(truncated)
            parts.append(f"### 片段 {idx} (来源: {origin})\n{truncated}")
        knowledge_section = "\n\n[知识库片段]\n" + "\n\n".join(parts) + "\n"

    # --- Skill injection ---
    skill_name = str(payload.get("skill_name") or "").strip()
    skill_prompt = str(payload.get("skill_prompt") or "").strip()
    skill_section = ""
    skill_rendered = False
    if skill_prompt:
        logger.warning("agent_provider: using legacy skill_prompt field; migrate to skill_name")
        skill_section = f"{skill_prompt}\n\n"
    elif skill_name:
        try:
            if render_skill is not None:
                skill_section = render_skill(
                    skill_name,
                    mention_name=sender,
                    user_request=prompt,
                    knowledge_hits=knowledge_section,
                    mode_instruction=str(payload.get("mode_instruction") or ""),
                )
                skill_rendered = True
            elif load_skill is not None:
                skill_section = load_skill(skill_name)
            else:
                skill_section = ""
        except Exception as exc:
            logger.warning("agent_provider: failed to load/render skill %s: %s", skill_name, exc)
            skill_section = ""
        if skill_section:
            skill_section = f"{skill_section.strip()}\n\n"

    if _job_wants_strict(job):
        output_rules = (
            "输出契约：仅输出一个符合下方严格协议的 JSON 对象；不要输出任何其它文本。\n"
        )
    else:
        output_rules = (
            "硬规则：\n"
            "1. 不要输出思考过程、工具日志、Markdown 计划或 JSON 外壳。\n"
            "2. 最终只输出一段微信群可直接发送的文本。\n"
        )
    # The legacy [SILENT] literal applies only when the model is expected to emit a freeform
    # reply (non-strict AND tool_agent).  Strict mode uses AgentResult actions
    # (silent / escalate) for the same intent.  The max_reply_chars rule above still
    # applies in strict mode so downstream sanitizers do not have to handle overflow.
    tool_output_rule = (
        "7. 如果你决定不回复，或用户消息只是闲聊/无意义内容，最终输出必须是字面量 [SILENT]，不要加任何解释。\n"
        if (is_tool_agent and not _job_wants_strict(job)) else ""
    )
    prompt_body = (
        f"{skill_section}"
        "你正在执行 wechat-deep-reply 任务。\n"
        "目标：处理一个微信群复杂请求，并只返回最终可发送到微信群的简短中文回复。\n"
        f"{output_rules}"
        "3. 如果涉及转账、密钥、密码、数据库、内部路径、授权/承诺/决策，回复需要本人确认。\n"
        "4. 不能读取、修改、删除、执行或以其他方式操作电脑本地文件、文件夹、系统命令、脚本、程序。\n"
        "5. 不确定就说不确定，不要编造事实。\n"
        f"6. 最多 {max_reply_chars} 字。超出部分会被系统强制截断。\n"
        f"{tool_output_rule}"
        f"{mention_rule}\n\n"
        f"用户请求：{prompt}"
        f"{'' if skill_rendered else knowledge_section}"
        f"{kb_tool_instruction}"
        f"{image_hint}"
    )
    if _job_wants_strict(job):
        prompt_body = f"{prompt_body}{_strict_prompt_suffix()}"
    return prompt_body


class EchoAgentProvider:
    """Deterministic local provider for worker smoke tests and dry-runs."""

    name = "echo"

    def __init__(self, worker_id: str = "echo-1") -> None:
        self.worker_id = worker_id

    def health(self) -> ProviderHealth:
        return ProviderHealth(ok=True, provider=self.name, ready=True, workers=1)

    def list_workers(self) -> List[WorkerStatus]:
        return [WorkerStatus(worker_id=self.worker_id, provider=self.name, status="idle")]

    def run(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        t0 = time.time()
        raw_payload = job.get("payload")
        payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        sender = str(job.get("sender") or payload.get("mention_name") or "").strip().lstrip("@")
        prompt = str(payload.get("prompt") or payload.get("clean_text") or payload.get("raw_text") or job.get("task_type") or "").strip()
        prefix = f"@{sender} " if sender else ""
        reply = postcheck(f"{prefix}已收到测试任务：{prompt[:80] or 'deep job'}")
        return AgentResult(True, "done", reply_text=reply, latency=time.time() - t0,
                           provider=self.name, worker_id=self.worker_id)

    def submit(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        """Echo provider: submit is a no-op, returns a fake session id."""
        return AgentResult(True, "submitted", reply_text="",
                           latency=0.0, provider=self.name, worker_id=self.worker_id,
                           raw={"bridge_session_id": f"echo-{int(time.time()*1000)}",
                                "bridge_user_msg_id": 0})

    def poll(self, external_session_id: str, external_user_msg_id: int,
             timeout: float | None = None) -> AgentResult:
        """Echo provider: poll always returns done with a synthetic reply."""
        return AgentResult(True, "done",
                           reply_text="[echo] task acknowledged",
                           latency=0.0, provider=self.name, worker_id=self.worker_id,
                           raw={"bridge_session_id": external_session_id,
                                "bridge_user_msg_id": external_user_msg_id})

    def cancel(self, job_id: str) -> bool:
        return False


class HermesProvider:
    """Provider backed by hermes-agent's `hermes chat` CLI one-shot invocation.

    Hermes has no HTTP bridge. Instead we spawn `hermes chat -q "<prompt>"`
    and read the final reply from stdout. The profile is selected via the
    `HERMES_HOME` environment variable, which is hermes's profile-aware home
    directory. Each worker process binds to one profile so multiple instances
    can run in parallel without clobbering each other's memory, sessions, or
    toolsets.
    """
    def __init__(self,
                 cli_path: str = "hermes",
                 hermes_home: str | None = None,
                 profile: str | None = None,
                 model: str | None = None,
                 skill: str | None = None,
                 toolsets: str | None = None,
                 worker_id: str = "hermes-1",
                 extra_args: Optional[List[str]] = None,
                 max_tool_rounds: int = 2,
                 leann_cli_path: str = "leann") -> None:
        self.cli_path = str(cli_path or "hermes")
        self.hermes_home = (str(hermes_home) if hermes_home else None) or os.environ.get("HERMES_HOME")
        self.profile = (str(profile) if profile else None) or os.environ.get("HERMES_PROFILE")
        self.model = (str(model) if model else None) or os.environ.get("HERMES_MODEL")
        self.skill = (str(skill) if skill else None) or os.environ.get("HERMES_SKILL")
        self.toolsets = (str(toolsets) if toolsets else None) or os.environ.get("HERMES_TOOLSETS")
        self.worker_id = worker_id
        self.name = "hermes"
        self.extra_args = list(extra_args or [])
        self.max_tool_rounds = max(0, int(max_tool_rounds or 2))
        self.leann_cli_path = str(leann_cli_path or "leann")

    def _build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.hermes_home:
            env["HERMES_HOME"] = self.hermes_home
        if self.profile:
            env["HERMES_PROFILE"] = self.profile
        return env

    def _prepare_model_job(self, job: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Return a model-safe job copy and the env for this run.

        Internal fields (keys starting with ``_``) are stripped from the payload
        shown to the model, and pre-fetched ``knowledge_hits`` are dropped so
        that the model must use the ``search_knowledge`` MCP tool instead.  The
        env is enriched with per-job MCP-server authorization variables.
        """
        job_copy = dict(job)
        raw_payload = job_copy.get("payload")
        inner_payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

        config_path = inner_payload.get("_config_path")
        raw_kb_ids = inner_payload.get("_allowed_kb_ids")
        allowed_kb_ids = (
            [str(x) for x in raw_kb_ids if isinstance(x, (str, int))]
            if isinstance(raw_kb_ids, list)
            else []
        )
        target = inner_payload.get("target") or {}
        target_id = str(
            inner_payload.get("target_id")
            or target.get("name")
            or target.get("username")
            or ""
        )
        wiki_dir = inner_payload.get("wiki_dir")

        model_payload = {
            k: _json_safe(v)
            for k, v in inner_payload.items()
            if not k.startswith("_") and k != "knowledge_hits"
        }
        model_payload["allowed_kb_ids"] = allowed_kb_ids

        model_job = dict(job_copy)
        model_job["payload"] = model_payload

        env = self._build_env()
        for key in (
            "WECHAT_MCP_CONFIG",
            "WECHAT_MCP_ALLOWED_KB_IDS",
            "WECHAT_MCP_TARGET_ID",
            "WECHAT_MCP_WIKI_DIR",
        ):
            env.pop(key, None)
        env["WECHAT_MCP_ALLOWED_KB_IDS"] = json.dumps(list(allowed_kb_ids))
        if config_path:
            env["WECHAT_MCP_CONFIG"] = str(config_path)
        if target_id:
            env["WECHAT_MCP_TARGET_ID"] = target_id
        if wiki_dir:
            env["WECHAT_MCP_WIKI_DIR"] = str(wiki_dir)

        return model_job, env

    def _args(self, prompt_path: str | None = None) -> List[str]:
        """Build Hermes CLI args.
        
        On Windows, subprocess.run(input=...) does not reliably deliver stdin
        to Hermes.  We use -q @file.txt syntax which Hermes reads directly.
        """
        args = [self.cli_path]
        if self.profile:
            args += ["--profile", self.profile]
        args += ["chat"]
        if prompt_path:
            args += ["-q", f"@{prompt_path}"]
        else:
            args += ["-q", "-"]
        # Non-interactive / embedded mode: suppress TUI/spinner and auto-accept hooks
        args += ["-Q", "--source", "tool", "--accept-hooks"]
        if self.model:
            args += ["-m", self.model]
        if self.skill:
            args += ["-s", self.skill]
        if self.toolsets:
            args += ["-t", self.toolsets]
        args += self.extra_args
        return args

    def health(self) -> ProviderHealth:
        import shutil
        try:
            resolved = shutil.which(self.cli_path)
            if not resolved and os.path.isfile(self.cli_path):
                resolved = self.cli_path
            ready = bool(resolved)
            return ProviderHealth(ok=ready, provider=self.name, ready=ready,
                                  workers=1 if ready else 0,
                                  raw={"cli_path": self.cli_path, "resolved": resolved,
                                       "profile": self.profile, "hermes_home": self.hermes_home})
        except Exception as e:
            return ProviderHealth(ok=False, provider=self.name, ready=False,
                                  error=repr(e), workers=0, raw={"cli_path": self.cli_path})

    def list_workers(self) -> List[WorkerStatus]:
        h = self.health()
        status = "idle" if h.ok else "unavailable"
        return [WorkerStatus(worker_id=self.worker_id, provider=self.name, status=status)]

    def run(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        t0 = time.time()
        model_job, env = self._prepare_model_job(job)
        # Stage 2 Phase B: Hermes is strict-only. Force the contract flag even if
        # the incoming payload omits it, and run the single-shot strict path.
        payload = dict(model_job.get("payload") or {})
        payload["reliable_result_contract"] = True
        model_job["payload"] = payload
        prompt = _build_wechat_deep_prompt(model_job)
        full_prompt = (
            f"{prompt}\n\n"
            "[WECHAT_AGENT_JOB_JSON]\n"
            f"{json.dumps(_json_safe(model_job), ensure_ascii=False, indent=2)}\n"
            "[/WECHAT_AGENT_JOB_JSON]"
        )
        wait = max(5.0, float(timeout or job.get("timeout") or 90))

        # Write prompt to temp file and use -q @file syntax (Windows stdin workaround)
        import tempfile
        prompt_file: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(full_prompt)
                prompt_file = Path(f.name)

            run_kwargs: Dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": wait,
                "env": env,
                "encoding": "utf-8",
                "errors": "replace",
                "creationflags": _NO_WINDOW_FLAGS,
            }
            if hasattr(subprocess, "STARTUPINFO"):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                run_kwargs["startupinfo"] = startupinfo
            proc = subprocess.run(self._args(prompt_path=str(prompt_file)), **run_kwargs)
        except subprocess.TimeoutExpired as e:
            captured_stdout = _decode_captured_stream(e.stdout)
            captured_stderr = _decode_captured_stream(e.stderr)
            return _strict_failed_result(
                "hermes chat timed out (strict mode)",
                latency=time.time() - t0,
                raw={"cli_path": self.cli_path, "timeout": True,
                     "stdout": captured_stdout, "stderr": captured_stderr},
                provider=self.name, worker_id=self.worker_id,
            )
        except Exception as e:
            return _strict_failed_result(
                f"hermes runner error (strict mode): {e!r}",
                latency=time.time() - t0,
                raw={"error": repr(e)},
                provider=self.name, worker_id=self.worker_id,
            )
        finally:
            if prompt_file and prompt_file.exists():
                try:
                    prompt_file.unlink()
                except Exception:
                    pass

        return self._strict_finalize_run(
            proc=proc, stdout=(proc.stdout or "").strip(), stderr=(proc.stderr or "").strip(), t0=t0,
        )
    def _strict_finalize_run(self, *, proc, stdout: str, stderr: str, t0: float) -> "AgentResult":
        """Validate the synchronous run output against the AgentResult contract.
        Strict mode rejects everything that is not a parseable AgentResult
        JSON object.  Non-zero rc and non-empty stdout both count as
        failure — the contract says the model must speak JSON, and a
        non-zero exit usually means it crashed before doing so.  Empty
        stdout is also a failure.
        """
        latency = time.time() - t0
        rc = int(getattr(proc, "returncode", 0) or 0)
        base_raw: Dict[str, Any] = {
            "rc": rc,
            "stderr": stderr,
            "stdout": stdout,
        }
        if not _strict_mode_available():
            return _strict_failed_result(
                "reliable_pipeline unavailable; cannot validate strict AgentResult",
                latency=latency, raw=base_raw,
                provider=self.name, worker_id=self.worker_id,
            )
        if rc != 0 and not stdout.strip():
            return _strict_failed_result(
                f"hermes chat failed (rc={rc}): {stderr[:200] or 'no output'}",
                latency=latency,
                raw={**base_raw, "stage": "rc_nonzero_no_stdout"},
                provider=self.name, worker_id=self.worker_id,
            )
        if rc != 0:
            return _strict_failed_result(
                f"hermes chat returned rc={rc} but stdout did not validate as AgentResult",
                latency=latency,
                raw={**base_raw, "stage": "rc_nonzero_with_stdout"},
                provider=self.name, worker_id=self.worker_id,
            )
        if not stdout.strip():
            return _strict_failed_result(
                "hermes chat returned empty stdout; strict mode requires AgentResult JSON",
                latency=latency,
                raw={**base_raw, "stage": "empty_stdout"},
                provider=self.name, worker_id=self.worker_id,
            )
        # Strict mode treats the entire stdout as the candidate document.
        # Anything other than a single AgentResult JSON object fails.
        try:
            contract = _strict_parse_stdout(stdout)
        except AgentResultContractError as exc:
            return _strict_failed_result(
                f"AgentResult contract violation: {exc}",
                latency=latency,
                raw={**base_raw, "stage": "contract_violation"},
                provider=self.name, worker_id=self.worker_id,
            )
        return self._build_strict_agent_result(contract, latency=latency,
                                                extra_raw={"rc": rc, "stderr": stderr})

    def _build_strict_agent_result(self, contract, *, latency: float,
                                    extra_raw: Dict[str, Any] | None = None) -> "AgentResult":
        """Project a validated ``AgentResultContract`` into the legacy AgentResult."""
        action = getattr(contract, "action", "reply")
        reply_text = getattr(contract, "reply_text", "") if action == "reply" else ""
        reason_code = getattr(contract, "reason_code", "")
        risk_level = getattr(contract, "risk_level", "low")
        status = _strict_status_for_action(action)
        contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else None
        raw_payload: Dict[str, Any] = {
            "strict": True,
            "action": action,
            "reason_code": reason_code,
            "risk_level": risk_level,
            "schema_version": getattr(contract, "schema_version", 1),
            # Durable worker reads ``raw["agent_result"]``; mirror the same
            # payload there so downstream storage can persist the contract
            # without re-parsing the prompt or stdout.
            "agent_result": contract_dict,
            "contract": contract_dict,
        }
        if extra_raw:
            raw_payload.update({k: v for k, v in extra_raw.items() if k not in raw_payload})
        return AgentResult(
            ok=True,
            status=status,
            reply_text=reply_text,
            latency=latency,
            provider=self.name,
            worker_id=self.worker_id,
            raw=raw_payload,
        )

    def _strict_finalize_poll(self, *, data: Dict[str, Any], session_id: str) -> "AgentResult":
        """Validate the async runner payload against the AgentResult contract."""
        latency = float(data.get("latency") or 0)
        stdout = str(data.get("stdout") or "")
        stderr = str(data.get("stderr") or "")
        rc = int(data.get("rc") or 0)
        timed_out = bool(data.get("timeout"))
        base_raw: Dict[str, Any] = {
            "rc": rc,
            "stderr": stderr,
            "stdout": stdout,
            "bridge_session_id": session_id,
        }
        if not _strict_mode_available():
            return _strict_failed_result(
                "reliable_pipeline unavailable; cannot validate strict AgentResult",
                latency=latency, raw=base_raw,
                provider=self.name, worker_id=self.worker_id,
            )
        if timed_out:
            return _strict_failed_result(
                "hermes chat timed out (strict mode)",
                latency=latency,
                raw={**base_raw, "stage": "timeout"},
                provider=self.name, worker_id=self.worker_id,
            )
        if rc != 0 and not stdout.strip():
            return _strict_failed_result(
                f"hermes chat failed (rc={rc}): {stderr[:200] or 'no output'}",
                latency=latency,
                raw={**base_raw, "stage": "rc_nonzero_no_stdout"},
                provider=self.name, worker_id=self.worker_id,
            )
        if rc != 0:
            return _strict_failed_result(
                f"hermes chat returned rc={rc} but stdout did not validate as AgentResult",
                latency=latency,
                raw={**base_raw, "stage": "rc_nonzero_with_stdout"},
                provider=self.name, worker_id=self.worker_id,
            )
        if not stdout.strip():
            return _strict_failed_result(
                "hermes chat returned empty stdout; strict mode requires AgentResult JSON",
                latency=latency,
                raw={**base_raw, "stage": "empty_stdout"},
                provider=self.name, worker_id=self.worker_id,
            )
        # Strict mode treats the entire stdout as the candidate document.
        try:
            contract = _strict_parse_stdout(stdout)
        except AgentResultContractError as exc:
            return _strict_failed_result(
                f"AgentResult contract violation: {exc}",
                latency=latency,
                raw={**base_raw, "stage": "contract_violation"},
                provider=self.name, worker_id=self.worker_id,
            )
        return self._build_strict_agent_result(contract, latency=latency,
                                                extra_raw={"rc": rc, "stderr": stderr})


    def submit(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        """Start a Hermes CLI run in a background helper process.

        The product loop receives a session id immediately; poll() only checks
        for a result file, so Hermes cannot block the M5 reconciler thread.
        """
        t0 = time.time()
        job_id = int(job.get("id") or 0)
        session_id = f"hermes-{job_id}-{int(t0*1000)}" if job_id else f"hermes-{int(t0*1000)}"
        try:
            base = Path(self.hermes_home or os.getcwd()) / ".wechat_agent_jobs"
            session_dir = base / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            model_job, env = self._prepare_model_job(job)
            # Stage 2 Phase B: Hermes submit is strict-only. Force the contract
            # flag so the prompt instructs the model to emit AgentResult JSON.
            payload = dict(model_job.get("payload") or {})
            payload["reliable_result_contract"] = True
            model_job["payload"] = payload
            prompt = _build_wechat_deep_prompt(model_job)
            full_prompt = (
                f"{prompt}\n\n"
                "[WECHAT_AGENT_JOB_JSON]\n"
                f"{json.dumps(_json_safe(model_job), ensure_ascii=False, indent=2)}\n"
                "[/WECHAT_AGENT_JOB_JSON]"
            )
            prompt_path = session_dir / "prompt.txt"
            result_path = session_dir / "result.json"
            runner_path = session_dir / "runner.py"
            prompt_path.write_text(full_prompt, encoding="utf-8")
            wait = max(5.0, float(timeout or job.get("timeout") or 240))
            runner_payload = {
                "args": self._args(prompt_path=str(prompt_path)),
                "env": env,
                "prompt_path": str(prompt_path),
                "result_path": str(result_path),
                "timeout": wait,
                "cwd": os.getcwd(),
            }
            runner_path.write_text(
                "import json, pathlib, subprocess, time\n"
                "def _decode(value):\n"
                "    if value is None:\n"
                "        return ''\n"
                "    if isinstance(value, (bytes, bytearray)):\n"
                "        try:\n"
                "            return value.decode('utf-8')\n"
                "        except Exception:\n"
                "            return value.decode('utf-8', errors='replace')\n"
                "    return value\n"
                "cfg=json.loads(pathlib.Path(__file__).with_name('runner_config.json').read_text(encoding='utf-8'))\n"
                "flags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
                "t0=time.time()\n"
                "out=None\n"
                "try:\n"
                "    p=subprocess.run(cfg['args'], capture_output=True, text=True, timeout=float(cfg['timeout']), env=cfg.get('env'), encoding='utf-8', errors='replace', creationflags=flags)\n"
                "    out={'ok': True, 'rc': p.returncode, 'stdout': _decode(p.stdout), 'stderr': _decode(p.stderr), 'latency': time.time()-t0}\n"
                "except subprocess.TimeoutExpired as e:\n"
                "    out={'ok': False, 'timeout': True, 'stdout': _decode(e.stdout), 'stderr': _decode(e.stderr), 'latency': time.time()-t0}\n"
                "except Exception as e:\n"
                "    out={'ok': False, 'error': repr(e), 'latency': time.time()-t0}\n"
                "finally:\n"
                "    if out is None:\n"
                "        out={'ok': False, 'error': 'unknown runner error'}\n"
                "    pathlib.Path(cfg['result_path']).write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')\n",
                encoding="utf-8",
            )
            (session_dir / "runner_config.json").write_text(json.dumps(runner_payload, ensure_ascii=False), encoding="utf-8")
            exe = Path(sys.executable).with_name("pythonw.exe")
            if not exe.exists():
                exe = Path(sys.executable)
            proc = subprocess.Popen([str(exe), str(runner_path)], cwd=str(session_dir),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=_NO_WINDOW_FLAGS)
            (session_dir / "meta.json").write_text(json.dumps({
                "pid": proc.pid,
                "started_at": t0,
                "deadline_at": t0 + wait,
                "result_path": str(result_path),
                "profile": self.profile,
                "model": self.model,
                "strict": True,
            }, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)
        return AgentResult(True, "submitted", reply_text="",
                            latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                           raw={"bridge_session_id": session_id, "bridge_user_msg_id": 1,
                                "result_dir": str(session_dir)})

    def poll(self, external_session_id: str, external_user_msg_id: int,
             timeout: float | None = None) -> AgentResult:
        """Poll the background Hermes helper result file without blocking.

        Stage 2 Phase B: Hermes is strict-only.  The result file is parsed as
        a whole-stdout AgentResult document.  Legacy jobs that still have
        ``strict: false`` in their meta are expected to have been terminal-
        expired by the explicit migration control before cutover; poll no
        longer extracts free-form display text.
        """
        t0 = time.time()
        session_id = str(external_session_id or "").strip()
        base = Path(self.hermes_home or os.getcwd()) / ".wechat_agent_jobs"
        session_dir = base / session_id
        meta_path = session_dir / "meta.json"
        result_path = session_dir / "result.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            if meta.get("strict") is False:
                return _strict_failed_result(
                    "legacy protocol job not migrated",
                    latency=time.time() - t0,
                    raw={"bridge_session_id": session_id, "stage": "legacy_not_migrated"},
                    provider=self.name, worker_id=self.worker_id,
                )
            if not result_path.exists():
                if meta.get("deadline_at") and time.time() > float(meta.get("deadline_at") or 0):
                    return _strict_failed_result(
                        "hermes async timed out (strict mode)",
                        latency=time.time() - t0,
                        raw={"bridge_session_id": session_id, "timeout": True},
                        provider=self.name, worker_id=self.worker_id,
                    )
                return AgentResult(False, "running", reply_text="",
                                   latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                                   raw={"bridge_session_id": session_id})
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            return AgentResult(False, "failed", error="poll failed", latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id,
                               raw={"bridge_session_id": session_id})
        return self._strict_finalize_poll(data=data, session_id=session_id)

    def cancel(self, job_id: str) -> bool:
        return False


def preflight_hermes_profile(
    prompt: str,
    *,
    cli_path: str = "hermes",
    profile: str | None = None,
    hermes_home: str | None = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Isolated dry-run helper for a Hermes profile.

    Does not accept a live job, does not enqueue, and does not persist provider
    stdout. It executes a supplied Hermes profile only when deliberately invoked,
    parses the whole stdout against the strict AgentResult contract, and returns
    only safe metadata.
    """
    t0 = time.time()
    if not _strict_mode_available():
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "strict contract validator unavailable",
            "stage": "validator_unavailable",
        }

    provider = HermesProvider(
        cli_path=cli_path,
        profile=profile,
        hermes_home=hermes_home,
        worker_id="preflight",
    )
    job: Dict[str, Any] = {
        "payload": {
            "reliable_result_contract": True,
            "allowed_kb_ids": [],
            "prompt": str(prompt or "").strip(),
            "clean_text": str(prompt or "").strip(),
        }
    }
    prompt_text = _build_wechat_deep_prompt(job)
    full_prompt = (
        f"{prompt_text}\n\n"
        "[WECHAT_AGENT_JOB_JSON]\n"
        f"{json.dumps(_json_safe(job), ensure_ascii=False, indent=2)}\n"
        "[/WECHAT_AGENT_JOB_JSON]"
    )

    import tempfile
    prompt_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(full_prompt)
            prompt_file = Path(f.name)

        run_kwargs: Dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": max(1.0, float(timeout)),
            "env": provider._build_env(),
            "encoding": "utf-8",
            "errors": "replace",
            "creationflags": _NO_WINDOW_FLAGS,
        }
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            run_kwargs["startupinfo"] = startupinfo
        proc = subprocess.run(provider._args(prompt_path=str(prompt_file)), **run_kwargs)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "hermes chat timed out",
            "stage": "timeout",
        }
    except Exception:
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "hermes runner error",
            "stage": "runner_error",
        }
    finally:
        if prompt_file and prompt_file.exists():
            try:
                prompt_file.unlink()
            except Exception:
                pass

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    rc = proc.returncode
    if rc != 0 and not stdout:
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "hermes chat failed",
            "stage": "rc_nonzero_no_stdout",
        }
    if rc != 0:
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "hermes chat returned nonzero exit",
            "stage": "rc_nonzero_with_stdout",
        }
    if not stdout:
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "hermes chat returned empty stdout",
            "stage": "empty_stdout",
        }
    try:
        contract = _strict_parse_stdout(stdout)
    except AgentResultContractError:
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "AgentResult contract violation",
            "stage": "contract_violation",
        }
    return {
        "ok": True,
        "status": "pass",
        "action": getattr(contract, "action", None),
        "schema_version": getattr(contract, "schema_version", 1),
        "reason": "preflight_ok",
        "stage": "parsed",
        "latency": round(time.time() - t0, 3),
    }


def provider_from_config(config: Dict[str, Any] | None = None,
                         instance_id: str | None = None) -> AgentProvider:
    """Build a provider. If `instance_id` is given, look up that instance in
    `agent_provider.instances`; otherwise fall back to the legacy single
    provider under `agent_provider.default` for backward compatibility.
    """
    cfg = config or {}
    raw_provider_cfg = cfg.get("agent_provider")
    provider_cfg: Dict[str, Any] = raw_provider_cfg if isinstance(raw_provider_cfg, dict) else {}
    raw_providers = provider_cfg.get("providers")
    providers: Dict[str, Any] = raw_providers if isinstance(raw_providers, dict) else {}
    raw_instances = provider_cfg.get("instances")
    instances: List[Dict[str, Any]] = raw_instances if isinstance(raw_instances, list) else []
    default_name = str(provider_cfg.get("default") or "hermes").lower()

    chosen: Dict[str, Any] | None = None
    if instance_id:
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            if str(inst.get("id") or "").strip() == instance_id:
                chosen = inst
                break
        if chosen is None:
            raise ValueError(f"unknown agent_provider instance: {instance_id}")

    if chosen is not None:
        kind = str(chosen.get("provider") or chosen.get("type") or "hermes").lower()
        worker_id = str(chosen.get("worker_id") or chosen.get("id") or kind)
        if kind == "echo":
            return EchoAgentProvider(worker_id=worker_id)
        if kind == "hermes":
            cfg_h = chosen if isinstance(chosen, dict) else {}
            return HermesProvider(
                cli_path=str(cfg_h.get("cli_path") or "hermes"),
                hermes_home=cfg_h.get("hermes_home"),
                profile=cfg_h.get("profile"),
                model=cfg_h.get("model"),
                skill=cfg_h.get("skill"),
                toolsets=cfg_h.get("toolsets"),
                worker_id=worker_id,
                extra_args=cfg_h.get("extra_args") or [],
                max_tool_rounds=cfg_h.get("max_tool_rounds", 2),
                leann_cli_path=cfg_h.get("leann_cli_path", "leann"),
            )
        raise ValueError(f"unsupported provider: {kind}")

    # Legacy single-provider path used by older callers.
    if default_name == "echo":
        return EchoAgentProvider()
    if default_name == "hermes":
        cfg_h = providers.get("hermes") or {}
        return HermesProvider(
            cli_path=str(cfg_h.get("cli_path") or "hermes"),
            hermes_home=cfg_h.get("hermes_home"),
            profile=cfg_h.get("profile"),
            model=cfg_h.get("model"),
            skill=cfg_h.get("skill"),
            toolsets=cfg_h.get("toolsets"),
            worker_id=str(cfg_h.get("worker_id") or "hermes-1"),
            extra_args=cfg_h.get("extra_args") or [],
            max_tool_rounds=cfg_h.get("max_tool_rounds", 2),
            leann_cli_path=cfg_h.get("leann_cli_path", "leann"),
        )
    raise ValueError(f"unsupported provider for M1: {default_name}")


def list_agent_instances(config: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    cfg = config or {}
    raw_provider_cfg = cfg.get("agent_provider")
    provider_cfg: Dict[str, Any] = raw_provider_cfg if isinstance(raw_provider_cfg, dict) else {}
    raw_instances = provider_cfg.get("instances")
    instances: List[Dict[str, Any]] = raw_instances if isinstance(raw_instances, list) else []
    return [inst for inst in instances if isinstance(inst, dict)]


if __name__ == "__main__":
    provider = HermesProvider()
    print(json.dumps(provider.health().to_dict(), ensure_ascii=False, indent=2, default=str))
