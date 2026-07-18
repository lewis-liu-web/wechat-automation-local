#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Target-authorized knowledge search MCP server for the WeChat bot.

This server exposes a single ``search_knowledge`` tool to Hermes.  The set of
searchable knowledge bases is fixed at process startup via environment
variables injected by the worker (per-job authorization):

* ``WECHAT_MCP_CONFIG``      - path to ``wechat_bot_targets.json`` (existing convention)
* ``WECHAT_MCP_ALLOWED_KB_IDS`` - JSON list of allowed scene KB IDs (required)
* ``WECHAT_MCP_TARGET_ID``   - target identifier, optional, used only for logging
* ``WECHAT_MCP_WIKI_DIR``    - optional override for the wiki root directory
* ``WECHAT_MCP_TRACE_ID``    - trace identifier for this search call
* ``WECHAT_MCP_TRACE_DIR``   - directory where the trace JSON is written atomically

The server deliberately does NOT decrypt databases, access contacts, or expose
any WeChat-data tools.  It only searches the wiki-based scene knowledge bases
that the current job authorizes.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent

# Allow imports from the parent wechat-decrypt directory regardless of CWD.
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

logger = logging.getLogger("wechat_kb_search_mcp_server")

try:
    from knowledge_retrieval import (  # type: ignore
        KnowledgeSearchResult,
        search_knowledge as _search_knowledge,
    )
except Exception as exc:  # pragma: no cover
    logger.warning("knowledge_retrieval facade unavailable: %s", exc)
    _search_knowledge = None  # type: ignore
    KnowledgeSearchResult = None  # type: ignore

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"fastmcp not available: {exc}") from exc


def _load_target_config(config_path: str) -> Dict[str, Any]:
    """Load the WeChat target config (wechat_bot_targets.json) safely.

    Unlike ``config.load_config``, this does not merge DB defaults or auto-detect
    paths, so it works when ``WECHAT_MCP_CONFIG`` points to the target config file.
    """
    path = Path(config_path)
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        logger.warning("failed to load target config %s: %s", config_path, exc)
    return {}


def _load_config() -> Dict[str, Any]:
    """Load the target configuration from the path provided by the worker."""
    config_path = os.environ.get("WECHAT_MCP_CONFIG")
    if not config_path:
        return {}
    cfg = _load_target_config(config_path)
    override = _wiki_dir_override()
    if override:
        cfg["wiki_dir"] = str(override)
    return cfg


def _load_allowed_kb_ids() -> List[str]:
    """Load the allowlist injected by the worker.

    The allowlist is required; an empty or malformed list yields no results.
    """
    raw = os.environ.get("WECHAT_MCP_ALLOWED_KB_IDS")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x is not None]
    except Exception:
        logger.warning("malformed WECHAT_MCP_ALLOWED_KB_IDS: %r", raw)
    return []


def _wiki_dir_override() -> Optional[Path]:
    """Return the wiki root override if the worker provided one."""
    raw = os.environ.get("WECHAT_MCP_WIKI_DIR")
    if not raw:
        return None
    p = Path(raw).resolve()
    if p.exists() and p.is_dir():
        return p
    return None


def _trace_context() -> Tuple[str, str]:
    """Return the trusted trace env values provided by the worker.

    The worker owns trace_id generation and trace_dir creation; the MCP server
    never accepts an agent-provided path.
    """
    return (
        os.environ.get("WECHAT_MCP_TRACE_ID") or "",
        os.environ.get("WECHAT_MCP_TRACE_DIR") or "",
    )


