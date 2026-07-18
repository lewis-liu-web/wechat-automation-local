#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable worker and sender for the staged WeChat reply pipeline.

This module is the bridge between ``reliable_pipeline`` (durable transport
primitives) and the rest of the WeChat control plane.  It deliberately owns
no knowledge retrieval, reply-decision policy, or WeChat UI automation.  The
worker side converts a leased turn job into one provider call and applies
the strict versioned ``AgentResult`` contract via ``apply_agent_result``.
The sender side claims a leased outbox row, resolves the configured target
strictly from the caller's ``config['targets']`` (no on-disk registry
fallback; an unconfigured target fails the send instead of being routed
into an unintended active chat), enforces the test-mode authorization
gate, dispatches ``wechat_sender.send_reply_detailed``, and records the
confirmation.

The module is import-only by default.  The control plane is expected to
invoke ``process_once`` / ``send_once`` on its own cadence; nothing in this
file starts a background thread.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import reliable_pipeline as pipeline
from wechat_sender import SendResult, send_reply_detailed

logger = logging.getLogger(__name__)


# --- Default values ----------------------------------------------------------

DEFAULT_REPLY_OWNER = "reliable-worker"
DEFAULT_SEND_OWNER = "reliable-sender"
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_DEADLINE_SECONDS = 300.0
DEFAULT_SEND_LEASE_SECONDS = 45.0
DEFAULT_SEND_IN_FLIGHT_LEASE_SECONDS = 180.0
DEFAULT_MAX_SEND_ATTEMPTS = 5
DEFAULT_SEND_LIMIT = 10
DEFAULT_RETRY_BASE_SECONDS = 5.0
DEFAULT_KNOWLEDGE_PROVIDER_TIMEOUT = 240.0
RELIABLE_RESULT_CONTRACT_FLAG = "reliable_result_contract"
TEST_TARGET_ONLY_NAME = "bot群聊测试"
TARGET_NOT_CONFIGURED_REASON = "target not configured"

# Knowledge-search trace provenance fields (Stage 3). The worker owns the
# trace directory and id; the model only sees the id via env, never the path.
TRACE_ENV_ID = "WECHAT_MCP_TRACE_ID"
TRACE_ENV_DIR = "WECHAT_MCP_TRACE_DIR"
TRACE_PAYLOAD_ID = "_knowledge_trace_id"
TRACE_PAYLOAD_DIR = "_knowledge_trace_dir"
TRACE_FILE_SUFFIX = ".json"
MAX_TRACE_BYTES = 1024 * 1024  # 1 MiB cap on a single trace file
TRACE_SCHEMA_KEYS = {"trace_id", "status", "provenance", "no_source_reason"}

# Whitelisted provider failure stages that may be persisted safely.  Anything
# outside this set is normalized to a fixed fallback so a misbehaving provider
# can never leak arbitrary error text or raw stdout via the ``stage`` key.
_ALLOWED_PROVIDER_FAILURE_STAGES = {
    "rc_nonzero_no_stdout",
    "rc_nonzero_with_stdout",
    "empty_stdout",
    "contract_violation",
    "timeout",
    "runner_error",
}

# Stages eligible for in-process retry in process_once.  These represent
# transient provider/parser failures where a second attempt has a real
# chance of succeeding; deterministic per-prompt gates (knowledge_gate,
# contract_reject, safety_intercept, unknown) are deliberately excluded so
# the retry budget is spent only on recoverable signal.
_RETRYABLE_PROVIDER_STAGES = frozenset({
    "contract_violation",
    "rc_nonzero_with_stdout",
    "rc_nonzero_no_stdout",
    "empty_stdout",
})

# In-process provider retry budget: 1 retry on top of the initial attempt.
# Kept minimal to bound latency and match the observed transient rate
# harvested from the Stage 4 shadow replay on 2026-07-18.
PROVIDER_RETRY_MAX_ATTEMPTS = 2

# Statuses that represent a successful terminal run.  Only when the
# provider reports one of these (in addition to ``ok=True`` and a valid
# ``AgentResult``) may the worker apply the contract.  Anything else
# (``submitted``, ``running``, ``pending``, ``queued``, ``in_progress``,
# ``ok`` without status, etc.) is treated as a non-terminal state and
# fails the job; the spec says Hermes stdout is never treated as a
# reply unless the process exited successfully.
_TERMINAL_SUCCESS_STATUSES = {"done", "sent", "completed", "succeeded", "success", "silent", "escalated"}
# Statuses that are unambiguous provider failures and always fail the job,
# regardless of the ``ok`` flag.  An unknown / empty status also fails
# (defensive: providers must say something meaningful).
_FAIL_PROVIDER_STATUSES = {
    "timeout", "failed", "error", "cancelled", "canceled", "no_reply",
    "rate_limited", "rate-limited", "unavailable",
}


# --- Errors ------------------------------------------------------------------

class ReliableWorkerError(RuntimeError):
    """Base class for recoverable worker/sender errors."""


class FinalFilterRequired(ReliableWorkerError):
    """Raised when the caller did not pass a final_filter to process_once."""


class TestModeTargetRejected(ReliableWorkerError):
    """Raised when a send would violate the test-mode target restriction."""


# --- Internal helpers --------------------------------------------------------

def _now() -> float:
    return time.time()


def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    if isinstance(d, dict):
        value = d.get(key, default)
        return value if value is not None else default
    return default


def _normalize_response_mode(mode: Optional[str]) -> str:
    """Normalize product response mode to one of the two supported values."""
    return "customer_service" if str(mode or "").lower() == "customer_service" else "group_assistant"


def _mode_instruction(mode: Optional[str]) -> str:
    """Return the behavioral instruction for the given response mode."""
    if _normalize_response_mode(mode) == "customer_service":
        return "当前响应模式：客服。用客服口吻，先确认诉求；如果知识库里有相关资料，请基于资料给出处理建议或排查步骤；只有资料无法覆盖时才追问一个必要澄清问题。不要替负责人承诺、报价、授权或做高风险决定。"
    return "当前响应模式：群助手。用自然、友好的群聊语气，简洁回答或参与讨论；不要替负责人承诺、报价、授权或做高风险决定。"


