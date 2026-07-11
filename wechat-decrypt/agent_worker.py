#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dry-run worker for AgentProvider jobs.

M1.5 only connects the local job queue to an AgentProvider.  It deliberately
does not send WeChat messages and is not imported by the monitor loop yet.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parent
try:
    sys.path.insert(0, str(ROOT))
except Exception:
    pass

import agent_jobs as jobs  # noqa: E402
from agent_provider import (  # noqa: E402
    AgentProvider,
    EchoAgentProvider,
    HermesProvider,
    provider_from_config,
)

try:
    from wechat_sender import send_reply as _send_reply  # noqa: E402
    _HAS_SENDER = True
except Exception:
    _HAS_SENDER = False

from reply_engine import sanitize_reply_text  # noqa: E402

DEFAULT_CONFIG_PATH = ROOT / "wechat_bot_targets.json"
DEFAULT_STOP_FILE = ROOT / "agent_worker.stop"
DEFAULT_STATE_FILE = ROOT / "temp" / "agent_worker_state.json"


def _write_state(path: Optional[Path], payload: Dict[str, Any]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"ts": time.time(), **payload}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _stop_requested(path: Optional[Path]) -> bool:
    return bool(path and path.exists())


def _load_config(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _provider_name(provider: AgentProvider) -> str:
    return str(getattr(provider, "name", provider.__class__.__name__)).lower()


def _resolve_mention_name(job: Dict[str, Any], payload: Dict[str, Any]) -> str:
    event_ctx = payload.get("event_context") or {}
    for value in (
        event_ctx.get("mention_name"),
        event_ctx.get("sender_display_name"),
        payload.get("mention_name"),
        payload.get("sender_display_name"),
        payload.get("sender_name"),
        payload.get("from_display_name"),
        job.get("sender"),
    ):
        name = str(value or "").strip().lstrip("@")
        if name:
            return name
    return ""



def build_provider(args: argparse.Namespace) -> AgentProvider:
    if args.provider == "echo":
        return EchoAgentProvider(worker_id=args.worker_id)
    if args.provider == "hermes":
        return HermesProvider(
            cli_path=str(args.hermes_cli or "hermes"),
            hermes_home=args.hermes_home,
            profile=args.profile,
            model=args.model,
            toolsets=args.toolsets,
            worker_id=args.worker_id,
        )
    cfg = _load_config(Path(args.config) if args.config else DEFAULT_CONFIG_PATH)
    if args.provider:
        cfg.setdefault("agent_provider", {})
        if isinstance(cfg["agent_provider"], dict):
            cfg["agent_provider"]["default"] = args.provider
    return provider_from_config(cfg)


def _send_result_back(job: Dict[str, Any], result_text: str, config_path: Path) -> Dict[str, Any]:
    """Send completed job result back to WeChat. Returns send status dict."""
    if not _HAS_SENDER:
        return {"sent": False, "reason": "wechat_sender not available"}

    payload = job.get("payload") or {}
    target_info = payload.get("target") or {}
    target_username = target_info.get("username") or ""
    mention_name = _resolve_mention_name(job, payload)
    message_local_id = job.get("message_local_id")

    if not target_username:
        return {"sent": False, "reason": "no target username in payload"}

    # Load config and find target
    cfg = _load_config(config_path)
    if not cfg:
        return {"sent": False, "reason": "failed to load config"}

    targets = cfg.get("targets") or []
    target = None
    for t in targets:
        if t.get("username") == target_username:
            target = t
            break

    if not target:
        return {"sent": False, "reason": f"target not found: {target_username}"}

    # Mention prefix is now applied by wechat_sender.send_reply_detailed(mention_name=...).
    reply_text = result_text

    send_mode = cfg.get("send_mode") or "foreground"
    try:
        ok = _send_reply(  # type: ignore
            text=reply_text,
            mode=send_mode,
            target=target,
            before_local_id=message_local_id,
            cfg=cfg,
            mention_name=mention_name,
        )
        return {"sent": bool(ok), "reason": "ok" if ok else "send_reply returned False"}
    except Exception as e:
        return {"sent": False, "reason": f"send_reply exception: {e}"}


def process_once(*,
                 provider: AgentProvider,
                 worker_id: str,
                 db_path: Optional[Path] = None,
                 timeout_seconds: float = 90,
                 max_global_running: int = 1,
                 per_group_concurrency: int = 1,
                 active_workers: int = 1,
                 send_enabled: bool = False,
                 config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Claim and run one job. Returns a structured worker action result."""
    timed_out = jobs.timeout_stale_running(timeout_seconds=timeout_seconds, db_path=db_path)
    provider_name = _provider_name(provider)
    job = jobs.claim_next_job(
        worker_id=worker_id,
        provider=provider_name,
        max_global_running=max_global_running,
        per_group_concurrency=per_group_concurrency,
        active_workers=active_workers,
        db_path=db_path,
    )
    if not job:
        return {"ok": True, "action": "idle", "timed_out": timed_out}

    result = provider.run(job, timeout=timeout_seconds)
    raw = result.raw or {}
    bridge_patch = {key: raw.get(key) for key in ("bridge_session_id", "bridge_user_msg_id") if raw.get(key)}
    if bridge_patch:
        jobs.merge_payload(int(job["id"]), bridge_patch, db_path=db_path)
    if result.ok and result.reply_text:
        cfg = _load_config(config_path or DEFAULT_CONFIG_PATH) or {}
        sanitized_text = sanitize_reply_text(result.reply_text, job.get("payload"), cfg)
        completed = jobs.complete_job(int(job["id"]), sanitized_text, db_path=db_path)
        # M3: Send result back to WeChat if enabled
        send_status = {"sent": False, "reason": "send_disabled"}
        if send_enabled and _HAS_SENDER:
            send_status = _send_result_back(job, sanitized_text, config_path or DEFAULT_CONFIG_PATH)
            if send_status.get("sent"):
                jobs.mark_sent(int(job["id"]), db_path=db_path)
            else:
                jobs.mark_send_failed(int(job["id"]), send_status.get("reason", "send failed"), db_path=db_path)
        return {
            "ok": completed,
            "action": "completed",
            "job_id": job["id"],
            "job_key": job["job_key"],
            "provider": result.provider,
            "worker_id": result.worker_id or worker_id,
            "latency": round(result.latency, 3),
            "reply_preview": sanitized_text[:120],
            "send_status": send_status,
            "timed_out": timed_out,
        }

    status = jobs.STATUS_TIMEOUT if result.status == "timeout" else jobs.STATUS_FAILED
    failed = jobs.fail_job(int(job["id"]), result.error or result.status or "provider failed",
                           status=status, db_path=db_path)
    return {
        "ok": failed,
        "action": status,
        "job_id": job["id"],
        "job_key": job["job_key"],
        "provider": result.provider or provider_name,
        "worker_id": result.worker_id or worker_id,
        "latency": round(result.latency, 3),
        "error": result.error,
        "timed_out": timed_out,
    }


def run_loop(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else None
    stop_file = Path(args.stop_file) if args.stop_file else None
    state_file = Path(args.state_file) if args.state_file else None
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if args.clear_stop and stop_file and stop_file.exists():
        try:
            stop_file.unlink()
        except Exception:
            pass
    provider = build_provider(args)
    if args.health:
        print(json.dumps(provider.health().to_dict(), ensure_ascii=False, indent=2, default=str))
        return 0

    _write_state(state_file, {
        "status": "running",
        "worker_id": args.worker_id,
        "provider": args.provider,
        "timeout": args.timeout,
        "worker_count": int(args.active_workers or 1),
        "pid": os.getpid(),
        "last_action": "start",
    })
    while True:
        if _stop_requested(stop_file):
            _write_state(state_file, {
                "status": "stopped",
                "worker_id": args.worker_id,
                "provider": args.provider,
                "timeout": args.timeout,
                "pid": os.getpid(),
                "last_action": "stop_file",
            })
            return 0
        result = process_once(
            provider=provider,
            worker_id=args.worker_id,
            db_path=db_path,
            timeout_seconds=args.timeout,
            max_global_running=args.max_global_running,
            per_group_concurrency=args.per_group_concurrency,
            active_workers=args.active_workers,
            send_enabled=args.send,
            config_path=config_path,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        else:
            print("agent_worker", result, flush=True)
        _write_state(state_file, {
            "status": "running",
            "worker_id": args.worker_id,
            "provider": args.provider,
            "timeout": args.timeout,
            "worker_count": int(args.active_workers or 1),
            "pid": os.getpid(),
            "last_action": result.get("action"),
            "last_result": result,
        })
        if args.once:
            return 0 if result.get("ok") else 1
        time.sleep(args.idle_sleep if result.get("action") == "idle" else args.poll_interval)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run one or more dry-run deep-agent jobs.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="wechat_bot_targets.json path")
    ap.add_argument("--db", default=str(jobs.DEFAULT_DB_PATH), help="agent_jobs sqlite path")
    ap.add_argument("--provider", choices=["echo", "hermes"], default="hermes")
    ap.add_argument("--worker-id", default="agent-worker-1")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--hermes-cli", default=os.environ.get("HERMES_CLI", "hermes"),
                    help="path to hermes executable (used when --provider hermes)")
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME"),
                    help="HERMES_HOME directory; hermes profile-aware home")
    ap.add_argument("--profile", default=os.environ.get("HERMES_PROFILE"),
                    help="hermes profile name (e.g. wechat-bot-worker1)")
    ap.add_argument("--model", default=os.environ.get("HERMES_MODEL"),
                    help="hermes model override, e.g. anthropic/claude-sonnet-4")
    ap.add_argument("--toolsets", default=os.environ.get("HERMES_TOOLSETS"),
                    help="comma-separated hermes toolsets to enable")
    ap.add_argument("--max-global-running", type=int, default=1)
    ap.add_argument("--active-workers", type=int, default=1,
                    help="how many concurrent worker processes are part of the pool")
    ap.add_argument("--per-group-concurrency", type=int, default=1)
    ap.add_argument("--poll-interval", type=float, default=0.2)
    ap.add_argument("--idle-sleep", type=float, default=2.0)
    ap.add_argument("--stop-file", default=str(DEFAULT_STOP_FILE))
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--clear-stop", action="store_true", help="remove stop-file before entering loop")
    ap.add_argument("--once", action="store_true", help="process at most one job")
    ap.add_argument("--health", action="store_true", help="print provider health and exit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--send", action="store_true", help="send completed job results back to WeChat")
    args = ap.parse_args(argv)
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
