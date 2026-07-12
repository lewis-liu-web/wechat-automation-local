"""Tests for control_api M5 dispatcher/reconciler fixes.

Stage 2 wiring: every fake provider returns an ``AgentResult`` whose
``raw["agent_result"]`` matches ``AgentResultContract.to_dict()`` — the
wire shape ``reliable_worker`` will hand to the reconciler once the
legacy callers (``agent_worker``, ``wechat_bot_monitor``, ``reply_engine``
manual routes) are migrated.  The legacy ``reply_text`` channel is
carried on the same fakes so the current production gate (today:
``poll_result.reply_text`` plus ``agent_jobs.sanitize_agent_result_text``)
still reaches the right terminal action: ``completed`` for a valid
reply contract, ``poll_error`` on poll exception, ``expired`` on
deadline.  Display-text rejection (Hermes box frames, ANSI, tool logs)
belongs to ``tests/test_agent_output_sanitization.py`` via the strict
contract path of ``HermesProvider`` and is not exercised here.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs
import control_api
from agent_provider import AgentResult
from reliable_pipeline import AgentResultContract


# ---------------------------------------------------------------------------
# Helpers: strict-contract AgentResult construction for the reconciler fakes.
# ---------------------------------------------------------------------------


def _contract(action="reply", reply_text="answer", reason_code="answered", risk_level="low"):
    return AgentResultContract(
        action=action,
        reply_text=reply_text,
        reason_code=reason_code,
        risk_level=risk_level,
    ).to_dict()


def _replier_result(*, action="reply", reply_text="", reason_code="answered", risk_level="low",
                    extra_raw=None):
    """Build the strict-contract AgentResult shape reconciler consumers expect.

    ``agent_jobs.sanitize_agent_result_text`` strips ANSI / box frames /
    surrounding whitespace before the result reaches ``complete_job``; mirror
    that here so the wire contract (``raw["agent_result"]``) and the legacy
    ``reply_text`` channel agree on the canonical text the reconciler would
    ultimately write back.
    """
    canonical_reply = reply_text.strip() if reply_text else ""
    contract = _contract(action=action, reply_text=canonical_reply,
                         reason_code=reason_code, risk_level=risk_level)
    raw = {"agent_result": contract, "strict": True, "schema_version": 1}
    if extra_raw:
        raw.update(extra_raw)
    status = "done" if action == "reply" else ("escalated" if action == "escalate" else action)
    return AgentResult(
        ok=True,
        status=status,
        reply_text=canonical_reply,
        raw=raw,
    )


def _provider_boilerplate(fake_provider, monkeypatch, *, health_ok=True):
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(
        control_api,
        "_cached_provider_health",
        lambda *a, **k: {"ok": health_ok, "ready": health_ok},
    )


# ---------------------------------------------------------------------------
# Dispatcher: payload['agent_timeout'] still flows into agent_deadline_at.
# ---------------------------------------------------------------------------


def test_dispatcher_deadline_uses_payload_agent_timeout(monkeypatch):
    """Dispatcher should read payload['agent_timeout'] for agent_deadline_at."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _replier_result(
        extra_raw={"bridge_session_id": "sess-1", "bridge_user_msg_id": 1},
    )

    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 42,
        "provider": "fake",
        "payload": {
            "agent_timeout": 900,
            "image_paths": ["img1.jpg"],
            "prompt": "test",
        },
    }
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)

    mark_submitted = MagicMock(return_value=True)
    merge_payload = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "mark_submitted", mark_submitted)
    monkeypatch.setattr(agent_jobs, "merge_payload", merge_payload)

    t0 = time.time()
    result = control_api._async_dispatcher_run_once({})

    assert result["ok"] is True
    assert result["action"] == "submitted"
    mark_submitted.assert_called_once()
    call_kwargs = mark_submitted.call_args.kwargs
    assert "agent_deadline_at" in call_kwargs
    assert t0 + 899 <= call_kwargs["agent_deadline_at"] <= t0 + 901
    # submit_timeout is capped at 240 even though agent_timeout is 900
    fake_provider.submit.assert_called_once()
    submit_call_kwargs = fake_provider.submit.call_args.kwargs
    assert submit_call_kwargs.get("timeout") == 240.0
    # Stage 2: dispatcher must force strict contract flag onto the submitted job.
    submitted_job = fake_provider.submit.call_args.args[0]
    assert submitted_job["payload"]["reliable_result_contract"] is True