def _event_message(snapshot: Any) -> Dict[str, Any]:
    """Return the inner WeChat message dict from a durable event snapshot.

    New ingress stores the full ``build_event_payload()`` snapshot in
    ``events[i]["message"]``; the WeChat message fields live inside
    ``snapshot["message"]``. Older/flat rows put the message fields directly
    in ``snapshot``. Accept both shapes without changing the durable schema.
    """
    if not isinstance(snapshot, dict):
        return {}
    inner = snapshot.get("message")
    if isinstance(inner, dict):
        return inner
    return snapshot


def _is_valid_snapshot(snapshot: Any) -> bool:
    """Return True when ``snapshot`` carries a replyable WeChat message.

    A snapshot must have recognizable message fields AND either text or
    image content to be a valid normalization source. Empty metadata or
    sender-only placeholders are ignored; they cannot authorize a reply or
    override the policy of an earlier valid event.
    """
    if not isinstance(snapshot, dict):
        return False
    msg = _event_message(snapshot)
    known_keys = (
        "message_content", "sender_username", "sender_display_name",
        "image_path", "session_image_paths", "local_type", "mention_name",
    )
    if not any(key in msg for key in known_keys):
        return False
    has_text = bool(str(msg.get("message_content") or "").strip())
    has_image = bool(str(msg.get("image_path") or "").strip()) or bool(
        msg.get("session_image_paths")
    )
    return has_text or has_image


