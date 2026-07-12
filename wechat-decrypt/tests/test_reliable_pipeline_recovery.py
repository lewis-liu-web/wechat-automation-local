"""Crash/restart cut-point tests for the Stage 1 durable pipeline."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reliable_pipeline as pipeline


TARGET = "wxid_target"
SENDER = "wxid_sender"


def _persist(db, local_id=1):
    event_id = "wx:message_0.db:Msg_target:%d" % local_id
    return pipeline.persist_inbound_event(
        event_id=event_id,
        target_id=TARGET,
        group_key=TARGET,
        sender_id=SENDER,
        local_id=local_id,
        payload={"local_id": local_id, "message_content": "message-%d" % local_id},
        received_at=float(local_id),
        db_path=db,
    )[0]


def _close_and_create(db, at=10):
    turns = pipeline.close_due_windows(now=at, db_path=db)
    assert len(turns) == 1
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    return jobs[0]


def _ready_job(db):
    _persist(db, 1)
    pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=1, max_window_seconds=30, now=1, db_path=db)
    return _close_and_create(db, at=2)


def _reply(job_id, db, now=10):
    return pipeline.apply_agent_result(
        job_id=job_id,
        result={
            "schema_version": 1,
            "action": "reply",
            "reply_text": "safe reply",
            "reason_code": "answered",
            "risk_level": "low",
        },
        final_filter=lambda text: text,
        db_path=db,
        now=now,
    )


def test_restart_after_inbound_before_job_creation_recovers_exactly_one_job():
    """Cut point 1: persisted inbox survives before any job is materialized."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _persist(db)
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=1, max_window_seconds=30, now=1, db_path=db)
        # Simulated restart: reopen through public durable APIs, then replay DB read.
        replay, inserted = pipeline.persist_inbound_event(
            event_id="wx:message_0.db:Msg_target:1", target_id=TARGET, group_key=TARGET,
            sender_id=SENDER, local_id=1, payload={"local_id": 1, "message_content": "message-1"},
            received_at=1, db_path=db,
        )
        assert inserted is False
        assert replay["status"] == pipeline.INBOUND_PENDING
        job = _close_and_create(db, at=3)
        assert pipeline.create_jobs_for_ready_turns(db_path=db) == []
        assert job["job_key"] == "turn:%s:1:1" % TARGET


def test_restart_after_job_creation_before_worker_claim_preserves_one_claim():
    """Cut point 2: a queued job is neither lost nor duplicated on restart."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        job = _ready_job(db)
        # Simulated restart: no worker state exists outside DB.
        first = pipeline.claim_next_job(owner="worker-a", now=10, db_path=db)
        assert first and first["id"] == job["id"]
        assert pipeline.claim_next_job(owner="worker-b", now=10, db_path=db) is None


def test_restart_after_hermes_result_before_send_claim_recovers_pending_outbox_once():
    """Cut point 3: completed reply survives before sender execution."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        claimed = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        effect = _reply(claimed["id"], db, now=11)
        outbox_id = effect["outbox"]["id"]
        # Simulated restart: sender sees exactly the durable pending row.
        after_restart = pipeline.claim_sendable(owner="sender", now=12, db_path=db)
        assert [row["id"] for row in after_restart] == [outbox_id]
        sent = pipeline.record_send_result(outbox_id=outbox_id, confirmed=True, detail={"confirmed": True}, now=13, db_path=db)
        assert sent["status"] == pipeline.OUTBOX_SENT
        assert pipeline.claim_sendable(owner="other-sender", now=14, db_path=db) == []


def test_restart_after_cua_attempt_without_db_confirmation_retries_then_confirms_once():
    """Cut point 4: unconfirmed click retries; it is never counted as sent early."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        claimed = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        effect = _reply(claimed["id"], db, now=11)
        outbox_id = effect["outbox"]["id"]
        first = pipeline.claim_sendable(owner="sender", now=11, db_path=db)
        assert [row["id"] for row in first] == [outbox_id]
        retry = pipeline.record_send_result(
            outbox_id=outbox_id, confirmed=False, detail={"cua_clicked": True, "db_confirmed": False},
            error="db confirmation timeout", now=12, db_path=db,
        )
        assert retry["status"] == pipeline.OUTBOX_RETRY
        assert pipeline.claim_sendable(owner="sender", now=16, db_path=db) == []
        second = pipeline.claim_sendable(owner="sender", now=17, db_path=db)
        assert [row["id"] for row in second] == [outbox_id]
        sent = pipeline.record_send_result(outbox_id=outbox_id, confirmed=True, detail={"db_confirmed": True}, now=18, db_path=db)
        assert sent["status"] == pipeline.OUTBOX_SENT
        assert pipeline.claim_sendable(owner="sender", now=19, db_path=db) == []
