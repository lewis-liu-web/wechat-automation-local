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

import json
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


def test_dispatcher_submit_rejection_does_not_leak_provider_error(tmp_path, monkeypatch):
    """Provider submit returning ok=False must not leak error text into fail_job or response."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="submit-reject-1",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        ok=False, status="failed", error="/secret/path/to/token: SECRET_TOKEN",
        raw={},
    )

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    assert "SECRET_TOKEN" not in str(result)
    assert "/secret/path" not in str(result)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] != agent_jobs.STATUS_DISPATCHING
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert stored["error"] == control_api._SAFE_SUBMIT_ERROR


def test_dispatcher_submit_exception_falls_back_to_release_when_fail_job_false(tmp_path, monkeypatch):
    """If fail_job returns False on a submit exception, dispatcher must release the dispatching lock."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="submit-exc-fallback",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.side_effect = RuntimeError("/secret/path: SECRET_TOKEN leaked")

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)
    fail_job = MagicMock(return_value=False)
    release_dispatching = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "release_dispatching", release_dispatching)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_EXCEPTION
    fail_job.assert_called_once()
    release_dispatching.assert_called_once_with(int(job["id"]), reason=control_api._SAFE_SUBMIT_EXCEPTION)


def test_dispatcher_submit_rejection_falls_back_to_release_when_fail_job_false(tmp_path, monkeypatch):
    """If fail_job returns False on a submit rejection, dispatcher must release the dispatching lock."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="submit-reject-fallback",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        ok=False, status="failed", error="/secret/path: SECRET_TOKEN",
        raw={},
    )

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)
    fail_job = MagicMock(return_value=False)
    release_dispatching = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "release_dispatching", release_dispatching)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    fail_job.assert_called_once()
    release_dispatching.assert_called_once_with(int(job["id"]), reason=control_api._SAFE_SUBMIT_ERROR)


def test_dispatcher_submit_exception_does_not_leak_and_fails_job(tmp_path, monkeypatch):
    """Provider submit raising must not leak exception text and must fail the dispatching job."""
    db = tmp_path / "agent_jobs.sqlite"
    job = agent_jobs.enqueue_job(
        job_key="submit-exc-1",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    assert claimed["status"] == agent_jobs.STATUS_DISPATCHING

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.side_effect = RuntimeError("/secret/path: SECRET_TOKEN leaked")

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)
    monkeypatch.setattr(agent_jobs, "merge_payload", MagicMock(return_value=True))
    fail_job = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_EXCEPTION
    assert "SECRET_TOKEN" not in str(result)
    assert "/secret/path" not in str(result)
    fail_job.assert_called_once()
    assert fail_job.call_args.args[0] == int(job["id"])
    assert fail_job.call_args.args[1] == control_api._SAFE_SUBMIT_EXCEPTION
    assert "SECRET_TOKEN" not in fail_job.call_args.args[1]


def test_dispatcher_submit_exception_transitions_real_job_out_of_dispatching(tmp_path, monkeypatch):
    """Provider submit raising must leave a real DB job terminal, not dispatching."""
    db = tmp_path / "agent_jobs.sqlite"
    # Redirect dispatcher's default DB path to the temp DB so it reads/writes the same file.
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="submit-exc-2",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.side_effect = RuntimeError("/secret/path: SECRET_TOKEN leaked")

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    # Force the dispatcher to use the same claimed job so submit is invoked on the real row.
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] != agent_jobs.STATUS_DISPATCHING
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert stored["error"] == control_api._SAFE_SUBMIT_EXCEPTION


def test_dispatcher_initial_provider_build_raises_returns_safe_unavailable(monkeypatch):
    """A secret-bearing provider construction exception must not escape; return safe unavailable."""
    monkeypatch.setattr(
        control_api, "_build_agent_provider",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("/secret/path: SECRET_TOKEN")),
    )

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "provider_unavailable"
    assert result["error"] == control_api._SAFE_PROVIDER_UNAVAILABLE
    assert "SECRET_TOKEN" not in str(result)
    assert "/secret/path" not in str(result)


def test_dispatcher_post_claim_provider_build_raises_fails_job_safely(tmp_path, monkeypatch):
    """If re-building the job's provider raises, the dispatching job must be terminal safely."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="post-claim-build-raise",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    initial_provider = MagicMock()
    initial_provider.name = "different"

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda provider_name=None, **k: (
        initial_provider if provider_name in (None, "") or provider_name == "different" else
        (_ for _ in ()).throw(RuntimeError("/secret/path: SECRET_TOKEN"))
    ))
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: {**claimed, "provider": "hermes"})

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_PROVIDER_UNAVAILABLE
    assert "SECRET_TOKEN" not in str(result)
    assert "/secret/path" not in str(result)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] != agent_jobs.STATUS_DISPATCHING
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert stored["error"] == control_api._SAFE_PROVIDER_UNAVAILABLE


