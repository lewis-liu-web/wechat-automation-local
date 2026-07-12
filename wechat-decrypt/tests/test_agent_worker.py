"""Tests for agent_worker.process_once strict-contract migration.

These tests pin the consumer-level injection introduced in Stage 2:
`process_once` must force `payload.reliable_result_contract = True` before
calling the provider, and must treat strict `silent`/`escalate` actions as
successful terminal states instead of provider failures.
"""

import sys
import time
from pathlib import Path

import pytest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_jobs
import agent_worker
from agent_provider import AgentResult


def _make_db(tmp_path: Path) -> Path:
    return tmp_path / "agent_jobs.sqlite"


def _enqueue(db_path: Path, *, payload: dict | None = None, task_type: str = "deep_agent") -> dict:
    return agent_jobs.enqueue_job(
        job_key=f"test-{time.time_ns()}",
        group_key="test-group",
        task_type=task_type,
        payload=payload or {},
        db_path=db_path,
    )


class _FakeProvider:
    name = "fake"

    def __init__(self, result: AgentResult):
        self.result = result
        self.captured_job = None

    def run(self, job: dict, timeout: float | None = None) -> AgentResult:
        self.captured_job = job
        return self.result

    def health(self):
        return {"ok": True, "ready": True}


def test_release_dispatching_returns_job_to_queued_and_clears_lock_meta(tmp_path):
    """Claim a job into dispatching, release it, and verify lock metadata is cleared."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "x"})
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    assert claimed["status"] == agent_jobs.STATUS_DISPATCHING
    assert claimed["dispatch_owner"] == "w1"
    assert claimed["dispatch_locked_until"] is not None

    released = agent_jobs.release_dispatching(int(job["id"]), reason="test release", db_path=db)
    assert released is True

    released_job = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert released_job["status"] == agent_jobs.STATUS_QUEUED
    assert released_job["dispatch_owner"] is None
    assert released_job["dispatch_locked_until"] is None
    assert released_job["finished_at"] is None


def test_process_once_injects_strict_flag_into_empty_payload(tmp_path):
    """Empty payload must be written back into job before setting the strict flag."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="hi", raw={"agent_result": {"action": "reply", "reply_text": "hi"}}))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "completed"
    assert provider.captured_job is not None
    assert provider.captured_job["payload"]["reliable_result_contract"] is True


def test_process_once_injects_strict_flag_onto_existing_payload(tmp_path):
    """Existing payload must have the strict flag added while preserving other keys."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "question", "agent_timeout": 60})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="hi", raw={"agent_result": {"action": "reply", "reply_text": "hi"}}))

    agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert provider.captured_job["payload"]["prompt"] == "question"
    assert provider.captured_job["payload"]["agent_timeout"] == 60
    assert provider.captured_job["payload"]["reliable_result_contract"] is True


def test_process_once_treats_strict_silent_as_successful_terminal(tmp_path):
    """Strict silent action must complete the job, not fail it."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": {
            "action": "silent",
            "reply_text": "",
            "reason_code": "smalltalk",
            "risk_level": "low",
        }
    }
    provider = _FakeProvider(AgentResult(ok=True, status="silent", reply_text="", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "silent"
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_SENT
    assert completed["send_status"] == agent_jobs.SEND_SKIPPED
    assert completed["result_text"] == ""


def test_process_once_treats_strict_escalate_as_successful_terminal(tmp_path):
    """Strict escalate action must complete the job, not fail it."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": {
            "action": "escalate",
            "reply_text": "",
            "reason_code": "human",
            "risk_level": "high",
        }
    }
    provider = _FakeProvider(AgentResult(ok=True, status="escalated", reply_text="", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "escalated"
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_SENT
    assert completed["send_status"] == agent_jobs.SEND_SKIPPED
    assert completed["result_text"] == ""


def test_process_once_does_not_mark_skipped_when_complete_job_fails(tmp_path, monkeypatch):
    """If complete_job fails, a strict silent action must not mark send skipped."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": {
            "action": "silent",
            "reply_text": "",
            "reason_code": "smalltalk",
            "risk_level": "low",
        }
    }
    provider = _FakeProvider(AgentResult(ok=True, status="silent", reply_text="", raw=raw))

    monkeypatch.setattr(agent_jobs, "complete_job", lambda *a, **k: False)
    monkeypatch.setattr(agent_jobs, "mark_send_skipped", MagicMock(return_value=True))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    agent_jobs.mark_send_skipped.assert_not_called()
    assert out["ok"] is False
    assert out["action"] == "silent"


def test_process_once_legacy_reply_text_still_required_for_success(tmp_path):
    """Legacy (non-strict) result still requires non-empty reply_text to complete."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="", raw={}))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    # Note: the job still gets strict flag injected, but the provider returns a
    # non-strict empty result; consumer keeps treating it as a failure.
    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True  # fail_job returns True
    assert provider.captured_job["payload"]["reliable_result_contract"] is True
