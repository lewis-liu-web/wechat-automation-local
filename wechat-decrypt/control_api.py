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
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent

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
    provider_from_config,
)
import manage_targets as mt  # noqa: E402
import target_registry as reg  # noqa: E402

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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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

        # ---- monitor control ----
        if method == "POST" and path == "/monitor/start":
            return _call_cli(["start", "--json"])
        if method == "POST" and path == "/monitor/stop":
            return _call_cli(["stop", "--json"])
        if method == "POST" and path == "/monitor/restart":
            return _call_cli(["restart", "--json"])

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
            send_result = agent_worker._send_result_back(job, job["result_text"], ROOT / "wechat_bot_targets.json")
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
            provider = _build_agent_provider("genericagent")
            recover = getattr(provider, "recover", None)
            if not callable(recover):
                return _err("provider does not support result recovery")
            result: Any = recover(job, timeout=float(body.get("timeout") or 15))
            if not result.ok or not result.reply_text:
                return _ok(action="recover_failed", job_id=job_id, result=result.to_dict())
            raw = result.raw or {}
            bridge_patch = {key: raw.get(key) for key in ("bridge_session_id", "bridge_user_msg_id") if raw.get(key)}
            if bridge_patch:
                agent_jobs.merge_payload(job_id, bridge_patch)
            stored = agent_jobs.recover_job_result(job_id, result.reply_text)
            if not stored:
                return _ok(action="recover_store_failed", job_id=job_id, result=result.to_dict())
            recovered = agent_jobs.get_job(job_id)
            send_result = None
            if body.get("send") is True and recovered:
                send_result = agent_worker._send_result_back(recovered, result.reply_text, ROOT / "wechat_bot_targets.json")
                if send_result.get("sent"):
                    agent_jobs.mark_sent(job_id)
                else:
                    agent_jobs.mark_send_failed(job_id, send_result.get("reason", "recover send failed"))
                recovered = agent_jobs.get_job(job_id)
            return _ok(action="recovered", job_id=job_id, result=result.to_dict(), send_result=send_result, job=recovered)
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
            kind = str(inst.get("provider") or inst.get("type") or "genericagent").lower()
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
            provider_name = str((params.get("provider") or ["genericagent"])[0]).lower()
            instance_id = (params.get("instance_id") or [None])[0]
            provider = _build_agent_provider(provider_name, instance_id=instance_id)
            return _ok(health=provider.health().to_dict())

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
        if method == "POST" and (m := _match(path, "/agent/instance/{id}/on-duty")):
            return _ok(**_async_loop_start({"instance_id": str(m[0])}))
        if method == "POST" and (m := _match(path, "/agent/instance/{id}/off-duty")):
            return _ok(**_async_loop_remove_instance(str(m[0])))

        # ---- knowledge bases ----
        if method == "GET" and path == "/kbs":
            return _ok(knowledge_bases=reg.list_kbs_extended())
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
                return _ok(**reg.search_local_kb(m[0], q, limit=limit))
            except Exception as e:
                return _err("search failed: %s" % (e,))
        if method == "POST" and (m := _match(path, "/kbs/{key}/open")):
            return _ok(action="opened", path=reg.open_kb_dir(m[0]))
        if method == "POST" and (m := _match(path, "/kbs/{key}/obsidian")):
            return _ok(action="opened_obsidian", **reg.open_kb_obsidian(m[0]))
        if method == "GET" and (m := _match(path, "/kbs/{key}/diagnose")):
            q = str((params.get("q") or [""])[0]).strip()
            try:
                return _ok(**reg.diagnose_local_kb(m[0], query=q))
            except Exception as e:
                return _err("diagnose failed: %s" % (e,))
        if method == "POST" and (m := _match(path, "/targets/{key}/kbs/replace")):
            kbs = list(body.get("knowledge_bases") or [])
            return _ok(action="bound", target=reg.bind_wiki(m[0], kbs, replace=True))

        return _err("not found: %s %s" % (method, path), status=404)

    # ---- HTTP verbs ----
    def do_GET(self):
        try:
            path, params = self._parse_path()
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