def test_dispatcher_provider_unavailable_does_not_leak_health_error(monkeypatch):
    """Provider health returning not-ready must not leak the health error string."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(
        control_api, "_cached_provider_health",
        lambda *a, **k: {"ok": False, "ready": False, "error": "/secret/path: SECRET_TOKEN"},
    )

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "provider_unavailable"
    assert result["error"] == control_api._SAFE_PROVIDER_UNAVAILABLE
    assert "SECRET_TOKEN" not in str(result)
    assert "/secret/path" not in str(result)


# ---------------------------------------------------------------------------
# Dispatcher submit-result shape safety (Stage 2 Phase A)
# ---------------------------------------------------------------------------


def test_dispatcher_malformed_submit_result_fails_job_safely(tmp_path, monkeypatch):
    """A malformed AgentResult (wrong raw type) must fail the job safely."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="malformed-1",
        group_key="g1",
        task_type="test",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    # Dataclass instance but raw is a string, not a dict; .raw.get() would raise
    # and the string could contain a secret.
    fake_provider.submit.return_value = AgentResult(
        ok=True, status="done", raw="C:/private/path SECRET_TOKEN",
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    assert "SECRET_TOKEN" not in str(result)
    assert "C:/private/path" not in str(result)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] != agent_jobs.STATUS_DISPATCHING
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert "C:/private/path" not in (stored["error"] or "")


def test_dispatcher_none_submit_result_fails_job_safely(tmp_path, monkeypatch):
    """A provider returning None from submit must fail the job safely."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="none-1",
        group_key="g1",
        task_type="test",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = None
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] != agent_jobs.STATUS_DISPATCHING
    assert stored["status"] == agent_jobs.STATUS_FAILED


def test_dispatcher_malformed_user_msg_id_fails_job_safely(tmp_path, monkeypatch):
    """A non-integer bridge_user_msg_id must not raise and must not leak."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="bad-msg-id-1",
        group_key="g1",
        task_type="test",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    secret = "C:/private/path SECRET_TOKEN"
    fake_provider.submit.return_value = AgentResult(
        ok=True,
        status="done",
        raw={"bridge_session_id": "s", "bridge_user_msg_id": secret},
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    result = control_api._async_dispatcher_run_once({})

    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    assert "SECRET_TOKEN" not in str(result)
    assert "C:/private/path" not in str(result)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] != agent_jobs.STATUS_DISPATCHING
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert "C:/private/path" not in (stored["error"] or "")


def test_reconciler_provider_build_raises_fails_job_safely(tmp_path, monkeypatch):
    """Provider construction raising in the reconciler must fail the pollable job safely.

    The underlying exception text (paths / secrets) must not appear in the returned
    result, in any mocked transition, or in the persisted job record.  The job must
    leave the active/locked submitted/agent_running state.
    """
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="reconciler-build-raise",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    agent_jobs.mark_submitted(
        int(job["id"]),
        external_provider="hermes",
        external_session_id="sess-reconciler-build",
        external_user_msg_id=1,
        next_poll_at=1.0,  # well in the past so list_pollable returns the job
        db_path=db,
    )

    real_fail_job = agent_jobs.fail_job
    fail_job_spy = MagicMock(wraps=real_fail_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job_spy)

    fake_provider = MagicMock()
    fake_provider.name = "never-called"
    monkeypatch.setattr(
        control_api,
        "_build_agent_provider",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("C:/private/path SECRET_TOKEN")),
    )
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(
        r["job_id"] == job["id"]
        and r["action"] == "failed"
        and r["error"] == control_api._SAFE_PROVIDER_UNAVAILABLE
        for r in result["results"]
    )
    assert "SECRET_TOKEN" not in str(result)
    assert "C:/private/path" not in str(result)

    fail_job_spy.assert_called_once()
    call_args = fail_job_spy.call_args
    assert call_args.args[0] == int(job["id"])
    assert call_args.args[1] == control_api._SAFE_PROVIDER_UNAVAILABLE
    assert "SECRET_TOKEN" not in str(call_args.args)
    assert "C:/private/path" not in str(call_args.args)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["status"] not in (agent_jobs.STATUS_SUBMITTED, agent_jobs.STATUS_AGENT_RUNNING)
    assert stored["error"] == control_api._SAFE_PROVIDER_UNAVAILABLE
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert "C:/private/path" not in (stored["error"] or "")

    fake_provider.poll.assert_not_called()


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
        "payload": {"reliable_result_contract": True},
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
    merge_payload.assert_any_call(9, {"bridge_session_id": "sess-9", "bridge_user_msg_id": 9})
    # Raw provider bodies must never be persisted on the strict success path.
    for call in merge_payload.call_args_list:
        assert "agent_raw_output" not in call.args[1]
        assert call.args[1].get("agent_result") is None
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
        "payload": {"reliable_result_contract": True},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job_silent = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job_silent", complete_job_silent)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 20 and r["action"] == "silent" for r in result["results"])
    complete_job_silent.assert_called_once_with(20, reason="silent")
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
        "payload": {"reliable_result_contract": True},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job_silent = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job_silent", complete_job_silent)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 21 and r["action"] == "escalate" for r in result["results"])
    complete_job_silent.assert_called_once_with(21, reason="escalate")
    update_poll_state.assert_called_once_with(21, next_poll_at=pytest.approx(time.time() + 60.0, abs=2.0), external_status="escalate")