def _write_trace(
    trace_id: str,
    trace_dir: str,
    status: str,
    provenance: List[Dict[str, Any]],
    no_source_reason: str,
) -> None:
    """Atomically write the search trace to a fixed file named by trace_id.

    The worker is the only component that reads this file; it validates and
    cleans the payload before persisting a summary in the durable job row.
    """
    if not trace_id or not trace_dir:
        return
    dir_path = Path(trace_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    trace_path = dir_path / f"{trace_id}.json"
    tmp_path = trace_path.with_suffix(".json.tmp")
    payload = {
        "trace_id": trace_id,
        "status": status,
        "provenance": provenance,
        "no_source_reason": no_source_reason,
    }
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(trace_path)
    except Exception as exc:
        logger.warning("failed to write trace %s: %s", trace_path, exc)


def _map_trace_status(status: str) -> str:
    """Map the facade status to the worker trace protocol status values."""
    if status in {"ok", "no_hit", "provider_failure"}:
        return status
    return "invalid"


mcp = FastMCP("wechat_kb_search", dependencies=[])


@mcp.tool()
def search_knowledge(query: str, limit: int = 5) -> Dict[str, Any]:
    """Search the authorized knowledge bases for the current WeChat target.

    The allowed KB IDs are fixed at process startup by the worker env variable
    ``WECHAT_MCP_ALLOWED_KB_IDS``; they cannot be widened by the agent input.

    Returns a stable structured dict:
      {
        "status": "ok" | "no_hit" | "provider_failure" | "invalid",
        "hits": [...],              # list of hit dicts, empty when not ok
        "provenance": [...],        # list of per-KB source metadata
        "error": "..." | None,     # provider error or None
        "no_source_reason": "..."   # why no hits were returned, or empty string
      }

    The ``status`` field lets Hermes distinguish:
      - ``ok``: hits were found and can be used to ground the reply.
      - ``no_hit``: all reachable providers returned no hits (safe to answer from context).
      - ``provider_failure``: at least one provider failed (do not fabricate from expected hits).
      - ``invalid``: the request was not authorized or malformed (e.g., empty allowlist).
    """
    allowed_kb_ids = _load_allowed_kb_ids()
    trace_id, trace_dir = _trace_context()

    if not allowed_kb_ids:
        no_source_reason = "no authorized knowledge bases"
        result = {
            "status": "invalid",
            "hits": [],
            "provenance": [],
            "error": None,
            "no_source_reason": no_source_reason,
        }
        _write_trace(trace_id, trace_dir, "invalid", [], no_source_reason)
        return result

    if _search_knowledge is None:
        no_source_reason = "knowledge_retrieval facade unavailable"
        result = {
            "status": "provider_failure",
            "hits": [],
            "provenance": [],
            "error": no_source_reason,
            "no_source_reason": no_source_reason,
        }
        _write_trace(trace_id, trace_dir, "provider_failure", [], no_source_reason)
        return result

    cfg = _load_config()
    config_path = os.environ.get("WECHAT_MCP_CONFIG") or ""
    try:
        raw = _search_knowledge(
            query=query,
            config=cfg,
            allowed_kb_ids=allowed_kb_ids,
            limit=limit,
            core_limit=0,
            scene_limit=limit,
            config_path=config_path,
        )
        status = raw.status
        hits = raw.hits
        provenance = raw.provenance
        error = raw.error
        if status == "ok":
            no_source_reason = ""
        elif status == "no_hit":
            no_source_reason = "query returned no hits"
        elif status == "provider_failure":
            no_source_reason = error or "one or more authorized knowledge providers failed"
        else:
            no_source_reason = error or "invalid knowledge request"
        result = {
            "status": status,
            "hits": hits,
            "provenance": provenance,
            "error": error,
            "no_source_reason": no_source_reason,
        }
        _write_trace(trace_id, trace_dir, _map_trace_status(status), provenance, no_source_reason)
        return result
    except Exception as exc:
        no_source_reason = str(exc)
        result = {
            "status": "provider_failure",
            "hits": [],
            "provenance": [],
            "error": no_source_reason,
            "no_source_reason": no_source_reason,
        }
        _write_trace(trace_id, trace_dir, "provider_failure", [], no_source_reason)
        return result


if __name__ == "__main__":
    mcp.run()
