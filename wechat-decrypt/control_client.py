#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP client for the WeChat bot control_api.

The UI (Streamlit) talks to this client only — it never imports
`manage_targets.py` or any other module under wechat-decrypt directly.
This keeps the UI decoupled from the bot runtime and from any
agent-specific code.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_BASE_URL = os.environ.get("WECHAT_CONTROL_API", "http://127.0.0.1:18590")
DEFAULT_TIMEOUT = float(os.environ.get("WECHAT_CONTROL_TIMEOUT", "5"))


class ControlAPIError(RuntimeError):
    pass


def _request(method: str, path: str, base_url: str = DEFAULT_BASE_URL,
             params: Optional[Dict[str, Any]] = None,
             body: Optional[Dict[str, Any]] = None,
             timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    url = base_url.rstrip("/") + path
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None},
                                    doseq=True, quote_via=urllib.parse.quote)
        url = url + ("?" + qs if qs else "")
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise ControlAPIError("HTTP %s: %s" % (e.code, body_text)) from e
    except urllib.error.URLError as e:
        raise ControlAPIError("unreachable: %s" % (e,)) from e
    except Exception as e:
        raise ControlAPIError("error: %r" % (e,)) from e


# ---- high-level methods used by the UI ----

def health(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/health", base_url=base_url)


def status(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/status", base_url=base_url)


def start_monitor(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/monitor/start", base_url=base_url, body={}, timeout=10)


def stop_monitor(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/monitor/stop", base_url=base_url, body={}, timeout=10)


def restart_monitor(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/monitor/restart", base_url=base_url, body={}, timeout=20)


def list_targets(kind: str = "all", base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    res = _request("GET", "/targets", base_url=base_url,
                   params={"kind": kind})
    targets = res.get("targets") or []
    return targets if isinstance(targets, list) else []


def target_info(key: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/targets/%s" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, timeout=10)


def enable_target(key: str, knowledge_bases: Optional[List[str]] = None,
                 category: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if knowledge_bases:
        body["knowledge_bases"] = list(knowledge_bases)
    if category:
        body["category"] = str(category)
    return _request("POST", "/targets/%s/on" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body=body)


def disable_target(key: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/off" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={})


def set_target_category(key: str, category: str,
                        base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/category" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"category": str(category)})

def set_target_field(key: str, field: str, value: Any,
                     base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/field" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"field": str(field), "value": value})




def delete_target(key: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """UI uses POST /targets/{key}/delete for safety; the DELETE HTTP verb
    is also accepted by the control_api but the UI prefers POST so the
    button works in browsers that block DELETE via <form>.
    """
    return _request("POST", "/targets/%s/delete" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={})


def scan_targets(include_contacts: bool = False,
                 base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/scan", base_url=base_url,
                    params={"include_contacts": "true" if include_contacts else "false"})


def list_kbs(base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    res = _request("GET", "/kbs", base_url=base_url)
    return res.get("knowledge_bases") or []


def kb_info(kb_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    res = _request("GET", "/kbs/%s/info" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, timeout=15)
    return res.get("knowledge_base") or {}


def save_kb(payload: Dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/kbs", base_url=base_url, body=payload)


def enable_kb(kb_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/kbs/%s/on" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body={})


def disable_kb(kb_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/kbs/%s/off" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body={})


def delete_kb(kb_id: str, remove_files: bool = False,
              base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/kbs/%s/delete" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body={"remove_files": remove_files})


def import_kb(kb_id: str, source: str, base_url: str = DEFAULT_BASE_URL,
             allow_empty: bool = False) -> Dict[str, Any]:
    return _request("POST", "/kbs/%s/import" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body={"source": source}, timeout=60)


def search_kb(kb_id: str, query: str, base_url: str = DEFAULT_BASE_URL,
              limit: int = 5) -> Dict[str, Any]:
    return _request("GET", "/kbs/%s/search" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, params={"q": query, "limit": str(limit)}, timeout=30)


def open_kb(kb_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/kbs/%s/open" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body={})


def open_kb_obsidian(kb_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/kbs/%s/obsidian" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body={})


def replace_target_kbs(key: str, knowledge_bases: List[str],
                       base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/kbs/replace" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"knowledge_bases": knowledge_bases})


def build_leann_kb(kb_id: str, docs: Optional[List[str]] = None, force: bool = False,
                   base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """Start an asynchronous LEANN build for a knowledge base."""
    body: Dict[str, Any] = {"force": force}
    if docs:
        body["docs"] = list(docs)
    return _request("POST", "/kbs/%s/leann/build" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body=body, timeout=10)


def leann_build_status(kb_id: str, build_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """Query the status of an asynchronous LEANN build."""
    return _request("GET", "/kbs/%s/leann/build/status" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, params={"build_id": build_id}, timeout=10)


def get_triggers(key: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    res = _request("GET", "/targets/%s/triggers" % urllib.parse.quote(key, safe=""),
                   base_url=base_url)
    return res.get("target") or {}


def get_default_triggers(base_url: str = DEFAULT_BASE_URL) -> List[str]:
    res = _request("GET", "/triggers/default", base_url=base_url)
    return res.get("default_triggers") or []


def replace_default_triggers(words: List[str], base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/triggers/default/replace", base_url=base_url,
                    body={"words": words})


def clear_default_triggers(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/triggers/default/clear", base_url=base_url, body={})


def add_triggers(key: str, words: List[str], base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/triggers/add" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"words": words})


def remove_triggers(key: str, words: List[str], base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/triggers/remove" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"words": words})


def replace_triggers(key: str, words: List[str], base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/triggers/replace" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"words": words})


def clear_triggers(key: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/triggers/clear" % urllib.parse.quote(key, safe=""),
                    base_url=base_url)


def events_recent(limit: int = 100, kind: Optional[str] = None,
                  target: Optional[str] = None,
                  base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    res = _request("GET", "/events/recent", base_url=base_url,
                   params={"limit": limit, "kind": kind, "target": target})
    return res.get("events") or []


def events_stats(since: Optional[float] = None,
                 base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    res = _request("GET", "/events/stats", base_url=base_url,
                   params={"since": since})
    return res.get("stats") or {}


def diagnose_kb(kb_id: str, query: str = "",
                 base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/kbs/%s/diagnose" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, params={"q": query}, timeout=30)


def run_dispatcher_once(instance_id: Optional[str] = None,
                        provider: Optional[str] = None,
                        max_global_dispatching: int = 2,
                        per_group_concurrency: int = 1,
                        base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/dispatcher/run-once", base_url=base_url, body={
        "instance_id": instance_id,
        "provider": provider,
        "max_global_dispatching": max_global_dispatching,
        "per_group_concurrency": per_group_concurrency,
    }, timeout=30)


def run_reconciler_once(limit: int = 10,
                        base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/reconciler/run-once", base_url=base_url,
                    body={"limit": int(limit)}, timeout=30)


def run_sender_once(limit: int = 5,
                    base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/sender/run-once", base_url=base_url,
                    body={"limit": int(limit)}, timeout=30)


def agent_jobs(limit: int = 50, status: Optional[str] = None,
               group_key: Optional[str] = None,
               base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    res = _request("GET", "/agent/jobs", base_url=base_url,
                   params={"limit": limit, "status": status, "group_key": group_key})
    return res.get("jobs") or []


def agent_jobs_stats(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    res = _request("GET", "/agent/jobs/stats", base_url=base_url)
    return res.get("stats") or {}


def create_agent_test_job(prompt: str, provider: str = "echo",
                          group_key: str = "manual-test",
                          sender: str = "tester",
                          target: Optional[Any] = None,
                          mention_name: str = "",
                          priority: int = 0,
                          task_type: str = "deep_free",
                          base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "prompt": prompt,
        "provider": provider,
        "group_key": group_key,
        "sender": sender,
        "mention_name": mention_name,
        "priority": priority,
        "task_type": task_type,
    }
    if isinstance(target, dict):
        body["target_username"] = target.get("username")
        body["target_name"] = target.get("name")
        body["target_table"] = target.get("table")
    elif isinstance(target, str) and target:
        body["group_key"] = target
    return _request("POST", "/agent/jobs/test", base_url=base_url, body=body)


def run_agent_worker_once(provider: str = "echo", timeout: float = 240,
                          base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/worker/run-once", base_url=base_url, body={
        "provider": provider,
        "timeout": timeout,
    }, timeout=max(DEFAULT_TIMEOUT, float(timeout) + 5))


def agent_worker_status(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/agent/worker/status", base_url=base_url)


def start_agent_worker(provider: str = "echo", timeout: float = 240,
                       worker_count: int = 1,
                       base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/worker/start", base_url=base_url, body={
        "provider": provider,
        "timeout": timeout,
        "worker_count": int(worker_count),
    }, timeout=10)


def stop_agent_worker(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/worker/stop", base_url=base_url, body={}, timeout=15)


def retry_job_send(job_id: int, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """Retry sending a completed job's result to WeChat."""
    return _request("POST", "/agent/jobs/%d/retry-send" % job_id, base_url=base_url, body={}, timeout=30)


def dismiss_job(job_id: int, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """Dismiss a failed/timed-out job so it no longer shows as abnormal."""
    return _request("POST", "/agent/jobs/%d/dismiss" % job_id, base_url=base_url, body={}, timeout=15)


def dismiss_jobs_batch(job_ids: List[int], base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """Dismiss multiple jobs at once. Returns summary of results."""
    ok_count = 0
    failed = []
    for job_id in job_ids:
        try:
            res = dismiss_job(job_id, base_url=base_url)
            if res.get("action") == "dismissed":
                ok_count += 1
            else:
                failed.append(job_id)
        except Exception:
            failed.append(job_id)
    return {"ok_count": ok_count, "failed_ids": failed, "total": len(job_ids)}


def recover_job_result(job_id: int, send: bool = False,
                       base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """Recover a late agent result, optionally send it back to WeChat."""
    return _request("POST", "/agent/jobs/%d/recover-result" % job_id,
                    base_url=base_url, body={"send": bool(send)}, timeout=30)


def agent_provider_health(provider: str = "hermes",
                          instance_id: Optional[str] = None,
                          base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    params: Dict[str, Any] = {"provider": provider}
    if instance_id:
        params["instance_id"] = instance_id
    res = _request("GET", "/agent/provider/health", base_url=base_url,
                   params=params, timeout=10)
    return res.get("health") or {}


def get_agent_job(job_id: int, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    res = _request("GET", "/agent/jobs/%d" % int(job_id), base_url=base_url, timeout=10)
    return res.get("job") or {}


def set_target_mode(key: str, mode: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/targets/%s/mode" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"mode": mode})


def set_target_dedicated_agent(key: str, instance_id: Optional[str],
                                base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    """Bind a target to a specific registered agent instance.

    ``instance_id`` of empty string or ``None`` clears the binding.
    """
    return _request("POST", "/targets/%s/dedicated-agent" % urllib.parse.quote(key, safe=""),
                    base_url=base_url, body={"instance_id": (instance_id or "")})


def list_agent_instances(base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    res = _request("GET", "/agent/instances", base_url=base_url, timeout=10)
    return res.get("instances") or []


def discover_hermes_profiles(base_url: str = DEFAULT_BASE_URL) -> List[Dict[str, Any]]:
    res = _request("GET", "/agent-providers/hermes/profiles", base_url=base_url, timeout=10)
    return res.get("profiles") or []


def register_agent_instance(instance: Dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/instances", base_url=base_url, body={"instance": instance}, timeout=10)



def start_agent_instance(instance_id: str, timeout: float = 240, worker_count: int = 1,
                         base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/instance/%s/start" % urllib.parse.quote(str(instance_id), safe=""),
                    base_url=base_url, body={"timeout": timeout, "worker_count": int(worker_count)},
                    timeout=15)


def agent_instance_on_duty(instance_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/instance/%s/on-duty" % urllib.parse.quote(str(instance_id), safe=""),
                    base_url=base_url, body={}, timeout=10)


def agent_instance_off_duty(instance_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/instance/%s/off-duty" % urllib.parse.quote(str(instance_id), safe=""),
                    base_url=base_url, body={}, timeout=10)


def async_loop_status(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/agent/async-loop/status", base_url=base_url, timeout=10)


def agent_pool_status(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/agent/pool/status", base_url=base_url, timeout=15)


def start_async_loop(instance_id: Optional[str] = None,
                     max_global_dispatching: int = 1,
                     per_group_concurrency: int = 1,
                     base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/async-loop/start", base_url=base_url, body={
        "instance_id": instance_id,
        "max_global_dispatching": int(max_global_dispatching),
        "per_group_concurrency": int(per_group_concurrency),
    }, timeout=10)


def stop_async_loop(base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("POST", "/agent/async-loop/stop", base_url=base_url, body={}, timeout=10)


__all__ = [
    "ControlAPIError",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "health",
    "status",
    "start_monitor",
    "stop_monitor",
    "restart_monitor",
    "list_targets",
    "target_info",
    "enable_target",
    "disable_target",
    "delete_target",
    "scan_targets",
    "list_kbs",
    "kb_info",
    "save_kb",
    "get_default_triggers",
    "replace_default_triggers",
    "clear_default_triggers",
    "enable_kb",
    "disable_kb",
    "delete_kb",
    "search_kb",
    "open_kb",
    "open_kb_obsidian",
    "diagnose_kb",
    "import_kb",
    "build_leann_kb",
    "leann_build_status",
    "replace_target_kbs",
    "set_target_field",
    "set_target_mode",

    "set_target_dedicated_agent",
    "set_target_category",
    "get_triggers",
    "add_triggers",
    "remove_triggers",
    "replace_triggers",
    "clear_triggers",
    "events_recent",
    "events_stats",
    "agent_jobs",
    "agent_jobs_stats",
    "get_agent_job",
    "create_agent_test_job",
    "retry_job_send",
    "recover_job_result",
    "dismiss_job",
    "dismiss_jobs_batch",
    "agent_provider_health",
    "run_agent_worker_once",
    "run_dispatcher_once",
    "run_reconciler_once",
    "run_sender_once",
    "agent_worker_status",
    "start_agent_worker",
    "stop_agent_worker",
    "list_agent_instances",
    "discover_hermes_profiles",
    "register_agent_instance",
    "start_agent_instance",
    "agent_instance_on_duty",
    "agent_instance_off_duty",
    "agent_pool_status",
    "async_loop_status",
    "start_async_loop",
    "stop_async_loop",
]