def test_reconciler_strict_silent_helper_failure_reports_failure(monkeypatch):
    """If complete_job_silent fails, the reconciler must report complete_failed, not success."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = _replier_result(action="silent")
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 25,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-25",
        "external_user_msg_id": 25,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job_silent = MagicMock(return_value=False)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job_silent", complete_job_silent)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(r["job_id"] == 25 and r["action"] == "complete_failed" for r in result["results"])
    update_poll_state.assert_not_called()


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
        "payload": {"reliable_result_contract": True},
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
        "payload": {"reliable_result_contract": True},
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


# ---------------------------------------------------------------------------
# Stage 2 Phase A: strict contract is authoritative; legacy reply_text fallback removed.
# ---------------------------------------------------------------------------


def test_reconciler_strict_terminal_only_reply_text_fails_not_legacy_complete(monkeypatch):
    """Strict job with terminal ok=True and only reply_text (no raw agent_result) must fail."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="legacy display text reply",
        raw={"bridge_session_id": "sess-rtx", "bridge_user_msg_id": 1},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 70,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-rtx",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 70 and r["action"] == "failed" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_called_once()
    assert "agent_result" in (fail_job.call_args[0][1] or "").lower()


def test_reconciler_strict_malformed_agent_result_fails(monkeypatch):
    """Strict job with malformed raw['agent_result'] must fail, not fall back to reply_text."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="some text",
        raw={"agent_result": {"action": "reply", "reply_text": "malformed"}},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 71,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-71",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 71 and r["action"] == "failed" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_called_once()


def test_reconciler_strict_running_with_no_agent_result_keeps_polling(monkeypatch):
    """Strict job still running with no raw agent_result must not fail; it keeps polling."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="running",
        reply_text="",
        raw={"bridge_session_id": "sess-run", "bridge_user_msg_id": 1},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 72,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-run",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 1,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 72 and r["action"] == "still_running" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_not_called()
    update_poll_state.assert_called_once_with(
        72,
        next_poll_at=pytest.approx(time.time() + 20.0, abs=2.0),
        external_status="running",
    )


def test_reconciler_strict_contract_reply_text_wins_over_display_text(monkeypatch):
    """Parsed contract reply_text is the source of truth; provider reply_text is ignored."""
    contract = _contract(action="reply", reply_text="from contract")
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="display text noise",
        raw={"agent_result": contract},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 73,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-73",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 73 and r["action"] == "completed" for r in result["results"])
    complete_job.assert_called_once_with(73, "from contract")
    fail_job.assert_not_called()


def test_reconciler_strict_malformed_agent_result_while_running_fails(monkeypatch):
    """A malformed agent_result produced while running must fail immediately, not keep polling."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="running",
        reply_text="",
        raw={"agent_result": {"action": "reply", "reply_text": "malformed"}},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 75,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-75",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 75 and r["action"] == "failed" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_called_once()


def test_reconciler_strict_malformed_agent_result_when_not_ok_fails(monkeypatch):
    """A malformed agent_result when poll_result.ok is False must still fail the job."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=False,
        status="failed",
        error="Hermes error",
        raw={"agent_result": {"action": "reply", "reply_text": "malformed"}},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 76,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-76",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 76 and r["action"] == "failed" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_called_once()


def test_reconciler_strict_empty_sanitized_reply_contract_fails(monkeypatch):
    """A strict reply contract that sanitizes to empty must fail."""
    contract = _contract(action="reply", reply_text="\x1b[32m\x1b[0m")
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="\x1b[32m\x1b[0m",
        raw={"agent_result": contract},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 77,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-77",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 77 and r["action"] == "failed" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_called_once()


def test_reconciler_non_strict_legacy_reply_text_still_works(monkeypatch):
    """Jobs without the strict flag keep the legacy reply_text path."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="legacy reply",
        raw={"bridge_session_id": "sess-legacy", "bridge_user_msg_id": 1},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 78,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-legacy",
        "external_user_msg_id": 1,
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
    assert any(r["job_id"] == 78 and r["action"] == "completed" for r in result["results"])
    complete_job.assert_called_once_with(78, "legacy reply")
    fail_job.assert_not_called()


def test_reconciler_strict_contract_accepts_any_terminal_success_status(monkeypatch):
    """A valid reply contract with terminal status 'success' should complete (no status/action pairing)."""
    contract = _contract(action="reply", reply_text="hello")
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="success",
        reply_text="hello",
        raw={"agent_result": contract},
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 74,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-74",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 74 and r["action"] == "completed" for r in result["results"])
    complete_job.assert_called_once_with(74, "hello")
    fail_job.assert_not_called()


def _secret_bearing_raw():
    return {
        "agent_result": {"action": "reply", "reply_text": "malformed"},
        "assistant": {
            "content": "leaked C:/Users/bob/secrets/keys.json and sk-live-abc123",
            "thinking": "internal path C:/\\u7528\\u6237/bob/app",
        },
        "tool_output": "token: sk-secret-xyz",
    }


def test_reconciler_strict_malformed_does_not_persist_raw_diagnostics(monkeypatch):
    """Malformed strict contract must fail without persisting raw provider bodies."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="some text",
        raw=_secret_bearing_raw(),
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 91,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-91",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 91 and r["action"] == "failed" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_called_once()
    merge_payload.assert_not_called()


