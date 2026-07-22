#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local HTTP control surface for the WeChat bot.

This is the M0 control plane. It is intentionally agent-agnostic:
it only wraps the existing `manage_targets.py` / `event_log.py` /
`wechat_bot_monitor` boundary. Any front-end (Streamlit today, FastAPI
front-end tomorrow) talks to this API.

Endpoints
---------
GET  /health
GET  /status
POST /monitor/start
POST /monitor/stop
POST /monitor/restart

GET  /targets
GET  /targets/{key}/triggers
POST /targets/{key}/triggers/add
POST /targets/{key}/triggers/remove
POST /targets/{key}/triggers/replace
POST /targets/{key}/triggers/clear

GET  /events/recent?limit=&target=&kind=
GET  /events/stats?since=

Run::

    python control_api.py --host 127.0.0.1 --port 18590
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import posixpath
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
CONSOLE_STATIC_DIR = ROOT / "console-static"
KB_UPLOAD_STAGING = ROOT / "temp" / "kb_uploads"

logger = logging.getLogger(__name__)

try:
    sys.path.insert(0, str(ROOT))
except Exception:
    pass

import event_log  # noqa: E402
import agent_jobs  # noqa: E402
import agent_worker  # noqa: E402
from agent_provider import (  # noqa: E402
    AgentResult,
    EchoAgentProvider,
    HermesProvider,
    discover_hermes_profiles,
    list_agent_instances,
    preflight_hermes_profile,
    provider_from_config,
)
import manage_targets as mt  # noqa: E402
import target_registry as reg  # noqa: E402
import digest_service  # noqa: E402
import reliable_pipeline  # noqa: E402
import reliable_worker  # noqa: E402
import reply_engine  # noqa: E402
from wechat_bot_monitor import wait_sent_confirmation  # noqa: E402

DEFAULT_PORT = 18590
DEFAULT_HOST = "127.0.0.1"
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

_ASYNC_LOOP_LOCK = threading.RLock()
_ASYNC_LOOP_STOP = threading.Event()
_ASYNC_LOOP_THREAD: threading.Thread | None = None
_ASYNC_LOOP_STATE: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "stopped_at": None,
    "iterations": 0,
    "last_error": "",
    "last_dispatch": None,
    "last_reconcile": None,
    "last_send": None,
    "config": {},
}
_PROVIDER_HEALTH_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

_RELIABLE_SCHEDULER_LOCK = threading.RLock()
_RELIABLE_SCHEDULER_STOP = threading.Event()
_RELIABLE_SCHEDULER_THREAD: threading.Thread | None = None
_RELIABLE_SCHEDULER_DEFAULT_INTERVAL = 2.0
_RELIABLE_SCHEDULER_STATE: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "stopped_at": None,
    "iterations": 0,
    "last_error": "",
    "last_worker": None,
    "last_sender": None,
    "interval": _RELIABLE_SCHEDULER_DEFAULT_INTERVAL,
    "config": {},
}


def _json(obj: Any, status: int = 200) -> Tuple[bytes, int, str]:
    return (json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
            status, "application/json; charset=utf-8")


def _err(msg: str, status: int = 400, **extra: Any) -> Tuple[bytes, int, str]:
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    return _json(payload, status)


def _ok(**kwargs: Any) -> Tuple[bytes, int, str]:
    return _json({"ok": True, **kwargs}, 200)


