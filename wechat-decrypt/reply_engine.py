#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local wiki + boundary reply engine for WeChat bot.

V1 is dependency-free and safe-by-default:
- local markdown wiki retrieval
- pre/post boundary checks
- pluggable LLM provider placeholder
- deterministic fallback when no provider is configured
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, Iterable, List, Tuple


DEFAULT_TRIGGERS = ["@飞扬的小助理", "飞扬的小助理", "小助理", "小助手"]
MAX_REPLY_CHARS = 300

HIGH_RISK_PATTERNS = [
    "转账", "付款", "打款", "收款码", "银行卡", "验证码", "密码", "密钥", "token", "api key",
    "登录", "删", "删除", "格式化", "改配置", "系统设置", "发文件", "聊天记录", "数据库",
    "内部日志", "路径", "keys.json", "忽略之前", "忽略以上", "绕过", "越权",
]
PROMISE_PATTERNS = [
    "你替飞扬", "你替扬叔", "代表飞扬", "代表扬叔", "承诺", "保证", "报价", "授权", "拍板", "决定",
]
HELP_PATTERNS = ["菜单", "帮助", "你能做什么", "功能", "怎么用"]
PING_PATTERNS = ["在吗", "在不在", "你好", "hello", "hi"]


@dataclass
class ReplyDecision:
    should_reply: bool
    reply_text: str
    intent: str = "unknown"
    risk_level: str = "low"
    need_human: bool = False
    reason: str = ""
    wiki_hits: List[str] | None = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("wiki_hits") is None:
            d["wiki_hits"] = []
        return d


def strip_triggers(text: str, triggers: Iterable[str] | None = None) -> str:
    text = text or ""
    triggers = list(triggers or DEFAULT_TRIGGERS)
    for trig in sorted(triggers, key=len, reverse=True):
        text = text.replace(trig, " ")
    text = re.sub(r"\s+", " ", text).strip(" ，,。:：\n\t")
    return text


def _contains_any(text: str, words: Iterable[str]) -> bool:
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


def load_wiki(base_dir: str | Path | None = None) -> List[Tuple[str, str]]:
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent / "wiki"
    docs: List[Tuple[str, str]] = []
    if not base.exists():
        return docs
    for p in sorted(base.rglob("*.md")):
        try:
            rel = str(p.relative_to(base)).replace("\\", "/")
            docs.append((rel, p.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            continue
    return docs


def retrieve_wiki(query: str, limit: int = 3, base_dir: str | Path | None = None) -> List[Tuple[str, str]]:
    docs = load_wiki(base_dir)
    return _rank_wiki_docs(query, docs, limit=limit)


def _rank_wiki_docs(query: str, docs: List[Tuple[str, str]], limit: int = 3) -> List[Tuple[str, str]]:
    if not docs:
        return []
    tokens = [t for t in re.split(r"[\s,，。！？!?:：；;、/\\]+", query or "") if len(t) >= 2]
    # Add useful Chinese substrings for short group messages.
    extra = ["小助手", "小助理", "帮助", "功能", "边界", "能做", "确认", "转达"]
    tokens += [t for t in extra if t in (query or "")]
    scored = []
    for rel, body in docs:
        hay = rel + "\n" + body
        score = 0
        for tok in set(tokens):
            score += hay.count(tok) * (3 if tok in rel else 1)
        if score:
            scored.append((score, rel, body))
    if not scored:
        # Core rules are always useful as grounding.
        scored = [(1, rel, body) for rel, body in docs if rel.startswith("core/")]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(rel, body[:1200]) for _, rel, body in scored[:limit]]


def _resolve_wiki_path(path_value: str | Path | None, default: Path | None = None) -> Path | None:
    if not path_value:
        return default
    p = Path(path_value)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parent / p


def retrieve_scoped_wiki(query: str, global_dir: str | Path | None = None,
                         group_dir: str | Path | None = None,
                         limit: int = 5) -> List[Tuple[str, str]]:
    """Retrieve global rules plus target-specific wiki without leaking other groups.

    Top-level wiki design:
    - wiki/core and wiki/local are shared by all groups.
    - target wiki_dir is only the group-specific overlay.
    """
    root = _resolve_wiki_path(global_dir, Path(__file__).resolve().parent / "wiki")
    docs: List[Tuple[str, str]] = []
    for sub in ("core", "local"):
        subdir = root / sub
        for rel, body in load_wiki(subdir):
            docs.append((f"{sub}/{rel}", body))
    gpath = _resolve_wiki_path(group_dir)
    if gpath:
        for rel, body in load_wiki(gpath):
            docs.append((f"group/{rel}", body))
    return _rank_wiki_docs(query, docs, limit=limit)


def precheck(user_text: str) -> ReplyDecision | None:
    if _contains_any(user_text, HIGH_RISK_PATTERNS):
        return ReplyDecision(
            True,
            "这个涉及敏感或高风险信息，我不能在群里直接处理，需要飞扬/扬叔确认。",
            intent="need_human",
            risk_level="high",
            need_human=True,
            reason="pre_boundary_high_risk",
        )
    if _contains_any(user_text, PROMISE_PATTERNS):
        return ReplyDecision(
            True,
            "这个需要飞扬/扬叔本人确认，我不能替他承诺、授权或做决定。",
            intent="need_human",
            risk_level="medium",
            need_human=True,
            reason="pre_boundary_promise",
        )
    return None


def postcheck(text: str) -> str:
    text = text or ""
    blocked = ["keys.json", "MINIMAX_API_KEY", "数据库", "内部日志", "自动化实现", "系统路径"]
    if _contains_any(text, blocked):
        return "这个问题我先收到啦，涉及内部信息或需要确认的内容，需要飞扬/扬叔确认后再处理。"
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS - 1].rstrip() + "…"
    return text