def _build_conversation_text(events: List[Dict[str, Any]]) -> str:
    """Aggregate event messages into a single conversation string.

    Events are already in durable order. Only events with a valid snapshot
    are aggregated; each line includes the sender display name and the
    message content so the agent can see multi-turn context.
    """
    lines: List[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        snapshot = ev.get("message")
        if not _is_valid_snapshot(snapshot):
            continue
        msg = _event_message(snapshot)
        sender = str(msg.get("sender_display_name") or msg.get("sender_username") or "").strip()
        text = str(msg.get("message_content") or "").strip()
        if not text:
            continue
        if sender:
            lines.append(f"{sender}: {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def _normalize_turn_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a durable turn payload into a provider-ready payload.

    Authorization and policy are promoted from the last event's snapshot
    (the ``build_event_payload()`` dict, or the legacy flat message dict);
    conversation content is aggregated from the inner WeChat message fields
    of all events in stored order. This preserves the durable snapshot
    semantics: a queued job does not change its KB scope or policy when
    config is edited later.

    If the durable row is malformed (no valid event snapshot, or no text
    and no images), the returned payload carries ``_normalization_error``
    so ``process_once`` can fail the job deterministically instead of
    dispatching an empty user request to Hermes.
    """
    normalized: Dict[str, Any] = {
        "schema_version": payload.get("schema_version", pipeline.SCHEMA_VERSION),
        RELIABLE_RESULT_CONTRACT_FLAG: True,
    }

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        normalized["_normalization_error"] = "turn payload has no events"
        normalized["prompt"] = ""
        return normalized

    # Find the last event that carries a valid dict snapshot.
    valid_snapshots: List[Dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        snapshot = ev.get("message")
        if _is_valid_snapshot(snapshot):
            valid_snapshots.append(snapshot)
    if not valid_snapshots:
        normalized["_normalization_error"] = "no valid event snapshot"
        normalized["prompt"] = ""
        return normalized

    last_snapshot = valid_snapshots[-1]
    last_message = _event_message(last_snapshot)

    # Snapshot fields that must be fixed at turn materialization time.
    # In the new ingress shape these live on the outer snapshot; in legacy flat
    # rows they are absent and the provider falls back to its own defaults.
    for key in ("_config_path", "_allowed_kb_ids", "target", "target_policy", "event_context"):
        if key in last_snapshot:
            normalized[key] = last_snapshot[key]

    # Aggregate conversation text from all events.
    normalized["prompt"] = _build_conversation_text(events)

    # Mention name from the last event's inner WeChat message.
    mention_name = str(last_message.get("mention_name") or "").strip()
    if not mention_name:
        mention_name = str(last_message.get("sender_display_name") or "").strip()
    normalized["mention_name"] = mention_name

    # Aggregate image paths from the inner WeChat messages of all valid events.
    image_paths: List[str] = []
    seen_image_paths: Set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        snapshot = ev.get("message")
        if not _is_valid_snapshot(snapshot):
            continue
        msg = _event_message(snapshot)
        for p in msg.get("session_image_paths") or []:
            path = str(p or "").strip()
            if path and path not in seen_image_paths:
                seen_image_paths.add(path)
                image_paths.append(path)
        p = msg.get("image_path")
        path = str(p or "").strip()
        if path and path not in seen_image_paths:
            seen_image_paths.add(path)
            image_paths.append(path)
    normalized["image_paths"] = image_paths

    # A pure image message is valid ingress; fail only when there is neither
    # text nor image content to act on. With the stricter validity check above
    # this should already be guaranteed, but keep the guard for robustness.
    if not normalized["prompt"].strip() and not image_paths:
        normalized["_normalization_error"] = "no text or image content in turn"

    # Response mode instruction from the last event's policy snapshot.
    target_policy = normalized.get("target_policy") or {}
    response_mode = target_policy.get("mode") if isinstance(target_policy, dict) else None
    normalized["mode_instruction"] = _mode_instruction(response_mode)

    # Carry the last event's local_id for observability.
    last_local_id = last_message.get("local_id")
    if last_local_id is not None:
        normalized["local_id"] = last_local_id

    return normalized


def _resolve_db_path(config: Optional[Dict[str, Any]], db_path: Optional[Path]) -> Path:
    if db_path is not None:
        return Path(db_path)
    cfg = config or {}
    section = _safe_get(cfg, "reliable_pipeline", {}) or {}
    candidate = _safe_get(section, "db_path", None)
    if candidate:
        return Path(candidate)
    return pipeline.DEFAULT_DB_PATH


def _resolve_test_target_only(config: Optional[Dict[str, Any]]) -> bool:
    section = _safe_get(config or {}, "reliable_pipeline", {}) or {}
    return bool(_safe_get(section, "test_target_only", False))


def _resolve_max_send_attempts(config: Optional[Dict[str, Any]], default: int = DEFAULT_MAX_SEND_ATTEMPTS) -> int:
    section = _safe_get(config or {}, "reliable_pipeline", {}) or {}
    raw = _safe_get(section, "max_send_attempts", default)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


def _resolve_retry_base(config: Optional[Dict[str, Any]]) -> float:
    section = _safe_get(config or {}, "reliable_pipeline", {}) or {}
    raw = _safe_get(section, "retry_base_seconds", DEFAULT_RETRY_BASE_SECONDS)
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_BASE_SECONDS


def _resolve_send_lease_seconds(config: Optional[Dict[str, Any]]) -> float:
    """Lease window granted to a row once a send is *authorized* (marker set).

    Must comfortably cover the worst-case ``send_reply_detailed`` duration
    (UI/OCR actions plus DB confirmation) so the lease cannot expire mid-send
    and let another worker reclaim + double-send.  Config:
    ``reliable_pipeline.send_lease_seconds``; default
    ``DEFAULT_SEND_IN_FLIGHT_LEASE_SECONDS``.

    RESIDUAL RISK (accepted, not closed): this is a fixed window, so a send
    that actually runs longer than the configured value can still be reclaimed
    mid-flight and duplicated.  Size it above the real worst-case sender +
    confirmation time for the deployment.
    """
    section = _safe_get(config or {}, "reliable_pipeline", {}) or {}
    raw = _safe_get(section, "send_lease_seconds", DEFAULT_SEND_IN_FLIGHT_LEASE_SECONDS)
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_SEND_IN_FLIGHT_LEASE_SECONDS


def _classify_provider_failure(result: Any) -> Dict[str, Any]:
    """Return a safe, minimal classification of a provider failure.

    Fixed schema: result type name, ok flag, normalized status, and a
    whitelisted ``stage`` enum.  Never includes the provider's error message,
    reply text, or raw body, which may contain filesystem paths, secrets,
    or user content.
    """
    if isinstance(result, dict):
        result_type = str(result.get("result_type") or "dict")
        ok = bool(result.get("ok"))
        status = str(result.get("status") or "").strip().lower()
        raw = result.get("raw")
    else:
        result_type = type(result).__name__
        ok = getattr(result, "ok", None) is True
        status = str(getattr(result, "status", "") or "").strip().lower()
        raw = getattr(result, "raw", None) if not isinstance(result, BaseException) else None
    if not isinstance(raw, dict):
        raw = {}
    stage = raw.get("stage")
    if not isinstance(stage, str) or stage not in _ALLOWED_PROVIDER_FAILURE_STAGES:
        if isinstance(result, subprocess.TimeoutExpired):
            stage = "timeout"
        elif isinstance(result, BaseException):
            stage = "runner_error"
        elif _provider_status_timeout(status):
            stage = "timeout"
        else:
            stage = "unknown"
    return {
        "result_type": result_type,
        "ok": ok,
        "status": status,
        "stage": stage,
    }


def _provider_status_terminal_success(status: str) -> bool:
    """Return True only when the provider reports a successful terminal run.

    Positive set only.  ``submitted``, ``running``, ``pending``, ``queued``,
    ``in_progress``, and any empty/unknown status all return False; they
    are not eligible to drive an outbox.
    """
    s = str(status or "").strip().lower()
    return s in _TERMINAL_SUCCESS_STATUSES


def _provider_status_failed(status: str) -> bool:
    """Return True when the provider reports an explicit failure status."""
    s = str(status or "").strip().lower()
    if not s:
        return True
    if s in _FAIL_PROVIDER_STATUSES:
        return True
    if s.startswith("fail") or s.startswith("error") or s.startswith("timeout"):
        return True
    return False


def _provider_status_timeout(status: str) -> bool:
    s = str(status or "").strip().lower()
    return s == "timeout" or s.startswith("timeout")


def _extract_turn_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    raw = job.get("payload")
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _requires_knowledge_trace(payload: Dict[str, Any]) -> bool:
    """Return whether the target policy requires an MCP search attempt."""
    target_policy = payload.get("target_policy")
    if not isinstance(target_policy, dict):
        return False
    if target_policy.get("reply_policy") != "knowledge_grounded":
        return False
    allowed_kb_ids = payload.get("_allowed_kb_ids")
    return isinstance(allowed_kb_ids, list) and bool(allowed_kb_ids)


# --- Knowledge-search trace provenance (Stage 3) ----------------------------

# The worker owns the trace directory and trace id. The id and directory are
# passed to the provider as internal payload keys (stripped from the model
# prompt by ``agent_provider._prepare_model_job``) and forwarded to the MCP
# server only via the trusted environment variables. Hermes never receives a
# generic filesystem capability; the path is fixed and worker-generated.


def _resolve_trace_base_dir(config: Optional[Dict[str, Any]], db_path: Optional[Path] = None) -> Path:
    """Return the worker-controlled base directory for knowledge traces.

    The trace base is resolved in order of trust: explicit env, then config,
    then the directory containing the actual pipeline DB being used by the
    current job.  This prevents a global default from leaking across multiple
    pipeline DB instances.
    """
    env_dir = os.environ.get(TRACE_ENV_DIR)
    if env_dir:
        return Path(env_dir)
    cfg = config or {}
    section = _safe_get(cfg, "reliable_pipeline", {}) or {}
    candidate = _safe_get(section, "knowledge_trace_dir", None)
    if candidate:
        return Path(candidate)
    resolved_db = db_path or pipeline.DEFAULT_DB_PATH
    return resolved_db.parent / "knowledge_traces"


def _create_trace_context(job_id: int, config: Optional[Dict[str, Any]], db_path: Optional[Path] = None) -> Tuple[str, Path]:
    """Generate a fresh trace id and a per-job trace directory under the base."""
    trace_id = uuid.uuid4().hex
    base_dir = _resolve_trace_base_dir(config, db_path)
    trace_dir = base_dir / f"job_{job_id}_{trace_id}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    return trace_id, trace_dir


def _trace_artifact_path(trace_id: str, trace_dir: Path) -> Path:
    """Return the fixed trace artifact path for a job."""
    return trace_dir / f"{trace_id}{TRACE_FILE_SUFFIX}"


def _load_trace_provenance(trace_id: str, trace_dir: Path) -> Dict[str, Any]:
    """Load, validate, and clean up the MCP trace artifact for one job.

    Returns a structured metadata summary. Missing files are reported as
    ``no_tool_call``; an attempted search with no hits is reported as
    ``no_hit``; malformed or out-of-scope artifacts are reported as
    ``invalid`` (or ``error`` when the MCP file itself reports an error).
    The file is deleted after reading; the per-job directory is removed if
    empty.
    """
    summary: Dict[str, Any] = {
        "trace_id": trace_id,
        "status": "no_tool_call",
        "provenance": [],
        "no_source_reason": "",
        "loaded_at": _now(),
    }
    if not trace_id or not trace_dir:
        return summary

    path = _trace_artifact_path(trace_id, trace_dir)
    if not path.is_file():
        return summary

    try:
        size = path.stat().st_size
        if size > MAX_TRACE_BYTES:
            summary["status"] = "invalid"
            summary["error"] = "trace file exceeds %d bytes" % MAX_TRACE_BYTES
            return summary

        with open(path, "r", encoding="utf-8") as f:
            raw = f.read(MAX_TRACE_BYTES + 1)
        if len(raw) > MAX_TRACE_BYTES:
            summary["status"] = "invalid"
            summary["error"] = "trace file exceeds %d bytes" % MAX_TRACE_BYTES
            return summary

        data = json.loads(raw)
        if not isinstance(data, dict):
            summary["status"] = "invalid"
            summary["error"] = "trace file is not a JSON object"
            return summary
        if set(data.keys()) != TRACE_SCHEMA_KEYS:
            summary["status"] = "invalid"
            summary["error"] = "trace file has unexpected schema keys"
            return summary
        if data.get("trace_id") != trace_id:
            summary["status"] = "invalid"
            summary["error"] = "trace_id mismatch"
            return summary

        provenance = data.get("provenance") or []
        if not isinstance(provenance, list):
            summary["status"] = "invalid"
            summary["error"] = "provenance is not a list"
            return summary

        no_source_reason = str(data.get("no_source_reason") or "").strip()
        mcp_status = str(data.get("status") or "").strip().lower()

        if mcp_status == "error":
            summary["status"] = "error"
            summary["no_source_reason"] = no_source_reason
            return summary
        if mcp_status == "provider_failure":
            summary["status"] = "provider_failure"
            summary["no_source_reason"] = no_source_reason
            return summary
        if mcp_status == "no_hit" or not provenance:
            summary["status"] = "no_hit"
            summary["no_source_reason"] = no_source_reason
            return summary
        if mcp_status in ("ok", "success") and provenance:
            summary["status"] = "ok"
            summary["provenance"] = provenance
            summary["no_source_reason"] = no_source_reason
            return summary

        summary["status"] = "invalid"
        summary["error"] = "unrecognized trace status: %r" % mcp_status
        return summary
    except Exception as exc:
        summary["status"] = "invalid"
        summary["error"] = "trace read failed: %s" % exc
        return summary
    finally:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
        try:
            if trace_dir.exists() and not any(trace_dir.iterdir()):
                trace_dir.rmdir()
        except Exception:
            pass



def _validate_trace_kb_authorization(provenance: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    """Reject successful grounded-search traces that escape the target allowlist."""
    if not _requires_knowledge_trace(payload) or provenance.get("status") != "ok":
        return True
    allowed_kb_ids = {
        str(kb_id).strip()
        for kb_id in (payload.get("_allowed_kb_ids") or [])
        if str(kb_id).strip()
    }
    for item in provenance.get("provenance") or []:
        kb_id = str(item.get("kb_id") or "").strip() if isinstance(item, dict) else ""
        if not kb_id or kb_id not in allowed_kb_ids:
            provenance["status"] = "invalid"
            provenance["provenance"] = []
            provenance["no_source_reason"] = ""
            provenance["error"] = "knowledge trace contains unauthorized KB"
            return False
    return True
def _build_provider_job(*, job: Dict[str, Any], config: Optional[Dict[str, Any]],
                          trace_id: str, trace_dir: Path) -> Dict[str, Any]:
    """Translate a leased durable turn job into a provider job dict.

    The provider contract flag is required so providers can opt in to the
    strict versioned ``AgentResult`` document. The raw turn payload is
    normalized into a provider-ready payload: all events in the durable
    turn are aggregated into a conversation prompt, and authorization/
    policy fields are promoted from the last event's snapshot so queued jobs
    do not change KB scope or policy when config is edited later.

    Internal trace keys ``_knowledge_trace_id`` and ``_knowledge_trace_dir``
    are attached here; ``agent_provider._prepare_model_job`` strips them
    from the model prompt and forwards them to the MCP server as trusted
    environment variables.
    """
    raw_payload = _extract_turn_payload(job)
    payload = _normalize_turn_payload(raw_payload)
    payload[TRACE_PAYLOAD_ID] = trace_id
    payload[TRACE_PAYLOAD_DIR] = str(trace_dir)
    target_id = str(job.get("target_id") or "")
    group_key = str(job.get("group_key") or "")
    provider_job: Dict[str, Any] = {
        "job_id": int(job.get("id") or 0),
        "turn_id": int(job.get("turn_id") or 0) if job.get("turn_id") is not None else None,
        "target_id": target_id,
        "group_key": group_key,
        "task_type": "reply",
        "payload": payload,
    }
    if config is not None:
        provider_job["config"] = config
    return provider_job
def _resolve_dedicated_instance_id(provider_job: Dict[str, Any]) -> Optional[str]:
    """Return the bound ``dedicated_agent_instance_id`` for a provider job.

    Reads ``provider_job["payload"]["target"]["dedicated_agent_instance_id"]``,
    strips whitespace, and returns the value when it is a non-empty string.
    Returns ``None`` for any deviation (missing keys, wrong types, empty
    string, or whitespace-only).  The deep-agent worker uses this to route
    the turn to the same provider instance the target is bound to; when
    no binding is present, the worker falls back to the legacy default
    provider.
    """
    payload = _safe_get(provider_job, "payload", {}) or {}
    target = _safe_get(payload, "target", {}) or {}
    if not isinstance(target, dict):
        return None
    raw = target.get("dedicated_agent_instance_id")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    return cleaned or None



def _looks_like_agent_result(value: Any) -> bool:
    """Duck-typed check: does ``value`` look like an ``AgentResult``?

    The worker deliberately does not import the provider module or
    require a particular class identity; ``AgentResult`` is a
    ``@dataclass`` carrying ``ok``, ``status``, ``reply_text``, ``raw``,
    ``error``, ``latency``, ``provider``, and ``worker_id``.  Any object
    exposing at least ``ok`` and ``status`` is treated as a candidate
    provider result.  Tests and stubs pass plain objects; production
    ``AgentProvider.run`` returns the real ``AgentResult`` dataclass.
    """
    if value is None:
        return False
    if not (hasattr(value, "ok") and hasattr(value, "status")):
        return False
    ok = getattr(value, "ok", None)
    status = getattr(value, "status", None)
    if not isinstance(ok, bool) or not isinstance(status, str):
        return False
    return True


def _resolve_contract_payload(result: Any) -> Optional[Any]:
    """Return the raw contract value embedded in a provider result, if any.

    The worker only accepts the strict wire contract carried inside
    ``result.raw['agent_result']``.  Display text (``result.reply_text``)
    is never trusted as a contract channel, even for test providers or
    legacy callers, so a misbehaving model cannot route a contract
    through the wrong channel.
    """
    raw = getattr(result, "raw", None)
    if isinstance(raw, dict):
        candidate = raw.get("agent_result")
        if candidate is not None:
            return candidate
    return None


def _validate_contract(value: Any) -> Optional[pipeline.AgentResultContract]:
    """Strict parse; returns None for any deviation from the wire contract."""
    try:
        return pipeline.parse_agent_result(value)
    except pipeline.AgentResultContractError:
        return None
    except Exception:
        return None


# --- Target lookup -----------------------------------------------------------

def _match_target(targets: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    """Return the target whose ``username`` or ``name`` matches ``key``.

    Mirrors ``target_registry.find_target`` semantics so the worker can
    resolve a target without touching the on-disk registry.  The caller
    owns which config dict is consulted; this helper is intentionally
    side-effect-free.
    """
    if not key:
        return None
    for target in targets or []:
        if not isinstance(target, dict):
            continue
        if key in (target.get("username"), target.get("name")):
            return target
    return None


def _resolve_target(config: Optional[Dict[str, Any]], target_id: str) -> Optional[Dict[str, Any]]:
    """Resolve the configured target for ``target_id`` from the caller's config.

    This is a hard precondition for every send: ``send_once`` refuses to
    invoke the sender unless the target is present in
    ``config['targets']``.  We deliberately do NOT fall back to the
    on-disk ``target_registry`` here; that path could route an outbox row
    into an unintended active chat if the caller's config and the
    registry disagree.  Returning ``None`` is the safe default; callers
    must treat it as a hard failure.
    """
    if not target_id:
        return None
    cfg = config or {}
    targets = _safe_get(cfg, "targets", [])
    if not isinstance(targets, list):
        return None
    return _match_target(list(targets), target_id)


def _target_raw_name(target: Optional[Dict[str, Any]]) -> str:
    """Return ``target['name']`` exactly as configured, no normalization.

    The test-mode authorization gate compares this string verbatim against
    ``bot群聊测试``.  Stripping whitespace or matching case-insensitively
    would let a near-miss name slip through, so we keep the comparison raw
    and exact.
    """
    if not isinstance(target, dict):
        return ""
    name = target.get("name")
    if name is None:
        return ""
    return str(name)


# --- process_once ------------------------------------------------------------

def _fetch_job(db: Path, job_id: int) -> Optional[Dict[str, Any]]:
    """Read a job row back after a failure path; tolerates missing rows."""
    try:
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM turn_jobs WHERE id=?", (int(job_id),)).fetchone()
        return pipeline._row(row)  # type: ignore[attr-defined]
    except Exception:
        return None


def process_once(
    provider: Any,
    owner: str,
    config: Optional[Dict[str, Any]] = None,
    provider_factory: Optional[Callable[[Optional[str]], Any]] = None,
    *,
    final_filter: Optional[Callable[[str], str]] = None,
    db_path: Optional[Path] = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    timeout: Optional[float] = None,
    now: Optional[float] = None,
    instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Claim one durable turn job, run the provider, and apply its contract.

    Returns a dict with ``status`` (``empty``/``failed``/``applied``),
    the leased ``job``, and the durable effect (``outbox`` or post-failure
    job state).

    ``final_filter`` is mandatory; the caller owns the deterministic safety
    filter applied only to ``action=reply`` contracts.  ``silent`` and
    ``escalate`` bypass it and never produce an outbox row.  The provider
    must report a successful terminal run (``ok=True`` AND
    ``status in {done, sent, completed, succeeded, success}``) AND a
    strict ``AgentResult`` document; anything else (timeouts, non-``done``
    statuses like ``submitted`` or ``running``, plain stdout, Hermes
    boxes, ANSI, JSON-looking tool output, ``AgentResult`` violations,
    or any exception) fails the durable job without creating an outbox
    so a leaked provider stdout can never reach the sender.

    For each leased job, the worker creates a unique knowledge-search trace
    id and directory under the worker-controlled base (env
    ``WECHAT_MCP_TRACE_DIR``, config ``knowledge_trace_dir``, or the
    fixed ``knowledge_traces`` directory under the DB parent). The id and
    directory are passed as internal payload keys and forwarded by the
    provider to the MCP server via environment variables. After the provider
    run, the worker reads the fixed trace artifact
    ``{trace_dir}/{trace_id}.json``, validates it, and persists a structured
    provenance summary alongside the durable job result.
    """
    if not callable(final_filter):
        raise FinalFilterRequired("process_once requires a callable final_filter")
    if provider_factory is None and (provider is None or not hasattr(provider, "run")):
        raise ReliableWorkerError("process_once requires a provider with a run() method")

    db = _resolve_db_path(config, db_path)
    ts = float(now if now is not None else _now())
    claimed = pipeline.claim_next_job(
        owner=str(owner or DEFAULT_REPLY_OWNER),
        lease_seconds=float(lease_seconds),
        deadline_seconds=float(deadline_seconds),
        db_path=db,
        now=ts,
        instance_id=instance_id,
    )
    if not claimed:
        return {"status": "empty", "job": None, "outbox": None}

    job_id = int(claimed.get("id") or 0)
    trace_id, trace_dir = _create_trace_context(job_id, config, db_path=db)
    provider_job = _build_provider_job(job=claimed, config=config,
                                         trace_id=trace_id, trace_dir=trace_dir)
    norm_error = provider_job.get("payload", {}).get("_normalization_error")
    if norm_error:
        logger.warning("job=%s normalization failed: %s", job_id, norm_error)
        provenance = _load_trace_provenance(trace_id, trace_dir)
        pipeline.fail_job(job_id=job_id, error="normalization failed: %s" % norm_error,
                          provenance=provenance, retryable=False, db_path=db, now=ts)
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    # Resolve the effective provider.  When the caller supplied a
    # ``provider_factory``, we route the turn to the agent instance
    # bound by the target (``payload.target.dedicated_agent_instance_id``)
    # and only fall back to the legacy ``provider`` when the factory
    # raises or returns ``None``.  Without a factory, ``provider`` is
    # the only runnable.
    effective_provider: Any = provider
    if provider_factory is not None:
        instance_id = _resolve_dedicated_instance_id(provider_job)
        resolved: Any = None
        try:
            resolved = provider_factory(instance_id)
        except Exception as exc:
            logger.warning(
                "provider_factory raised for job=%s instance_id=%r: %s; "
                "falling back to legacy provider",
                job_id, instance_id, exc,
            )
            resolved = None
        else:
            if resolved is None:
                logger.warning(
                    "provider_factory returned None for job=%s instance_id=%r; "
                    "falling back to legacy provider",
                    job_id, instance_id,
                )
            else:
                effective_provider = resolved

    if effective_provider is None or not hasattr(effective_provider, "run"):
        provenance = _load_trace_provenance(trace_id, trace_dir)
        pipeline.fail_job(job_id=job_id, error="no runnable provider available",
                          provenance=provenance, retryable=False, db_path=db, now=ts)
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    provider_timeout = timeout
    if provider_timeout is None and _requires_knowledge_trace(provider_job.get("payload") or {}):
        provider_timeout = DEFAULT_KNOWLEDGE_PROVIDER_TIMEOUT
    # Bounded in-process retry around the provider invocation.  Stages in
    # ``_RETRYABLE_PROVIDER_STAGES`` are transient (parse / runner races) and
    # worth a second attempt; deterministic per-prompt gates (knowledge_gate,
    # contract_reject, safety_intercept, unknown) and budget-exhausting
    # failures (timeout, runner_error) fail terminally so the retry budget is
    # spent only on recoverable signal.  ``pipeline.fail_job`` is never called
    # inside the loop; it runs exactly once after the loop with the final
    # attempt's diagnostics plus a ``retry_count`` field.
    retry_base = _resolve_retry_base(config)
    attempt = 0
    contract = None
    final_failure = None  # type: ignore[assignment]
    while attempt < PROVIDER_RETRY_MAX_ATTEMPTS:
        attempt += 1
        try:
            result = effective_provider.run(provider_job, timeout=provider_timeout)
        except Exception as exc:
            # runner_error / timeout — never retry; budget already spent or
            # the runner itself faulted.
            logger.warning(
                "provider run raised for job=" + str(job_id) + " (attempt " + str(attempt) + "/" + str(PROVIDER_RETRY_MAX_ATTEMPTS) + "): " + repr(exc)
            )
            diag = _classify_provider_failure(exc)
            final_failure = {
                "error": "provider run failed",
                "diagnostics": diag,
                "retryable": diag.get("stage") in _RETRYABLE_PROVIDER_STAGES,
            }
            break

        provider_status = str(getattr(result, "status", "") or "").strip()
        if not _looks_like_agent_result(result):
            # Anything that does not expose ok + status (or whose values are
            # the wrong type) is a contract violation.
            diag = _classify_provider_failure({
                "result_type": type(result).__name__,
                "ok": False,
                "status": provider_status.lower(),
                "raw": {"stage": "contract_violation"},
            })
            final_failure = {
                "error": "provider returned non-AgentResult",
                "diagnostics": diag,
                "retryable": diag.get("stage") in _RETRYABLE_PROVIDER_STAGES,
            }
        elif result.ok is not True or _provider_status_failed(provider_status):
            # ok=False / failed / error / cancelled / no_reply: retry only
            # when the provider explicitly tagged the failure with a
            # transient stage.  Deterministic per-prompt gates
            # (knowledge_gate, contract_reject, safety_intercept, unknown)
            # fail terminally.
            diag = _classify_provider_failure(result)
            final_failure = {
                "error": "provider result failed",
                "diagnostics": diag,
                "retryable": diag.get("stage") in _RETRYABLE_PROVIDER_STAGES,
            }
        elif not _provider_status_terminal_success(provider_status):
            diag = _classify_provider_failure({
                "result_type": type(result).__name__,
                "ok": result.ok is True,
                "status": provider_status,
                "raw": {"stage": "contract_violation"},
            })
            status_repr = repr(provider_status or "<empty>")
            final_failure = {
                "error": "provider status " + status_repr + " is not a successful terminal run",
                "diagnostics": diag,
                "retryable": diag.get("stage") in _RETRYABLE_PROVIDER_STAGES,
            }
        else:
            contract_value = _resolve_contract_payload(result)
            contract = _validate_contract(contract_value)
            if contract is None:
                diag = _classify_provider_failure({
                    "result_type": type(result).__name__,
                    "ok": result.ok is True,
                    "status": provider_status,
                    "raw": {"stage": "contract_violation"},
                })
                final_failure = {
                    "error": "invalid agent result contract",
                    "diagnostics": diag,
                    "retryable": diag.get("stage") in _RETRYABLE_PROVIDER_STAGES,
                }
            else:
                final_failure = None
                break

        if (final_failure is not None
                and final_failure["retryable"]
                and attempt < PROVIDER_RETRY_MAX_ATTEMPTS):
            stage_val = final_failure["diagnostics"].get("stage")
            logger.info(
                "provider attempt " + str(attempt) + "/" + str(PROVIDER_RETRY_MAX_ATTEMPTS) + " for job=" + str(job_id) + " failed retryable stage=" + repr(stage_val) + "; sleeping " + str(retry_base) + "s before retry"
            )
            time.sleep(retry_base)
            continue
        break

    if final_failure is not None:
        # Load the trace artifact written by the MCP server for this job.
        # The trace is read only when we are about to record a terminal
        # failure so the provenance summary can be correlated with the
        # search that happened before the failure.  Provider failure
        # remains a separate status in the job row; the provenance
        # summary only describes the knowledge search.
        provenance = _load_trace_provenance(trace_id, trace_dir)
        retry_count = max(0, attempt - 1)
        pipeline.fail_job(
            job_id=job_id,
            error=final_failure["error"],
            provenance=provenance,
            retryable=False,
            db_path=db,
            now=ts,
            provider_diagnostics={**final_failure["diagnostics"], "retry_count": retry_count},
        )
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    # Success: load provenance and proceed to post-success gates + apply.
    provenance = _load_trace_provenance(trace_id, trace_dir)


    payload = provider_job.get("payload") or {}
    if _requires_knowledge_trace(payload) and provenance.get("status") == "no_tool_call":
        pipeline.fail_job(
            job_id=job_id,
            error="required knowledge search was not invoked",
            provenance=provenance,
            retryable=False,
            db_path=db,
            now=ts,
        )
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    if _requires_knowledge_trace(payload) and not _validate_trace_kb_authorization(provenance, payload):
        pipeline.fail_job(
            job_id=job_id,
            error="invalid knowledge search trace",
            provenance=provenance,
            retryable=False,
            db_path=db,
            now=ts,
        )
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    mention = str(payload.get("mention_name") or "").strip()
    applied = pipeline.apply_agent_result(
        job_id=job_id,
        result=contract,
        final_filter=final_filter,
        mention_name=mention,
        max_send_attempts=_resolve_max_send_attempts(config),
        provenance=provenance,
        db_path=db,
        now=ts,
    )
    return {"status": "applied", "job": applied.get("job"), "outbox": applied.get("outbox")}


# --- send_once ---------------------------------------------------------------

def _resolve_test_target_names(config: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    """Return the allowlist of target names permitted in test mode.

    Only accepts a list of strings; empty/invalid values fall back to the
    single default test target.  Names are stripped and empty entries are
    dropped, but no case normalization is applied so matching remains exact.
    """
    section = _safe_get(config or {}, "reliable_pipeline", {}) or {}
    raw = section.get("test_target_names")
    if not isinstance(raw, list):
        return (TEST_TARGET_ONLY_NAME,)
    names = [str(n).strip() for n in raw if isinstance(n, str) and str(n).strip()]
    return tuple(names) if names else (TEST_TARGET_ONLY_NAME,)


def _enforce_test_target_only(target: Optional[Dict[str, Any]], config: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return an error string when the gate rejects the target, else None.

    Compares ``target['name']`` verbatim against the configured allowlist
    (default: ``bot群聊测试``); any deviation in whitespace, case, suffix,
    or absence rejects the send.
    """
    if not _resolve_test_target_only(config):
        return None
    if not isinstance(target, dict):
        return "test_mode_target_rejected: target not configured"
    name = _target_raw_name(target)
    allowed = _resolve_test_target_names(config)
    if name not in allowed:
        return "test_mode_target_rejected: target name %r is not in %r" % (name, allowed)
    return None


def _record_skip(db: Path, outbox_id: int, error: str, ts: float, retry_base: float,
                 owner: str, lease_id: str) -> Dict[str, Any]:
    """Record a non-send terminal state for an outbox row (any gate rejection)."""
    return pipeline.record_send_result(
        outbox_id=int(outbox_id),
        owner=owner,
        lease_id=lease_id,
        confirmed=False,
        detail={"skipped": True, "reason": error[:200]},
        error=error[:500],
        db_path=db,
        now=ts,
        retry_base_seconds=retry_base,
    )


def send_once(
    owner: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    db_path: Optional[Path] = None,
    limit: int = DEFAULT_SEND_LIMIT,
    lease_seconds: float = DEFAULT_SEND_LEASE_SECONDS,
    confirm: Optional[Callable[[dict, int, str, float], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Claim sendable outbox rows and dispatch ``send_reply_detailed``.

    For each claimed row, the configured target is resolved from the
    caller's ``config['targets']`` only.  When the target is not configured
    in that list the send is rejected and ``send_reply_detailed`` is NOT
    invoked; an outbox row whose target cannot be resolved from the
    caller's config is treated as a hard precondition failure and recorded
    as a retryable miss so a later config refresh can re-evaluate.  When
    ``cfg.reliable_pipeline.test_target_only`` is set, rows whose target
    name is not exactly ``bot群聊测试`` are rejected without invoking the
    sender.  ``SendResult.ok`` is recorded as a confirmed send; any other
    outcome is treated as a retry/dead-letter per ``record_send_result``'s
    exponential-backoff rules.  The caller-supplied ``now`` is honored for
    claim, backoff computation, and durable writes so deterministic tests
    can exercise retry timing.
    """
    db = _resolve_db_path(config, db_path)
    ts = float(now if now is not None else _now())
    retry_base = _resolve_retry_base(config)
    send_owner = str(owner or DEFAULT_SEND_OWNER)
    # Once a send is authorized the marker extends the lease to cover the
    # worst-case send+confirmation duration; never shorter than the claim lease.
    send_lease = max(_resolve_send_lease_seconds(config), float(lease_seconds))
    claimed = pipeline.claim_sendable(
        owner=send_owner,
        limit=max(1, int(limit)),
        lease_seconds=float(lease_seconds),
        db_path=db,
        now=ts,
    )
    if not claimed:
        return {"status": "empty", "processed": [], "skipped": []}

    processed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in claimed:
        outbox_id = int(row.get("id") or 0)
        target_id = str(row.get("target_id") or "")
        lease_id = str(row.get("lease_id") or "")
        target = _resolve_target(config, target_id)
        # Hard precondition: never call the sender without a target from
        # the caller's config.  ``send_reply_detailed(..., target=None)``
        # falls back to whatever chat WeChat is currently showing, which
        # could route the message into the wrong conversation.  Treat an
        # unconfigured target the same way as a test-mode rejection.
        if target is None:
            gate_error = "%s: target_id=%r" % (TARGET_NOT_CONFIGURED_REASON, target_id)
            updated = _record_skip(db, outbox_id, gate_error, ts, retry_base, send_owner, lease_id)
            skipped.append({"outbox_id": outbox_id, "target_id": target_id, "error": gate_error, "row": updated})
            continue
        gate_error = _enforce_test_target_only(target, config)
        if gate_error is not None:
            updated = _record_skip(db, outbox_id, gate_error, ts, retry_base, send_owner, lease_id)
            skipped.append({"outbox_id": outbox_id, "target_id": target_id, "error": gate_error, "row": updated})
            continue
        # Persist the durable send-start marker immediately before invoking the
        # sender.  ``record_send_started`` is guarded by the current lease; a
        # False return means the row was reclaimed by another owner, so we must
        # not send (the new owner will retry it).  Use a fresh timestamp rather
        # than the claim-time ``ts`` so the lease-validity check and extension
        # reflect the real mark moment; when the caller injected ``now`` (tests)
        # this stays on that same deterministic clock.
        mark_ts = float(now if now is not None else _now())
        if not pipeline.record_send_started(outbox_id=outbox_id, lease_id=lease_id,
                                            lease_extension_seconds=send_lease,
                                            db_path=db, now=mark_ts):
            logger.warning("send aborted for outbox=%s: lease no longer owned by %s", outbox_id, send_owner)
            skipped.append({"outbox_id": outbox_id, "target_id": target_id,
                            "error": "send aborted: lease lost before send"})
            continue
        try:
            send_result: SendResult = send_reply_detailed(
                str(row.get("reply_text") or ""),
                target=target,
                before_local_id=int(row.get("before_local_id") or 0) or None,
                cfg=config,
                confirm=confirm,
                log=log,
                mention_name=str(row.get("mention_name") or "") or None,
            )
        except Exception as exc:
            logger.warning("send_reply_detailed raised for outbox=%s: %s", outbox_id, exc)
            updated = pipeline.record_send_result(
                outbox_id=outbox_id,
                owner=send_owner,
                lease_id=lease_id,
                confirmed=False,
                detail={"exception": repr(exc)},
                error="send_reply_detailed raised: %r" % (exc,),
                db_path=db,
                now=ts,
                retry_base_seconds=retry_base,
            )
            processed.append({"outbox_id": outbox_id, "ok": False, "row": updated, "exception": repr(exc)})
            continue

        detail = {
            "mode": getattr(send_result, "mode", "") or "",
            "attempted": list(getattr(send_result, "attempted", []) or []),
            "reason": getattr(send_result, "reason", "") or "",
            "confirmed": getattr(send_result, "confirmed", None),
        }
        confirmed = bool(getattr(send_result, "ok", False))
        updated = pipeline.record_send_result(
            outbox_id=outbox_id,
            owner=send_owner,
            lease_id=lease_id,
            confirmed=confirmed,
            detail=detail,
            error="" if confirmed else (detail["reason"] or "send confirmation failed"),
            db_path=db,
            now=ts,
            retry_base_seconds=retry_base,
        )
        processed.append({"outbox_id": outbox_id, "ok": confirmed, "row": updated,
                          "reason": detail["reason"], "attempted": detail["attempted"]})
    return {"status": "processed" if processed else "empty", "processed": processed, "skipped": skipped}


__all__ = [
    "DEFAULT_REPLY_OWNER",
    "DEFAULT_SEND_OWNER",
    "RELIABLE_RESULT_CONTRACT_FLAG",
    "TEST_TARGET_ONLY_NAME",
    "TARGET_NOT_CONFIGURED_REASON",
    "ReliableWorkerError",
    "FinalFilterRequired",
    "TestModeTargetRejected",
    "process_once",
    "send_once",
]