def test_reconciler_strict_not_ok_does_not_persist_raw_diagnostics(monkeypatch):
    """Strict poll returning ok=False must not persist raw provider bodies."""
    contract = _contract(action="reply", reply_text="should not be used")
    raw = _secret_bearing_raw()
    raw["agent_result"] = contract
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=False,
        status="failed",
        error="Hermes error",
        raw=raw,
    )
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 92,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-92",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
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
    assert any(r["job_id"] == 92 and r["action"] == "poll_error" for r in result["results"])
    complete_job.assert_not_called()
    fail_job.assert_not_called()
    update_poll_state.assert_called_once()
    # No raw body or agent diagnostics may reach the payload.
    for call in merge_payload.call_args_list:
        assert "agent_raw_output" not in call.args[1]
        assert call.args[1].get("assistant") is None
        assert call.args[1].get("tool_output") is None


def test_reconciler_poll_exception_never_leaks_path_or_secret(monkeypatch):
    """Provider.poll exceptions must use a safe marker regardless of strict/legacy jobs."""

    class _LeakyProvider:
        name = "leaky"

        def poll(self, session_id, user_msg_id, timeout=None):
            raise RuntimeError("/private/path SECRET_TOKEN")

    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: _LeakyProvider())
    monkeypatch.setattr(
        control_api,
        "_cached_provider_health",
        lambda *a, **k: {"ok": True, "ready": True},
    )

    now = time.time()
    far_job = {
        "id": 101,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "leaky",
        "external_session_id": "sess-101",
        "external_user_msg_id": 1,
        "agent_deadline_at": now + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
    }
    near_job = {
        "id": 102,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "leaky",
        "external_session_id": "sess-102",
        "external_user_msg_id": 2,
        "agent_deadline_at": now + 10,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [far_job, near_job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    mark_expired = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "mark_expired", mark_expired)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    far = next(r for r in result["results"] if r["job_id"] == 101)
    near = next(r for r in result["results"] if r["job_id"] == 102)
    assert far["action"] == "poll_error"
    assert near["action"] == "expired"
    assert far["error"] == "poll exception"
    assert near["error"] == "poll exception"
    assert "/private/path" not in str(far)
    assert "SECRET_TOKEN" not in str(far)
    assert "/private/path" not in str(near)
    assert "SECRET_TOKEN" not in str(near)

    complete_job.assert_not_called()
    fail_job.assert_not_called()
    mark_expired.assert_called_once_with(102, reason="poll exception")
    update_poll_state.assert_called_once_with(
        101,
        next_poll_at=pytest.approx(time.time() + 5.0, abs=2.0),
        external_status="error",
    )
    # Ensure no mock call arguments leaked the raw exception text.
    leaked = str({
        "mark_expired": mark_expired.call_args,
        "update_poll_state": update_poll_state.call_args,
    })
    assert "/private/path" not in leaked
    assert "SECRET_TOKEN" not in leaked


def _secret_provider_result():
    """Return a provider result with a secret-bearing error string."""
    return AgentResult(
        ok=False,
        status="failed",
        error="/private/path SECRET_TOKEN",
        raw={},
    )


def test_reconciler_strict_provider_error_does_not_leak_in_poll_error(monkeypatch):
    """Strict job with provider-returned error must use a safe backoff marker."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = _secret_provider_result()
    _provider_boilerplate(fake_provider, monkeypatch)

    job = {
        "id": 111,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-111",
        "external_user_msg_id": 1,
        "agent_deadline_at": time.time() + 9999,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    mark_expired = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "mark_expired", mark_expired)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    row = next(r for r in result["results"] if r["job_id"] == 111)
    assert row["action"] == "poll_error"
    assert row["error"] == "poll result failed"
    assert "/private/path" not in str(result)
    assert "SECRET_TOKEN" not in str(result)
    complete_job.assert_not_called()
    fail_job.assert_not_called()
    mark_expired.assert_not_called()
    update_poll_state.assert_called_once_with(
        111,
        next_poll_at=pytest.approx(time.time() + 5.0, abs=2.0),
        external_status="error",
    )
    # Mock call arguments must not carry the raw error text.
    leaked = str({"update_poll_state": update_poll_state.call_args})
    assert "/private/path" not in leaked
    assert "SECRET_TOKEN" not in leaked


def test_reconciler_strict_provider_error_does_not_leak_on_expiry(monkeypatch):
    """Strict near-deadline expiry must use a safe marker, not provider error."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = _secret_provider_result()
    _provider_boilerplate(fake_provider, monkeypatch)

    now = time.time()
    job = {
        "id": 112,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-112",
        "external_user_msg_id": 1,
        "agent_deadline_at": now + 10,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {"reliable_result_contract": True},
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    mark_expired = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "mark_expired", mark_expired)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    row = next(r for r in result["results"] if r["job_id"] == 112)
    assert row["action"] == "expired"
    assert row["error"] == "poll expired"
    assert "/private/path" not in str(result)
    assert "SECRET_TOKEN" not in str(result)
    complete_job.assert_not_called()
    fail_job.assert_not_called()
    update_poll_state.assert_not_called()
    mark_expired.assert_called_once_with(112, reason="poll expired")
    leaked = str({"mark_expired": mark_expired.call_args})
    assert "/private/path" not in leaked
    assert "SECRET_TOKEN" not in leaked


def test_reconciler_legacy_provider_error_does_not_leak_on_expiry(monkeypatch):
    """Legacy near-deadline expiry must use a safe marker, not provider error."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = _secret_provider_result()
    _provider_boilerplate(fake_provider, monkeypatch)

    now = time.time()
    job = {
        "id": 113,
        "status": agent_jobs.STATUS_AGENT_RUNNING,
        "external_provider": "fake",
        "external_session_id": "sess-113",
        "external_user_msg_id": 1,
        "agent_deadline_at": now + 10,
        "next_poll_at": 0,
        "reconcile_attempts": 0,
        "payload": {},  # no reliable_result_contract
    }
    monkeypatch.setattr(agent_jobs, "list_pollable", lambda **k: [job])

    complete_job = MagicMock(return_value=True)
    fail_job = MagicMock(return_value=True)
    mark_expired = MagicMock(return_value=True)
    update_poll_state = MagicMock(return_value=True)
    monkeypatch.setattr(agent_jobs, "complete_job", complete_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job)
    monkeypatch.setattr(agent_jobs, "mark_expired", mark_expired)
    monkeypatch.setattr(agent_jobs, "update_poll_state", update_poll_state)

    result = control_api._async_reconciler_run_once({})

    row = next(r for r in result["results"] if r["job_id"] == 113)
    assert row["action"] == "expired"
    assert row["error"] == "poll expired"
    assert "/private/path" not in str(result)
    assert "SECRET_TOKEN" not in str(result)
    complete_job.assert_not_called()
    fail_job.assert_not_called()
    update_poll_state.assert_not_called()
    mark_expired.assert_called_once_with(113, reason="poll expired")
    leaked = str({"mark_expired": mark_expired.call_args})
    assert "/private/path" not in leaked
    assert "SECRET_TOKEN" not in leaked


# ---------------------------------------------------------------------------
# Stage 2 Phase A: poll result shape validation.
# ---------------------------------------------------------------------------


class _MalformedPollResult:
    """Not an AgentResult; would leak secret if its attributes were read."""

    def __init__(self, secret: str):
        self.ok = True
        self.status = "done"
        self.reply_text = "answer"
        self.raw = {"agent_result": {"action": "reply", "reply_text": "answer"}, "secret": secret}


class _RaisingOkAgentResult(AgentResult):
    """AgentResult subclass whose ``ok`` property raises."""

    def __init__(self, secret: str):
        self._secret = secret

    @property
    def ok(self):
        raise RuntimeError(self._secret)

    @property
    def status(self):
        return "done"

    @property
    def reply_text(self):
        return "answer"

    @property
    def raw(self):
        return {"agent_result": {"action": "reply", "reply_text": "answer"}}


class _HostileDict(dict):
    """Dict subclass whose iteration raises."""

    def __init__(self, secret: str, *args, **kwargs):
        self._secret = secret
        super().__init__(*args, **kwargs)

    def __iter__(self):
        raise RuntimeError(self._secret)


def _hostile_raw_result(secret: str):
    return AgentResult(
        ok=True,
        status="done",
        reply_text="answer",
        raw=_HostileDict(secret, {"agent_result": {"action": "reply", "reply_text": "answer"}}),
    )


@pytest.mark.parametrize(
    "poll_return, expected_error",
    [
        (None, control_api._SAFE_POLL_ERROR),
        ("C:/private/path SECRET_TOKEN", control_api._SAFE_POLL_ERROR),
        (_MalformedPollResult("C:/private/path SECRET_TOKEN"), control_api._SAFE_POLL_ERROR),
        (_RaisingOkAgentResult("C:/private/path SECRET_TOKEN"), control_api._SAFE_POLL_ERROR),
        (_hostile_raw_result("C:/private/path SECRET_TOKEN"), "terminal success missing agent_result contract"),
    ],
    ids=["none", "string", "malformed_object", "raising_property", "hostile_raw_dict"],
)
def test_reconciler_invalid_poll_result_fails_job_safely(tmp_path, monkeypatch, poll_return, expected_error):
    """Invalid poll result shapes must fail the pollable job without leaking text."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="poll-invalid-1",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p", "reliable_result_contract": True},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    agent_jobs.mark_submitted(
        int(job["id"]),
        external_provider="fake",
        external_session_id="sess-poll-invalid",
        external_user_msg_id=1,
        next_poll_at=1.0,
        db_path=db,
    )

    real_fail_job = agent_jobs.fail_job
    fail_job_spy = MagicMock(wraps=real_fail_job)
    monkeypatch.setattr(agent_jobs, "fail_job", fail_job_spy)

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = poll_return
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    result = control_api._async_reconciler_run_once({})

    assert result["ok"] is True
    assert any(
        r["job_id"] == job["id"]
        and r["action"] == "failed"
        and (r.get("error") == expected_error or r.get("reason") == expected_error)
        for r in result["results"]
    )
    assert "SECRET_TOKEN" not in str(result)
    assert "C:/private/path" not in str(result)

    fail_job_spy.assert_called_once()
    call_args = fail_job_spy.call_args
    assert call_args.args[0] == int(job["id"])
    assert call_args.args[1] == expected_error
    assert "SECRET_TOKEN" not in str(call_args.args)
    assert "C:/private/path" not in str(call_args.args)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["status"] not in (agent_jobs.STATUS_SUBMITTED, agent_jobs.STATUS_AGENT_RUNNING)
    assert stored["error"] == expected_error
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert "C:/private/path" not in (stored["error"] or "")


