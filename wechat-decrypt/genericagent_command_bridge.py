#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GenericAgent command provider bridge for wechat-auto.

Reads JSON payload from stdin and prints {"reply": "..."} to stdout.
This keeps wechat-auto on the generic command-provider contract while using
GenericAgent as just another external agent application.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List


# Command-provider pipes are UTF-8 by contract. On Windows, pythonw may
# otherwise inherit the ANSI code page (GBK/CP936), corrupting Chinese JSON
# before it reaches the agent and/or when stdout is printed back.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _postcheck(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"<summary>.*?</summary>", "", text, flags=re.S).strip()
    text = text.replace("[ROUND END]", "").strip()
    if len(text) > 1200:
        text = text[:1200].rstrip() + "..."
    return text


def _load_client(code_root: Path, llm_no: int):
    root_str = str(code_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from llmcore import (  # type: ignore
        reload_mykeys, LLMSession, ToolClient, ClaudeSession, MixinSession,
        NativeToolClient, NativeClaudeSession, NativeOAISession,
    )

    mykeys, _changed = reload_mykeys()
    sessions: List[Any] = []
    for key, cfg in mykeys.items():
        if not any(x in key for x in ["api", "config", "cookie"]):
            continue
        try:
            if "native" in key and "claude" in key:
                sessions.append(NativeToolClient(NativeClaudeSession(cfg=cfg)))
            elif "native" in key and "oai" in key:
                sessions.append(NativeToolClient(NativeOAISession(cfg=cfg)))
            elif "claude" in key:
                sessions.append(ToolClient(ClaudeSession(cfg=cfg)))
            elif "oai" in key:
                sessions.append(ToolClient(LLMSession(cfg=cfg)))
            elif "mixin" in key:
                sessions.append({"mixin_cfg": cfg})
        except Exception:
            continue

    for i, sess in enumerate(list(sessions)):
        if isinstance(sess, dict) and "mixin_cfg" in sess:
            try:
                mixin = MixinSession(sessions, sess["mixin_cfg"])
                if isinstance(mixin._sessions[0], (NativeClaudeSession, NativeOAISession)):
                    sessions[i] = NativeToolClient(mixin)
                else:
                    sessions[i] = ToolClient(mixin)
            except Exception:
                sessions[i] = None
    sessions = [s for s in sessions if s is not None and not isinstance(s, dict)]
    if not sessions:
        raise RuntimeError("no GenericAgent LLM sessions available")
    return sessions[int(llm_no) % len(sessions)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-root", default=r"D:\Program Files\GenericAgent\GenericAgent-wechat-auto")
    ap.add_argument("--llm-no", type=int, default=3)
    args = ap.parse_args()

    payload: Dict[str, Any] = json.load(sys.stdin)
    prompt = payload.get("prompt") or payload.get("clean_text") or ""
    input_text = (
        "你是群聊小助手。请根据下面的群消息、本地wiki片段和边界约束，"
        "只输出一段可以直接发送到微信群的中文回复；不要解释过程，不要使用工具，不要写summary。\n\n"
        + str(prompt)
    )
    client = _load_client(Path(args.code_root), args.llm_no)
    messages = [{"role": "user", "content": input_text}]
    resp = None
    gen = client.chat(messages, tools=[])
    try:
        while True:
            next(gen)
    except StopIteration as e:
        resp = e.value
    reply = _postcheck(str(getattr(resp, "content", "") or ""))
    print(json.dumps({"reply": reply}, ensure_ascii=False))
    return 0 if reply else 2


if __name__ == "__main__":
    raise SystemExit(main())
