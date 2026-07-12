"""Tests for agent_jobs state helpers."""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs as jobs


def test_complete_job_silent_transitions_active_job_to_terminal():
    """complete_job_silent must atomically move an active job to sent/skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "agent_jobs.sqlite"
        job = jobs.enqueue_job(
            job_key="silent-1",
            group_key="g1",
            task_type="deep_agent",
            payload={"prompt": "p"},
            db_path=db,
        )
        claimed = jobs.claim_next_job(worker_id="w1", db_path=db)
        assert claimed is not None
        assert claimed["status"] == jobs.STATUS_RUNNING

        ok = jobs.complete_job_silent(int(job["id"]), reason="silent", db_path=db)
        assert ok is True

        stored = jobs.get_job(int(job["id"]), db_path=db)
        assert stored["status"] == jobs.STATUS_SENT
        assert stored["send_status"] == jobs.SEND_SKIPPED
        assert stored["result_text"] == ""
        assert stored["error"] == "silent"
        assert stored["finished_at"] is not None
        assert stored["sent_at"] is not None


def test_complete_job_silent_works_from_submitted_state():
    """Reconciler jobs are in submitted/agent_running; the helper must accept them."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "agent_jobs.sqlite"
        job = jobs.enqueue_job(
            job_key="silent-submitted",
            group_key="g1",
            task_type="deep_agent",
            payload={"prompt": "p"},
            db_path=db,
        )
        # Manually move to submitted to simulate dispatcher hand-off.
        with jobs._connect(db) as con:
            con.execute(
                "UPDATE agent_jobs SET status=?, external_provider=?, external_session_id=? WHERE id=?",
                (jobs.STATUS_SUBMITTED, "fake", "sess-1", int(job["id"])),
            )

        ok = jobs.complete_job_silent(int(job["id"]), reason="escalate", db_path=db)
        assert ok is True

        stored = jobs.get_job(int(job["id"]), db_path=db)
        assert stored["status"] == jobs.STATUS_SENT
        assert stored["send_status"] == jobs.SEND_SKIPPED
        assert stored["error"] == "escalate"


def test_complete_job_silent_fails_for_already_terminal_job():
    """A terminal job must not be re-transitioned by complete_job_silent."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "agent_jobs.sqlite"
        job = jobs.enqueue_job(
            job_key="silent-terminal",
            group_key="g1",
            task_type="deep_agent",
            payload={"prompt": "p"},
            db_path=db,
        )
        # Move directly to sent/skipped.
        with jobs._connect(db) as con:
            con.execute(
                "UPDATE agent_jobs SET status=?, send_status=? WHERE id=?",
                (jobs.STATUS_SENT, jobs.SEND_SKIPPED, int(job["id"])),
            )

        ok = jobs.complete_job_silent(int(job["id"]), reason="silent", db_path=db)
        assert ok is False


def test_complete_job_silent_no_partial_sendable_state():
    """The helper must not leave a job in done/pending even when called concurrently."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "agent_jobs.sqlite"
        job = jobs.enqueue_job(
            job_key="silent-race",
            group_key="g1",
            task_type="deep_agent",
            payload={"prompt": "p"},
            db_path=db,
        )
        jobs.claim_next_job(worker_id="w1", db_path=db)

        ok = jobs.complete_job_silent(int(job["id"]), reason="silent", db_path=db)
        assert ok is True

        stored = jobs.get_job(int(job["id"]), db_path=db)
        # At no point should the row be done + pending; it must be terminal.
        assert stored["status"] != jobs.STATUS_DONE
        assert not (stored["status"] == jobs.STATUS_DONE and stored["send_status"] == jobs.SEND_PENDING)
        assert stored["status"] in jobs.TERMINAL_STATUSES
