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

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import reliable_pipeline as pipeline
from wechat_sender import SendResult, send_reply_detailed

logger = logging.getLogger(__name__)


# --- Default values ----------------------------------------------------------

DEFAULT_REPLY_OWNER = "reliable-worker"
DEFAULT_SEND_OWNER = "reliable-sender"
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_DEADLINE_SECONDS = 300.0
DEFAULT_SEND_LEASE_SECONDS = 45.0
DEFAULT_MAX_SEND_ATTEMPTS = 5
DEFAULT_SEND_LIMIT = 10
DEFAULT_RETRY_BASE_SECONDS = 5.0
RELIABLE_RESULT_CONTRACT_FLAG = "reliable_result_contract"
TEST_TARGET_ONLY_NAME = "bot群聊测试"
TARGET_NOT_CONFIGURED_REASON = "target not configured"
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


def _build_provider_job(*, job: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Translate a leased durable turn job into a provider job dict.

    The provider contract flag is required so providers can opt in to the
    strict versioned ``AgentResult`` document. The raw turn payload is
    normalized into a provider-ready payload: all events in the durable
    turn are aggregated into a conversation prompt, and authorization/
    policy fields are promoted from the last event's snapshot so queued jobs
    do not change KB scope or policy when config is edited later.
    """
    raw_payload = _extract_turn_payload(job)
    payload = _normalize_turn_payload(raw_payload)
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
    )
    if not claimed:
        return {"status": "empty", "job": None, "outbox": None}

    job_id = int(claimed.get("id") or 0)
    provider_job = _build_provider_job(job=claimed, config=config)
    norm_error = provider_job.get("payload", {}).get("_normalization_error")
    if norm_error:
        logger.warning("job=%s normalization failed: %s", job_id, norm_error)
        pipeline.fail_job(job_id=job_id, error="normalization failed: %s" % norm_error,
                          retryable=False, db_path=db, now=ts)
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
        pipeline.fail_job(job_id=job_id, error="no runnable provider available",
                          retryable=False, db_path=db, now=ts)
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    try:
        result = effective_provider.run(provider_job, timeout=timeout)
    except Exception as exc:  # provider raised; transition job to failed
        logger.warning("provider run raised for job=%s: %s", job_id, exc)
        pipeline.fail_job(job_id=job_id, error="provider run failed",
                          retryable=False, db_path=db, now=ts)
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    if not _looks_like_agent_result(result):
        # Anything that does not expose ``ok`` + ``status`` (or whose
        # values are the wrong type) is a contract violation.  The
        # provider is supposed to return an ``AgentResult``-shaped
        # object; a bare string or arbitrary dict cannot be trusted to
        # carry the versioned document, so we fail the job without
        # creating an outbox.
        pipeline.fail_job(job_id=job_id, error="provider returned non-AgentResult",
                          retryable=False, db_path=db, now=ts)
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    provider_status = str(getattr(result, "status", "") or "").strip()
    if result.ok is not True or _provider_status_failed(provider_status):
        # An explicit ``ok=False`` or a known-failure status (``failed``,
        # ``error``, ``timeout``, ``cancelled``, ``no_reply``, ...) is
        # a non-retryable failure.  The provider did not produce a
        # usable document; ``fail_job`` without retry keeps the lease
        # free and the job in a terminal state.  Never echo the provider's
        # error attribute; it may contain filesystem paths or secrets.
        pipeline.fail_job(job_id=job_id, error="provider result failed",
                          retryable=False, db_path=db, now=ts)
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    if not _provider_status_terminal_success(provider_status):
        # ``ok=True`` is not enough; only a successful terminal status
        # (done/sent/completed/succeeded/success) may apply the
        # contract.  ``submitted``, ``running``, ``pending``, ``queued``,
        # ``in_progress``, or any other in-progress marker means the
        # process has not actually exited successfully, so the
        # document cannot be trusted and no outbox is created.
        pipeline.fail_job(
            job_id=job_id,
            error=("provider status %r is not a successful terminal run"
                   % (provider_status or "<empty>",)),
            retryable=False,
            db_path=db,
            now=ts,
        )
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    contract_value = _resolve_contract_payload(result)
    contract = _validate_contract(contract_value)
    if contract is None:
        # Provider produced something that is not the strict wire contract;
        # plain text, box frames, ANSI, JSON-looking tool output, etc. all
        # land here.  No outbox may be created from this state.  The
        # persisted/returned reason is a fixed safe marker — never provider
        # body text such as reply_text or raw stdout, which could contain
        # user content or secrets.
        pipeline.fail_job(
            job_id=job_id,
            error="invalid agent result contract",
            retryable=False,
            db_path=db,
            now=ts,
        )
        return {"status": "failed", "job": _fetch_job(db, job_id), "outbox": None}

    payload = provider_job.get("payload") or {}
    mention = str(payload.get("mention_name") or "").strip()
    applied = pipeline.apply_agent_result(
        job_id=job_id,
        result=contract,
        final_filter=final_filter,
        mention_name=mention,
        max_send_attempts=_resolve_max_send_attempts(config),
        db_path=db,
        now=ts,
    )
    return {"status": "applied", "job": applied.get("job"), "outbox": applied.get("outbox")}


# --- send_once ---------------------------------------------------------------

def _enforce_test_target_only(target: Optional[Dict[str, Any]], config: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return an error string when the gate rejects the target, else None.

    Compares ``target['name']`` verbatim against ``bot群聊测试``; any
    deviation in whitespace, case, suffix, or absence rejects the send.
    """
    if not _resolve_test_target_only(config):
        return None
    if not isinstance(target, dict):
        return "test_mode_target_rejected: target not configured"
    name = _target_raw_name(target)
    if name != TEST_TARGET_ONLY_NAME:
        return "test_mode_target_rejected: target name %r is not %r" % (name, TEST_TARGET_ONLY_NAME)
    return None


def _record_skip(db: Path, outbox_id: int, error: str, ts: float, retry_base: float) -> Dict[str, Any]:
    """Record a non-send terminal state for an outbox row (any gate rejection)."""
    return pipeline.record_send_result(
        outbox_id=int(outbox_id),
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
    claimed = pipeline.claim_sendable(
        owner=str(owner or DEFAULT_SEND_OWNER),
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
        target = _resolve_target(config, target_id)
        # Hard precondition: never call the sender without a target from
        # the caller's config.  ``send_reply_detailed(..., target=None)``
        # falls back to whatever chat WeChat is currently showing, which
        # could route the message into the wrong conversation.  Treat an
        # unconfigured target the same way as a test-mode rejection.
        if target is None:
            gate_error = "%s: target_id=%r" % (TARGET_NOT_CONFIGURED_REASON, target_id)
            updated = _record_skip(db, outbox_id, gate_error, ts, retry_base)
            skipped.append({"outbox_id": outbox_id, "target_id": target_id, "error": gate_error, "row": updated})
            continue
        gate_error = _enforce_test_target_only(target, config)
        if gate_error is not None:
            updated = _record_skip(db, outbox_id, gate_error, ts, retry_base)
            skipped.append({"outbox_id": outbox_id, "target_id": target_id, "error": gate_error, "row": updated})
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