def test_dispatcher_injects_strict_contract_flag_into_empty_payload(monkeypatch):
    """Empty payload must be written back into job before setting the strict flag."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _replier_result(
        extra_raw={"bridge_session_id": "sess-empty", "bridge_user_msg_id": 1},
    )

    _provider_boilerplate(fake_provider, monkeypatch)

    job = {"id": 44, "provider": "fake", "payload": {}}
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)
    monkeypatch.setattr(agent_jobs, "mark_submitted", MagicMock(return_value=True))
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=True))

    result = control_api._async_dispatcher_run_once({})

    assert result["ok"] is True
    submitted_job = fake_provider.submit.call_args.args[0]
    assert submitted_job["payload"]["reliable_result_contract"] is True


def test_dispatcher_deadline_falls_back_for_text_job(monkeypatch):
    """Dispatcher uses 300s fallback for plain text jobs without agent_timeout."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _replier_result(
        extra_raw={"bridge_session_id": "sess-2", "bridge_user_msg_id": 2},
    )

    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 43,
        "provider": "fake",
        "payload": {"prompt": "text only"},
    }
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)

    monkeypatch.setattr(agent_jobs, "mark_submitted", MagicMock(return_value=True))
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=True))

    t0 = time.time()
    result = control_api._async_dispatcher_run_once({})

    assert result["ok"] is True
    mark_submitted = agent_jobs.mark_submitted
    call_kwargs = mark_submitted.call_args.kwargs
    assert t0 + 299 <= call_kwargs["agent_deadline_at"] <= t0 + 301
    assert fake_provider.submit.call_args.kwargs.get("timeout") == 30.0
    # Stage 2: dispatcher must force strict contract flag onto the submitted job.
    submitted_job = fake_provider.submit.call_args.args[0]
    assert submitted_job["payload"]["reliable_result_contract"] is True


def test_dispatcher_persists_strict_flag_and_skips_submit_on_merge_failure(monkeypatch):
    """If merge_payload fails, dispatcher must release lock and not submit."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _replier_result(
        extra_raw={"bridge_session_id": "sess-merge", "bridge_user_msg_id": 1},
    )

    _provider_boilerplate(fake_provider, monkeypatch)

    job = {"id": 45, "provider": "fake", "payload": {"prompt": "p"}}
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)
    release_dispatching = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "release_dispatching", release_dispatching)
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=False))
    monkeypatch.setattr(agent_jobs, "mark_submitted", MagicMock(return_value=True))

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "persistence_failed"
    assert result["job_id"] == 45
    fake_provider.submit.assert_not_called()
    release_dispatching.assert_called_once()


def test_dispatcher_fails_job_on_submit_rejection(monkeypatch):
    """Provider submit returning ok=False must fail the dispatching job."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        ok=False, status="failed", error="provider rejected", raw={}
    )

    _provider_boilerplate(fake_provider, monkeypatch)

    job = {"id": 46, "provider": "fake", "payload": {"prompt": "p"}}
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=True))
    fail_job = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    fail_job.assert_called_once()
    assert fail_job.call_args.args[0] == 46


def test_dispatcher_fails_job_on_missing_session_id(monkeypatch):
    """Submit without bridge_session_id must fail the dispatching job."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _replier_result(extra_raw={})

    _provider_boilerplate(fake_provider, monkeypatch)

    job = {"id": 47, "provider": "fake", "payload": {"prompt": "p"}}
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=True))
    fail_job = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "no_session"
    fail_job.assert_called_once()
    assert fail_job.call_args.args[0] == 47


def test_dispatcher_records_session_info_if_mark_submitted_fails(monkeypatch):
    """External submit succeeded but mark_submitted failed: persist session info for audit."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _replier_result(
        extra_raw={"bridge_session_id": "sess-mark", "bridge_user_msg_id": 99},
    )

    _provider_boilerplate(fake_provider, monkeypatch)

    job = {"id": 48, "provider": "fake", "payload": {"prompt": "p"}}
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=True))
    monkeypatch.setattr(agent_jobs, "mark_submitted", MagicMock(return_value=False))
    mark_submission_failed = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "mark_submission_failed", mark_submission_failed)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "mark_submitted_failed"
    assert result["external_session_id"] == "sess-mark"
    assert result["external_user_msg_id"] == 99
    mark_submission_failed.assert_called_once()
    call_kwargs = mark_submission_failed.call_args.kwargs
    assert call_kwargs["external_session_id"] == "sess-mark"
    assert call_kwargs["external_user_msg_id"] == 99


# ---------------------------------------------------------------------------
# Reconciler: strict contract actions
# ---------------------------------------------------------------------------