def _parse_since(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            # ISO date YYYY-MM-DD treated as 00:00 local time
            import datetime as _dt
            return _dt.datetime.fromisoformat(value).timestamp()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# router helpers (intentionally tiny — no FastAPI dep needed for M0)
# ---------------------------------------------------------------------------

def _match(path: str, pattern: str) -> Optional[List[str]]:
    p = pattern.split("/")
    s = path.split("/")
    if len(p) != len(s):
        return None
    out: List[str] = []
    for a, b in zip(p, s):
        if a.startswith("{") and a.endswith("}"):
            out.append(b)
        elif a != b:
            return None
    return out


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {}


_CONSOLE_MIME_MAP: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _resolve_console_static(path: str) -> Tuple[Optional[Path], str]:
    """Resolve a /console/* path to a static file path and MIME type.

    Returns (None, "") for non-console paths, traversal attempts,
    unsupported extensions, or missing files.
    """
    if path in ("/console", "/console/"):
        rel = "index.html"
    elif path.startswith("/console/"):
        rel = path[len("/console/"):]
    else:
        return (None, "")
    # URL-decode before normalization so encoded traversal (%2E%2E, ..%2F) is rejected.
    rel = urllib.parse.unquote(rel)
    rel_norm = posixpath.normpath("/" + rel).lstrip("/")
    if not rel_norm or rel_norm.startswith("..") or rel_norm.startswith("/"):
        return (None, "")
    base = CONSOLE_STATIC_DIR.resolve()
    try:
        candidate = (base / rel_norm).resolve()
    except (OSError, ValueError):
        return (None, "")
    try:
        candidate.relative_to(base)
    except ValueError:
        return (None, "")
    if not candidate.is_file():
        return (None, "")
    mime = _CONSOLE_MIME_MAP.get(candidate.suffix.lower())
    if not mime:
        return (None, "")
    return (candidate, mime)


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------

class ControlHandler(BaseHTTPRequestHandler):
    server_version = "WeChatControlAPI/0.1"

    def log_message(self, format, *args):  # silence default access log
        return

    def _route(self, method: str, path: str, params: Dict[str, List[str]],
               body: Dict[str, Any]):
        # ---- health/status ----
        if method == "GET" and path in ("/health", "/healthz"):
            return _ok(health="ok", ts=time.time())
        if method == "GET" and path == "/status":
            pids = mt._monitor_pids()
            return _ok(
                monitor_running=bool(pids),
                monitor_pids=pids,
                event_total=event_log.get_stats().get("total"),
                ts=time.time(),
            )

        # ---- helper: wait for monitor process state ----
        def _wait_for_monitor(running: bool, timeout: float = 30.0, interval: float = 0.5) -> None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if bool(_monitor_pids()) == running:
                    break
                time.sleep(interval)

        # ---- monitor control ----
        if method == "POST" and path == "/monitor/start":
            return _call_cli(["start", "--json"])
        if method == "POST" and path == "/monitor/stop":
            resp = _call_cli(["stop", "--json"])
            _wait_for_monitor(running=False, timeout=30.0)
            return resp
        if method == "POST" and path == "/monitor/restart":
            resp = _call_cli(["restart", "--json"])
            _wait_for_monitor(running=True, timeout=30.0)
            return resp

        # ---- targets ----
        if method == "GET" and path == "/targets":
            kind = (params.get("kind") or ["all"])[0]
            targets, candidates = _list_targets_by_kind(kind)
            return _ok(count=len(targets), targets=targets, candidates=candidates)
        if method == "GET" and (m := _match(path, "/targets/{key}")):
            try:
                return _ok(**reg.inspect_target(m[0], config_path=reg.CONFIG_PATH, candidates_path=reg.CANDIDATES_PATH))
            except ValueError as e:
                return _err(str(e), status=404)
        if method == "POST" and path == "/targets/scan":
            include_contacts = (params.get("include_contacts") or ["false"])[0].lower() in ("1", "true", "yes")
            return _call_cli(["scan"] + (["--include-contacts"] if include_contacts else []) + ["--json"])
        if method == "POST" and (m := _match(path, "/targets/{key}/on")):
            kbs = list(body.get("knowledge_bases") or [])
            return _ok(action="enabled", target=reg.enable_candidate(
                m[0], knowledge_bases=kbs, category=body.get("category"),
            ))
        if method == "POST" and (m := _match(path, "/targets/{key}/category")):
            return _ok(action="categorized", target=reg.set_category(m[0], body.get("category")))
        if method == "POST" and (m := _match(path, "/targets/{key}/off")):
            return _ok(action="disabled", target=reg.set_enabled(m[0], False))
        if method == "POST" and (m := _match(path, "/targets/{key}/delete")):
            return _ok(action="deleted", target=_delete_target(m[0]))
        if method == "POST" and (m := _match(path, "/targets/{key}/field")):
            field = body.get("field")
            value = body.get("value")
            if not field:
                return _err("field is required")
            return _ok(action="field_updated", target=reg.set_target_field(m[0], field, value))
        if method == "POST" and (m := _match(path, "/targets/{key}/mode")):
            mode = str(body.get("mode") or "").strip().lower()
            mode = "customer_service" if mode == "customer_service" else "group_assistant"
            return _ok(action="mode_updated", target=reg.set_target_mode_bundle(m[0], mode, config_path=reg.CONFIG_PATH))
        if method == "POST" and (m := _match(path, "/targets/{key}/dedicated-agent")):
            instance_id_raw = str(body.get("instance_id") or "").strip()
            instance_id: Optional[str] = instance_id_raw or None
            try:
                target = reg.set_target_dedicated_agent_instance_id(
                    m[0], instance_id, config_path=reg.CONFIG_PATH
                )
            except ValueError as exc:
                return _err(str(exc) or "invalid instance_id", status=400)
            return _ok(action="set_dedicated_agent", target=target)
        if method == "DELETE" and (m := _match(path, "/targets/{key}")):
            return _ok(action="deleted", target=_delete_target(m[0]))

        # ---- triggers ----
        m = _match(path, "/targets/{key}/triggers")
        if method == "GET" and m:
            return _ok(target=reg.get_triggers(m[0]))
        if method == "POST" and (m := _match(path, "/targets/{key}/triggers/add")):
            words = list(body.get("words") or [])
            if not words:
                return _err("words is required")
            return _ok(action="added", target=reg.set_triggers(m[0], words, replace=False))
        if method == "POST" and (m := _match(path, "/targets/{key}/triggers/remove")):
            words = list(body.get("words") or [])
            if not words:
                return _err("words is required")
            return _ok(action="removed", target=reg.remove_triggers(m[0], words, clear=False))
        if method == "POST" and (m := _match(path, "/targets/{key}/triggers/replace")):
            words = list(body.get("words") or [])
            if not words:
                return _err("words is required")
            return _ok(action="replaced", target=reg.set_triggers(m[0], words, replace=True))
        if method == "POST" and (m := _match(path, "/targets/{key}/triggers/clear")):
            return _ok(action="cleared", target=reg.remove_triggers(m[0], [], clear=True))

        # ---- default (global) triggers ----
        if method == "GET" and path == "/triggers/default":
            return _ok(default_triggers=reg.get_default_triggers())
        if method == "POST" and path == "/triggers/default/replace":
            words = list(body.get("words") or [])
            return _ok(action="replaced", default_triggers=reg.set_default_triggers(words))
        if method == "POST" and path == "/triggers/default/clear":
            return _ok(action="cleared", default_triggers=reg.set_default_triggers([]))

        # ---- events ----
        if method == "GET" and path == "/events/recent":
            limit = int((params.get("limit") or ["50"])[0])
            kind = (params.get("kind") or [None])[0]
            target = (params.get("target") or [None])[0]
            return _ok(events=event_log.get_recent(limit=limit, kind=kind, target=target))
        if method == "GET" and path == "/events/stats":
            since = _parse_since((params.get("since") or [""])[0])
            return _ok(stats=event_log.get_stats(since=since))

        # ---- agent jobs / provider visibility (M1.5 dry-run only) ----
        if method == "GET" and path == "/agent/jobs":
            limit = int((params.get("limit") or ["50"])[0])
            status = (params.get("status") or [None])[0]
            group_key = (params.get("group_key") or [None])[0]
            return _ok(jobs=agent_jobs.list_jobs(status=status, group_key=group_key, limit=limit))
        if method == "GET" and path == "/agent/jobs/stats":
            return _ok(stats=agent_jobs.count_jobs())
        if method == "GET" and (m := _match(path, "/agent/jobs/{id}")):
            job_id = int(m[0])
            job = agent_jobs.get_job(job_id)
            if not job:
                return _err("job not found: %d" % job_id, status=404)
            return _ok(job=job)
        if method == "POST" and path == "/agent/jobs/test":
            prompt = str(body.get("prompt") or "").strip()
            if not prompt:
                return _err("prompt is required")
            group_key = str(body.get("group_key") or "manual-test").strip()
            job_key = str(body.get("job_key") or "manual:%s:%d" % (group_key, int(time.time() * 1000)))
            # Build payload with optional target info for M3 send-back
            payload: Dict[str, Any] = {"prompt": prompt, "clean_text": prompt, "source": "control_api_manual_test"}
            if body.get("target_username"):
                payload["target"] = {
                    "name": body.get("target_name"),
                    "username": body.get("target_username"),
                    "table": body.get("target_table"),
                }
            if body.get("mention_name"):
                payload["mention_name"] = str(body.get("mention_name"))
            job = agent_jobs.enqueue_job(
                job_key=job_key,
                group_key=group_key,
                target_name=str(body.get("target_name") or "手动测试"),
                sender=str(body.get("sender") or "tester"),
                message_local_id=body.get("message_local_id"),
                task_type=str(body.get("task_type") or "deep_free"),
                priority=int(body.get("priority") or 0),
                provider=body.get("provider"),
                payload=payload,
            )
            try:
                event_log.log_event("job_queued", target=job.get("target_name"), sender=job.get("sender"), payload={"job_id": job.get("id"), "job_key": job.get("job_key"), "source": "manual_test"})
            except Exception:
                pass
            return _ok(action="queued", job=job)
        if method == "POST" and path == "/agent/worker/run-once":
            provider_name = str(body.get("provider") or "echo").lower()
            provider = _build_agent_provider(provider_name)
            result = agent_worker.process_once(
                provider=provider,
                worker_id=str(body.get("worker_id") or "control-api-worker-1"),
                timeout_seconds=float(body.get("timeout") or 240),
                max_global_running=int(body.get("max_global_running") or 1),
                per_group_concurrency=int(body.get("per_group_concurrency") or 1),
                active_workers=int(body.get("active_workers") or 1),
            )
            return _ok(**result)
        if method == "POST" and (m := _match(path, "/agent/jobs/{id}/retry-send")):
            job_id = int(m[0])
            job = agent_jobs.get_job(job_id)
            if not job:
                return _err("job not found: %d" % job_id)
            if not job.get("result_text"):
                return _err("job has no result to send")
            reply_text = agent_worker.sanitize_reply_text(
                job["result_text"], job.get("payload"), reg.load_config()
            )
            if reply_text != job["result_text"]:
                agent_jobs.update_result_text(job_id, reply_text)
            send_result = agent_worker._send_result_back(job, reply_text, ROOT / "wechat_bot_targets.json")
            if send_result.get("sent"):
                agent_jobs.mark_sent(job_id)
                return _ok(action="sent", job_id=job_id, send_result=send_result)
            else:
                agent_jobs.mark_send_failed(job_id, send_result.get("reason", "retry failed"))
                return _ok(action="send_failed", job_id=job_id, send_result=send_result)
        if method == "POST" and (m := _match(path, "/agent/jobs/{id}/dismiss")):
            job_id = int(m[0])
            ok = agent_jobs.dismiss_job(job_id)
            if ok:
                return _ok(action="dismissed", job_id=job_id)
            return _err("job not found or not dismissable: %d" % job_id)
        if method == "POST" and (m := _match(path, "/agent/jobs/{id}/recover-result")):
            job_id = int(m[0])
            job = agent_jobs.get_job(job_id)
            if not job:
                return _err("job not found: %d" % job_id)
            provider = _build_agent_provider("hermes")
            recover = getattr(provider, "recover", None)
            if not callable(recover):
                return _err("provider does not support result recovery")
            result: Any = recover(job, timeout=float(body.get("timeout") or 15))
            safe_result, original_raw = _safe_recover_result_snapshot(result)
            if safe_result is None:
                return _ok(action="recover_failed", job_id=job_id,
                           result={"error": "invalid recover result shape"})
            if not safe_result.ok or not safe_result.reply_text:
                return _ok(action="recover_failed", job_id=job_id,
                           result={"ok": safe_result.ok,
                                   "status": safe_result.status,
                                   "has_reply": bool(safe_result.reply_text)})
            bridge_patch = agent_jobs._safe_bridge_patch(original_raw)
            if bridge_patch:
                agent_jobs.merge_payload(job_id, bridge_patch)
            reply_text = agent_worker.sanitize_reply_text(
                safe_result.reply_text, job.get("payload"), reg.load_config()
            )
            stored = agent_jobs.recover_job_result(job_id, reply_text)
            if not stored:
                return _ok(action="recover_store_failed", job_id=job_id,
                           result={"ok": safe_result.ok,
                                   "status": safe_result.status,
                                   "has_reply": bool(safe_result.reply_text)})
            recovered = agent_jobs.get_job(job_id)
            send_result = None
            if body.get("send") is True and recovered:
                send_result = agent_worker._send_result_back(recovered, reply_text, ROOT / "wechat_bot_targets.json")
                if send_result.get("sent"):
                    agent_jobs.mark_sent(job_id)
                else:
                    agent_jobs.mark_send_failed(job_id, send_result.get("reason", "recover send failed"))
                recovered = agent_jobs.get_job(job_id)
            return _ok(action="recovered", job_id=job_id,
                       result={"ok": safe_result.ok,
                               "status": safe_result.status,
                               "has_reply": bool(safe_result.reply_text)},
                       send_result=send_result, job=recovered)
        if method == "GET" and path == "/agent/worker/status":
            return _ok(**_agent_worker_status())
        if method == "GET" and path == "/agent/instances":
            cfg = reg.load_config()
            return _ok(instances=list_agent_instances(cfg))
        if method == "GET" and path == "/agent-providers/hermes/profiles":
            return _ok(profiles=discover_hermes_profiles())
        if method == "POST" and path == "/agent/instances":
            return _ok(**_register_agent_instance(body.get("instance") if isinstance(body, dict) else None))
        if method == "GET" and path == "/agent/pool/status":
            return _ok(**_agent_pool_status())
        if method == "POST" and (m := _match(path, "/agent/instance/{id}/start")):
            instance_id = str(m[0])
            cfg = reg.load_config()
            instances = {str(inst.get("id")): inst for inst in list_agent_instances(cfg) if inst.get("id")}
            inst = instances.get(instance_id)
            if not inst:
                return _err("unknown agent_provider instance: %s" % instance_id)
            kind = str(inst.get("provider") or inst.get("type") or "hermes").lower()
            return _ok(**_agent_worker_start(
                provider=kind,
                worker_id=str(inst.get("worker_id") or instance_id),
                timeout=float(body.get("timeout") or 240),
                idle_sleep=float(body.get("idle_sleep") or 2),
                worker_count=int(body.get("worker_count") or 1),
                instance=inst if kind == "hermes" else None,
            ))
        if method == "POST" and path == "/agent/worker/start":
            provider_name = str(body.get("provider") or "echo").lower()
            return _ok(**_agent_worker_start(
                provider=provider_name,
                worker_id=str(body.get("worker_id") or "agent-worker-service-1"),
                timeout=float(body.get("timeout") or 240),
                idle_sleep=float(body.get("idle_sleep") or 2),
                worker_count=int(body.get("worker_count") or 1),
                instance=body.get("instance"),
            ))
        if method == "POST" and path == "/agent/worker/stop":
            return _ok(**_agent_worker_stop())
        if method == "GET" and path == "/agent/provider/health":
            provider_name = str((params.get("provider") or ["hermes"])[0]).lower()
            instance_id = (params.get("instance_id") or [None])[0]
            provider = _build_agent_provider(provider_name, instance_id=instance_id)
            return _ok(health=provider.health().to_dict())
        if method == "POST" and path == "/agent/preflight/run":
            return _ok(**_preflight_configured_hermes(body))


        # ---- Stage 1 reliable pipeline (run-once + status + scheduler) ----
        if method == "GET" and path == "/reliable-pipeline/status":
            return _ok(**_reliable_pipeline_status())
        if method == "POST" and path == "/reliable-pipeline/worker/run-once":
            return _ok(**_reliable_worker_run_once(body))
        if method == "POST" and path == "/reliable-pipeline/sender/run-once":
            return _ok(**_reliable_sender_run_once(body))
        if method == "GET" and path == "/reliable-pipeline/scheduler/status":
            return _ok(**_reliable_scheduler_status())
        if method == "POST" and path == "/reliable-pipeline/scheduler/start":
            return _ok(**_reliable_scheduler_start(body))
        if method == "POST" and path == "/reliable-pipeline/scheduler/stop":
            return _ok(**_reliable_scheduler_stop())
        if method == "POST" and (m := _match(path, "/reliable-pipeline/turn-jobs/{id}/quarantine")):
            return _ok(**_reliable_quarantine_turn_job(m[0], body))
        if method == "POST" and (m := _match(path, "/reliable-pipeline/outbox/{id}/requeue")):
            return _ok(**_reliable_requeue_outbox(m[0], body))
        # ---- M5 async: dispatcher / reconciler / sender ----
        if method == "POST" and path == "/agent/dispatcher/run-once":
            return _ok(**_async_dispatcher_run_once(body))
        if method == "POST" and path == "/agent/reconciler/run-once":
            return _ok(**_async_reconciler_run_once(body))
        if method == "POST" and path == "/agent/sender/run-once":
            return _ok(**_async_sender_run_once(body))
        if method == "GET" and path == "/agent/async-loop/status":
            return _ok(**_async_loop_status())
        if method == "POST" and path == "/agent/async-loop/start":
            return _ok(**_async_loop_start(body))
        if method == "POST" and path == "/agent/async-loop/stop":
            return _ok(**_async_loop_stop())
        if method == "POST" and path == "/agent/async-jobs/expire-legacy":
            return _ok(**_expire_legacy_async_jobs())
        if method == "POST" and (m := _match(path, "/agent/instance/{id}/on-duty")):
            return _ok(**_async_loop_start({"instance_id": str(m[0])}))
        if method == "POST" and (m := _match(path, "/agent/instance/{id}/off-duty")):
            return _ok(**_async_loop_remove_instance(str(m[0])))

        # ---- knowledge bases ----
        if method == "GET" and path == "/kbs":
            return _ok(knowledge_bases=reg.list_kbs_extended(config_path=reg.CONFIG_PATH))
        if method == "GET" and path == "/kbs/leann/indexes":
            return _ok(**reg.list_leann_indexes(config_path=reg.CONFIG_PATH))
        if method == "GET" and (m := _match(path, "/kbs/leann/indexes/{name}/info")):
            return _ok(**reg.get_leann_index_info(m[0], config_path=reg.CONFIG_PATH))
        if method == "POST" and path == "/kbs":
            kb_id = str(body.get("id") or "").strip()
            if not kb_id:
                return _err("id is required")
            kb_type = str(body.get("type") or "local").strip().lower()
            try:
                if kb_type == "local" and not body.get("path"):
                    out = reg.create_local_kb_dir(
                        kb_id,
                        description=str(body.get("description") or ""),
                        replace=bool(body.get("replace")),
                        source=str(body.get("source") or "local_folder"),
                        config_path=reg.CONFIG_PATH,
                    )
                else:
                    out = reg.add_knowledge_base(
                        kb_id,
                        kb_type=kb_type,
                        knowledge_base_id=body.get("knowledge_base_id"),
                        path=body.get("path"),
                        description=str(body.get("description") or ""),
                        executable=body.get("executable"),
                        scope=str(body.get("scope") or "scene"),
                        limit=body.get("limit"),
                        timeout=body.get("timeout"),
                        enabled=body.get("enabled", True),
                        replace=bool(body.get("replace")),
                        source=body.get("source"),
                        docs_dir=body.get("docs_dir"),
                        config_path=reg.CONFIG_PATH,
                    )
                return _ok(action="saved", knowledge_base=out)
            except Exception as e:
                return _err(str(e))
        if method == "POST" and (m := _match(path, "/kbs/{key}/on")):
            return _ok(action="enabled", knowledge_base=reg.set_knowledge_base_enabled(m[0], True))
        if method == "POST" and (m := _match(path, "/kbs/{key}/off")):
            return _ok(action="disabled", knowledge_base=reg.set_knowledge_base_enabled(m[0], False))
        if method == "POST" and (m := _match(path, "/kbs/{key}/delete")):
            return _ok(action="deleted", knowledge_base=reg.delete_knowledge_base(m[0], remove_files=bool(body.get("remove_files"))))
        if method == "POST" and (m := _match(path, "/kbs/{key}/import")):
            source = str(body.get("source") or "").strip()
            if not source:
                return _err("source is required")
            try:
                result = reg.import_kb_file(m[0], source)
                return _ok(action="imported", **result)
            except Exception as e:
                return _err("import failed: %s" % (e,))
        if method == "GET" and (m := _match(path, "/kbs/{key}/info")):
            info = reg.get_kb_info(m[0], config_path=reg.CONFIG_PATH)
            if not info:
                return _err("knowledge base not found: %s" % m[0], status=404)
            return _ok(knowledge_base=info)
        if method == "GET" and (m := _match(path, "/kbs/{key}/search")):
            q = str((params.get("q") or [""])[0]).strip()
            limit = int((params.get("limit") or ["5"])[0])
            try:
                return _ok(**reg.search_kb(m[0], q, limit=limit))
            except Exception as e:
                return _err("search failed: %s" % (e,))
        if method == "POST" and (m := _match(path, "/kbs/{key}/open")):
            return _ok(action="opened", path=reg.open_kb_dir(m[0]))
        if method == "POST" and (m := _match(path, "/kbs/{key}/obsidian")):
            return _ok(action="opened_obsidian", **reg.open_kb_obsidian(m[0]))
        if method == "GET" and (m := _match(path, "/kbs/{key}/diagnose")):
            q = str((params.get("q") or [""])[0]).strip()
            try:
                return _ok(**reg.diagnose_kb(m[0], query=q))
            except Exception as e:
                return _err("diagnose failed: %s" % (e,))
        if method == "POST" and (m := _match(path, "/kbs/{key}/leann/build")):
            body = body or {}
            docs_dir = body.get("docs_dir")
            force = bool(body.get("force"))
            try:
                return _ok(**reg.build_leann_kb(m[0], docs_dir=docs_dir, force=force, config_path=reg.CONFIG_PATH))
            except Exception as e:
                return _err("build failed: %s" % (e,))
        if method == "GET" and (m := _match(path, "/kbs/{key}/leann/build/status")):
            build_id = str((params.get("build_id") or [""])[0]).strip()
            status = reg.get_leann_build_status(build_id)
            status.setdefault("key", m[0])
            return _ok(**status)
        if method == "POST" and (m := _match(path, "/targets/{key}/kbs/replace")):
            kbs = list(body.get("knowledge_bases") or [])
            return _ok(action="bound", target=reg.bind_wiki(m[0], kbs, replace=True))

        # ---- overview ----
        if method == "GET" and path == "/overview/today":
            return _ok(**_overview_today())
        if method == "GET" and path == "/overview/history":
            try:
                days = int((params.get("days") or ["7"])[0])
            except (ValueError, TypeError):
                days = 7
            return _ok(**_overview_history(days))
        if method == "GET" and path == "/overview/topics":
            cfg = reg.load_config()
            return _ok(**digest_service.get_topics_state(cfg))
        if method == "POST" and path == "/overview/topics/refresh":
            cfg = reg.load_config()
            target = str(body.get("target") or "").strip() if isinstance(body, dict) else ""
            target = target or None
            if target:
                enabled_targets = {
                    t.get("username")
                    for t in (cfg.get("targets") or [])
                    if t.get("enabled") and t.get("username")
                }
                if target not in enabled_targets:
                    return _err("target not found or not enabled: %s" % target, status=404)
            return _ok(**digest_service.refresh_now(cfg, target_id=target))

        # ---- KB file upload staging (SPA replacement for Streamlit uploader) ----
        if method == "POST" and (m := _match(path, "/kbs/{kb}/upload")):
            kb_id = m[0]
            info = reg.get_kb_info(kb_id, config_path=reg.CONFIG_PATH)
            if not info:
                return _err("knowledge base not found: %s" % kb_id, status=404)
            if (info.get("type") or "local") != "local":
                return _err("knowledge base is not local: %s" % kb_id, status=400)
            files = body.get("files") if isinstance(body, dict) else None
            if not isinstance(files, list):
                return _err("files must be a list")
            if len(files) > 100:
                return _err("too many files: max 100", status=413)

            MAX_SINGLE = 20 * 1024 * 1024
            MAX_TOTAL = 50 * 1024 * 1024
            safe_kb = "".join(c for c in kb_id if c.isalnum() or c in "_-")
            if not safe_kb:
                return _err("invalid kb id")
            staging = KB_UPLOAD_STAGING / safe_kb
            try:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return _err("staging failed: %s" % e)

            staged: List[str] = []
            total = 0
            for f in files:
                if not isinstance(f, dict):
                    return _err("invalid file entry")
                raw_name = str(f.get("filename") or "")
                name = Path(raw_name).name
                if not name or name in (".", ".."):
                    return _err("invalid filename: %s" % raw_name)
                try:
                    data = base64.b64decode(f.get("content_b64") or "", validate=True)
                except Exception:
                    return _err("invalid base64 for %s" % name)
                size = len(data)
                if size > MAX_SINGLE:
                    return _err("file too large: %s" % name, status=413)
                total += size
                if total > MAX_TOTAL:
                    return _err("total upload size exceeds 50MB", status=413)
                (staging / name).write_bytes(data)
                staged.append(name)
            return _ok(staged=len(staged), staging_dir=str(staging))

        # ---- LEANN build log ----
        if method == "GET" and (m := _match(path, "/kbs/{kb}/leann/build/log")):
            kb_id = m[0]
            build_id = str((params.get("build_id") or [""])[0]).strip()
            if build_id:
                status = reg.get_leann_build_status(build_id)
            else:
                latest_build_id = reg.get_latest_leann_build_for_kb(kb_id)
                if not latest_build_id:
                    return _err("no build found for kb: %s" % kb_id, status=404)
                status = reg.get_leann_build_status(latest_build_id)
            log_path_str = status.get("log_path") if isinstance(status, dict) else None
            if not log_path_str:
                return _err("log path not found for build", status=404)
            try:
                log_path = Path(str(log_path_str)).resolve()
                root = ROOT.resolve()
                log_path.relative_to(root)
            except (ValueError, OSError):
                return _err("log path outside root", status=404)
            if not log_path.is_file():
                return _err("log file not found: %s" % log_path, status=404)
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return _err("cannot read log: %s" % e, status=500)
            tail = text[-4000:] if len(text) > 4000 else text
            return _ok(log_tail=tail, log_path=str(log_path))

        return _err("not found: %s %s" % (method, path), status=404)

    # ---- HTTP verbs ----
    def do_GET(self):
        try:
            path, params = self._parse_path()
            if path.startswith("/console"):
                static_path, ctype = _resolve_console_static(path)
                if static_path:
                    data = static_path.read_bytes()
                    cc = "no-cache" if path in ("/console", "/console/") else "max-age=3600"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", cc)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                body = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return
            resp = self._route("GET", path, params, body={})
            self._send(*resp)
        except Exception as e:
            self._send(*_err("internal error: %r" % (e,), status=500))

    def do_POST(self):
        try:
            path, params = self._parse_path()
            body = _read_json_body(self)
            resp = self._route("POST", path, params, body=body)
            self._send(*resp)
        except Exception as e:
            self._send(*_err("internal error: %r" % (e,), status=500))

    # ---- internal helpers ----
    def _parse_path(self) -> Tuple[str, Dict[str, List[str]]]:
        raw = self.path
        if "?" in raw:
            p, qs = raw.split("?", 1)
        else:
            p, qs = raw, ""
        params: Dict[str, List[str]] = {}
        if qs:
            for part in qs.split("&"):
                if not part:
                    continue
                if "=" in part:
                    k, v = part.split("=", 1)
                else:
                    k, v = part, ""
                params.setdefault(urllib.parse.unquote(k), []).append(urllib.parse.unquote(v))
        # Path may be percent-encoded; always decode it once.
        return urllib.parse.unquote(p), params

    def _send(self, body: bytes, status: int, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        # Streamlit-side buttons use POST /targets/{key}/delete for safety;
        # we still expose raw DELETE for programmatic clients.
        try:
            path, params = self._parse_path()
            body = _read_json_body(self)
            resp = self._route("DELETE", path, params, body=body)
            self._send(*resp)
        except Exception as e:
            self._send(*_err("internal error: %r" % (e,), status=500))


# ---------------------------------------------------------------------------
# local helpers
# ---------------------------------------------------------------------------

def _delete_target(key: str):
    """Delete a configured target by name or username.

    Delegates to target_registry.delete_target() so the CLI and HTTP surface
    share exactly the same hard-delete + candidate-reset logic.
    """
    from target_registry import delete_target
    t = delete_target(key)
    try:
        event_log.log_event("target_delete", target=key, payload={"name": t.get("name")})
    except Exception:
        pass
    return t

def _list_targets_by_kind(kind: str):
    """Return (targets, candidates) for the requested kind.

    Output targets include both configured and pending candidates so the
    UI can show one combined list with status hints.
    """
    data = reg.list_items(kind=kind) if hasattr(reg, "list_items") else {"targets": [], "candidates": []}
    targets = list(data.get("targets") or [])
    candidates = list(data.get("candidates") or [])
    rows: list = []
    for t in targets:
        row = dict(t)
        row.setdefault("status", "enabled" if t.get("enabled") else "disabled")
        row.setdefault("type", "group" if (t.get("username") or "").endswith("@chatroom") else "contact")
        rows.append(row)
    if kind in ("all", "pending"):
        pending_rows: list = []
        configured_users = {(t.get("username") or "").lower() for t in rows}
        for c in candidates:
            username = (c.get("username") or "")
            if not username:
                continue
            if username.lower() in configured_users:
                # Already a real target; never re-list as pending.
                continue
            if (c.get("status") or "pending") in ("enabled",):
                # Stale "enabled" candidate with no matching target: treat as pending
                # without rewriting the on-disk candidates file.
                c = dict(c)
                c["status"] = "pending"
                c.pop("enabled_at", None)
            pending_rows.append({
                "name": c.get("name") or username,
                "username": username,
                "db": c.get("db") or "message_0.db",
                "table": c.get("table") or "",
                "last_local_id": int(c.get("last_local_id") or 0),
                "enabled": False,
                "status": "pending",
                "triggers": [],
                "knowledge_bases": c.get("suggested_knowledge_bases") or [],
                "type": c.get("type") or "group",
                "is_candidate": True,
            })
    if kind == "enabled":
        rows = [r for r in rows if r.get("status") == "enabled"]
    elif kind == "disabled":
        rows = [r for r in rows if r.get("status") == "disabled"]
    elif kind == "pending":
        rows = pending_rows
    else:
        rows = rows + pending_rows
    return rows, candidates


def _local_day_start(now: Optional[float] = None) -> float:
    """Return the Unix timestamp for the start of the local day."""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _overview_targets_map(cfg: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, bool]]:
    """Return (name_map, enabled_map) for configured targets by username."""
    name_map: Dict[str, str] = {}
    enabled_map: Dict[str, bool] = {}
    for t in cfg.get("targets") or []:
        tid = t.get("username")
        if not tid:
            continue
        name_map[tid] = t.get("name") or tid
        enabled_map[tid] = bool(t.get("enabled"))
    return name_map, enabled_map


def _overview_zero_totals() -> Dict[str, int]:
    return {"received": 0, "replied": 0, "failed": 0, "escalated": 0, "dead": 0}


def _overview_today() -> Dict[str, Any]:
    """Return today's per-target and aggregate pipeline counts.

    If the pipeline DB is unreachable the response still contains the
    configured target list with all counts set to zero plus a warning.
    """
    cfg = reg.load_config()
    db_path = reliable_worker._resolve_db_path(cfg, None)
    t0 = _local_day_start()
    date_str = time.strftime("%Y-%m-%d", time.localtime(t0))
    name_map, enabled_map = _overview_targets_map(cfg)
    zero_totals = _overview_zero_totals()

    targets: Dict[str, Dict[str, Any]] = {}
    for tid, name in name_map.items():
        targets[tid] = {
            "target_id": tid,
            "name": name,
            "enabled": enabled_map.get(tid, False),
            **zero_totals,
        }

    metrics = [
        ("received", "SELECT target_id, COUNT(*) FROM inbound_events WHERE received_at >= ? GROUP BY target_id"),
        ("replied", "SELECT target_id, COUNT(*) FROM send_outbox WHERE status='sent' AND sent_at >= ? GROUP BY target_id"),
        ("failed", "SELECT target_id, COUNT(*) FROM turn_jobs WHERE status IN ('failed','timeout') AND finished_at >= ? GROUP BY target_id"),
        ("escalated", "SELECT target_id, COUNT(*) FROM turn_jobs WHERE status='escalated' AND finished_at >= ? GROUP BY target_id"),
        ("dead", "SELECT target_id, COUNT(*) FROM send_outbox WHERE status='dead_letter' AND dead_at >= ? GROUP BY target_id"),
    ]

    try:
        conn = reliable_pipeline.open_db(db_path)
        for key, sql in metrics:
            cur = conn.execute(sql, (t0,))
            for row in cur.fetchall():
                tid = str(row[0])
                cnt = int(row[1] or 0)
                if tid not in targets:
                    targets[tid] = {
                        "target_id": tid,
                        "name": name_map.get(tid, tid),
                        "enabled": enabled_map.get(tid, False),
                        **zero_totals,
                    }
                targets[tid][key] = cnt
    except Exception:
        return {
            "generated_at": time.time(),
            "date": date_str,
            "totals": zero_totals,
            "targets": list(targets.values()),
            "warning": "pipeline db unavailable",
        }

    target_list = list(targets.values())
    totals = {key: sum(t.get(key, 0) for t in target_list) for key in zero_totals}
    return {
        "generated_at": time.time(),
        "date": date_str,
        "totals": totals,
        "targets": target_list,
    }


def _overview_history(days: int) -> Dict[str, Any]:
    """Return daily series and per-target aggregates for the last N days.

    Missing days are zero-filled. The DB-unavailable path returns a fully
    zero-filled series and an empty per_target list with a warning.
    """
    days = max(1, min(62, int(days)))
    cfg = reg.load_config()
    db_path = reliable_worker._resolve_db_path(cfg, None)
    t0 = _local_day_start() - (days - 1) * 86400
    name_map, _ = _overview_targets_map(cfg)
    zero_series = {"received": 0, "replied": 0, "failed": 0, "dead": 0}

    metrics = [
        ("received", "SELECT date(received_at, 'unixepoch', 'localtime') AS d, target_id, COUNT(*) FROM inbound_events WHERE received_at >= ? GROUP BY d, target_id"),
        ("replied", "SELECT date(sent_at, 'unixepoch', 'localtime') AS d, target_id, COUNT(*) FROM send_outbox WHERE status='sent' AND sent_at >= ? GROUP BY d, target_id"),
        ("failed", "SELECT date(finished_at, 'unixepoch', 'localtime') AS d, target_id, COUNT(*) FROM turn_jobs WHERE status IN ('failed','timeout') AND finished_at >= ? GROUP BY d, target_id"),
        ("dead", "SELECT date(dead_at, 'unixepoch', 'localtime') AS d, target_id, COUNT(*) FROM send_outbox WHERE status='dead_letter' AND dead_at >= ? GROUP BY d, target_id"),
    ]

    series: Dict[str, Dict[str, Any]] = {}
    per_target: Dict[str, Dict[str, Any]] = {}

    try:
        conn = reliable_pipeline.open_db(db_path)
        for key, sql in metrics:
            cur = conn.execute(sql, (t0,))
            for row in cur.fetchall():
                d = str(row[0])
                tid = str(row[1])
                cnt = int(row[2] or 0)
                if d not in series:
                    series[d] = {"date": d, **zero_series}
                series[d][key] += cnt
                if tid not in per_target:
                    per_target[tid] = {
                        "target_id": tid,
                        "name": name_map.get(tid, tid),
                        **zero_series,
                    }
                per_target[tid][key] += cnt
    except Exception:
        dates = [time.strftime("%Y-%m-%d", time.localtime(t0 + i * 86400)) for i in range(days)]
        return {
            "days": days,
            "series": [{"date": d, **zero_series} for d in dates],
            "per_target": [],
            "warning": "pipeline db unavailable",
        }

    result_series = []
    for i in range(days):
        d = time.strftime("%Y-%m-%d", time.localtime(t0 + i * 86400))
        result_series.append(series.get(d, {"date": d, **zero_series}))

    return {
        "days": days,
        "series": result_series,
        "per_target": list(per_target.values()),
    }


def _register_agent_instance(instance: Any) -> Dict[str, Any]:
    if not isinstance(instance, dict):
        raise ValueError("instance must be an object")
    instance_id = str(instance.get("id") or "").strip()
    provider = str(instance.get("provider") or instance.get("type") or "").strip().lower()
    if not instance_id:
        raise ValueError("instance.id is required")
    if provider not in ("hermes", "echo"):
        raise ValueError("unsupported instance provider: %s" % (provider or "(empty)"))
    normalized = dict(instance)
    normalized["id"] = instance_id
    normalized["provider"] = provider
    if provider == "hermes" and not str(normalized.get("profile") or "").strip():
        raise ValueError("hermes instance profile is required")
    cfg = reg.load_config(reg.CONFIG_PATH)
    ap_cfg = cfg.setdefault("agent_provider", {})
    if not isinstance(ap_cfg, dict):
        ap_cfg = {}
        cfg["agent_provider"] = ap_cfg
    instances = ap_cfg.setdefault("instances", [])
    if not isinstance(instances, list):
        instances = []
        ap_cfg["instances"] = instances
    for idx, existing in enumerate(instances):
        if isinstance(existing, dict) and str(existing.get("id") or "").strip() == instance_id:
            merged = dict(existing)
            for key, value in normalized.items():
                if value not in (None, ""):
                    merged[key] = value
            instances[idx] = merged
            reg.save_json_atomic(reg.CONFIG_PATH, cfg)
            return {"created": False, "instance": merged}
    instances.append(normalized)
    reg.save_json_atomic(reg.CONFIG_PATH, cfg)
    return {"created": True, "instance": normalized}


def _build_agent_provider(provider_name: str | None = None, instance_id: str | None = None):
    if provider_name == "echo" and not instance_id:
        return EchoAgentProvider(worker_id="control-api-echo")
    cfg = reg.load_config()
    if instance_id:
        return provider_from_config(cfg, instance_id=instance_id)
    if provider_name:
        cfg.setdefault("agent_provider", {})
    if provider_name and isinstance(cfg.get("agent_provider"), dict):
        cfg["agent_provider"]["default"] = provider_name
    return provider_from_config(cfg)


def _cached_provider_health(cache_key: str, provider: Any, *, ttl: float = 20.0) -> Dict[str, Any]:
    """Cache provider health checks so CLI providers do not spawn every UI/loop tick."""
    now = time.time()
    cached = _PROVIDER_HEALTH_CACHE.get(cache_key)
    if cached and now - float(cached[0]) <= max(1.0, float(ttl)):
        return dict(cached[1])
    health = provider.health().to_dict()
    _PROVIDER_HEALTH_CACHE[cache_key] = (now, dict(health))
    return health


def _agent_instance_kind(instance_id: str | None) -> str:
    if not instance_id:
        return "hermes"
    cfg = reg.load_config()
    for inst in list_agent_instances(cfg):
        if str(inst.get("id") or "") == str(instance_id):
            return str(inst.get("provider") or inst.get("type") or "hermes").lower()
    return ""


def _agent_pool_status() -> Dict[str, Any]:
    """Return per-instance on-duty/busy/health state for the M6 pool UI."""
    cfg = reg.load_config()
    loop = _async_loop_status()
    raw_loop_cfg = loop.get("config")
    loop_cfg: Dict[str, Any] = raw_loop_cfg if isinstance(raw_loop_cfg, dict) else {}
    on_duty_ids = [str(x) for x in (loop_cfg.get("instance_ids") or ([] if not loop_cfg.get("instance_id") else [loop_cfg.get("instance_id")]))]
    active_statuses = {
        agent_jobs.STATUS_DISPATCHING,
        agent_jobs.STATUS_SUBMITTED,
        agent_jobs.STATUS_AGENT_RUNNING,
        agent_jobs.STATUS_RUNNING,
    }
    active_jobs = [j for j in agent_jobs.list_jobs(limit=200) if j.get("status") in active_statuses]
    rows = []
    for inst in list_agent_instances(cfg):
        iid = str(inst.get("id") or "")
        kind = str(inst.get("provider") or inst.get("type") or "hermes").lower()
        health = None
        try:
            provider = provider_from_config(cfg, instance_id=iid)
            health = _cached_provider_health("pool:%s" % iid, provider, ttl=20.0)
        except Exception as e:
            health = {"ok": False, "ready": False, "error": repr(e), "provider": kind}
        current = None
        for job in active_jobs:
            payload = job.get("payload") or {}
            if isinstance(payload, dict) and str(payload.get("agent_instance_id") or "") == iid:
                current = job
                break
        ready = bool((health or {}).get("ready") or (health or {}).get("ok"))
        status = "offline"
        if iid in on_duty_ids:
            status = "busy" if current else ("idle" if ready else "offline")
        elif ready:
            status = "available"
        rows.append({
            "id": iid,
            "label": inst.get("label") or iid,
            "provider": kind,
            "on_duty": iid in on_duty_ids,
            "reliable_on_duty": bool(inst.get("reliable_on_duty")),
            "status": status,
            "health": health,
            "current_job": None if not current else {
                "id": current.get("id"),
                "target_name": current.get("target_name"),
                "sender": current.get("sender"),
                "task_type": current.get("task_type"),
                "status": current.get("status"),
                "created_at": current.get("created_at"),
                "submitted_at": current.get("submitted_at"),
                "external_provider": current.get("external_provider"),
                "external_session_id": current.get("external_session_id"),
            },
        })
    return {"running": bool(loop.get("running")), "loop": loop, "instances": rows}



# ---------------------------------------------------------------------------
# Stage 1 reliable pipeline: helpers + sanitizers + run-once / status
# ---------------------------------------------------------------------------



# Strict scalar whitelist per row kind. We keep ONLY:
#   * opaque identifiers (``id``, ``turn_id``, ``job_id``, ``target_id``,
#     ``group_key``)
#   * status enum values
#   * numeric counters / lease timestamps
#   * creation / sent / deadline / next_attempt timestamps
# We deliberately drop every string that could carry content:
#   * ``payload_json`` / ``result_json`` (raw contract bodies)
#   * ``reply_text`` / ``mention_name`` (send-side content)
#   * ``error`` (the worker embeds reply snippets here, see
#     ``reliable_worker.process_once`` failure paths)
#   * ``reason`` / ``attempted`` / ``exception`` (sender diagnostic
#     strings that may echo user content or exception reprs)
#   * ``outbox_key`` / ``job_key`` / ``lease_owner`` (low-information
#     identifiers that are unnecessary for operational status)
# This satisfies the no-body-leak guarantee even when the underlying
# schemas add fields or the worker starts embedding extra diagnostics.
_RELIABLE_JOB_FIELDS: Tuple[str, ...] = (
    "id", "turn_id", "target_id", "group_key",
    "status",
    "attempts",
    "deadline_at",
    "created_at", "started_at", "finished_at",
)
_RELIABLE_OUTBOX_FIELDS: Tuple[str, ...] = (
    "id", "job_id", "target_id", "group_key",
    "before_local_id",
    "status",
    "attempts", "max_attempts", "next_attempt_at",
    "created_at", "sent_at", "dead_at",
)
_RELIABLE_DEAD_LETTER_FIELDS: Tuple[str, ...] = (
    "id", "job_id", "target_id", "group_key",
    "status",
    "attempts", "max_attempts",
    "created_at", "dead_at", "next_attempt_at",
    # Stage 5 console: operators need send_started_at to know whether the
    # safe requeue path will accept the row (NULL only).  Scalar timestamp
    # only — never reply_text / error / result_json.
    "send_started_at",
)
_RELIABLE_PROCESSED_FIELDS: Tuple[str, ...] = (
    "outbox_id", "target_id", "ok",
)
_RELIABLE_SKIPPED_FIELDS: Tuple[str, ...] = (
    "outbox_id", "target_id",
)


def _reliable_pipeline_section(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the ``config['reliable_pipeline']`` section, or ``{}``."""
    if not isinstance(config, dict):
        return {}
    section = config.get("reliable_pipeline")
    return section if isinstance(section, dict) else {}


def _reliable_pipeline_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """``True`` only when ``config.reliable_pipeline.enabled`` is literally ``True``.

    Default ``False`` — the reliable pipeline must be opted in
    explicitly.  Truthy non-``True`` values (``1``, ``"yes"``,
    ``"true"``, etc.) do NOT enable it: this keeps a misconfigured
    string from accidentally opening worker/sender run-once against the
    production WeChat pipeline.
    """
    return _reliable_pipeline_section(config).get("enabled") is True


def _project_row(row: Any, fields: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """Whitelist-projection: keep only ``fields`` from ``row`` (dict or sqlite3.Row)."""
    if row is None:
        return None
    out: Dict[str, Any] = {}
    for key in fields:
        if not isinstance(row, dict) and not hasattr(row, "keys"):
            break
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            continue
        out[key] = value
    return out


def _sanitize_process_once_result(result: Any) -> Dict[str, Any]:
    """Strip body content from a ``process_once`` result before returning."""
    if not isinstance(result, dict):
        return {"status": "unknown", "job": None, "outbox": None}
    sanitized: Dict[str, Any] = {"status": result.get("status")}
    if "job" in result:
        sanitized["job"] = _project_row(result.get("job"), _RELIABLE_JOB_FIELDS)
    if "outbox" in result:
        sanitized["outbox"] = _project_row(result.get("outbox"), _RELIABLE_OUTBOX_FIELDS)
    return sanitized


def _sanitize_send_outcome(outcome: Any, fields: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    if not isinstance(outcome, dict):
        return None
    row = outcome.get("row")
    projected_row = _project_row(row, _RELIABLE_OUTBOX_FIELDS)
    out: Dict[str, Any] = {}
    for key in fields:
        if key in outcome:
            out[key] = outcome.get(key)
    if projected_row is not None:
        out["row"] = projected_row
    return out


def _sanitize_send_once_result(result: Any) -> Dict[str, Any]:
    """Strip body content from a ``send_once`` result before returning."""
    if not isinstance(result, dict):
        return {"status": "unknown", "processed": [], "skipped": []}
    processed_raw = result.get("processed") or []
    skipped_raw = result.get("skipped") or []
    return {
        "status": result.get("status"),
        "processed": [
            _sanitize_send_outcome(item, _RELIABLE_PROCESSED_FIELDS)
            for item in processed_raw if isinstance(item, dict)
        ],
        "skipped": [
            _sanitize_send_outcome(item, _RELIABLE_SKIPPED_FIELDS)
            for item in skipped_raw if isinstance(item, dict)
        ],
    }


def _sanitize_dead_letter(row: Any) -> Optional[Dict[str, Any]]:
    """Project a dead-letter row down to scalar metadata only.

    Also stamps ``requeue_safe`` so the control console can disable the
    requeue button when ``send_started_at`` is set (post-send-start archive).
    """
    projected = _project_row(row, _RELIABLE_DEAD_LETTER_FIELDS)
    if projected is None:
        return None
    # Always surface the key so UI clients need not special-case missing fields.
    if "send_started_at" not in projected:
        projected["send_started_at"] = None
    projected["requeue_safe"] = projected.get("send_started_at") is None
    return projected


def _reliable_worker_run_once(body: Dict[str, Any]) -> Dict[str, Any]:
    """Process one durable turn job through ``reliable_worker.process_once``.

    The reliable pipeline must be explicitly enabled via
    ``config['reliable_pipeline']['enabled']``; otherwise this returns
    ``{"action": "disabled"}`` without invoking the worker.

    The deterministic safety filter is always
    ``reply_engine.postcheck`` — there is no body knob that can bypass it.
    The provider is resolved by ``process_once`` using the immutable
    ``provider_factory`` built from this config; the legacy ``provider`` is a
    fallback when the target binding is missing or the factory fails.
    ``test_target_only`` lives in the same config and is therefore
    unspoofable by the HTTP body.

    When ``body.get("instance_id")`` is provided, only jobs whose payload
    carries the same ``dedicated_agent_instance_id`` are claimed. The
    scheduler uses this to route dedicated targets to their matching instance;
    unbound jobs are drained only by the general run.
    """
    cfg = reg.load_config()
    if not _reliable_pipeline_enabled(cfg):
        return {
            "action": "disabled",
            "enabled": False,
            "reason": "config.reliable_pipeline.enabled is not true",
        }
    owner = str(body.get("owner") or reliable_worker.DEFAULT_REPLY_OWNER).strip() or reliable_worker.DEFAULT_REPLY_OWNER
    timeout_raw = body.get("timeout")
    timeout: Optional[float]
    try:
        timeout = float(timeout_raw) if timeout_raw is not None else None
    except (TypeError, ValueError):
        return _err_reliable("timeout must be a number")
    instance_id = str(body.get("instance_id") or "").strip() or None
    provider = _build_agent_provider()
    def _provider_factory(instance_id=None):
        return provider_from_config(cfg, instance_id=instance_id)
    try:
        result = reliable_worker.process_once(
            provider=provider,
            owner=owner,
            config=cfg,
            provider_factory=_provider_factory,
            final_filter=reply_engine.postcheck,
            timeout=timeout,
            instance_id=instance_id,
        )
    except reliable_worker.FinalFilterRequired:
        # Programming error: control_api always passes reply_engine.postcheck.
        # We never echo the exception message because FinalFilterRequired
        # formats include the runtime stack frame, which could leak paths.
        return _err_reliable("final_filter is required")
    except Exception:
        # Worker / provider raised.  We deliberately do not include the
        # exception repr in the response: provider exceptions frequently
        # embed filesystem paths and outbound URLs that should never
        # leave the control plane.
        return _err_reliable("worker raised; see control_api log")
    return {"action": "processed", "instance_id": instance_id, **_sanitize_process_once_result(result)}


def _reliable_sender_run_once(body: Dict[str, Any]) -> Dict[str, Any]:
    """Claim and dispatch one batch of sendable outbox rows.

    Requires ``config['reliable_pipeline']['enabled']``. The
    ``test_target_only`` gate is enforced inside ``send_once`` itself;
    the body cannot override it because it is read from the same config
    the worker reads.
    """
    cfg = reg.load_config()
    if not _reliable_pipeline_enabled(cfg):
        return {
            "action": "disabled",
            "enabled": False,
            "reason": "config.reliable_pipeline.enabled is not true",
        }
    owner = str(body.get("owner") or reliable_worker.DEFAULT_SEND_OWNER).strip() or reliable_worker.DEFAULT_SEND_OWNER
    limit_raw = body.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else reliable_worker.DEFAULT_SEND_LIMIT
    except (TypeError, ValueError):
        return _err_reliable("limit must be an integer", status=400)
    try:
        result = reliable_worker.send_once(
            owner=owner,
            config=cfg,
            limit=max(1, int(limit)),
            confirm=lambda target, before_local_id, text, timeout: wait_sent_confirmation(
                target, before_local_id, text, config_path=reg.CONFIG_PATH, timeout=timeout,
            ),
        )
    except Exception:
        # See _reliable_worker_run_once: do not echo the exception repr.
        return _err_reliable("sender raised; see control_api log", status=500)
    return {"action": "processed", **_sanitize_send_once_result(result)}


def _reliable_pipeline_status() -> Dict[str, Any]:
    """Return ``enabled``, scalar ``counts``, and sanitized dead-letter summaries.

    Never echoes reply text, payload JSON, result JSON, error strings,
    DB path values, or any exception repr: the only caller-visible
    scalar set is the whitelist in ``_RELIABLE_DEAD_LETTER_FIELDS``.
    When the database is unreachable the response carries a fixed
    ``db_status: "unavailable"`` marker and empty ``counts`` /
    ``dead_letters`` — no exception text or filesystem path is ever
    returned, so the status surface cannot leak either diagnostics
    or absolute filesystem locations.
    """
    cfg = reg.load_config()
    section = _reliable_pipeline_section(cfg)
    db_path = reliable_worker._resolve_db_path(cfg, None)
    try:
        counts = reliable_pipeline.counts(db_path=db_path)
        raw_dl = reliable_pipeline.list_dead_letters(limit=50, db_path=db_path)
    except Exception:
        return {
            "enabled": bool(section.get("enabled")),
            "test_target_only": bool(section.get("test_target_only")),
            "db_status": "unavailable",
            "counts": {},
            "dead_letters": [],
        }
    dead_letters = [item for item in (_sanitize_dead_letter(row) for row in raw_dl) if item]
    return {
        "enabled": bool(section.get("enabled")),
        "test_target_only": bool(section.get("test_target_only")),
        "db_status": "ok",
        "counts": counts,
        "dead_letters": dead_letters,
    }


def _err_reliable(msg: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"action": "error", "ok": False, "error": msg}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Stage 1 reliable pipeline scheduler
# ---------------------------------------------------------------------------

def _reliable_result_is_error(result: Any) -> bool:
    """Return True when a run-once helper reports a handled error dict."""
    return isinstance(result, dict) and result.get("ok") is False and result.get("action") == "error"


def _resolve_scheduler_interval(config: Optional[Dict[str, Any]]) -> float:
    """Read scheduler interval from config with a small documented default."""
    section = _reliable_pipeline_section(config)
    try:
        return max(0.2, float(section.get("scheduler_interval_seconds") or _RELIABLE_SCHEDULER_DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        return _RELIABLE_SCHEDULER_DEFAULT_INTERVAL


def _active_on_duty_instance_ids() -> List[str]:
    """Return instance IDs that are on-duty and healthy enough to run durable jobs.

    On-duty state comes from two sources:
    - Runtime manual overrides via the agent pool / on-duty endpoint.
    - Persistent config flag ``reliable_on_duty: true`` on agent_provider instances.
      This makes dedicated reliable routing survive control_api restarts without
      requiring a manual /agent/instance/{id}/on-duty call after startup.

    In both cases the instance must pass a health check; unhealthy instances are
    not routed dedicated jobs.
    """
    try:
        pool_status = _agent_pool_status()
    except Exception:
        return []
    health_by_id = {}
    active_ids = set()
    for inst in (pool_status.get("instances") or []):
        iid = str(inst.get("id") or "").strip()
        if not iid:
            continue
        health_by_id[iid] = inst.get("health") or {}
        if not inst.get("on_duty"):
            continue
        health = health_by_id[iid]
        if health.get("ok") or health.get("ready"):
            active_ids.add(iid)
    # Persistent on-duty instances declared in config
    try:
        cfg = reg.load_config()
    except Exception:
        cfg = None
    if isinstance(cfg, dict):
        instances = (cfg.get("agent_provider") or {}).get("instances") or []
        for inst in instances:
            if isinstance(inst, dict) and inst.get("reliable_on_duty"):
                iid = str(inst.get("id") or "").strip()
                if not iid:
                    continue
                health = health_by_id.get(iid) or {}
                if health.get("ok") or health.get("ready"):
                    active_ids.add(iid)
    return [iid for iid in active_ids if iid]


def _reliable_scheduler_tick() -> None:
    """Run one worker-then-sender iteration and record the outcome.

    Worker and sender are isolated: an unexpected exception in the worker
    never prevents the sender from draining previously ready outbox rows,
    and an exception in the sender never masks the worker result.  Exceptions
    are logged at the source; the public state carries only fixed safe markers
    and action summaries so no exception repr or helper-provided error text can
    leak through the status route.  Handled error dicts are logged as warnings
    without interpolating their text so user content is never written to logs.

    A general worker run drains only unbound jobs; each on-duty instance then
    gets a dedicated run that can claim only jobs bound to that instance.
    """
    worker_result: Any = None
    sender_result: Any = None
    worker_error = ""
    sender_error = ""

    try:
        worker_result = _reliable_worker_run_once({})
    except Exception:
        logger.exception("Reliable scheduler worker iteration failed")
        worker_error = "worker iteration error"

    # Route dedicated jobs only to instances that are on-duty and healthy.
    on_duty_ids = _active_on_duty_instance_ids()
    for iid in on_duty_ids:
        try:
            _reliable_worker_run_once({"instance_id": iid})
        except Exception:
            logger.exception("Reliable scheduler dedicated iteration failed instance_id=%s", iid)

    # Always run the sender so previously ready outbox rows can drain even when
    # the worker iteration raises or reports an error.
    try:
        sender_result = _reliable_sender_run_once({})
    except Exception:
        logger.exception("Reliable scheduler sender iteration failed")
        sender_error = "sender iteration error"

    # If a helper returned a handled error dict, use a fixed safe marker in the
    # public state and log only a fixed safe warning (do not interpolate text).
    if _reliable_result_is_error(worker_result):
        logger.warning("Reliable scheduler worker returned a handled error")
        worker_error = "worker returned error"
    if _reliable_result_is_error(sender_result):
        logger.warning("Reliable scheduler sender returned a handled error")
        sender_error = "sender returned error"
    error = worker_error or sender_error or ""
    with _RELIABLE_SCHEDULER_LOCK:
        _RELIABLE_SCHEDULER_STATE["last_worker"] = _scheduler_action_summary(worker_result)
        _RELIABLE_SCHEDULER_STATE["last_sender"] = _scheduler_action_summary(sender_result)
        _RELIABLE_SCHEDULER_STATE["iterations"] = int(_RELIABLE_SCHEDULER_STATE.get("iterations") or 0) + 1
        _RELIABLE_SCHEDULER_STATE["last_error"] = error


def _scheduler_action_summary(result: Any) -> Dict[str, Any]:
    """Return a safe action summary, omitting any helper error text."""
    if not isinstance(result, dict):
        return {"action": "unknown"}
    return {"action": result.get("action") or "unknown"}


def _reliable_scheduler_main(config: Dict[str, Any]) -> None:
    """Background loop: one tick per interval until stopped."""
    interval = float(config.get("interval") or _RELIABLE_SCHEDULER_DEFAULT_INTERVAL)
    with _RELIABLE_SCHEDULER_LOCK:
        _RELIABLE_SCHEDULER_STATE["interval"] = interval
    while not _RELIABLE_SCHEDULER_STOP.is_set():
        try:
            _reliable_scheduler_tick()
        except Exception:
            # Defensive: tick catches per-step errors, so this should be rare.
            logger.exception("Reliable scheduler tick failed")
            with _RELIABLE_SCHEDULER_LOCK:
                _RELIABLE_SCHEDULER_STATE["last_error"] = "scheduler tick error"
        _RELIABLE_SCHEDULER_STOP.wait(max(0.2, interval))
    with _RELIABLE_SCHEDULER_LOCK:
        _RELIABLE_SCHEDULER_STATE["running"] = False
        _RELIABLE_SCHEDULER_STATE["stopped_at"] = time.time()


def _reliable_scheduler_status() -> Dict[str, Any]:
    """Return scheduler state: running, interval, iterations, last action/error."""
    with _RELIABLE_SCHEDULER_LOCK:
        state = dict(_RELIABLE_SCHEDULER_STATE)
    thread = _RELIABLE_SCHEDULER_THREAD
    state["running"] = bool(thread and thread.is_alive() and not _RELIABLE_SCHEDULER_STOP.is_set())
    state["thread_alive"] = bool(thread and thread.is_alive())
    return state


def _reliable_scheduler_start(body: Dict[str, Any]) -> Dict[str, Any]:
    """Start the dedicated reliable-pipeline scheduler thread.

    Idempotent: duplicate starts return ``already_running``.  Gated by
    ``config['reliable_pipeline']['enabled']`` so the scheduler cannot be
    manually started against a disabled pipeline.
    """
    global _RELIABLE_SCHEDULER_THREAD
    try:
        cfg = reg.load_config()
    except Exception:
        logger.exception("Reliable scheduler start failed to read config")
        return _err_reliable("scheduler start failed; see control_api log")
    if not _reliable_pipeline_enabled(cfg):
        return {
            "action": "disabled",
            "enabled": False,
            "reason": "config.reliable_pipeline.enabled is not true",
        }
    try:
        with _RELIABLE_SCHEDULER_LOCK:
            if _RELIABLE_SCHEDULER_THREAD and _RELIABLE_SCHEDULER_THREAD.is_alive():
                return {"action": "already_running", **_reliable_scheduler_status()}
            interval = _resolve_scheduler_interval(cfg)
            config = {"interval": interval}
            _RELIABLE_SCHEDULER_STOP.clear()
            _RELIABLE_SCHEDULER_STATE.update({
                "running": True,
                "started_at": time.time(),
                "stopped_at": None,
                "iterations": 0,
                "last_error": "",
                "last_worker": None,
                "last_sender": None,
                "interval": interval,
                "config": config,
            })
            _RELIABLE_SCHEDULER_THREAD = threading.Thread(
                target=_reliable_scheduler_main,
                args=(config,),
                name="wechat-reliable-scheduler",
                daemon=True,
            )
            _RELIABLE_SCHEDULER_THREAD.start()
        return {"action": "started", **_reliable_scheduler_status()}
    except Exception:
        logger.exception("Reliable scheduler start failed")
        return _err_reliable("scheduler start failed; see control_api log")


def _reliable_scheduler_stop() -> Dict[str, Any]:
    """Signal the scheduler to stop and wait for the thread to finish."""
    try:
        _RELIABLE_SCHEDULER_STOP.set()
        thread = _RELIABLE_SCHEDULER_THREAD
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        with _RELIABLE_SCHEDULER_LOCK:
            _RELIABLE_SCHEDULER_STATE["running"] = False
            _RELIABLE_SCHEDULER_STATE["stopped_at"] = time.time()
        return {"action": "stopped" if not (thread and thread.is_alive()) else "stop_requested", **_reliable_scheduler_status()}
    except Exception:
        logger.exception("Reliable scheduler stop failed")
        return _err_reliable("scheduler stop failed; see control_api log")


def _reliable_scheduler_auto_start() -> Optional[Dict[str, Any]]:
    """Auto-start the scheduler when the reliable pipeline is enabled.

    Called once during control_api startup; failures are swallowed so a
    config problem does not prevent the rest of the control plane from serving.
    """
    try:
        cfg = reg.load_config()
    except Exception:
        return None
    if not _reliable_pipeline_enabled(cfg):
        return None
    return _reliable_scheduler_start({})


def _reliable_quarantine_turn_job(job_id: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """Move a queued/running reliable turn job to a terminal failed state.

    Uses ``reliable_pipeline.fail_job(..., retryable=False)`` so the job can
    never re-enter dispatch and no send_outbox row is created.  The action is
    explicit and non-retryable.  Unlike worker/sender run-once, this endpoint is
    intentionally available even when the reliable pipeline is globally disabled
    so legacy queued jobs can be safely quarantined before enabling the pipeline.
    """
    try:
        cfg = reg.load_config()
        job_id = int(job_id)
        if job_id <= 0:
            return _err_reliable("job_id must be a positive integer")
        db_path = reliable_worker._resolve_db_path(cfg, None)
        reason = str(body.get("reason") or "quarantined by control-api").strip()
        if not reason:
            reason = "quarantined by control-api"
        quarantined = reliable_pipeline.fail_job(
            job_id=job_id,
            error=reason,
            retryable=False,
            db_path=db_path,
        )
    except (TypeError, ValueError):
        return _err_reliable("job_id must be a positive integer")
    except Exception:
        logger.exception("Reliable quarantine failed")
        return _err_reliable("quarantine failed; see control_api log")
    return {
        "action": "quarantined" if quarantined else "not_quarantined",
        "job_id": job_id,
        "retryable": False,
        "quarantined": bool(quarantined),
    }


def _reliable_requeue_outbox(outbox_id: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """Recover a dead-letter outbox row that provably never reached the sender.

    Safe path only: delegates to ``reliable_pipeline.requeue_dead_letter``,
    which refuses any row whose ``send_started_at`` is set (i.e. any row a send
    was ever authorized for).  The pre-tracking legacy override
    (``recover_legacy_gate_rejection``) is deliberately NOT exposed over HTTP —
    it requires manual content verification and is invoked directly.  ``actor``
    is fixed to ``control_api`` because this surface has no caller
    authentication, so a client-supplied identity would not be trustworthy.
    Available even when the pipeline is globally disabled (like quarantine) so
    safe tracked dead-letters (``send_started_at IS NULL``) can be recovered
    before enabling; pre-tracking legacy rows are refused here by design.
    """
    try:
        cfg = reg.load_config()
        outbox_id = int(outbox_id)
        if outbox_id <= 0:
            return _err_reliable("outbox_id must be a positive integer")
        db_path = reliable_worker._resolve_db_path(cfg, None)
        if not isinstance(body, dict):
            return _err_reliable("request body must be a JSON object")
        reason = str(body.get("reason") or "").strip()
        if not reason:
            return _err_reliable("reason is required")
        updated = reliable_pipeline.requeue_dead_letter(
            outbox_id=outbox_id,
            reason=reason,
            actor="control_api",
            db_path=db_path,
        )
    except (TypeError, ValueError) as exc:
        return _err_reliable("requeue rejected: %s" % (exc,))
    except Exception:
        logger.exception("Reliable outbox requeue failed")
        return _err_reliable("requeue failed; see control_api log", status=500)
    return {
        "action": "requeued",
        "outbox_id": outbox_id,
        "status": updated.get("status"),
        "requeue_count": updated.get("requeue_count"),
    }


# ---------------------------------------------------------------------------
# M5 async flow: dispatcher / reconciler / sender
# ---------------------------------------------------------------------------

# Fixed safe markers for provider-returned errors in the dispatcher.
# The exception path explicitly uses "submit exception" so that raw exception
# text is never persisted or returned.  A failed submit returns the same
# marker regardless of the underlying provider error.
_SAFE_SUBMIT_ERROR = "submit failed"
_SAFE_SUBMIT_EXCEPTION = "submit exception"
_SAFE_PROVIDER_UNAVAILABLE = "provider unavailable"

# Fixed safe markers for provider-returned poll errors in the reconciler.
# The exception path explicitly uses "poll exception" so that raw exception
# text is never trusted or echoed.
_SAFE_POLL_ERROR = "poll result failed"
_SAFE_POLL_EXPIRED = "poll expired"


def _async_dispatcher_run_once(body: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatcher: claim queued jobs and submit them to external agent.
    
    Transitions: queued -> dispatching -> submitted
    Respects global_external_concurrency (default 1) and per_group_concurrency (default 1).
    """
    t0 = time.time()
    worker_id = str(body.get("worker_id") or "control-dispatcher")
    instance_id = body.get("instance_id")
    instance_provider = _agent_instance_kind(str(instance_id)) if instance_id else str(body.get("provider") or "").strip().lower()
    max_global = int(body.get("max_global_dispatching") or 1)
    per_group = int(body.get("per_group_concurrency") or 1)
    
    provider_name_for_health = instance_provider or None
    try:
        provider = _build_agent_provider(provider_name_for_health, instance_id=instance_id)
        health_dict = _cached_provider_health("dispatch:%s:%s" % (instance_id or "default", getattr(provider, "name", instance_provider or "")), provider, ttl=20.0)
    except Exception:
        health_dict = {"ok": False, "ready": False}
    if not bool(health_dict.get("ok")) or not bool(health_dict.get("ready")):
        return {"ok": True, "action": "provider_unavailable", "instance_id": instance_id,
                "provider": provider_name_for_health or "",
                "error": _SAFE_PROVIDER_UNAVAILABLE, "duration": round(time.time() - t0, 3)}

    job = agent_jobs.claim_dispatchable(
        worker_id=worker_id,
        provider=instance_provider or None,
        max_global_dispatching=max_global,
        per_group_concurrency=per_group,
        instance_id=str(instance_id) if instance_id else None,
    )
    if not job:
        return {"ok": True, "action": "idle", "duration": round(time.time() - t0, 3)}
    
    provider_name = str(job.get("provider") or "").strip().lower() or (instance_provider or None)
    if provider_name and provider_name != str(getattr(provider, "name", "")).lower():
        try:
            provider = _build_agent_provider(provider_name, instance_id=instance_id)
        except Exception:
            transitioned = agent_jobs.fail_job(int(job["id"]), _SAFE_PROVIDER_UNAVAILABLE,
                                                status=agent_jobs.STATUS_FAILED)
            if not transitioned:
                agent_jobs.release_dispatching(int(job["id"]), reason=_SAFE_PROVIDER_UNAVAILABLE)
            return {"ok": False, "action": "submit_failed", "job_id": job["id"],
                    "error": _SAFE_PROVIDER_UNAVAILABLE, "duration": round(time.time() - t0, 3)}
    if instance_id:
        agent_jobs.merge_payload(int(job["id"]), {"agent_instance_id": str(instance_id)})
    
    # Compute timeouts from payload; payload agent_timeout drives agent_deadline_at.
    raw_payload = job.get("payload") or {}
    has_images = isinstance(raw_payload, dict) and bool(raw_payload.get("image_paths"))
    configured_timeout = raw_payload.get("agent_timeout") if isinstance(raw_payload, dict) else None
    try:
        agent_timeout = float(configured_timeout) if configured_timeout is not None else None
    except Exception:
        agent_timeout = None

    if agent_timeout is not None:
        deadline_seconds = agent_timeout
        submit_timeout = min(agent_timeout, 240.0)
    elif has_images:
        deadline_seconds = 600.0
        submit_timeout = 240.0
    else:
        deadline_seconds = 300.0
        submit_timeout = 30.0
    agent_deadline_at = time.time() + deadline_seconds

    # Stage 2: force strict AgentResult contract for every dispatcher-submitted job.
    # Persist the flag before submit so retries/restarts keep the strict contract.
    if not isinstance(job.get("payload"), dict):
        job["payload"] = {}
    job["payload"]["reliable_result_contract"] = True
    if not agent_jobs.merge_payload(int(job["id"]), {"reliable_result_contract": True}):
        agent_jobs.release_dispatching(
            int(job["id"]),
            reason="failed to persist strict contract flag",
        )
        return {"ok": False, "action": "persistence_failed", "job_id": job["id"],
                "duration": round(time.time() - t0, 3)}

    try:
        submit_result = provider.submit(job, timeout=submit_timeout)
    except Exception:
        transitioned = agent_jobs.fail_job(
            int(job["id"]),
            _SAFE_SUBMIT_EXCEPTION,
            status=agent_jobs.STATUS_FAILED,
        )
        if not transitioned:
            agent_jobs.release_dispatching(int(job["id"]), reason=_SAFE_SUBMIT_EXCEPTION)
        return {"ok": False, "action": "submit_failed", "job_id": job["id"],
                "error": _SAFE_SUBMIT_EXCEPTION, "duration": round(time.time() - t0, 3)}

    # Stage 2 Phase A: validate the submit result is a usable AgentResult shape
    # before dereferencing .ok or .raw.  A malformed dataclass or wrong-type
    # raw field could raise or leak provider content, so treat it the same as a
    # submit rejection: fail the job, release the dispatch lock, and return a
    # fixed safe marker with no raw text.
    try:
        is_agent_result = isinstance(submit_result, AgentResult)
        submit_ok = submit_result.ok if is_agent_result else None
        raw = submit_result.raw if is_agent_result else None
    except Exception:
        is_agent_result = False
        submit_ok = None
        raw = None

    if (
        not is_agent_result
        or type(submit_ok) is not bool
        or type(raw) is not dict
    ):
        transitioned = agent_jobs.fail_job(
            int(job["id"]),
            _SAFE_SUBMIT_ERROR,
            status=agent_jobs.STATUS_FAILED,
        )
        if not transitioned:
            agent_jobs.release_dispatching(int(job["id"]), reason=_SAFE_SUBMIT_ERROR)
        return {"ok": False, "action": "submit_failed", "job_id": job["id"],
                "error": _SAFE_SUBMIT_ERROR, "duration": round(time.time() - t0, 3)}

    if not submit_ok:
        transitioned = agent_jobs.fail_job(
            int(job["id"]),
            _SAFE_SUBMIT_ERROR,
            status=agent_jobs.STATUS_FAILED,
        )
        if not transitioned:
            agent_jobs.release_dispatching(int(job["id"]), reason=_SAFE_SUBMIT_ERROR)
        return {"ok": False, "action": "submit_failed", "job_id": job["id"],
                "error": _SAFE_SUBMIT_ERROR, "duration": round(time.time() - t0, 3)}

    bridge_patch = agent_jobs._safe_bridge_patch(raw)

    if type(raw) is not dict:
        transitioned = agent_jobs.fail_job(
            int(job["id"]),
            _SAFE_SUBMIT_ERROR,
            status=agent_jobs.STATUS_FAILED,
        )
        if not transitioned:
            agent_jobs.release_dispatching(int(job["id"]), reason=_SAFE_SUBMIT_ERROR)
        return {"ok": False, "action": "submit_failed", "job_id": job["id"],
                "error": _SAFE_SUBMIT_ERROR, "duration": round(time.time() - t0, 3)}

    if "bridge_session_id" not in raw:
        transitioned = agent_jobs.fail_job(int(job["id"]), "submit returned no session id",
                           status=agent_jobs.STATUS_FAILED)
        if not transitioned:
            agent_jobs.release_dispatching(int(job["id"]), reason=_SAFE_SUBMIT_ERROR)
        return {"ok": False, "action": "no_session", "job_id": job["id"],
                "error": _SAFE_SUBMIT_ERROR, "duration": round(time.time() - t0, 3)}

    if (
        "bridge_session_id" not in bridge_patch
        or "bridge_user_msg_id" not in bridge_patch
        or bridge_patch["bridge_user_msg_id"] == 0
    ):
        transitioned = agent_jobs.fail_job(
            int(job["id"]),
            _SAFE_SUBMIT_ERROR,
            status=agent_jobs.STATUS_FAILED,
        )
        if not transitioned:
            agent_jobs.release_dispatching(int(job["id"]), reason=_SAFE_SUBMIT_ERROR)
        return {"ok": False, "action": "submit_failed", "job_id": job["id"],
                "error": _SAFE_SUBMIT_ERROR, "duration": round(time.time() - t0, 3)}

    session_id = bridge_patch["bridge_session_id"]
    user_msg_id = bridge_patch["bridge_user_msg_id"]
    ok = agent_jobs.mark_submitted(
        int(job["id"]),
        external_provider=provider.name,
        external_session_id=session_id,
        external_user_msg_id=user_msg_id,
        agent_deadline_at=agent_deadline_at,
        next_poll_at=time.time() + 5.0,  # first poll in 5s
    )
    if not ok:
        agent_jobs.mark_submission_failed(
            int(job["id"]),
            external_provider=provider.name,
            external_session_id=session_id,
            external_user_msg_id=user_msg_id,
            error="mark_submitted failed after provider submit",
        )
        return {"ok": False, "action": "mark_submitted_failed", "job_id": job["id"],
                "external_session_id": session_id,
                "external_user_msg_id": user_msg_id,
                "duration": round(time.time() - t0, 3)}
    
    return {"ok": True, "action": "submitted", "job_id": job["id"],
            "external_session_id": session_id,
            "external_user_msg_id": user_msg_id,
            "agent_deadline_at": agent_deadline_at,
            "duration": round(time.time() - t0, 3)}


# Fixed safe markers for provider-returned poll errors in the reconciler.
# The exception path explicitly uses "poll exception" so that raw exception
# text is never trusted or echoed.
_SAFE_POLL_ERROR = "poll result failed"
_SAFE_POLL_EXPIRED = "poll expired"


def _safe_poll_result_snapshot(poll_result: Any) -> Optional[Tuple[AgentResult, Any]]:
    """Validate scalar fields and return safe scalars plus the original raw.

    Reads every field the reconciler dereferences (``ok``, ``status``,
    ``reply_text``, ``error``, ``latency``, ``provider``, ``worker_id``) inside a
    single exception guard, exact-type-checks each captured value, and returns
    the original ``raw`` so the caller can apply ``_safe_bridge_patch`` to it.
    A provider-controlled dict subclass is accepted here; the canonical exact
    ``type(raw) is dict`` policy is enforced later by ``_safe_bridge_patch``.

    Returns ``None`` when the result is not a valid AgentResult shape, so the
    caller can use the standard ``if snapshot is None`` guard.
    """
    if not isinstance(poll_result, AgentResult):
        return None

    try:
        ok = poll_result.ok
        status = poll_result.status
        reply_text = getattr(poll_result, "reply_text", "")
        error = getattr(poll_result, "error", "")
        latency = getattr(poll_result, "latency", 0.0)
        provider = getattr(poll_result, "provider", "")
        worker_id = getattr(poll_result, "worker_id", "")
    except Exception:
        return None

    if type(ok) is not bool:
        return None
    if type(status) is not str:
        return None
    if type(reply_text) is not str:
        return None
    if type(error) is not str and error is not None:
        return None
    if type(latency) not in (int, float):
        return None
    if type(provider) is not str:
        return None
    if type(worker_id) is not str:
        return None

    try:
        raw = getattr(poll_result, "raw", None)
    except Exception:
        raw = None

    return AgentResult(
        ok=ok,
        status=status,
        reply_text=reply_text,
        error=error if error is not None else "",
        latency=float(latency),
        provider=provider,
        worker_id=worker_id,
        raw=None,
    ), raw


def _safe_recover_result_snapshot(recover_result: Any) -> Tuple[Optional[AgentResult], Any]:
    """Validate scalar fields and return safe scalars plus the original raw.

    ``raw`` is read in a separate exception guard so a raising ``raw`` accessor
    does not fail a recovery that otherwise has valid scalar fields.  The
    caller passes the returned ``raw`` to ``_safe_bridge_patch``; that helper
    safely returns ``{}`` for any non-exact dict or invalid bridge value.
    """
    if not isinstance(recover_result, AgentResult):
        return None, None

    try:
        ok = recover_result.ok
        status = recover_result.status
        reply_text = getattr(recover_result, "reply_text", "")
    except Exception:
        return None, None

    if type(ok) is not bool or type(status) is not str or type(reply_text) is not str:
        return None, None

    try:
        raw = getattr(recover_result, "raw", None)
    except Exception:
        raw = None

    return AgentResult(
        ok=ok,
        status=status,
        reply_text=reply_text,
        raw=None,
    ), raw


def _async_reconciler_run_once(body: Dict[str, Any]) -> Dict[str, Any]:
    """Reconciler: poll submitted/agent_running jobs for completion.

    Transitions: submitted/agent_running -> done/failed/expired
    Returns the first batch of results to avoid blocking too long.
    """
    t0 = time.time()
    limit = int(body.get("limit") or 10)
    jobs_to_poll = agent_jobs.list_pollable(limit=limit)
    if not jobs_to_poll:
        return {"ok": True, "action": "idle", "polled": 0, "duration": round(time.time() - t0, 3)}
    
    results = []
    now = time.time()
    for job in jobs_to_poll:
        job_id = int(job["id"])
        session_id = str(job.get("external_session_id") or "")
        user_msg_id = int(job.get("external_user_msg_id") or 0)
        deadline = float(job.get("agent_deadline_at") or 0)
        attempts = int(job.get("reconcile_attempts") or 0)

        # Check if expired first (no poll done yet, no raw to merge)
        now = time.time()
        if deadline and now > deadline:
            agent_jobs.mark_expired(job_id, reason="past deadline")
            results.append({"job_id": job_id, "action": "expired"})
            continue

        # Build provider from job's external_provider.  A construction failure must not
        # escape to the HTTP handler; transition the job to failed with a fixed safe
        # marker and never leak the underlying exception text.
        ext_provider = str(job.get("external_provider") or "hermes")
        payload = job.get("payload") or {}
        instance_id = payload.get("agent_instance_id") if isinstance(payload, dict) else None
        try:
            provider = _build_agent_provider(
                ext_provider, instance_id=str(instance_id) if instance_id else None
            )
        except Exception:
            transitioned = agent_jobs.fail_job(
                job_id, _SAFE_PROVIDER_UNAVAILABLE, status=agent_jobs.STATUS_FAILED
            )
            if not transitioned:
                agent_jobs.release_dispatching(job_id, reason=_SAFE_PROVIDER_UNAVAILABLE)
            results.append(
                {"job_id": job_id, "action": "failed", "error": _SAFE_PROVIDER_UNAVAILABLE}
            )
            continue

        # Poll with exception guard. Never persist/return raw exception text.
        caught_poll_exception = False
        try:
            poll_result = provider.poll(session_id, user_msg_id, timeout=10)
        except Exception:
            caught_poll_exception = True
            poll_result = AgentResult(False, "failed", error="poll exception",
                                       provider=ext_provider, worker_id="reconciler")

        # Validate the returned poll result before any attribute use. Malformed
        # objects, raising property getters, or wrong-typed fields must not
        # escape to the HTTP handler.
        maybe_snapshot = _safe_poll_result_snapshot(poll_result)
        if maybe_snapshot is None:
            agent_jobs.fail_job(job_id, _SAFE_POLL_ERROR, status=agent_jobs.STATUS_FAILED)
            results.append({"job_id": job_id, "action": "failed", "error": _SAFE_POLL_ERROR})
            continue
        snapshot, original_raw = maybe_snapshot

        # Use only the validated snapshot from now on; raw is captured separately
        # so provider-controlled mappings can be read for the contract while
        # bridge metadata is independently rejected by the exact-dict policy.
        poll_result = snapshot

        if caught_poll_exception:
            poll_error_marker = "poll exception"
            poll_expired_marker = "poll exception"
        else:
            poll_error_marker = _SAFE_POLL_ERROR
            poll_expired_marker = _SAFE_POLL_EXPIRED

        contract_raw: Dict[str, Any] = {}
        if isinstance(original_raw, dict):
            try:
                contract_raw = {k: original_raw[k] for k in original_raw}
            except Exception:
                contract_raw = {}
        agent_result = contract_raw.get("agent_result") if type(contract_raw) is dict else None

        # Stage 2 Phase A: strict AgentResult contract is authoritative for jobs that requested it.
        strict_contract = bool(payload.get("reliable_result_contract"))
        if strict_contract:
            # If the provider produced an agent_result, it must parse successfully.
            contract = None
            if agent_result is not None:
                try:
                    contract = reliable_pipeline.parse_agent_result(agent_result)
                except Exception:
                    agent_jobs.fail_job(job_id, "malformed agent_result contract",
                                        status=agent_jobs.STATUS_FAILED)
                    results.append({"job_id": job_id, "action": "failed",
                                    "reason": "malformed agent_result contract"})
                    continue

            # Provider not ready yet: keep polling with backoff, do not fail.
            if poll_result.ok is not True:
                if deadline and now + 30.0 > deadline:
                    agent_jobs.mark_expired(job_id, reason=poll_expired_marker)
                    results.append({"job_id": job_id, "action": "expired", "error": poll_expired_marker})
                else:
                    backoff = min(60.0, 5.0 * (2 ** min(attempts, 3)))
                    agent_jobs.update_poll_state(job_id, next_poll_at=now + backoff,
                                                external_status="error")
                    results.append({"job_id": job_id, "action": "poll_error",
                                    "error": poll_error_marker})
                continue

            # Still running: keep polling.
            if poll_result.status == "running":
                if job.get("status") == agent_jobs.STATUS_SUBMITTED:
                    agent_jobs.mark_agent_running(job_id, next_poll_at=now + 10.0)
                else:
                    # Backoff: 10s, 20s, 40s, max 60s
                    backoff = min(60.0, 10.0 * (2 ** min(attempts, 3)))
                    agent_jobs.update_poll_state(job_id, next_poll_at=now + backoff,
                                                external_status="running")
                results.append({"job_id": job_id, "action": "still_running"})
                continue

            # Terminal success: contract is authoritative; legacy reply_text fallback is rejected.
            if not reliable_worker._provider_status_terminal_success(poll_result.status):
                if deadline and now + 30.0 > deadline:
                    agent_jobs.mark_expired(job_id, reason=poll_expired_marker)
                    results.append({"job_id": job_id, "action": "expired", "error": poll_expired_marker})
                else:
                    backoff = min(60.0, 5.0 * (2 ** min(attempts, 3)))
                    agent_jobs.update_poll_state(job_id, next_poll_at=now + backoff,
                                                external_status="error")
                    results.append({"job_id": job_id, "action": "poll_error",
                                    "error": poll_error_marker})
                continue

            if contract is None:
                agent_jobs.fail_job(job_id, "terminal success missing agent_result contract",
                                    status=agent_jobs.STATUS_FAILED)
                results.append({"job_id": job_id, "action": "failed",
                                "reason": "terminal success missing agent_result contract"})
                continue

            # Apply the validated contract; any terminal success status may carry it.
            if contract.action == "reply":
                reply_text = agent_worker.sanitize_reply_text(
                    contract.reply_text, payload, reg.load_config()
                )
                if not reply_text:
                    agent_jobs.fail_job(job_id, "empty reply_text in strict reply contract",
                                        status=agent_jobs.STATUS_FAILED)
                    results.append({"job_id": job_id, "action": "failed",
                                    "reason": "empty reply_text in strict reply contract"})
                else:
                    ok = agent_jobs.complete_job(job_id, reply_text)
                    if ok:
                        # Persist only safe bridge metadata on terminal success.
                        bridge_patch = agent_jobs._safe_bridge_patch(original_raw)
                        if bridge_patch:
                            agent_jobs.merge_payload(job_id, bridge_patch)
                        agent_jobs.update_poll_state(job_id, next_poll_at=now + 60.0,
                                                    external_status="done")
                        results.append({"job_id": job_id, "action": "completed",
                                        "reply_preview": reply_text[:80]})
                    else:
                        results.append({"job_id": job_id, "action": "complete_failed"})
                continue
            elif contract.action == "silent":
                ok = agent_jobs.complete_job_silent(job_id, reason="silent")
                if ok:
                    # Persist only safe bridge metadata on terminal success.
                    bridge_patch = agent_jobs._safe_bridge_patch(original_raw)
                    if bridge_patch:
                        agent_jobs.merge_payload(job_id, bridge_patch)
                    agent_jobs.update_poll_state(job_id, next_poll_at=now + 60.0,
                                                external_status="silent")
                    results.append({"job_id": job_id, "action": "silent"})
                else:
                    results.append({"job_id": job_id, "action": "complete_failed"})
                continue
            elif contract.action == "escalate":
                ok = agent_jobs.complete_job_silent(job_id, reason="escalate")
                if ok:
                    # Persist only safe bridge metadata on terminal success.
                    bridge_patch = agent_jobs._safe_bridge_patch(original_raw)
                    if bridge_patch:
                        agent_jobs.merge_payload(job_id, bridge_patch)
                    agent_jobs.update_poll_state(job_id, next_poll_at=now + 60.0,
                                                external_status="escalate")
                    results.append({"job_id": job_id, "action": "escalate"})
                else:
                    results.append({"job_id": job_id, "action": "complete_failed"})
                continue
            else:
                # Defensive: parse_agent_result should only return known actions.
                agent_jobs.fail_job(job_id, "unknown agent_result action",
                                    status=agent_jobs.STATUS_FAILED)
                results.append({"job_id": job_id, "action": "failed",
                                "reason": "unknown agent_result action"})
                continue

        # Legacy path for jobs that did not request the strict contract.
        # Snapshot carries raw=None so we never persist provider-controlled raw
        # output here; this is intentional and prevents raw secret leaks.
        if poll_result.ok and poll_result.reply_text:
            reply_text = agent_worker.sanitize_reply_text(
                poll_result.reply_text, payload, reg.load_config()
            )
            if not reply_text:
                agent_jobs.fail_job(job_id, "empty after sanitize",
                                    status=agent_jobs.STATUS_FAILED)
                agent_jobs.update_poll_state(job_id, next_poll_at=now + 60.0,
                                            external_status="failed")
                results.append({"job_id": job_id, "action": "failed",
                                "reason": "empty after sanitize"})
            else:
                ok = agent_jobs.complete_job(job_id, reply_text)
                if ok:
                    agent_jobs.update_poll_state(job_id, next_poll_at=now + 60.0,
                                                external_status="done")
                    results.append({"job_id": job_id, "action": "completed",
                                    "reply_preview": reply_text[:80]})
                else:
                    results.append({"job_id": job_id, "action": "complete_failed"})
        elif poll_result.status == "running":
            if job.get("status") == agent_jobs.STATUS_SUBMITTED:
                agent_jobs.mark_agent_running(job_id, next_poll_at=now + 10.0)
            else:
                # Backoff: 10s, 20s, 40s, max 60s
                backoff = min(60.0, 10.0 * (2 ** min(attempts, 3)))
                agent_jobs.update_poll_state(job_id, next_poll_at=now + backoff,
                                            external_status="running")
            results.append({"job_id": job_id, "action": "still_running"})
        else:
            if deadline and now + 30.0 > deadline:
                agent_jobs.mark_expired(job_id, reason=poll_expired_marker)
                results.append({"job_id": job_id, "action": "expired",
                                "error": poll_expired_marker})
            else:
                # Backoff: 5s, 10s, 20s, max 60s
                backoff = min(60.0, 5.0 * (2 ** min(attempts, 3)))
                agent_jobs.update_poll_state(job_id, next_poll_at=now + backoff,
                                            external_status="error")
                results.append({"job_id": job_id, "action": "poll_error",
                                "error": poll_error_marker})

    return {"ok": True, "action": "polled", "polled": len(results),
            "results": results, "duration": round(time.time() - t0, 3)}



def _async_sender_run_once(body: Dict[str, Any]) -> Dict[str, Any]:
    """Sender: claim done+pending jobs and send to WeChat.
    
    Transitions: done+pending -> sent/send_failed
    Uses the existing agent_worker._send_result_back to do the actual sending.
    """
    t0 = time.time()
    limit = int(body.get("limit") or 5)
    jobs_to_send = agent_jobs.list_sendable(limit=limit)
    if not jobs_to_send:
        return {"ok": True, "action": "idle", "sent": 0, "duration": round(time.time() - t0, 3)}
    
    results = []
    for job in jobs_to_send:
        job_id = int(job["id"])
        # Atomically claim: mark as sending
        ok = agent_jobs.mark_sending(job_id)
        if not ok:
            continue
        # Increment send_attempts
        agent_jobs.increment_send_attempts(job_id)
        # Sanitize and ensure the persisted text matches what is actually sent
        reply_text = agent_worker.sanitize_reply_text(
            job.get("result_text") or "", job.get("payload"), reg.load_config()
        )
        if reply_text != job.get("result_text"):
            agent_jobs.update_result_text(job_id, reply_text)
        # Send
        send_result = agent_worker._send_result_back(
            job, reply_text,
            ROOT / "wechat_bot_targets.json",
        )
        if send_result.get("sent"):
            agent_jobs.mark_sent(job_id)
            results.append({"job_id": job_id, "action": "sent"})
        else:
            agent_jobs.mark_send_failed(
                job_id,
                send_result.get("reason", "send failed"),
            )
            results.append({"job_id": job_id, "action": "send_failed",
                            "reason": send_result.get("reason")})

    return {"ok": True, "action": "sent", "sent": len(results),
            "results": results, "duration": round(time.time() - t0, 3)}


def _async_loop_remove_instance(instance_id: str) -> Dict[str, Any]:
    """Remove one instance from the async-loop on-duty set; stop loop if none remain."""
    instance_id = str(instance_id)
    with _ASYNC_LOOP_LOCK:
        thread = _ASYNC_LOOP_THREAD
        running = bool(thread and thread.is_alive() and not _ASYNC_LOOP_STOP.is_set())
        cfg: Dict[str, Any] = _ASYNC_LOOP_STATE.get("config") or {}
        ids = list(cfg.get("instance_ids") or [])
        if not ids and cfg.get("instance_id"):
            ids = [x.strip() for x in str(cfg["instance_id"]).split(",") if x.strip()]
        on_duty = instance_id in ids
        if running and on_duty:
            ids = [x for x in ids if x != instance_id]
            if ids:
                cfg["instance_ids"] = ids
                cfg["instance_id"] = ids[0] if len(ids) == 1 else ",".join(ids)
                cur_max = int(cfg.get("max_global_dispatching") or 1)
                cfg["max_global_dispatching"] = max(1, min(cur_max, len(ids)))
                _ASYNC_LOOP_STATE["config"] = cfg
                action = "removed"
            else:
                _ASYNC_LOOP_STOP.set()
                _ASYNC_LOOP_STATE["stopped_at"] = time.time()
                action = "stopping"
        elif not running:
            action = "not_running"
        else:
            action = "not_on_duty"
    # Build status snapshot outside the lock to avoid re-entrant deadlock.
    status = _async_loop_status()
    return {"action": action, "instance_id": instance_id, **status}


def _async_loop_status() -> Dict[str, Any]:
    with _ASYNC_LOOP_LOCK:
        state = dict(_ASYNC_LOOP_STATE)
    thread = _ASYNC_LOOP_THREAD
    state["running"] = bool(thread and thread.is_alive() and not _ASYNC_LOOP_STOP.is_set())
    state["thread_alive"] = bool(thread and thread.is_alive())
    return state


def _preflight_configured_hermes(body: Dict[str, Any]) -> Dict[str, Any]:
    """Run a strict preflight for one configured Hermes provider only.

    HTTP callers can select a registered instance, but cannot provide an
    executable path or Hermes home directory. This keeps the local control API
    from becoming an arbitrary-command launcher while preserving an explicit
    per-profile preflight.
    """
    instance_id = str(body.get("instance_id") or "").strip() or None
    try:
        provider = _build_agent_provider(instance_id=instance_id)
    except Exception:
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "configured Hermes provider unavailable",
            "stage": "provider_unavailable",
        }
    if not isinstance(provider, HermesProvider):
        return {
            "ok": False,
            "status": "fail",
            "action": None,
            "schema_version": None,
            "reason": "configured provider is not Hermes",
            "stage": "provider_invalid",
        }
    return preflight_hermes_profile(
        prompt=str(body.get("prompt") or "").strip(),
        cli_path=provider.cli_path,
        profile=provider.profile,
        hermes_home=provider.hermes_home,
        timeout=float(body.get("timeout") or 30),
    )


def _expire_legacy_async_jobs(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Explicit, manual control to terminal-expire active legacy async jobs.

    Identifies jobs that are in dispatching/submitted/agent_running, have an
    external session id, and do not have the strict contract flag in their
    payload. Marks them expired with a fixed safe reason. Queued jobs are left
    untouched; the strict-only provider will process them correctly. Returns
    only sanitized scalar IDs/counts and never the payload or raw provider text.
    """
    job_ids = agent_jobs.list_legacy_async_jobs(db_path=db_path)
    expired: List[int] = []
    for job_id in job_ids:
        if agent_jobs.mark_expired(
            job_id,
            reason="legacy protocol job expired before strict cutover",
            db_path=db_path,
        ):
            expired.append(job_id)
    return {
        "ok": True,
        "action": "expired",
        "count": len(expired),
        "job_ids": expired,
    }


def _async_loop_start(body: Dict[str, Any]) -> Dict[str, Any]:
    """Start the control_api-owned M5 loop.

    User impact: once started, queued complex jobs no longer need manual
    run-once clicks. The loop submits, polls, and sends in the background.
    """
    global _ASYNC_LOOP_THREAD
    raw_instance_id = body.get("instance_id")
    if raw_instance_id:
        requested_instance_ids = [x.strip() for x in str(raw_instance_id).split(",") if x.strip()]
    else:
        cfg = reg.load_config(reg.CONFIG_PATH)
        registered = [
            str(inst.get("id") or "").strip()
            for inst in list_agent_instances(cfg)
            if isinstance(inst, dict) and str(inst.get("id") or "").strip()
        ]
        discovered = [
            str(profile.get("id") or "").strip()
            for profile in discover_hermes_profiles()
            if isinstance(profile, dict) and str(profile.get("id") or "").strip()
        ]
        requested_instance_ids = registered or discovered
    instance_id = requested_instance_ids[0] if len(requested_instance_ids) == 1 else ",".join(requested_instance_ids)
    with _ASYNC_LOOP_LOCK:
        if _ASYNC_LOOP_THREAD and _ASYNC_LOOP_THREAD.is_alive():
            raw_state_config = _ASYNC_LOOP_STATE.get("config")
            state_config: Dict[str, Any] = raw_state_config if isinstance(raw_state_config, dict) else {}
            if requested_instance_ids:
                ids = list(state_config.get("instance_ids") or ([] if not state_config.get("instance_id") else [state_config.get("instance_id")]))
                existing = [str(x) for x in ids]
                for iid in requested_instance_ids:
                    if str(iid) not in existing:
                        ids.append(str(iid))
                state_config["instance_ids"] = ids
                state_config["instance_id"] = ids[0] if len(ids) == 1 else ",".join(str(x) for x in ids)
                state_config["max_global_dispatching"] = max(int(state_config.get("max_global_dispatching") or 1), len(ids))
                _ASYNC_LOOP_STATE["config"] = state_config
                return {"action": "joined", **_async_loop_status()}
            return {"action": "already_running", **_async_loop_status()}
        instance_ids = [str(x) for x in requested_instance_ids]
        config = {
            "instance_id": instance_id,
            "instance_ids": instance_ids,
            "worker_id": str(body.get("worker_id") or "control-async-loop"),
            "max_global_dispatching": int(body.get("max_global_dispatching") or max(1, len(instance_ids) or 1)),
            "per_group_concurrency": int(body.get("per_group_concurrency") or 1),
            "dispatch_interval": float(body.get("dispatch_interval") or 2.0),
            "reconcile_interval": float(body.get("reconcile_interval") or 5.0),
            "sender_interval": float(body.get("sender_interval") or 3.0),
            "idle_sleep": float(body.get("idle_sleep") or 1.0),
            "reconcile_limit": int(body.get("reconcile_limit") or 10),
            "send_limit": int(body.get("send_limit") or 5),
        }
        _ASYNC_LOOP_STOP.clear()
        _ASYNC_LOOP_STATE.update({
            "running": True,
            "started_at": time.time(),
            "stopped_at": None,
            "iterations": 0,
            "last_error": "",
            "last_dispatch": None,
            "last_reconcile": None,
            "last_send": None,
            "config": config,
        })
        _ASYNC_LOOP_THREAD = threading.Thread(
            target=_async_loop_main,
            args=(config,),
            name="wechat-agent-m5-loop",
            daemon=True,
        )
        _ASYNC_LOOP_THREAD.start()
    return {"action": "started", **_async_loop_status()}


def _async_loop_stop() -> Dict[str, Any]:
    _ASYNC_LOOP_STOP.set()
    thread = _ASYNC_LOOP_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    with _ASYNC_LOOP_LOCK:
        _ASYNC_LOOP_STATE["running"] = False
        _ASYNC_LOOP_STATE["stopped_at"] = time.time()
    return {"action": "stopped" if not (thread and thread.is_alive()) else "stop_requested", **_async_loop_status()}


def _async_loop_main(config: Dict[str, Any]) -> None:
    last_dispatch = 0.0
    last_reconcile = 0.0
    last_send = 0.0
    while not _ASYNC_LOOP_STOP.is_set():
        now = time.time()
        try:
            agent_jobs.release_stale_dispatching(lock_timeout_seconds=0.0)
            if now - last_dispatch >= float(config.get("dispatch_interval") or 2.0):
                instance_ids = list(config.get("instance_ids") or [])
                if not instance_ids:
                    instance_ids = [None]
                cursor = int(config.get("dispatch_cursor") or 0) % max(1, len(instance_ids))
                ordered_instance_ids = instance_ids[cursor:] + instance_ids[:cursor]
                config["dispatch_cursor"] = (cursor + 1) % max(1, len(instance_ids))
                dispatch_results = []
                for iid in ordered_instance_ids:
                    dispatch_body = {
                        "worker_id": "%s:%s" % (config.get("worker_id") or "control-async-loop", iid or "default"),
                        "instance_id": iid,
                        "max_global_dispatching": config.get("max_global_dispatching") or max(1, len(instance_ids)),
                        "per_group_concurrency": config.get("per_group_concurrency") or 1,
                    }
                    dispatch_results.append(_async_dispatcher_run_once(dispatch_body))
                dispatch_result = {"ok": all(bool(r.get("ok")) for r in dispatch_results), "action": "pool_dispatch", "instances": ordered_instance_ids, "results": dispatch_results}
                last_dispatch = now
                with _ASYNC_LOOP_LOCK:
                    _ASYNC_LOOP_STATE["last_dispatch"] = dispatch_result

            if now - last_reconcile >= float(config.get("reconcile_interval") or 5.0):
                reconcile_result = _async_reconciler_run_once({"limit": config.get("reconcile_limit") or 10})
                last_reconcile = now
                with _ASYNC_LOOP_LOCK:
                    _ASYNC_LOOP_STATE["last_reconcile"] = reconcile_result

            if now - last_send >= float(config.get("sender_interval") or 3.0):
                send_result = _async_sender_run_once({"limit": config.get("send_limit") or 5})
                last_send = now
                with _ASYNC_LOOP_LOCK:
                    _ASYNC_LOOP_STATE["last_send"] = send_result

            with _ASYNC_LOOP_LOCK:
                _ASYNC_LOOP_STATE["iterations"] = int(_ASYNC_LOOP_STATE.get("iterations") or 0) + 1
                _ASYNC_LOOP_STATE["last_error"] = ""
        except Exception as e:
            with _ASYNC_LOOP_LOCK:
                _ASYNC_LOOP_STATE["last_error"] = repr(e)
        _ASYNC_LOOP_STOP.wait(max(0.2, float(config.get("idle_sleep") or 1.0)))

    with _ASYNC_LOOP_LOCK:
        _ASYNC_LOOP_STATE["running"] = False
        _ASYNC_LOOP_STATE["stopped_at"] = time.time()


def _agent_worker_stop_file() -> Path:
    return ROOT / "agent_worker.stop"


def _agent_worker_state_file() -> Path:
    return ROOT / "temp" / "agent_worker_state.json"


def _agent_worker_pids() -> List[int]:
    out = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'agent_worker\\.py' -and $_.Name -match '^pythonw?\\.exe$' } | Select-Object ProcessId | ForEach-Object { $_.ProcessId }",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_SUBPROCESS_FLAGS,
    )
    return [int(x) for x in (out.stdout or "").split() if x.strip().isdigit()]


def _agent_worker_state() -> Dict[str, Any]:
    path = _agent_worker_state_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _agent_worker_status() -> Dict[str, Any]:
    pids = _agent_worker_pids()
    return {
        "running": bool(pids),
        "pids": pids,
        "stop_file": str(_agent_worker_stop_file()),
        "stop_requested": _agent_worker_stop_file().exists(),
        "state": _agent_worker_state(),
    }


def _agent_worker_start(provider: str = "echo", worker_id: str = "agent-worker-service-1",
                        timeout: float = 240, idle_sleep: float = 2,
                        worker_count: int = 1,
                        instance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    pids = _agent_worker_pids()
    if pids:
        # Check if the running worker matches the requested provider.
        # If not, stop it first so the new start picks up the new settings.
        current_state = _agent_worker_state()
        current_provider = str(current_state.get("provider") or "")
        try:
            current_timeout = float(current_state.get("timeout") or 0)
        except Exception:
            current_timeout = 0
        try:
            current_workers = int(current_state.get("worker_count") or 1)
        except Exception:
            current_workers = 1
        needs_restart = bool(current_provider and current_provider != provider)
        if current_timeout and abs(current_timeout - float(timeout)) > 0.01:
            needs_restart = True
        if current_workers != int(worker_count):
            needs_restart = True
        if needs_restart:
            _agent_worker_stop()
            pids = _agent_worker_pids()
        if pids:
            return {
                "action": "already_running",
                "note": "worker did not stop in time; restart manually",
                **_agent_worker_status(),
            }
    stop_file = _agent_worker_stop_file()
    try:
        if stop_file.exists():
            stop_file.unlink()
    except Exception:
        pass
    stdout_log = ROOT / "agent_worker_stdout.log"
    stderr_log = ROOT / "agent_worker_stderr.log"
    py = Path(sys.executable)
    pythonw = py.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else py
    count = max(1, int(worker_count))
    started: List[Dict[str, Any]] = []
    inst_cfg = instance if isinstance(instance, dict) else None
    for i in range(count):
        wid = worker_id if count == 1 else f"{worker_id}-{i+1}"
        args = [
            str(ROOT / "agent_worker.py"),
            "--provider", provider,
            "--worker-id", wid,
            "--timeout", str(float(timeout)),
            "--idle-sleep", str(float(idle_sleep)),
            "--active-workers", str(count),
            "--max-global-running", str(count),
            "--json",
            "--clear-stop",
            "--send",
        ]
        if provider == "hermes" and inst_cfg:
            cli_path = str(inst_cfg.get("cli_path") or "hermes")
            args += ["--hermes-cli", cli_path]
            if inst_cfg.get("hermes_home"):
                args += ["--hermes-home", str(inst_cfg["hermes_home"])]
            if inst_cfg.get("profile"):
                args += ["--profile", str(inst_cfg["profile"])]
            if inst_cfg.get("model"):
                args += ["--model", str(inst_cfg["model"])]
            if inst_cfg.get("toolsets"):
                args += ["--toolsets", str(inst_cfg["toolsets"])]
        with stdout_log.open("a", encoding="utf-8") as out, stderr_log.open("a", encoding="utf-8") as err:
            proc = subprocess.Popen([str(exe)] + args, cwd=str(ROOT), stdout=out, stderr=err, creationflags=_SUBPROCESS_FLAGS)
        started.append({"pid": proc.pid, "worker_id": wid})
    time.sleep(1.0)
    return {"action": "started", "started": started, "worker_count": count, **_agent_worker_status()}


def _agent_worker_stop() -> Dict[str, Any]:
    stop_file = _agent_worker_stop_file()
    try:
        stop_file.write_text("stop requested at %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception:
        pass
    waited = 0
    while _agent_worker_pids() and waited < 20:
        time.sleep(0.5)
        waited += 1
    return {"action": "stopped" if not _agent_worker_pids() else "stop_requested", **_agent_worker_status()}


# ---------------------------------------------------------------------------
# call into existing CLI implementation
# ---------------------------------------------------------------------------

def _call_cli(args: List[str]) -> Tuple[bytes, int, str]:
    """Invoke manage_targets.main() with the given args and return its
    print_json output as the API response.

    Pass the full argv (including flags like ``--include-contacts``).  Older
    code dropped everything except the subcommand name, so console scan never
    honored include_contacts and some commands printed nothing useful.
    """
    import io, contextlib
    buf = io.StringIO()
    err = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            rc = mt.main(argv=list(args))
            if rc is None:
                rc = 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except Exception as e:
            return _err("manage_targets error: %r" % (e,), status=500)
    out = buf.getvalue().strip()
    err_out = err.getvalue().strip()
    if rc not in (0, None) and not out:
        return _err(
            "manage_targets failed rc=%s%s" % (rc, (": " + err_out[:500]) if err_out else ""),
            status=500,
        )
    if not out:
        return _ok(action=args[0] if args else "cli", rc=rc)
    try:
        payload = json.loads(out)
        if isinstance(payload, dict):
            payload.setdefault("ok", True)
            if "action" not in payload and args:
                payload["action"] = args[0]
        return _json(payload, 200)
    except Exception:
        return _ok(action=args[0] if args else "cli", raw=out, rc=rc)


# ---------------------------------------------------------------------------
# mini wrapper around `manage_targets.cmd_status` to expose pids
# ---------------------------------------------------------------------------

def _monitor_pids() -> List[int]:  # type: ignore[no-redef]
    return mt._monitor_pids()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("WECHAT_CONTROL_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WECHAT_CONTROL_PORT", str(DEFAULT_PORT))))
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), ControlHandler)
    try:
        auto = _reliable_scheduler_auto_start()
        if auto:
            print("control_api: reliable scheduler auto-started: %s" % auto.get("action"), flush=True)
    except Exception:
        pass
    try:
        print("control_api: http://%s:%s (pid=%s)" % (args.host, args.port, os.getpid()), flush=True)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _reliable_scheduler_stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
