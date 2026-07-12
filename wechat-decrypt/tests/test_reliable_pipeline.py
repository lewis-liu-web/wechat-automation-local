import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reliable_pipeline as pipeline


def _event(local_id, text="hello"):
    return {
        "local_id": local_id,
        "message_content": text,
        "sender_username": "wxid_sender",
        "sender_display_name": "Sender",
        "local_type": 1,
    }


def _persist(db, local_id=1, text="hello"):
    return pipeline.persist_inbound_event(
        event_id="wx:message_0.db:Msg_target:%d" % local_id,
        target_id="wxid_target",
        group_key="wxid_target",
        sender_id="wxid_sender",
        local_id=local_id,
        payload=_event(local_id, text),
        received_at=float(local_id),
        db_path=db,
    )


def _ready_job(db):
    _persist(db, 1, "first")
    pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
    turns = pipeline.close_due_windows(now=2, db_path=db)
    assert len(turns) == 1
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    return jobs[0]


def test_inbound_event_is_idempotent_before_cursor_advance():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        first, inserted = _persist(db)
        second, duplicated = _persist(db)
        assert inserted is True
        assert duplicated is False
        assert first["id"] == second["id"]
        assert pipeline.counts(db_path=db)["inbound_events"] == {"pending": 1}


def test_open_turn_recovers_after_restart_and_materializes_one_job():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _persist(db, 1, "first")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=2, max_window_seconds=10, now=1, db_path=db)
        _persist(db, 2, "second")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:2", debounce_seconds=2, max_window_seconds=10, now=2, db_path=db)
        # The second call opens a separate sender window only when sender changes;
        # use the durable window key and prove the original is visible after reopen.
        assert pipeline.close_due_windows(now=5, db_path=db)
        jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
        again = pipeline.create_jobs_for_ready_turns(db_path=db)
        assert len(jobs) == 1
        assert again == []
        assert jobs[0]["payload"]["events"][-1]["local_id"] == 2


def test_job_claim_is_leased_and_group_serial_after_restart():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        claimed = pipeline.claim_next_job(owner="worker-a", lease_seconds=5, now=10, db_path=db)
        assert claimed and claimed["status"] == pipeline.JOB_RUNNING
        assert pipeline.claim_next_job(owner="worker-b", now=11, db_path=db) is None
        pipeline.reclaim_expired_leases(now=16, db_path=db)
        recovered = pipeline.claim_next_job(owner="worker-b", now=16, db_path=db)
        assert recovered and recovered["id"] == claimed["id"]


def test_reply_silent_and_escalate_have_distinct_durable_effects():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        reply_job = _ready_job(db)
        claimed = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        applied = pipeline.apply_agent_result(
            job_id=claimed["id"],
            result={"schema_version": 1, "action": "reply", "reply_text": "safe", "reason_code": "ok", "risk_level": "low"},
            final_filter=lambda text: text,
            db_path=db,
            now=11,
        )
        assert applied["outbox"]["status"] == pipeline.OUTBOX_PENDING

        _persist(db, 2)
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:2", debounce_seconds=1, max_window_seconds=10, now=20, db_path=db)
        pipeline.close_due_windows(now=22, db_path=db)
        pipeline.create_jobs_for_ready_turns(db_path=db)
        silent = pipeline.claim_next_job(owner="worker", now=22, db_path=db)
        applied_silent = pipeline.apply_agent_result(
            job_id=silent["id"],
            result={"schema_version": 1, "action": "silent", "reply_text": "", "reason_code": "smalltalk", "risk_level": "low"},
            final_filter=lambda text: text,
            db_path=db,
            now=23,
        )
        assert applied_silent["outbox"] is None

        _persist(db, 3)
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:3", debounce_seconds=1, max_window_seconds=10, now=30, db_path=db)
        pipeline.close_due_windows(now=32, db_path=db)
        pipeline.create_jobs_for_ready_turns(db_path=db)
        escalate = pipeline.claim_next_job(owner="worker", now=32, db_path=db)
        applied_escalate = pipeline.apply_agent_result(
            job_id=escalate["id"],
            result={"schema_version": 1, "action": "escalate", "reply_text": "", "reason_code": "human", "risk_level": "high"},
            final_filter=lambda text: text,
            db_path=db,
            now=33,
        )
        assert applied_escalate["job"]["status"] == pipeline.JOB_ESCALATED
        assert pipeline.counts(db_path=db)["send_outbox"] == {"pending": 1}


def test_outbox_retries_then_dead_letters_and_never_reclaims_terminal_row():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result={"schema_version": 1, "action": "reply", "reply_text": "safe", "reason_code": "ok", "risk_level": "low"},
            final_filter=lambda text: text,
            max_send_attempts=2,
            db_path=db,
            now=11,
        )
        outbox_id = applied["outbox"]["id"]
        first = pipeline.claim_sendable(owner="sender", now=11, db_path=db)
        assert [row["id"] for row in first] == [outbox_id]
        retry = pipeline.record_send_result(outbox_id=outbox_id, confirmed=False, error="not confirmed", now=12, db_path=db)
        assert retry["status"] == pipeline.OUTBOX_RETRY
        assert pipeline.claim_sendable(owner="sender", now=17, db_path=db)[0]["id"] == outbox_id
        dead = pipeline.record_send_result(outbox_id=outbox_id, confirmed=False, error="still not confirmed", now=18, db_path=db)
        assert dead["status"] == pipeline.OUTBOX_DEAD
        assert pipeline.claim_sendable(owner="sender", now=999, db_path=db) == []
        assert pipeline.list_dead_letters(db_path=db)[0]["id"] == outbox_id


def test_agent_result_contract_rejects_terminal_text_and_invalid_actions():
    valid = pipeline.parse_agent_result({"schema_version": 1, "action": "reply", "reply_text": "ok", "reason_code": "", "risk_level": "low"})
    assert valid.action == "reply"
    invalid = [
        "╭─ ⚕ Hermes ─╮\nreply\n╰─╯",
        "\x1b[31merror\x1b[0m",
        '{"tool":"leann_search"}',
        {"schema_version": 1, "action": "reply", "reply_text": "", "reason_code": "", "risk_level": "low"},
        {"schema_version": 1, "action": "unknown", "reply_text": "", "reason_code": "", "risk_level": "low"},
    ]
    for value in invalid:
        try:
            pipeline.parse_agent_result(value)
        except pipeline.AgentResultContractError:
            continue
        raise AssertionError("invalid result accepted: %r" % (value,))