def test_reconciler_completes_strict_reply_and_persists_wire_contract(monkeypatch):
    """Strict reply contract completes the job and persists raw["agent_result"]."""
    wire = AgentResultContract(
        action="reply",
        reply_text="Hello world",
        reason_code="answered",
        risk_level="low",
    )
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = _replier_result(
        action="reply",
        reply_text="  Hello world  ",
        reason_code="answered",
        risk_level="low",
        extra_raw={"bridge_session_id": "sess-9", "bridge_user_msg_id": 9},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 9,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-9",
        "external_user_msg_id": 9,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    merge_payload = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "merge_payload", merge_payload)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 9 and r["action"] == "completed" for r in result["results"])
    complete_job.assert_called_once_with(9, "Hello world")
    fail_job.assert_not_called()
    merge_payload.assert_any_call(9, {"agent_raw_output": fake_provider.poll.return_value.raw})
    update_poll_state.assert_called_once_with(
        9,
        next_poll_at=pytest.approx(time.time() + 60.0, abs=2.0),
        external_status="done",
    )

    # Wire-contract assertion: the fake that completed the job carried the
    # expected AgentResult wire shape under raw["agent_result"].  Once the
    # production reconciler switches its gate to raw["agent_result"], this
    # invariant is what keeps the test meaningful.
    wire_seen = fake_provider.poll.return_value.raw["agent_result"]
    assert wire_seen == wire.to_dict()


def test_reconciler_strict_silent_marks_terminal_skip(monkeypatch):
    """Strict silent action completes the job and marks send skipped."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = _replier_result(action="silent")
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 20,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-20",
        "external_user_msg_id": 20,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    mark_send_skipped = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "mark_send_skipped", mark_send_skipped)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 20 and r["action"] == "silent" for r in result["results"])
    complete_job.assert_called_once_with(20, "")
    mark_send_skipped.assert_called_once_with(20, reason="silent")
    update_poll_state.assert_called_once_with(20, next_poll_at=pytest.approx(time.time() + 60.0, abs=2.0), external_status="silent")


def test_reconciler_strict_escalate_marks_terminal_skip(monkeypatch):
    """Strict escalate action completes the job and marks send skipped."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = _replier_result(action="escalate")
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 21,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-21",
        "external_user_msg_id": 21,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    mark_send_skipped = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "mark_send_skipped", mark_send_skipped)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 21 and r["action"] == "escalate" for r in result["results"])
    complete_job.assert_called_once_with(21, "")
    mark_send_skipped.assert_called_once_with(21, reason="escalate")
    update_poll_state.assert_called_once_with(21, next_poll_at=pytest.approx(time.time() + 60.0, abs=2.0), external_status="escalate")


def test_reconciler_strict_ignores_contract_when_poll_not_ok(monkeypatch):
    """ok=False with raw['agent_result'] must fall through to error/backoff, not strict complete."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    contract = _contract(action="reply", reply_text="should not be used")
    fake_provider.poll.return_value = AgentResult(
        ok=False,
        status="failed",
        error="Hermes error",
        raw={"agent_result": contract},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 22,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-22",
        "external_user_msg_id": 22,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 22 and r["action"] == "poll_error" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_not_called()
    update_poll_state.assert_called_once_with(22, next_poll_at=pytest.approx(time.time() + 5.0, abs=2.0), external_status="error")


def test_reconciler_strict_mismatched_status_falls_through(monkeypatch):
    """reply action without done status must not be processed as strict reply."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    contract = _contract(action="reply", reply_text="not done")
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="running",
        reply_text="",
        raw={"agent_result": contract},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 23,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-23",
        "external_user_msg_id": 23,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    mark_agent_running = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)
    monkeypatch.setattr(agent_jobs, "mark_agent_running", mark_agent_running)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 23 and r["action"] == "still_running" for r in result["results"])
    complete_job.assert_not_called()



def test_reconciler_treats_poll_exceptions_as_poll_errors(monkeypatch):
    """Reconciler should treat provider.poll exceptions as poll errors with backoff."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.side_effect = RuntimeError("provider boom")
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 8,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-8",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    merge_payload = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "merge_payload", merge_payload)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 8 and r["action"] == "poll_error" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_not_called()
    update_poll_state.assert_called_once_with(
        8,
        next_poll_at=pytest.approx(time.time() + 5.0, abs=2.0),
        external_status="error",
    )

def test_reconciler_truncates_reply_to_payload_max_reply_chars(monkeypatch):
    """Reconciler must apply payload max_reply_chars to polled replies."""
    raw_output = {"assistant": {"content": "a" * 100}}

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        True,
        "done",
        reply_text="a" * 100,
        raw=raw_output,
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)

    job = {
        "id": 10,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-10",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"max_reply_chars": 15},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    merge_payload = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)

    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "merge_payload", merge_payload)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)
    # Keep config empty so payload cap is the only source.
    monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: {})

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 10 and r["action"] == "completed" for r in result["results"])
    complete_job.assert_called_once()
    saved_text = complete_job.call_args[0][1]
    assert len(saved_text) == 15
    fail_job.assert_not_called()