def _register_agent_instance(instance: Any) -> Dict[str, Any]:
    if not isinstance(instance, dict):
        raise ValueError("instance must be an object")
    instance_id = str(instance.get("id") or "").strip()
    provider = str(instance.get("provider") or instance.get("type") or "").strip().lower()
    if not instance_id:
        raise ValueError("instance.id is required")
    if provider not in ("hermes", "genericagent", "echo"):
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
        return "genericagent"
    cfg = reg.load_config()
    for inst in list_agent_instances(cfg):
        if str(inst.get("id") or "") == str(instance_id):
            return str(inst.get("provider") or inst.get("type") or "genericagent").lower()
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
        kind = str(inst.get("provider") or inst.get("type") or "genericagent").lower()
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
# M5 async flow: dispatcher / reconciler / sender
# ---------------------------------------------------------------------------

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
    provider = _build_agent_provider(provider_name_for_health, instance_id=instance_id)
    health_dict = _cached_provider_health("dispatch:%s:%s" % (instance_id or "default", getattr(provider, "name", instance_provider or "")), provider, ttl=20.0)
    if not bool(health_dict.get("ok")) or not bool(health_dict.get("ready")):
        return {"ok": True, "action": "provider_unavailable", "instance_id": instance_id,
                "provider": getattr(provider, "name", instance_provider or ""),
                "error": health_dict.get("error") or "provider not ready", "duration": round(time.time() - t0, 3)}

    job = agent_jobs.claim_dispatchable(
        worker_id=worker_id,
        provider=instance_provider or None,
        max_global_dispatching=max_global,
        per_group_concurrency=per_group,
    )
    if not job:
        return {"ok": True, "action": "idle", "duration": round(time.time() - t0, 3)}
    
    provider_name = str(job.get("provider") or "").strip().lower() or (instance_provider or None)
    if provider_name and provider_name != str(getattr(provider, "name", "")).lower():
        provider = _build_agent_provider(provider_name, instance_id=instance_id)
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

    submit_result = provider.submit(job, timeout=submit_timeout)
    if not submit_result.ok:
        agent_jobs.fail_job(
            int(job["id"]),
            submit_result.error or "submit failed",
            status=agent_jobs.STATUS_FAILED,
        )
        return {"ok": False, "action": "submit_failed", "job_id": job["id"],
                "error": submit_result.error, "duration": round(time.time() - t0, 3)}

    raw = submit_result.raw or {}
    session_id = str(raw.get("bridge_session_id") or "")
    user_msg_id = int(raw.get("bridge_user_msg_id") or 0)
    if not session_id:
        agent_jobs.fail_job(int(job["id"]), "submit returned no session id",
                           status=agent_jobs.STATUS_FAILED)
        return {"ok": False, "action": "no_session", "job_id": job["id"],
                "duration": round(time.time() - t0, 3)}

    
    ok = agent_jobs.mark_submitted(
        int(job["id"]),
        external_provider=provider.name,
        external_session_id=session_id,
        external_user_msg_id=user_msg_id,
        agent_deadline_at=agent_deadline_at,
        next_poll_at=time.time() + 5.0,  # first poll in 5s
    )
    if not ok:
        return {"ok": False, "action": "mark_submitted_failed", "job_id": job["id"],
                "duration": round(time.time() - t0, 3)}
    
    return {"ok": True, "action": "submitted", "job_id": job["id"],
            "external_session_id": session_id,
            "external_user_msg_id": user_msg_id,
            "agent_deadline_at": agent_deadline_at,
            "duration": round(time.time() - t0, 3)}


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

        # Build provider from job's external_provider
        ext_provider = str(job.get("external_provider") or "genericagent")
        payload = job.get("payload") or {}
        instance_id = payload.get("agent_instance_id") if isinstance(payload, dict) else None
        provider = _build_agent_provider(ext_provider, instance_id=str(instance_id) if instance_id else None)

        # Poll with exception guard
        try:
            poll_result = provider.poll(session_id, user_msg_id, timeout=10)
        except Exception as e:
            poll_result = AgentResult(False, "failed", error="poll exception: %r" % (e,),
                                       provider=ext_provider, worker_id="reconciler")

        if poll_result.ok and poll_result.reply_text:
            reply_text = agent_jobs.sanitize_agent_result_text(poll_result.reply_text)
            raw = getattr(poll_result, "raw", None) or None
            if raw:
                agent_jobs.merge_payload(job_id, {"agent_raw_output": raw})
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
            raw = getattr(poll_result, "raw", None) or None
            if raw:
                agent_jobs.merge_payload(job_id, {"agent_raw_output": raw})
            if deadline and now + 30.0 > deadline:
                agent_jobs.mark_expired(job_id, reason=str(poll_result.error or "poll failed"))
                results.append({"job_id": job_id, "action": "expired",
                                "error": poll_result.error})
            else:
                # Backoff: 5s, 10s, 20s, max 60s
                backoff = min(60.0, 5.0 * (2 ** min(attempts, 3)))
                agent_jobs.update_poll_state(job_id, next_poll_at=now + backoff,
                                            external_status="error")
                results.append({"job_id": job_id, "action": "poll_error",
                                "error": poll_result.error})
    
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
        # Send
        send_result = agent_worker._send_result_back(
            job, job.get("result_text") or "",
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
    """
    import io, contextlib
    buf = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(buf):
        try:
            mt.main(argv=[args[0]] + (["--json"] if "--json" in args else []))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except Exception as e:
            return _err("manage_targets error: %r" % (e,), status=500)
    out = buf.getvalue().strip()
    if not out:
        return _ok(action=args[0])
    try:
        return _json(json.loads(out), 200)
    except Exception:
        return _ok(action=args[0], raw=out)


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
        print("control_api: http://%s:%s (pid=%s)" % (args.host, args.port, os.getpid()), flush=True)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
