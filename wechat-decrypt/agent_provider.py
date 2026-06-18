#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent provider protocol and first GenericAgent bridge implementation.

This module is the provider boundary for deep WeChat jobs.  It does not enqueue
or send WeChat messages; callers pass a job payload and receive a structured
result that can be post-checked and sent by the product layer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol


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

logger = logging.getLogger(__name__)

import re

_NOISE_LINE_PATTERNS = [
    re.compile(r"^Initializing\b", re.IGNORECASE),
    re.compile(r"^Query:"),
    re.compile(r"^preparing\b", re.IGNORECASE),
    re.compile(r"^\s*read\b", re.IGNORECASE),
    re.compile(r"^\s*find\b", re.IGNORECASE),
    re.compile(r"^[─╭╮╰╯│\s]+$"),
    re.compile(r"resume session", re.IGNORECASE),
]


def _clean_agent_output(text: str) -> str:
    """Return only sendable lines from agent stdout, dropping tool/init noise."""
    text = str(text or "").strip()
    if not text:
        return ""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    lines = text.splitlines()
    useful: List[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if any(p.search(line) for p in _NOISE_LINE_PATTERNS):
            continue
        useful.append(line)
    if not useful:
        return ""
    return "\n".join(useful)


def _extract_hermes_reply(stdout: str) -> str:
    """Extract the actual reply from Hermes terminal output.

    Strips the Hermes box frame, ANSI codes, tool hints. Uses the last
    non-empty Hermes box because Hermes may emit intermediate reasoning
    boxes before the final reply box.
    """
    text = str(stdout or "").strip()
    hermes_reply = ""
    boxes = list(re.finditer(r"╭─ ⚕ Hermes ─+╮(.*?)╰─+╯", text, re.DOTALL))
    if boxes:
        last_box = boxes[-1]
        box_content = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", last_box.group(1))
        lines = [line.strip().strip("│").strip() for line in box_content.split("\n") if line.strip()]
        hermes_reply = "\n".join(line for line in lines if line)
    if not hermes_reply:
        hermes_reply = _clean_agent_output(text)
    return postcheck(hermes_reply)


def _read_hermes_reply_file(session_dir: Path) -> str:
    """Read final reply from response.txt or reply.txt if Hermes wrote it there."""
    for filename in ("response.txt", "reply.txt"):
        try:
            reply_path = session_dir / filename
            if reply_path.exists():
                return reply_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""




def _trim_reply_json_block(text: str) -> str:
    """Remove trailing JSON object from the reply, including an optional 'json' label.

    Kept for backward compatibility with old skill prompts that still ask for JSON.
    """
    text = str(text or "").rstrip()
    if not text:
        return ""
    lines = text.splitlines()
    cut_index = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "json":
            if i + 1 < len(lines) and lines[i + 1].strip().startswith("{"):
                cut_index = i
                break
        elif stripped.startswith("{") and stripped.endswith("}"):
            try:
                json.loads(stripped)
                cut_index = i
                break
            except Exception:
                pass
    if cut_index is not None:
        trimmed = "\n".join(lines[:cut_index]).rstrip()
        return trimmed
    return text






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


def _sendable_reply_from_assistant(content: str) -> str:
    return postcheck(_clean_agent_output(str(content or "").strip()))


def _build_wechat_deep_prompt(job: Dict[str, Any]) -> str:
    raw_payload = job.get("payload")
    payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    prompt = str(payload.get("prompt") or job.get("prompt") or payload.get("clean_text") or payload.get("raw_text") or "").strip()
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
    if image_paths:
        path_hint = (
            "\n\n用户随消息发送了图片，本地解码路径如下：\n"
            + "\n".join(f"- {p}" for p in image_paths)
            + "\n如果当前 agent 不能直接读取这些图片文件，不要编造图片内容；请说明需要视觉工具读取图片。"
        )
        image_hint = f"{image_hint}{path_hint}"

    # --- Knowledge hits injection ---
    _KB_MAX_HITS = 5
    _KB_DEFAULT_HIT_MAX_CHARS = 4000
    _KB_TOTAL_BUDGET_CHARS = 12000
    knowledge_hits = payload.get("knowledge_hits") or []
    knowledge_section = ""
    if knowledge_hits:
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

    return (
        f"{skill_section}"
        "你正在执行 wechat-deep-reply 任务。\n"
        "目标：处理一个微信群复杂请求，并只返回最终可发送到微信群的简短中文回复。\n"
        "硬规则：\n"
        "1. 不要输出思考过程、工具日志、Markdown 计划或 JSON 外壳。\n"
        "2. 最终只输出一段微信群可直接发送的文本。\n"
        "3. 如果涉及转账、密钥、密码、数据库、内部路径、授权/承诺/决策，回复需要本人确认。\n"
        "4. 不能读取、修改、删除、执行或以其他方式操作电脑本地文件、文件夹、系统命令、脚本、程序。\n"
        "5. 不确定就说不确定，不要编造事实。\n"
        "6. 最多 600 字。\n"
        f"{mention_rule}\n\n"
        f"用户请求：{prompt}"
        f"{'' if skill_rendered else knowledge_section}"
        f"{image_hint}"
    )


class GenericAgentProvider:
    """Provider backed by GenericAgent's desktop_bridge.py HTTP API."""

    name = "genericagent"

    def __init__(self, bridge_url: str = "http://127.0.0.1:14168",
                 bridge_cwd: str | None = None,
                 session_id: str | None = None,
                 poll_interval: float = 1.0,
                 worker_id: str = "genericagent-bridge-1") -> None:
        self.bridge_url = str(bridge_url or "http://127.0.0.1:14168").rstrip("/")
        self.bridge_cwd = bridge_cwd
        self.session_id = (session_id or "").strip()
        self.poll_interval = max(0.2, float(poll_interval or 1.0))
        self.worker_id = worker_id

    def health(self) -> ProviderHealth:
        try:
            data = _http_json("GET", f"{self.bridge_url}/status", None, timeout=5) or {}
            ready = bool(data.get("ready"))
            return ProviderHealth(ok=ready, provider=self.name, ready=ready,
                                  workers=1 if ready else 0, raw=data)
        except Exception as e:
            return ProviderHealth(ok=False, provider=self.name, ready=False, error=repr(e), workers=0)

    def list_workers(self) -> List[WorkerStatus]:
        h = self.health()
        status = "idle" if h.ok else "unavailable"
        return [WorkerStatus(worker_id=self.worker_id, provider=self.name, status=status)]

    def _ensure_session(self, timeout: float) -> str:
        if self.session_id:
            return self.session_id
        body = {"cwd": self.bridge_cwd} if self.bridge_cwd else {}
        created = _http_json("POST", f"{self.bridge_url}/session/new", body, timeout=timeout) or {}
        sid = str(created.get("sessionId") or "")
        if not sid:
            raise RuntimeError("GenericAgent bridge did not return sessionId")
        return sid

    def run(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        t0 = time.time()
        max_wait = max(1.0, float(timeout or job.get("timeout") or 90))
        raw_payload = job.get("payload")
        payload_for_timeout = raw_payload if isinstance(raw_payload, dict) else {}
        if payload_for_timeout.get("image_paths"):
            max_wait = max(max_wait, 240.0)
        try:
            session_id = self._ensure_session(timeout=min(max_wait, 30))
            payload = dict(job)
            payload["payload"] = _json_safe(payload.get("payload") or {})
            prompt = _build_wechat_deep_prompt(payload)
            rich_prompt = (
                f"{prompt}\n\n"
                "[WECHAT_AGENT_JOB_JSON]\n"
                f"{json.dumps(_json_safe(payload), ensure_ascii=False, indent=2)}\n"
                "[/WECHAT_AGENT_JOB_JSON]"
            )
            submitted = _http_json(
                "POST",
                f"{self.bridge_url}/session/{urllib.parse.quote(session_id)}/prompt",
                {"prompt": rich_prompt},
                timeout=min(max_wait, 30),
            ) or {}
            if submitted.get("accepted") is not True:
                return AgentResult(False, "failed", error="GenericAgent bridge rejected job",
                                   latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                                   raw=submitted)
            try:
                user_msg_id = int(submitted.get("userMessageId") or submitted.get("seq") or 0)
            except Exception:
                user_msg_id = 0
            bridge_meta = {"bridge_session_id": session_id, "bridge_user_msg_id": user_msg_id}

            deadline = time.time() + max_wait
            last_data: Dict[str, Any] | None = None
            while time.time() < deadline:
                time.sleep(self.poll_interval)
                data = _http_json(
                    "GET",
                    f"{self.bridge_url}/session/{urllib.parse.quote(session_id)}/messages?after={user_msg_id}&limit=20",
                    None,
                    timeout=min(max_wait, 10),
                ) or {}
                last_data = data
                assistant_msg = _last_assistant_message(data.get("messages") or [], min_id=user_msg_id)
                if assistant_msg:
                    content = str(assistant_msg.get("content") or "").strip()
                    reply = postcheck(_clean_agent_output(content))
                    if not reply:
                        return AgentResult(False, "failed", error="agent returned no sendable reply",
                                            latency=time.time() - t0, provider=self.name,
                                           worker_id=self.worker_id, raw={**bridge_meta, "assistant": assistant_msg})
                    return AgentResult(True, "done", reply_text=reply, latency=time.time() - t0,
                                        provider=self.name, worker_id=self.worker_id,
                                       raw={**bridge_meta, "assistant": assistant_msg})
            return AgentResult(False, "timeout", error="agent timed out",
                                latency=time.time() - t0, provider=self.name,
                               worker_id=self.worker_id, raw={**bridge_meta, "last_data": last_data or {}})
        except (urllib.error.URLError, TimeoutError) as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)
        except Exception as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)

    def recover(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        """Recover a completed bridge reply for a job that timed out locally."""
        t0 = time.time()
        wait = max(1.0, float(timeout or 10))
        raw_payload = job.get("payload")
        payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        job_id = int(job.get("id") or 0)
        job_key = str(job.get("job_key") or "")
        session_id = str(payload.get("bridge_session_id") or "").strip()
        try:
            user_msg_id = int(payload.get("bridge_user_msg_id") or 0)
        except Exception:
            user_msg_id = 0

        try:
            if not session_id:
                sessions = _http_json("GET", f"{self.bridge_url}/sessions", None, timeout=wait) or {}
                for sess in sessions.get("sessions") or []:
                    sid = str(sess.get("sessionId") or sess.get("id") or "")
                    if not sid:
                        continue
                    data = _http_json(
                        "GET",
                        f"{self.bridge_url}/session/{urllib.parse.quote(sid)}/messages?limit=20",
                        None,
                        timeout=min(wait, 10),
                    ) or {}
                    for msg in data.get("messages") or []:
                        if msg.get("role") != "user":
                            continue
                        content = str(msg.get("content") or "")
                        if (job_id and f'"id": {job_id}' in content) or (job_key and job_key in content):
                            session_id = sid
                            try:
                                user_msg_id = int(msg.get("id") or user_msg_id or 0)
                            except Exception:
                                pass
                            break
                    if session_id:
                        break
            if not session_id:
                return AgentResult(False, "failed", error="bridge session not found", latency=time.time() - t0,
                                   provider=self.name, worker_id=self.worker_id)

            data = _http_json(
                "GET",
                f"{self.bridge_url}/session/{urllib.parse.quote(session_id)}/messages?limit=20",
                None,
                timeout=min(wait, 10),
            ) or {}
            assistant_msg = _last_assistant_message(data.get("messages") or [], min_id=user_msg_id)
            if not assistant_msg:
                return AgentResult(False, "failed", error="assistant reply not found", latency=time.time() - t0,
                                   provider=self.name, worker_id=self.worker_id,
                                   raw={"bridge_session_id": session_id, "bridge_user_msg_id": user_msg_id})
            reply = _sendable_reply_from_assistant(str(assistant_msg.get("content") or ""))
            if not reply:
                return AgentResult(False, "failed", error="no sendable recovered reply", latency=time.time() - t0,
                                   provider=self.name, worker_id=self.worker_id,
                                   raw={"bridge_session_id": session_id, "assistant": assistant_msg})
            return AgentResult(True, "done", reply_text=reply, latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id,
                               raw={"bridge_session_id": session_id, "bridge_user_msg_id": user_msg_id,
                                    "assistant": assistant_msg})
        except Exception as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)

    def submit(self, job: Dict[str, Any], timeout: float | None = None) -> AgentResult:
        """Submit a job to GenericAgent bridge and return immediately with session info.
        
        This is the first phase of M5 async flow: just submit the prompt, don't wait for result.
        Returns AgentResult with raw containing bridge_session_id and bridge_user_msg_id.
        """
        t0 = time.time()
        wait = max(1.0, float(timeout or 30))
        try:
            session_id = self._ensure_session(timeout=min(wait, 15))
            payload = dict(job)
            payload["payload"] = _json_safe(payload.get("payload") or {})
            prompt = _build_wechat_deep_prompt(payload)
            rich_prompt = (
                f"{prompt}\n\n"
                "[WECHAT_AGENT_JOB_JSON]\n"
                f"{json.dumps(_json_safe(payload), ensure_ascii=False, indent=2)}\n"
                "[/WECHAT_AGENT_JOB_JSON]"
            )
            submitted = _http_json(
                "POST",
                f"{self.bridge_url}/session/{urllib.parse.quote(session_id)}/prompt",
                {"prompt": rich_prompt},
                timeout=min(wait, 15),
            ) or {}
            if submitted.get("accepted") is not True:
                return AgentResult(False, "failed", error="GenericAgent bridge rejected job",
                                   latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                                   raw=submitted)
            try:
                user_msg_id = int(submitted.get("userMessageId") or submitted.get("seq") or 0)
            except Exception:
                user_msg_id = 0
            return AgentResult(True, "submitted", reply_text="",
                               latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                               raw={"bridge_session_id": session_id, "bridge_user_msg_id": user_msg_id})
        except (urllib.error.URLError, TimeoutError) as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)
        except Exception as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)

    def poll(self, external_session_id: str, external_user_msg_id: int,
             timeout: float | None = None) -> AgentResult:
        """Poll GenericAgent bridge for a completed assistant reply.
        
        This is the second phase of M5 async flow: check if the agent has finished.
        Returns AgentResult with status="done" if reply found, status="running" if still in progress.
        """
        t0 = time.time()
        wait = max(1.0, float(timeout or 10))
        session_id = str(external_session_id or "").strip()
        try:
            user_msg_id = int(external_user_msg_id or 0)
        except Exception:
            user_msg_id = 0
        if not session_id:
            return AgentResult(False, "failed", error="missing external_session_id",
                               latency=time.time() - t0, provider=self.name, worker_id=self.worker_id)
        try:
            data = _http_json(
                "GET",
                f"{self.bridge_url}/session/{urllib.parse.quote(session_id)}/messages?after={user_msg_id}&limit=20",
                None,
                timeout=min(wait, 10),
            ) or {}
            assistant_msg = _last_assistant_message(data.get("messages") or [], min_id=user_msg_id)
            if not assistant_msg:
                # Still running, no reply yet
                return AgentResult(False, "running", reply_text="",
                                   latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                                   raw={"bridge_session_id": session_id, "bridge_user_msg_id": user_msg_id})
            content = str(assistant_msg.get("content") or "").strip()
            reply = postcheck(_clean_agent_output(content))
            if not reply:
                return AgentResult(False, "failed", error="agent returned no sendable reply",
                                   latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                                   raw={"bridge_session_id": session_id, "bridge_user_msg_id": user_msg_id,
                                        "assistant": assistant_msg})
            return AgentResult(True, "done", reply_text=reply,
                               latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                               raw={"bridge_session_id": session_id, "bridge_user_msg_id": user_msg_id,
                                    "assistant": assistant_msg})
        except (urllib.error.URLError, TimeoutError) as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)
        except Exception as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id)

    def cancel(self, job_id: str) -> bool:
        # The bridge exposes session cancel, but M1 jobs do not own sessions yet.
        # Keep the provider protocol stable and add targeted cancellation when
        # async external job ids are introduced.
        return False


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

    Hermes has no GenericAgent-style HTTP bridge. Instead we spawn
    `hermes chat -q "<prompt>"` and read the final reply from stdout.
    The profile is selected via the `HERMES_HOME` environment variable, which
    is hermes's profile-aware home directory. Each worker process binds to
    one profile so multiple instances can run in parallel without clobbering
    each other's memory, sessions, or toolsets.
    """

    name = "hermes"

    def __init__(self,
                 cli_path: str = "hermes",
                 hermes_home: str | None = None,
                 profile: str | None = None,
                 model: str | None = None,
                 toolsets: str | None = None,
                 worker_id: str = "hermes-1",
                 extra_args: Optional[List[str]] = None) -> None:
        self.cli_path = str(cli_path or "hermes")
        self.hermes_home = (str(hermes_home) if hermes_home else None) or os.environ.get("HERMES_HOME")
        self.profile = (str(profile) if profile else None) or os.environ.get("HERMES_PROFILE")
        self.model = (str(model) if model else None) or os.environ.get("HERMES_MODEL")
        self.toolsets = (str(toolsets) if toolsets else None) or os.environ.get("HERMES_TOOLSETS")
        self.worker_id = worker_id
        self.extra_args = list(extra_args or [])

    def _build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.hermes_home:
            env["HERMES_HOME"] = self.hermes_home
        if self.profile:
            env["HERMES_PROFILE"] = self.profile
        return env

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
        if self.model:
            args += ["-m", self.model]
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
        payload = dict(job)
        payload["payload"] = _json_safe(payload.get("payload") or {})
        prompt = _build_wechat_deep_prompt(payload)
        full_prompt = (
            f"{prompt}\n\n"
            "[WECHAT_AGENT_JOB_JSON]\n"
            f"{json.dumps(_json_safe(payload), ensure_ascii=False, indent=2)}\n"
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
            
            proc = subprocess.run(
                self._args(prompt_path=str(prompt_file)),
                capture_output=True,
                text=True,
                timeout=wait,
                env=self._build_env(),
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW_FLAGS,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(False, "timeout", error="hermes chat timed out",
                               latency=time.time() - t0, provider=self.name,
                               worker_id=self.worker_id, raw={"cli_path": self.cli_path})
        except Exception as e:
            return AgentResult(False, "failed", error=repr(e),
                               latency=time.time() - t0, provider=self.name,
                               worker_id=self.worker_id)
        finally:
            if prompt_file and prompt_file.exists():
                try:
                    prompt_file.unlink()
                except Exception:
                    pass

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0 and not stdout:
            return AgentResult(False, "failed",
                               error=f"hermes chat failed (rc={proc.returncode}): {stderr[:200] or 'no output'}",
                               latency=time.time() - t0, provider=self.name,
                               worker_id=self.worker_id, raw={"stderr": stderr, "rc": proc.returncode})
        
        reply = _extract_hermes_reply(stdout)
        if not reply:
            return AgentResult(False, "failed", error="hermes returned no sendable reply",
                               latency=time.time() - t0, provider=self.name,
                               worker_id=self.worker_id, raw={"stdout": stdout, "stderr": stderr})
        return AgentResult(True, "done", reply_text=reply,
                           latency=time.time() - t0, provider=self.name,
                           worker_id=self.worker_id,
                           raw={"profile": self.profile, "model": self.model, "rc": proc.returncode})

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
            payload = dict(job)
            payload["payload"] = _json_safe(payload.get("payload") or {})
            prompt = _build_wechat_deep_prompt(payload)
            full_prompt = (
                f"{prompt}\n\n"
                "[WECHAT_AGENT_JOB_JSON]\n"
                f"{json.dumps(_json_safe(payload), ensure_ascii=False, indent=2)}\n"
                "[/WECHAT_AGENT_JOB_JSON]"
            )
            prompt_path = session_dir / "prompt.txt"
            result_path = session_dir / "result.json"
            runner_path = session_dir / "runner.py"
            prompt_path.write_text(full_prompt, encoding="utf-8")
            wait = max(5.0, float(timeout or job.get("timeout") or 240))
            runner_payload = {
                "args": self._args(prompt_path=str(prompt_path)),
                "env": self._build_env(),
                "prompt_path": str(prompt_path),
                "result_path": str(result_path),
                "timeout": wait,
                "cwd": os.getcwd(),
            }
            runner_path.write_text(
                "import json, pathlib, subprocess, time\n"
                "cfg=json.loads(pathlib.Path(__file__).with_name('runner_config.json').read_text(encoding='utf-8'))\n"
                "flags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
                "t0=time.time()\n"
                "out=None\n"
                "try:\n"
                "    p=subprocess.run(cfg['args'], capture_output=True, text=True, timeout=float(cfg['timeout']), env=cfg.get('env'), encoding='utf-8', errors='replace', creationflags=flags)\n"
                "    out={'ok': True, 'rc': p.returncode, 'stdout': p.stdout or '', 'stderr': p.stderr or '', 'latency': time.time()-t0}\n"
                "except subprocess.TimeoutExpired as e:\n"
                "    out={'ok': False, 'timeout': True, 'stdout': e.stdout or '', 'stderr': e.stderr or '', 'latency': time.time()-t0}\n"
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
        """Poll the background Hermes helper result file without blocking."""
        t0 = time.time()
        session_id = str(external_session_id or "").strip()
        base = Path(self.hermes_home or os.getcwd()) / ".wechat_agent_jobs"
        session_dir = base / session_id
        meta_path = session_dir / "meta.json"
        result_path = session_dir / "result.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            if not result_path.exists():
                if meta.get("deadline_at") and time.time() > float(meta.get("deadline_at") or 0):
                    return AgentResult(False, "timeout", error="hermes async timed out",
                                       latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                                       raw={"bridge_session_id": session_id})
                return AgentResult(False, "running", reply_text="",
                                   latency=time.time() - t0, provider=self.name, worker_id=self.worker_id,
                                   raw={"bridge_session_id": session_id})
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as e:
            return AgentResult(False, "failed", error=repr(e), latency=time.time() - t0,
                               provider=self.name, worker_id=self.worker_id,
                               raw={"bridge_session_id": session_id})
        stdout = str(data.get("stdout") or "").strip()
        stderr = str(data.get("stderr") or "").strip()
        rc = int(data.get("rc") or 0)
        # Helper: try to read response.txt/reply.txt when Hermes writes the final reply to a file
        # instead of printing it to stdout.
        def _read_reply_txt() -> str:
            return _read_hermes_reply_file(session_dir)

        def _trim_if_legacy(text: str) -> str:
            """Only trim JSON for legacy skill_prompt jobs that still ask for JSON."""
            return _trim_reply_json_block(text)

        def _extract(text: str) -> str:
            reply = _extract_hermes_reply(text)
            if not reply:
                reply = _read_reply_txt()
            return reply

        if data.get("timeout"):
            reply = _extract(stdout)
            if reply:
                # For legacy prompts that asked for JSON metadata, keep only the prose part.
                reply = _trim_if_legacy(reply)
                return AgentResult(True, "done", reply_text=reply,
                                   latency=float(data.get("latency") or 0), provider=self.name,
                                   worker_id=self.worker_id, raw=data)
            return AgentResult(False, "timeout", error="hermes chat timed out",
                               latency=float(data.get("latency") or 0), provider=self.name,
                               worker_id=self.worker_id, raw=data)
        if rc != 0 and not stdout:
            return AgentResult(False, "failed", error=f"hermes chat failed (rc={rc}): {stderr[:200] or 'no output'}",
                               latency=float(data.get("latency") or 0), provider=self.name,
                               worker_id=self.worker_id, raw=data)
        reply = _extract(stdout)
        if not reply:
            return AgentResult(False, "failed", error="hermes returned no sendable reply",
                               latency=float(data.get("latency") or 0), provider=self.name,
                               worker_id=self.worker_id, raw=data)
        # For legacy prompts that asked for JSON metadata, keep only the prose part.
        reply = _trim_if_legacy(reply)
        return AgentResult(True, "done", reply_text=reply,
                           latency=float(data.get("latency") or 0), provider=self.name,
                           worker_id=self.worker_id, raw=data)

    def cancel(self, job_id: str) -> bool:
        return False


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
    default_name = str(provider_cfg.get("default") or "genericagent").lower()

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
        kind = str(chosen.get("provider") or chosen.get("type") or "genericagent").lower()
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
                toolsets=cfg_h.get("toolsets"),
                worker_id=worker_id,
                extra_args=cfg_h.get("extra_args") or [],
            )
        if kind != "genericagent":
            raise ValueError(f"unsupported provider: {kind}")
        return GenericAgentProvider(
            bridge_url=str(chosen.get("bridge_url") or "http://127.0.0.1:14168"),
            bridge_cwd=chosen.get("bridge_cwd"),
            poll_interval=float(chosen.get("bridge_poll_interval") or 1.0),
            worker_id=worker_id,
        )

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
            toolsets=cfg_h.get("toolsets"),
            worker_id=str(cfg_h.get("worker_id") or "hermes-1"),
            extra_args=cfg_h.get("extra_args") or [],
        )
    if default_name != "genericagent":
        raise ValueError(f"unsupported provider for M1: {default_name}")
    raw_ga_cfg = providers.get("genericagent")
    ga_cfg: Dict[str, Any] = raw_ga_cfg if isinstance(raw_ga_cfg, dict) else {}
    raw_reply_engine = cfg.get("reply_engine")
    reply_engine: Dict[str, Any] = raw_reply_engine if isinstance(raw_reply_engine, dict) else {}
    return GenericAgentProvider(
        bridge_url=str(ga_cfg.get("bridge_url") or reply_engine.get("bridge_url") or "http://127.0.0.1:14168"),
        bridge_cwd=ga_cfg.get("bridge_cwd") or reply_engine.get("bridge_cwd"),
        poll_interval=float(ga_cfg.get("bridge_poll_interval") or reply_engine.get("bridge_poll_interval") or 1.0),
    )


def list_agent_instances(config: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    cfg = config or {}
    raw_provider_cfg = cfg.get("agent_provider")
    provider_cfg: Dict[str, Any] = raw_provider_cfg if isinstance(raw_provider_cfg, dict) else {}
    raw_instances = provider_cfg.get("instances")
    instances: List[Dict[str, Any]] = raw_instances if isinstance(raw_instances, list) else []
    return [inst for inst in instances if isinstance(inst, dict)]


if __name__ == "__main__":
    provider = GenericAgentProvider()
    print(json.dumps(provider.health().to_dict(), ensure_ascii=False, indent=2, default=str))
