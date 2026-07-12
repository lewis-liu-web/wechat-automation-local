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
from typing import Any, Dict, List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent

# Allow imports from the parent wechat-decrypt directory regardless of CWD.
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

logger = logging.getLogger("wechat_kb_search_mcp_server")

try:
    from reply_engine import (  # type: ignore
        _knowledge_hits_to_payload,
        retrieve_knowledge_layers,
    )
except Exception as exc:  # pragma: no cover
    logger.warning("reply_engine retrieval unavailable: %s", exc)
    retrieve_knowledge_layers = None
    _knowledge_hits_to_payload = None

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


def _build_target(allowed_kb_ids: List[str]) -> Dict[str, Any]:
    """Build a minimal target dict that only knows the authorized KB IDs."""
    target: Dict[str, Any] = {"knowledge_bases": allowed_kb_ids}
    override = _wiki_dir_override()
    if override:
        target["wiki_dir"] = str(override)
    return target


mcp = FastMCP("wechat_kb_search", dependencies=[])


@mcp.tool()
def search_knowledge(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search the authorized knowledge bases for the current WeChat target.

    Returns a list of knowledge hits. Each hit is a dict with keys:
    ``kb_id``, ``source``, ``rel_path``, ``content``.  If no knowledge bases are
    authorized or the query matches nothing, an empty list is returned.
    """
    if retrieve_knowledge_layers is None:
        raise RuntimeError("reply_engine retrieval unavailable")

    allowed_kb_ids = _load_allowed_kb_ids()
    if not allowed_kb_ids:
        logger.info("search_knowledge called with no allowed_kb_ids; returning empty")
        return []

    cfg = _load_config()
    target = _build_target(allowed_kb_ids)
    hits = retrieve_knowledge_layers(
        query=query,
        config=cfg,
        target=target,
        limit=limit,
        core_limit=0,
        scene_limit=limit,
        skip_core=True,
    ).get("scene", [])
    if not hits:
        return []
    if _knowledge_hits_to_payload is not None:
        return _knowledge_hits_to_payload(hits)
    return [hit for hit in hits]


if __name__ == "__main__":
    mcp.run()