def test_reconciler_route_invalid_poll_result_no_http_leak(tmp_path, monkeypatch):
    """Calling the reconciler HTTP route with an invalid poll result must not leak repr text."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="poll-route-invalid",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p", "reliable_result_contract": True},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    agent_jobs.mark_submitted(
        int(job["id"]),
        external_provider="fake",
        external_session_id="sess-poll-route-invalid",
        external_user_msg_id=1,
        next_poll_at=1.0,
        db_path=db,
    )

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = secret
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/reconciler/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["ok"] is True
    row = next(r for r in result["results"] if r["job_id"] == job["id"])
    assert row["action"] == "failed"
    assert row["error"] == control_api._SAFE_POLL_ERROR
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["error"] == control_api._SAFE_POLL_ERROR
    assert secret not in (stored["error"] or "")


def test_safe_poll_result_snapshot_uses_exact_scalar_types():
    """Snapshot must accept only base bool/str scalars, not subclasses or other types."""
    maybe = control_api._safe_poll_result_snapshot(AgentResult(ok=True, status="done", reply_text="hi"))
    assert maybe is not None
    assert maybe[0] is not None

    class StrSub(str):
        pass

    assert control_api._safe_poll_result_snapshot(AgentResult(ok=1, status="done", reply_text="hi")) is None
    assert control_api._safe_poll_result_snapshot(AgentResult(ok=True, status=StrSub("done"), reply_text="hi")) is None
    assert control_api._safe_poll_result_snapshot(AgentResult(ok=True, status="done", reply_text=StrSub("hi"))) is None
    assert control_api._safe_poll_result_snapshot("not an agent result") is None


def test_safe_bridge_patch_accepts_only_base_types_and_exact_non_bool_values():
    """Bridge metadata must be base str/non-empty str and base int/non-bool to be persisted."""
    assert agent_jobs._safe_bridge_patch({
        "bridge_session_id": "s",
        "bridge_user_msg_id": 1,
    }) == {"bridge_session_id": "s", "bridge_user_msg_id": 1}
    assert agent_jobs._safe_bridge_patch({"bridge_session_id": "s"}) == {"bridge_session_id": "s"}
    assert agent_jobs._safe_bridge_patch({"bridge_user_msg_id": 1}) == {"bridge_user_msg_id": 1}
    assert agent_jobs._safe_bridge_patch({}) == {}
    assert agent_jobs._safe_bridge_patch(None) == {}
    # Malicious or wrong-typed values must be rejected.
    assert agent_jobs._safe_bridge_patch({
        "bridge_session_id": 123,
        "bridge_user_msg_id": True,
        "evil": "C:/private/path SECRET_TOKEN",
    }) == {}
    assert agent_jobs._safe_bridge_patch({
        "bridge_session_id": "",
        "bridge_user_msg_id": 0,
    }) == {"bridge_user_msg_id": 0}
    assert agent_jobs._safe_bridge_patch({"bridge_user_msg_id": 0}) == {"bridge_user_msg_id": 0}
    # Dict subclasses are rejected before their accessors can run.
    class _HostileGetDict(dict):
        def get(self, key, default=None):
            raise RuntimeError("C:/private/path SECRET_TOKEN")
    assert agent_jobs._safe_bridge_patch(_HostileGetDict({
        "bridge_session_id": "s",
        "bridge_user_msg_id": 1,
    })) == {}


class _HostileBridgeValue:
    """Object whose string/repr/bool/int conversion raises to catch unsafe coercion."""

    def __init__(self, secret: str):
        self._secret = secret

    def __str__(self):
        raise RuntimeError(self._secret)

    def __repr__(self):
        raise RuntimeError(self._secret)

    def __bool__(self):
        raise RuntimeError(self._secret)

    def __int__(self):
        raise RuntimeError(self._secret)


class _HostileGetDict(dict):
    """Dict subclass whose ``get`` accessor raises to catch unsafe inspection."""

    def get(self, key, default=None):
        raise RuntimeError("C:/private/path SECRET_TOKEN")


def test_dispatcher_route_rejects_hostile_bridge_session_id(tmp_path, monkeypatch):
    """Hostile bridge_session_id must fail submit without leaking or coercing the value."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="hostile-session-id",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        ok=True,
        status="done",
        raw={
            "bridge_session_id": _HostileBridgeValue(secret),
            "bridge_user_msg_id": 1,
        },
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/dispatcher/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in (stored["error"] or "")
    assert secret not in str(stored.get("payload") or {})
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_dispatcher_route_rejects_hostile_bridge_user_msg_id(tmp_path, monkeypatch):
    """Hostile bridge_user_msg_id must fail submit without leaking or coercing the value."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="hostile-msg-id",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        ok=True,
        status="done",
        raw={
            "bridge_session_id": "sess-hostile-msg-id",
            "bridge_user_msg_id": _HostileBridgeValue(secret),
        },
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/dispatcher/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in (stored["error"] or "")
    assert secret not in str(stored.get("payload") or {})
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_dispatcher_route_rejects_zero_user_msg_id_but_preserves_safe_session_id(tmp_path, monkeypatch):
    """A zero bridge_user_msg_id must fail submit while the safe session id is not merged."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="zero-msg-id",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = AgentResult(
        ok=True,
        status="done",
        raw={"bridge_session_id": "sess-zero", "bridge_user_msg_id": 0},
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/dispatcher/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["error"] == control_api._SAFE_SUBMIT_ERROR
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_dispatcher_route_rejects_raising_ok_accessor(tmp_path, monkeypatch):
    """A raising ``ok`` accessor on a submit result must fail safely."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="raising-ok",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    class _RaisingOkResult(AgentResult):
        @property
        def ok(self):
            raise RuntimeError(secret)

        @ok.setter
        def ok(self, _value):
            pass

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _RaisingOkResult(
        ok=True,
        status="done",
        raw={"bridge_session_id": "s", "bridge_user_msg_id": 1},
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/dispatcher/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in (stored["error"] or "")
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_dispatcher_route_rejects_raising_raw_accessor(tmp_path, monkeypatch):
    """A raising ``raw`` accessor on a submit result must fail safely."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="raising-raw",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None

    class _RaisingRawSubmitResult(AgentResult):
        @property
        def raw(self):
            raise RuntimeError(secret)

        @raw.setter
        def raw(self, _value):
            pass

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.submit.return_value = _RaisingRawSubmitResult(
        ok=True,
        status="done",
        raw={"bridge_session_id": "s", "bridge_user_msg_id": 1},
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})
    monkeypatch.setattr(agent_jobs, "claim_dispatchable", lambda **k: claimed)

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/dispatcher/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "submit_failed"
    assert result["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert stored["error"] == control_api._SAFE_SUBMIT_ERROR
    assert secret not in (stored["error"] or "")
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_reconciler_route_ignores_hostile_bridge_values_on_success(tmp_path, monkeypatch):
    """A valid strict contract with hostile bridge metadata must complete without persisting secrets."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="hostile-bridge-success",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p", "reliable_result_contract": True},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    agent_jobs.mark_submitted(
        int(job["id"]),
        external_provider="fake",
        external_session_id="sess-hostile-bridge-success",
        external_user_msg_id=1,
        next_poll_at=1.0,
        db_path=db,
    )

    contract = _contract(action="reply", reply_text="hello from contract")
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="hello from contract",
        raw={
            "agent_result": contract,
            "bridge_session_id": _HostileBridgeValue(secret),
            "bridge_user_msg_id": _HostileBridgeValue(secret),
            "assistant": {"content": "leaked %s" % secret},
        },
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/reconciler/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["ok"] is True
    row = next(r for r in result["results"] if r["job_id"] == job["id"])
    assert row["action"] == "completed"
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_DONE
    assert stored["result_text"] == "hello from contract"
    assert secret not in (stored["error"] or "")
    assert secret not in str(stored.get("payload") or {})
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_recover_result_route_ignores_hostile_bridge_values_on_success(tmp_path, monkeypatch):
    """Recover-result must complete without persisting hostile bridge metadata."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="recover-hostile-bridge",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    agent_jobs.fail_job(int(job["id"]), "simulate prior failure", status=agent_jobs.STATUS_FAILED, db_path=db)

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.recover.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="recovered reply",
        raw={
            "bridge_session_id": _HostileBridgeValue(secret),
            "bridge_user_msg_id": _HostileBridgeValue(secret),
            "assistant": {"content": "leaked %s" % secret},
        },
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(
        "POST", "/agent/jobs/%d/recover-result" % int(job["id"]), {}, {}
    )
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "recovered"
    assert result["job_id"] == job["id"]
    assert result["result"] == {"ok": True, "status": "done", "has_reply": True}
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_DONE
    assert stored["result_text"] == "recovered reply"
    assert secret not in (stored["error"] or "")
    assert secret not in str(stored.get("payload") or {})
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_recover_result_route_fails_safely_when_recover_returns_no_reply(tmp_path, monkeypatch):
    """Recover-result with no reply_text must not persist hostile bridge values."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="recover-no-reply",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    agent_jobs.fail_job(int(job["id"]), "simulate prior failure", status=agent_jobs.STATUS_FAILED, db_path=db)

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.recover.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="",
        raw={
            "bridge_session_id": _HostileBridgeValue(secret),
            "bridge_user_msg_id": _HostileBridgeValue(secret),
        },
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(
        "POST", "/agent/jobs/%d/recover-result" % int(job["id"]), {}, {}
    )
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "recover_failed"
    assert result["result"] == {"ok": True, "status": "done", "has_reply": False}
    assert secret not in str(body_b)
    assert secret not in str(result)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert secret not in (stored["error"] or "")
    assert secret not in str(stored.get("payload") or {})
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


def test_recover_result_route_ignores_hostile_get_dict_subclass(tmp_path, monkeypatch):
    """A dict subclass overriding ``get`` must not be queried for bridge metadata."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="recover-hostile-get-dict",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    agent_jobs.fail_job(int(job["id"]), "simulate prior failure", status=agent_jobs.STATUS_FAILED, db_path=db)

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.recover.return_value = AgentResult(
        ok=True,
        status="done",
        reply_text="recovered reply",
        raw=_HostileGetDict({
            "bridge_session_id": "sess-recover",
            "bridge_user_msg_id": 42,
        }),
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(
        "POST", "/agent/jobs/%d/recover-result" % int(job["id"]), {}, {}
    )
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "recovered"
    assert result["result"] == {"ok": True, "status": "done", "has_reply": True}

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_DONE
    assert stored["result_text"] == "recovered reply"
    payload = stored.get("payload") or {}
    assert "bridge_session_id" not in payload
    assert "bridge_user_msg_id" not in payload


def test_recover_result_route_ignores_raising_raw_accessor(tmp_path, monkeypatch):
    """A raising ``raw`` accessor must not fail a valid recovery."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="recover-raising-raw",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    agent_jobs.fail_job(int(job["id"]), "simulate prior failure", status=agent_jobs.STATUS_FAILED, db_path=db)

    class _RaisingRawResult(AgentResult):
        @property
        def raw(self):
            raise RuntimeError("C:/private/path SECRET_TOKEN")

        @raw.setter
        def raw(self, _value):
            pass  # discard dataclass initialization

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.recover.return_value = _RaisingRawResult(
        ok=True,
        status="done",
        reply_text="recovered reply",
        raw=None,
    )
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(
        "POST", "/agent/jobs/%d/recover-result" % int(job["id"]), {}, {}
    )
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "recovered"
    assert result["result"] == {"ok": True, "status": "done", "has_reply": True}
    assert "SECRET_TOKEN" not in str(body_b)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_DONE
    assert stored["result_text"] == "recovered reply"
    payload = stored.get("payload") or {}
    assert "bridge_session_id" not in payload
    assert "bridge_user_msg_id" not in payload


def test_recover_result_route_rejects_non_agent_result(tmp_path, monkeypatch):
    """Recover returning a non-AgentResult object must not leak or crash."""
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="recover-bad-result",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p"},
        db_path=db,
    )
    agent_jobs.fail_job(int(job["id"]), "simulate prior failure", status=agent_jobs.STATUS_FAILED, db_path=db)

    class _BadResult:
        ok = True
        status = "done"
        reply_text = "leaked"

        def to_dict(self):
            raise RuntimeError("C:/private/path SECRET_TOKEN")

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.recover.return_value = _BadResult()
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(
        "POST", "/agent/jobs/%d/recover-result" % int(job["id"]), {}, {}
    )
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["action"] == "recover_failed"
    assert result["result"] == {"error": "invalid recover result shape"}
    assert "SECRET_TOKEN" not in str(body_b)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert "bridge_session_id" not in (stored.get("payload") or {})
    assert "bridge_user_msg_id" not in (stored.get("payload") or {})


class _HostileGetDict(dict):
    """Dict subclass whose ``get`` accessor raises for the secret value."""

    def __init__(self, secret: str, *args, **kwargs):
        self._secret = secret
        super().__init__(*args, **kwargs)

    def get(self, key, default=None):
        if key in ("bridge_session_id", "bridge_user_msg_id"):
            raise RuntimeError(self._secret)
        return super().get(key, default)


def test_reconciler_route_ignores_hostile_get_dict_bridge_values_on_success(tmp_path, monkeypatch):
    """A cooperative raw subclass with hostile bridge values completes without persisting them."""
    secret = "C:/private/path SECRET_TOKEN"
    db = tmp_path / "agent_jobs.sqlite"
    monkeypatch.setattr(agent_jobs, "DEFAULT_DB_PATH", db)

    job = agent_jobs.enqueue_job(
        job_key="hostile-get-dict",
        group_key="g1",
        task_type="deep_agent",
        payload={"prompt": "p", "reliable_result_contract": True},
        db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    agent_jobs.mark_submitted(
        int(job["id"]),
        external_provider="fake",
        external_session_id="sess-hostile",
        external_user_msg_id=1,
        next_poll_at=1.0,
        db_path=db,
    )

    raw = _HostileGetDict(
        secret,
        {
            "agent_result": _contract(reply_text="hello from hostile raw"),
            "bridge_session_id": "sess-hostile",
            "bridge_user_msg_id": 1,
        },
    )
    poll_return = AgentResult(
        ok=True,
        status="done",
        reply_text="hello",
        raw=raw,
    )

    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.poll.return_value = poll_return
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(control_api, "_cached_provider_health", lambda *a, **k: {"ok": True, "ready": True})

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route("POST", "/agent/reconciler/run-once", {}, {})
    result = json.loads(body_b.decode("utf-8"))

    assert status == 200
    assert result["ok"] is True
    assert any(
        r["job_id"] == job["id"] and r["action"] == "completed"
        for r in result["results"]
    )
    assert "SECRET_TOKEN" not in str(body_b)
    assert "C:/private/path" not in str(body_b)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_DONE
    assert stored["result_text"] == "hello from hostile raw"
    payload = stored.get("payload") or {}
    assert "bridge_session_id" not in payload
    assert "bridge_user_msg_id" not in payload
    assert "SECRET_TOKEN" not in str(payload)
    assert "C:/private/path" not in str(payload)
    assert "SECRET_TOKEN" not in (stored["error"] or "")
    assert "C:/private/path" not in (stored["error"] or "")
