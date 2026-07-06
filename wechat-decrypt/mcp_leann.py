#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LEANN MCP server.

Exposes a single ``leann_search`` tool that delegates to the ``leann`` CLI.
Keeping the search as a subprocess avoids loading the heavy
sentence-transformers/FAISS stack inside the MCP server process for every
Hermes invocation.
"""

import json
import os
import subprocess

from mcp.server.fastmcp import FastMCP

# Import the canonical LEANN environment builder so the MCP server uses the
# same offline cache and HF mirror settings as the rest of the project.
try:
    from target_registry import _leann_env
except Exception:  # pragma: no cover - defensive
    _leann_env = None

mcp = FastMCP("leann", instructions="本地 LEANN 知识库语义搜索工具")

_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
_LEANN_CLI = os.environ.get("LEANN_CLI", "leann")
_LEANN_TIMEOUT = float(os.environ.get("LEANN_TIMEOUT", "120"))


def _build_env() -> dict:
    """Build the subprocess env, preferring the project-wide LEANN settings."""
    config_path = os.environ.get("CONFIG_PATH") or os.environ.get("WECHAT_CONFIG_PATH")
    if config_path:
        try:
            return _leann_env(config_path=config_path) if _leann_env else dict(os.environ)
        except Exception:
            pass
    if _leann_env:
        try:
            return _leann_env()
        except Exception:
            pass
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    return env


@mcp.tool()
def leann_search(index_name: str, query: str, top_k: int = 3) -> str:
    """搜索本地 LEANN 语义索引，返回摘要文本。

    Args:
        index_name: 知识库索引名称（字符串）
        query: 搜索查询（字符串）
        top_k: 返回结果数量（整数，默认 3）
    """
    name = str(index_name or "").strip()
    q = str(query or "").strip()
    if not name:
        return "错误：index_name 不能为空"
    if not q:
        return "错误：query 不能为空"

    top_k = max(1, int(top_k or 3))
    env = _build_env()

    cmd = [
        str(_LEANN_CLI),
        "search",
        name,
        q,
        "--top-k",
        str(top_k),
        "--json",
        "--non-interactive",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(_LEANN_TIMEOUT)),
            env=env,
            creationflags=_NO_WINDOW_FLAGS,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0 or not stdout:
            return f"错误：LEANN search 失败（rc={proc.returncode}）{stderr[:200] or stdout[:200] or '无输出'}"

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return f"LEANN search 返回非 JSON 输出：\n{stdout[:2000]}"

        if isinstance(data, dict):
            if not data.get("ok"):
                return f"LEANN search 返回失败：{data.get('error') or stdout[:500]}"
            hits = data.get("hits") or []
        elif isinstance(data, list):
            hits = data
        else:
            hits = []

        if not hits:
            return "未找到相关结果。"

        lines = [f"索引 '{name}' 搜索结果（top {len(hits)}）："]
        for idx, hit in enumerate(hits[:top_k], start=1):
            if isinstance(hit, dict):
                text = hit.get("text") or hit.get("content") or hit.get("summary") or str(hit)
                meta = hit.get("metadata") or {}
                source = meta.get("source") or meta.get("file_name") or meta.get("title") or hit.get("id") or ""
            else:
                text = str(hit)
                source = ""
            lines.append(f"[{idx}] {source}\n{text}")
        return "\n\n".join(lines)
    except subprocess.TimeoutExpired:
        return f"错误：LEANN search 超时（>{_LEANN_TIMEOUT}s）"
    except Exception as exc:  # pragma: no cover - defensive
        return f"错误：LEANN search 异常 {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    mcp.run()
