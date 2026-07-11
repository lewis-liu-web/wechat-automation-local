"""Tests for control_api M5 dispatcher/reconciler fixes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
from unittest.mock import MagicMock

import pytest

import agent_jobs
import control_api
from agent_provider import AgentResult


PURE_HERMES_NOISE = "│\n────\n│\nResume this session with: y/N"


def test_sanitize_pure_hermes_noise_is_empty():
    """Lock in the property that pure Hermes frame/resume noise sanitizes to empty."""
    assert agent_jobs.sanitize_agent_result_text(PURE_HERMES_NOISE) == ""


def test_dispatcher_deadline_uses_payload_agent_timeout(monkeypatch):
    """Dispatcher should read payload['agent_timeout'] for agent_deadline_at."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        True,
        "submitted",
        raw={"bridge_session_id": "sess-1", "bridge_user_msg_id": 1},
    )

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(
        control_api,
        "_cached_provider_health",
        lambda *a, **k: {"ok": True, "ready": True},
    )

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


def test_dispatcher_deadline_falls_back_for_text_job(monkeypatch):
    """Dispatcher uses 300s fallback for plain text jobs without agent_timeout."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        True,
        "submitted",
        raw={"bridge_session_id": "sess-2", "bridge_user_msg_id": 2},
    )

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(
        control_api,
        "_cached_provider_health",
        lambda *a, **k: {"ok": True, "ready": True},
    )

    job = {
        "id": 43,
        "provider": "fake",
        "payload": {"prompt": "text only"},
    }
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: job)

    mark_submitted = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "mark_submitted", mark_submitted)
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=True))

    t0 = time.time()
    result = control_api._async_dispatcher_run_once({})

    assert result["ok"] is True
    call_kwargs = mark_submitted.call_args.kwargs
    assert t0 + 299 <= call_kwargs["agent_deadline_at"] <= t0 + 301
    assert fake_provider.submit.call_args.kwargs.get("timeout") == 30.0


def test_reconciler_treats_empty_sanitize_as_failed_and_preserves_raw(monkeypatch):
    """Reconciler must fail jobs whose reply sanitizes to empty and preserve raw output."""
    raw_output = {"stdout": "raw-test-output"}

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        True,
        "done",
        reply_text=PURE_HERMES_NOISE,
        raw=raw_output,
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)

    job = {
        "id": 7,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-7",
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
    mark_expired = MagicMock(return_value=True)
    mark_agent_running = MagicMock(return_value=True)

    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "merge_payload", merge_payload)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)
    monkeypatch.setattr(agent_jobs, "mark_expired", mark_expired)
    monkeypatch.setattr(agent_jobs, "mark_agent_running", mark_agent_running)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 7 and r["action"] == "failed" for r in result["results"])

    complete_job.assert_not_called()
    fail_job.assert_called_once_with(7, "empty after sanitize", status=agent_jobs.STATUS_FAILED)
    merge_payload.assert_any_call(7, {"agent_raw_output": raw_output})
    update_poll_state.assert_called_once_with(7, next_poll_at=pytest.approx(time.time() + 60.0, abs=2.0),
                                              external_status="failed")


def test_reconciler_catches_poll_exception_and_backoffs(monkeypatch):
    """Reconciler should treat provider.poll exceptions as poll errors with backoff."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.side_effect = RuntimeError("provider boom")
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)

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
    update_poll_state.assert_called_once_with(8, next_poll_at=pytest.approx(time.time() + 5.0, abs=2.0),
                                              external_status="error")


def test_reconciler_merges_raw_on_successful_completion(monkeypatch):
    """Reconciler should preserve raw output even for successful replies."""
    raw_output = {"assistant": {"content": "hello"}}

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        True,
        "done",
        reply_text="  Hello world  ",
        raw=raw_output,
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)

    job = {
        "id": 9,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-9",
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
    assert any(r["job_id"] == 9 and r["action"] == "completed" for r in result["results"])
    complete_job.assert_called_once_with(9, "Hello world")
    fail_job.assert_not_called()
    merge_payload.assert_any_call(9, {"agent_raw_output": raw_output})
    update_poll_state.assert_called_once_with(9, next_poll_at=pytest.approx(time.time() + 60.0, abs=2.0),
                                              external_status="done")


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