def _clean_agent_output(text: str) -> str:
    text = text or ""
    text = text.replace("[ROUND END]", "")
    text = re.sub(r"<summary>[\s\S]*?</summary>", "", text).strip()
    # Prefer the final non-empty prose line; discard obvious tool/code noise.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bad_prefix = ("LLM Running", "🛠️", "```", "<thinking>", "</thinking>", "<summary>")
    useful = [ln for ln in lines if not ln.startswith(bad_prefix) and not ln.startswith("{") and not ln.startswith("}")]
    return " ".join(useful[-3:]).strip() if useful else text.strip()


def _call_subagent_provider(prompt: str, config: Dict[str, Any]) -> str | None:
    if not config.get("use_subagent", False):
        return None
    code_root = Path(config.get("code_root") or (Path(__file__).resolve().parents[1]))
    agentmain = code_root / "agentmain.py"
    if not agentmain.exists():
        return None
    task_prefix = str(config.get("subagent_task_prefix") or "wechat_reply")
    task = f"{task_prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    llm_no = str(config.get("llm_no", 1))
    timeout = float(config.get("llm_timeout", 120))
    input_text = (
        "你是飞扬的小助理。请根据下面的群消息、本地wiki片段和边界约束，"
        "只输出一段可以直接发送到微信群的中文回复；不要解释过程，不要使用工具，不要写summary。\n\n"
        + prompt
    )
    try:
        cmd = [sys.executable, str(agentmain), "--task", task, "--input", input_text, "--llm_no", llm_no]
        proc = subprocess.Popen(
            cmd, cwd=str(code_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace"
        )
        out_path = code_root / "temp" / task / "output.txt"
        deadline = time.time() + timeout
        polled = False
        while time.time() < deadline:
            time.sleep(0.5)
            if out_path.exists():
                polled = True
                text = out_path.read_text(encoding="utf-8", errors="replace")
                if "[ROUND END]" in text:
                    proc.terminate()
                    proc.wait(timeout=5)
                    return _clean_agent_output(text)
        # timeout: gracefully terminate
        if not polled and proc.poll() is None:
            # maybe output not created yet, fallback to stdout on quick failure
            try:
                out, err = proc.communicate(timeout=10)
                return _clean_agent_output(out) if out and out.strip() else None
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if out_path.exists():
            text = out_path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return _clean_agent_output(text)
    except Exception:
        return None
    return None


def call_llm_provider(prompt: str, config: Dict[str, Any] | None = None) -> str | None:
    """Optional provider hook. Prefer GenericAgent subagent with llm_no=1 (minimax-anthropic)."""
    config = config or {}
    sub = _call_subagent_provider(prompt, config)
    if sub:
        return sub
    cmd = config.get("llm_provider_cmd") or os.environ.get("WECHAT_REPLY_LLM_CMD")
    if not cmd:
        return None
    if isinstance(cmd, str):
        cmd = cmd.split()
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=float(config.get("llm_timeout", 20)))
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        return None
    return None


def fallback_reply(clean_text: str, wiki_hits: List[Tuple[str, str]]) -> Tuple[str, str, bool]:
    text = clean_text or ""
    if not text:
        return "我在，有事可以直接说，我会尽量基于已有信息帮你整理或转达。", "smalltalk", False
    if _contains_any(text, HELP_PATTERNS):
        return "我可以在被叫到时，基于已有资料做简短说明、整理问题、回答常见问题；需要飞扬/扬叔本人判断的事，我会提示需要他确认。", "assistant_help", False
    if _contains_any(text, PING_PATTERNS):
        return "我在，有事可以直接说。", "smalltalk", False
    if wiki_hits:
        names = "、".join(rel for rel, _ in wiki_hits[:2])
        return f"我先按已有资料理解：这件事我可以帮你整理或说明；如果涉及决定、承诺或执行，还需要飞扬/扬叔确认。", "wiki_qa", False
    return "我先收到啦，这个问题需要结合更多背景，建议等飞扬/扬叔确认后再处理。", "need_human", True


def resolve_wiki_dir(config: Dict[str, Any], target: Dict[str, Any]) -> str:
    root = Path(__file__).resolve().parent
    value = target.get("wiki_dir")
    if not value:
        mapping = config.get("group_wiki_dirs") or {}
        for key in (target.get("name"), target.get("username"), target.get("table")):
            if key and key in mapping:
                value = mapping[key]
                break
    value = value or config.get("wiki_dir") or str(root / "wiki")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def build_prompt(raw_text: str, clean_text: str, wiki_hits: List[Tuple[str, str]], context_messages: list | None = None) -> str:
    wiki = "\n\n".join([f"## {rel}\n{body}" for rel, body in wiki_hits])
    ctx_lines = []
    if context_messages:
        for cm in context_messages:
            role = "用户" if cm.get('real_sender_id') and cm['real_sender_id'] != 2 else "我"
            sender = cm.get('real_sender_id') or ''
            ts = cm.get('create_time_local') or cm.get('create_time') or ''
            lid = cm.get('local_id') or ''
            content = cm.get('message_content', '')
            if content and isinstance(content, str):
                prefix = f"[{ts} #{lid} sender={sender}] " if (ts or lid or sender) else ""
                ctx_lines.append(f"{prefix}{role}: {content}")
    ctx_block = "\n".join(ctx_lines[-20:])
    return f"""你是飞扬的小助理，只在微信群中被明确叫到时回复。\n强边界：不能冒充飞扬/扬叔本人；不能替他承诺、授权、报价、决策或执行高风险操作；不能泄露密钥、系统路径、数据库、内部日志、自动化实现细节；知识库无依据时说明不确定。\n回复要求：简短、自然、适合微信群，最多{MAX_REPLY_CHARS}字。\n\n{('[群聊上下文]\n' + ctx_block + '\n\n') if ctx_lines else ''}[群消息]\n{raw_text}\n\n[清洗后问题]\n{clean_text}\n\n[本地wiki]\n{wiki}\n\n请只输出要发送到微信群的一段中文回复。"""


def generate_reply(message: Dict[str, Any] | str,
                   target: Dict[str, Any] | None = None,
                   config: Dict[str, Any] | None = None) -> ReplyDecision:
    config = config or {}
    target = target or {}
    raw_text = message if isinstance(message, str) else (message.get("content") or message.get("str_content") or message.get("message") or message.get("message_content") or "")
    triggers = target.get("triggers") or config.get("default_triggers") or DEFAULT_TRIGGERS
    clean = strip_triggers(raw_text, triggers)

    pre = precheck(clean or raw_text)
    if pre:
        return pre

    global_wiki_dir = config.get("wiki_dir") or str(Path(__file__).resolve().parent / "wiki")
    group_wiki_dir = resolve_wiki_dir(config, target)
    wiki_hits = retrieve_scoped_wiki(clean or raw_text, global_dir=global_wiki_dir, group_dir=group_wiki_dir)
    context_messages = (None if isinstance(message, str) else message.get('context_messages')) or []
    prompt = build_prompt(raw_text, clean, wiki_hits, context_messages=context_messages)

    llm_text = call_llm_provider(prompt, config.get("reply_engine", config))
    if llm_text:
        reply = postcheck(llm_text)
        return ReplyDecision(True, reply, intent="llm", risk_level="low", need_human=False,
                             reason="llm_provider", wiki_hits=[rel for rel, _ in wiki_hits])

    reply, intent, need_human = fallback_reply(clean, wiki_hits)
    return ReplyDecision(True, postcheck(reply), intent=intent,
                         risk_level="medium" if need_human else "low",
                         need_human=need_human,
                         reason="safe_fallback_no_provider",
                         wiki_hits=[rel for rel, _ in wiki_hits])


if __name__ == "__main__":
    sample = "小助手 你能做什么"
    d = generate_reply(sample, {}, {"default_triggers": DEFAULT_TRIGGERS})
    print(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))