import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import json
import pytest
import reliable_pipeline as pipeline


def _event(local_id, text="hello", dedicated_id=None, shadow=None):
    payload = {
        "local_id": local_id,
        "message_content": text,
        "sender_username": "wxid_sender",
        "sender_display_name": "Sender",
        "local_type": 1,
    }
    if dedicated_id is not None or shadow is not None:
        payload["target"] = {}
    if dedicated_id is not None:
        payload["target"]["dedicated_agent_instance_id"] = dedicated_id
    if shadow is not None:
        payload["target"]["reliable_pipeline_shadow"] = shadow
    return payload


def _event_id(target_id, local_id):
    return "wx:message_0.db:Msg_%s:%d" % (target_id, local_id)


def _persist(db, local_id=1, text="hello", dedicated_id=None, target_id="wxid_target", shadow=None):
    # Backwards-compatible default event id used by existing tests.
    event_id = "wx:message_0.db:Msg_target:%d" % local_id if target_id == "wxid_target" else _event_id(target_id, local_id)
    return pipeline.persist_inbound_event(
        event_id=event_id,
        target_id=target_id,
        group_key=target_id,
        sender_id="wxid_sender",
        local_id=local_id,
        payload=_event(local_id, text, dedicated_id=dedicated_id, shadow=shadow),
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
        retry = pipeline.record_send_result(outbox_id=outbox_id, owner="sender", lease_id=first[0]["lease_id"], confirmed=False, error="not confirmed", now=12, db_path=db)
        assert retry["status"] == pipeline.OUTBOX_RETRY
        second = pipeline.claim_sendable(owner="sender", now=17, db_path=db)
        assert second[0]["id"] == outbox_id
        dead = pipeline.record_send_result(outbox_id=outbox_id, owner="sender", lease_id=second[0]["lease_id"], confirmed=False, error="still not confirmed", now=18, db_path=db)
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


# --- Provenance persistence on durable job rows (Stage 3) -------------------

def test_apply_agent_result_persists_provenance_summary():
    """The pipeline layer must store a structured provenance summary in the
    ``turn_jobs.provenance_json`` column without mutating the strict 5-field
    ``result_json``.
    """
    import json
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        provenance = {"trace_id": "abc", "status": "ok", "provenance": [{"kb_id": "kb1"}], "no_source_reason": ""}
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result={"schema_version": 1, "action": "reply", "reply_text": "safe", "reason_code": "ok", "risk_level": "low"},
            final_filter=lambda text: text,
            provenance=provenance,
            db_path=db,
            now=11,
        )
        row = applied["job"]
        assert row["status"] == pipeline.JOB_DONE
        result = row["result"]
        assert set(result.keys()) == {"schema_version", "action", "reply_text", "reason_code", "risk_level"}
        assert row["provenance"]
        assert row["provenance"]["status"] == "ok"


def test_fail_job_persists_provenance_summary():
    """Failed jobs also record the provenance summary alongside the error."""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        provenance = {"trace_id": "abc", "status": "no_tool_call", "provenance": [], "no_source_reason": ""}
        pipeline.fail_job(
            job_id=job["id"],
            error="provider result failed",
            provenance=provenance,
            db_path=db,
            now=11,
        )
        with pipeline._connect(db) as con:
            row = con.execute("SELECT status, provenance_json FROM turn_jobs WHERE id=?", (job["id"],)).fetchone()
        assert row["status"] == pipeline.JOB_FAILED
        assert json.loads(row["provenance_json"])["status"] == "no_tool_call"


def test_fail_job_persists_provider_diagnostics():
    """Failed jobs record safe provider diagnostics (type/ok/status) without
    leaking error messages or reply text."""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        diagnostics = {"result_type": "AgentResult", "ok": False, "status": "failed"}
        pipeline.fail_job(
            job_id=job["id"],
            error="provider result failed",
            provenance={"trace_id": "abc", "status": "no_tool_call", "provenance": [], "no_source_reason": ""},
            provider_diagnostics=diagnostics,
            db_path=db,
            now=11,
        )
        with pipeline._connect(db) as con:
            row = con.execute("SELECT status, provider_diagnostics_json FROM turn_jobs WHERE id=?", (job["id"],)).fetchone()
        assert row["status"] == pipeline.JOB_FAILED
        persisted = json.loads(row["provider_diagnostics_json"])
        assert persisted == diagnostics


def test_provider_diagnostics_column_migration():
    """The provider_diagnostics_json column is added by migration on open_db."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        # Create a DB with only the base schema (no migrations).
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE IF NOT EXISTS turn_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT NOT NULL UNIQUE,
                turn_id INTEGER NOT NULL UNIQUE,
                target_id TEXT NOT NULL,
                group_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                lease_owner TEXT,
                lease_until REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                deadline_at REAL,
                result_json TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
        """)
        con.commit()
        con.close()
        # open_db should add the column via migration.
        con2 = pipeline.open_db(db)
        rows = con2.execute("PRAGMA table_info(turn_jobs)").fetchall()
        names = [str(r["name"]) for r in rows]
        assert "provider_diagnostics_json" in names
        con2.close()


def test_dedicated_instance_id_propagated_to_job_payload():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _persist(db, 1, "first", dedicated_id="hermes-wechat-bot-worker3")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
        turns = pipeline.close_due_windows(now=2, db_path=db)
        assert len(turns) == 1
        jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
        assert len(jobs) == 1
        assert jobs[0]["payload"]["dedicated_agent_instance_id"] == "hermes-wechat-bot-worker3"
        assert jobs[0]["payload"]["target"]["dedicated_agent_instance_id"] == "hermes-wechat-bot-worker3"
        assert jobs[0]["payload"]["events"][0]["message"]["target"]["dedicated_agent_instance_id"] == "hermes-wechat-bot-worker3"


def test_conflicting_dedicated_instance_ids_raise_and_leave_no_turn():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _persist(db, 1, "first", dedicated_id="worker-a")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=2, max_window_seconds=10, now=1, db_path=db)
        _persist(db, 2, "second", dedicated_id="worker-b")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:2", debounce_seconds=2, max_window_seconds=10, now=2, db_path=db)
        with pytest.raises(ValueError, match="conflicting dedicated_agent_instance_id"):
            pipeline.close_due_windows(now=5, db_path=db)
        counts = pipeline.counts(db_path=db)
        assert counts["turns"] == {}
        assert counts["turn_jobs"] == {}
        # Window remains open so a future consistent close can retry.
        assert counts["inbound_events"] == {"pending": 2}
        with pipeline._connect(db) as con:
            windows = con.execute("SELECT COUNT(*) AS n FROM turn_windows").fetchone()["n"]
        assert windows == 1


def test_general_claim_skips_bound_job():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _persist(db, 1, "bound", dedicated_id="hermes-wechat-bot-worker3")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
        pipeline.close_due_windows(now=2, db_path=db)
        pipeline.create_jobs_for_ready_turns(db_path=db)
        # General runner (no instance_id) must not claim the bound job.
        claimed = pipeline.claim_next_job(owner="general", now=10, db_path=db)
        assert claimed is None
        counts = pipeline.counts(db_path=db)
        assert counts["turn_jobs"] == {"queued": 1}


def test_instance_claims_only_own_bound_job():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        # Three distinct targets so each event forms its own window/job.
        _persist(db, 1, "bound-a", dedicated_id="worker-a", target_id="target_a")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target_a:1", debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
        _persist(db, 2, "unbound", target_id="target_u")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target_u:2", debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
        _persist(db, 3, "bound-b", dedicated_id="worker-b", target_id="target_b")
        pipeline.add_event_to_window("wx:message_0.db:Msg_target_b:3", debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
        pipeline.close_due_windows(now=2, db_path=db)
        pipeline.create_jobs_for_ready_turns(db_path=db)
        # General runner should claim only the unbound job.
        general_claimed = pipeline.claim_next_job(owner="general", now=10, db_path=db)
        assert general_claimed is not None
        assert general_claimed["payload"]["dedicated_agent_instance_id"] == ""
        assert general_claimed["payload"]["events"][0]["message"]["message_content"] == "unbound"
        # worker-a should claim only its own bound job.
        a_claimed = pipeline.claim_next_job(owner="worker-a", instance_id="worker-a", now=10, db_path=db)
        assert a_claimed is not None
        assert a_claimed["payload"]["dedicated_agent_instance_id"] == "worker-a"
        assert a_claimed["payload"]["events"][0]["message"]["message_content"] == "bound-a"
        # worker-b should claim only its own bound job.
        b_claimed = pipeline.claim_next_job(owner="worker-b", instance_id="worker-b", now=10, db_path=db)
        assert b_claimed is not None
        assert b_claimed["payload"]["dedicated_agent_instance_id"] == "worker-b"
        assert b_claimed["payload"]["events"][0]["message"]["message_content"] == "bound-b"
        # No jobs left.
        assert pipeline.claim_next_job(owner="general", now=10, db_path=db) is None
        assert pipeline.claim_next_job(owner="worker-a", instance_id="worker-a", now=10, db_path=db) is None
        assert pipeline.claim_next_job(owner="worker-b", instance_id="worker-b", now=10, db_path=db) is None


# --- Shadow mode: record decisions, never create sendable outbox (Stage 4) ---

def _ready_shadow_job(db):
    _persist(db, 1, "first", shadow=True)
    pipeline.add_event_to_window("wx:message_0.db:Msg_target:1", debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
    turns = pipeline.close_due_windows(now=2, db_path=db)
    assert len(turns) == 1
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    return jobs[0]


def _shadow_record(db, job_id):
    with pipeline._connect(db) as con:
        row = con.execute("SELECT shadow_json FROM turn_jobs WHERE id=?", (job_id,)).fetchone()
    return json.loads(row["shadow_json"]) if row["shadow_json"] else None


def _outbox_rows(db, job_id):
    with pipeline._connect(db) as con:
        return con.execute("SELECT COUNT(*) AS n FROM send_outbox WHERE job_id=?", (job_id,)).fetchone()["n"]


def test_shadow_flag_propagated_to_turn_and_job_payload():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        job = _ready_shadow_job(db)
        assert job["payload"]["shadow"] is True


def test_shadow_flag_snapshotted_from_config_at_ingress():
    """Monitor ingress whitelists ``payload['target']`` fields and drops the
    config flag, so the pipeline snapshots ``reliable_pipeline_shadow`` from
    the targets config referenced by ``payload['_config_path']`` at persist
    time.  This is the production recovery path for the flag.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        config = Path(tmp) / "wechat_bot_targets.json"
        config.write_text(json.dumps({"targets": [
            {"username": "wxid_target", "reliable_pipeline_shadow": True},
            {"username": "wxid_plain", "reliable_pipeline_shadow": False},
        ]}), encoding="utf-8")
        for local_id, target_id in ((1, "wxid_target"), (2, "wxid_plain")):
            payload = _event(local_id, "msg-%d" % local_id)
            payload["target"] = {"username": target_id}
            payload["_config_path"] = str(config)
            pipeline.persist_inbound_event(
                event_id=_event_id(target_id, local_id),
                target_id=target_id,
                group_key=target_id,
                sender_id="wxid_sender",
                local_id=local_id,
                payload=payload,
                received_at=float(local_id),
                db_path=db,
            )
            pipeline.add_event_to_window(_event_id(target_id, local_id), debounce_seconds=1,
                                         max_window_seconds=10, now=float(local_id), db_path=db)
        pipeline.close_due_windows(now=5, db_path=db)
        jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
        assert len(jobs) == 2
        by_target = {job["target_id"]: job for job in jobs}
        assert by_target["wxid_target"]["payload"]["shadow"] is True
        assert by_target["wxid_plain"]["payload"]["shadow"] is False
        # Explicit payload values win over the config lookup.
        db2 = Path(tmp) / "pipeline2.sqlite"
        payload = _event(1, "explicit")
        payload["target"] = {"username": "wxid_plain", "reliable_pipeline_shadow": True}
        payload["_config_path"] = str(config)
        pipeline.persist_inbound_event(
            event_id=_event_id("wxid_plain", 1),
            target_id="wxid_plain",
            group_key="wxid_plain",
            sender_id="wxid_sender",
            local_id=1,
            payload=payload,
            received_at=1.0,
            db_path=db2,
        )
        pipeline.add_event_to_window(_event_id("wxid_plain", 1), debounce_seconds=1,
                                     max_window_seconds=10, now=1, db_path=db2)
        pipeline.close_due_windows(now=2, db_path=db2)
        jobs2 = pipeline.create_jobs_for_ready_turns(db_path=db2)
        assert jobs2[0]["payload"]["shadow"] is True


def test_apply_agent_result_shadow_reply_records_decision_without_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_shadow_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result={"schema_version": 1, "action": "reply", "reply_text": "safe", "reason_code": "ok", "risk_level": "low"},
            final_filter=lambda text: text,
            mention_name="Sender",
            db_path=db,
            now=11,
        )
        assert applied["applied"] is True
        assert applied["shadow"] is True
        assert applied["job"]["status"] == pipeline.JOB_DONE
        assert applied["outbox"] is None
        assert _outbox_rows(db, job["id"]) == 0
        record = _shadow_record(db, job["id"])
        assert record["shadow"] is True
        assert record["would_send"] is True
        assert record["reply_text"] == "safe"
        assert record["reply_chars"] == len("safe")
        assert record["mention_name"] == "Sender"
        assert "recorded_at" in record


def test_apply_agent_result_shadow_silent_marks_would_not_send():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_shadow_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result={"schema_version": 1, "action": "silent", "reply_text": "", "reason_code": "smalltalk", "risk_level": "low"},
            final_filter=lambda text: text,
            db_path=db,
            now=11,
        )
        assert applied["job"]["status"] == pipeline.JOB_DONE
        assert applied["outbox"] is None
        assert _outbox_rows(db, job["id"]) == 0
        record = _shadow_record(db, job["id"])
        assert record["shadow"] is True
        assert record["would_send"] is False
        assert record["action"] == "silent"
        assert record["reason_code"] == "smalltalk"


def test_apply_agent_result_shadow_still_applies_final_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_shadow_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result={"schema_version": 1, "action": "reply", "reply_text": "unsafe", "reason_code": "", "risk_level": "low"},
            final_filter=lambda text: "",
            db_path=db,
            now=11,
        )
        assert applied["job"]["status"] == pipeline.JOB_FAILED
        assert applied["outbox"] is None
        assert _outbox_rows(db, job["id"]) == 0
        record = _shadow_record(db, job["id"])
        assert record["shadow"] is True
        assert record["would_send"] is False
        assert record["error"] == "reply rejected by final safety filter"


def test_non_shadow_job_still_creates_outbox():
    """Regression guard: a non-shadow reply still creates a sendable outbox
    row and records no shadow decision."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _ready_job(db)
        job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result={"schema_version": 1, "action": "reply", "reply_text": "safe", "reason_code": "ok", "risk_level": "low"},
            final_filter=lambda text: text,
            db_path=db,
            now=11,
        )
        assert applied["shadow"] is False
        assert applied["job"]["status"] == pipeline.JOB_DONE
        assert applied["outbox"]["status"] == pipeline.OUTBOX_PENDING
        assert _outbox_rows(db, job["id"]) == 1
        assert _shadow_record(db, job["id"]) is None


# --- Dead-letter recovery: send-start tracking, immutable audit, requeue -----

def _make_outbox(db, reply_text="safe reply", key="t1"):
    # Distinct target/group/sender per key so repeated calls in one DB form
    # separate windows/turns/jobs (same-sender events would aggregate).
    target_id = "target_%s" % key
    event_id = "wx:message_0.db:Msg_%s:1" % key
    pipeline.persist_inbound_event(
        event_id=event_id, target_id=target_id, group_key=target_id,
        sender_id="sender_%s" % key, local_id=1,
        payload={"local_id": 1, "message_content": reply_text,
                 "sender_username": "sender_%s" % key, "local_type": 1},
        received_at=1.0, db_path=db,
    )
    pipeline.add_event_to_window(event_id, debounce_seconds=1, max_window_seconds=10, now=1, db_path=db)
    pipeline.close_due_windows(now=2, db_path=db)
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1, "expected exactly one new job for key=%s, got %d" % (key, len(jobs))
    job = pipeline.claim_next_job(owner="worker", now=10, db_path=db)
    assert job["id"] == jobs[0]["id"]
    applied = pipeline.apply_agent_result(
        job_id=job["id"],
        result={"schema_version": 1, "action": "reply", "reply_text": reply_text, "reason_code": "ok", "risk_level": "low"},
        final_filter=lambda text: text,
        db_path=db,
        now=11,
    )
    return applied["outbox"]["id"]


def _make_dead_outbox(db, *, send_started_at, error="test_mode_target_rejected: x",
                      result=None, attempts=5, reply_text="safe reply", dead_at=50.0, key="t1"):
    outbox_id = _make_outbox(db, reply_text=reply_text, key=key)
    with pipeline._connect(db) as con:
        con.execute(
            "UPDATE send_outbox SET status=?, attempts=?, error=?, result_json=?, "
            "send_started_at=?, dead_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
            (pipeline.OUTBOX_DEAD, attempts, error,
             json.dumps(result) if result is not None else None,
             send_started_at, dead_at, outbox_id),
        )
    return outbox_id


def test_send_started_migration_backfills_legacy_rows():
    """Pre-tracking rows get a non-NULL sentinel so the safe requeue refuses them."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE IF NOT EXISTS send_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outbox_key TEXT NOT NULL UNIQUE,
                job_id INTEGER NOT NULL UNIQUE,
                target_id TEXT NOT NULL,
                group_key TEXT NOT NULL,
                before_local_id INTEGER NOT NULL,
                mention_name TEXT NOT NULL DEFAULT '',
                reply_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                lease_owner TEXT,
                lease_until REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                next_attempt_at REAL NOT NULL,
                error TEXT,
                result_json TEXT,
                created_at REAL NOT NULL,
                sent_at REAL,
                dead_at REAL
            );
            INSERT INTO send_outbox
                (outbox_key, job_id, target_id, group_key, before_local_id, reply_text,
                 status, attempts, next_attempt_at, error, created_at, dead_at)
            VALUES
                ('k1', 1, 't', 'g', 0, 'legacy msg', 'dead_letter', 5, 0,
                 'send confirmation failed', 42.0, 50.0);
        """)
        con.commit()
        con.close()
        con2 = pipeline.open_db(db)
        row = con2.execute(
            "SELECT send_started_at, requeue_count FROM send_outbox WHERE outbox_key='k1'").fetchone()
        assert row["send_started_at"] == 42.0  # backfilled to created_at sentinel
        assert row["requeue_count"] == 0
        cols = {str(r["name"]) for r in con2.execute(
            "PRAGMA table_info(send_outbox_recovery_audit)").fetchall()}
        assert {"legacy_override", "verification_evidence", "prior_send_started_at"} <= cols
        con2.close()
        # Safe requeue must refuse the legacy (non-NULL sentinel) row.
        with pytest.raises(ValueError):
            pipeline.requeue_dead_letter(outbox_id=1, reason="x", actor="tester", db_path=db)


def test_recovery_audit_table_rejects_update_and_delete():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        outbox_id = _make_dead_outbox(db, send_started_at=None)
        pipeline.requeue_dead_letter(outbox_id=outbox_id, reason="gate", actor="tester", db_path=db)
        with pipeline._connect(db) as con:
            n = con.execute("SELECT COUNT(*) AS c FROM send_outbox_recovery_audit").fetchone()["c"]
            assert n == 1
            with pytest.raises(Exception):
                con.execute("UPDATE send_outbox_recovery_audit SET reason='tamper'")
            with pytest.raises(Exception):
                con.execute("DELETE FROM send_outbox_recovery_audit")


def test_record_send_started_requires_valid_lease_and_extends():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        outbox_id = _make_outbox(db)
        claimed = pipeline.claim_sendable(owner="sender", lease_seconds=45, now=100.0, db_path=db)
        assert claimed and claimed[0]["id"] == outbox_id
        lease_id = claimed[0]["lease_id"]
        assert lease_id
        # A wrong lease token cannot mark.
        assert pipeline.record_send_started(outbox_id=outbox_id, lease_id="other-token", now=101.0, db_path=db) is False
        # The claim's lease token marks and extends the lease.
        assert pipeline.record_send_started(outbox_id=outbox_id, lease_id=lease_id,
                                            lease_extension_seconds=45, now=101.0, db_path=db) is True
        with pipeline._connect(db) as con:
            row = con.execute("SELECT send_started_at, lease_until FROM send_outbox WHERE id=?",
                              (outbox_id,)).fetchone()
        assert row["send_started_at"] == 101.0
        assert row["lease_until"] == 146.0  # 101 + 45
        # COALESCE: a later mark does not move the original send_started_at but
        # still renews the lease window.
        assert pipeline.record_send_started(outbox_id=outbox_id, lease_id=lease_id,
                                            lease_extension_seconds=45, now=105.0, db_path=db) is True
        with pipeline._connect(db) as con:
            row2 = con.execute("SELECT send_started_at, lease_until FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row2["send_started_at"] == 101.0  # unchanged
        assert row2["lease_until"] == 150.0  # renewed: 105 + 45


def test_record_send_started_rejects_expired_lease():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        outbox_id = _make_outbox(db)
        claimed = pipeline.claim_sendable(owner="sender", lease_seconds=45, now=100.0, db_path=db)
        lease_id = claimed[0]["lease_id"]
        # lease_until = 145; marking at 200 (expired) must fail.
        assert pipeline.record_send_started(outbox_id=outbox_id, lease_id=lease_id,
                                            lease_extension_seconds=45, now=200.0, db_path=db) is False


def test_record_send_result_rejects_stale_lease_token_finalize():
    """A stale claim's lease token cannot finalize a row reclaimed under the
    SAME owner string (production shares ``DEFAULT_SEND_OWNER``)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        outbox_id = _make_outbox(db)
        # Claim A authorizes a send under the shared owner string.
        claim_a = pipeline.claim_sendable(owner="reliable-sender", lease_seconds=45, now=100.0, db_path=db)
        lease_a = claim_a[0]["lease_id"]
        assert pipeline.record_send_started(outbox_id=outbox_id, lease_id=lease_a,
                                            lease_extension_seconds=45, now=100.0, db_path=db) is True
        # A's lease (until 145) expires; the row is reclaimed under the SAME owner.
        claim_b = pipeline.claim_sendable(owner="reliable-sender", lease_seconds=45, now=200.0, db_path=db)
        assert claim_b and claim_b[0]["id"] == outbox_id
        lease_b = claim_b[0]["lease_id"]
        assert lease_b != lease_a  # fresh token per claim
        # Stale claim A tries to finalize with its old token -> rejected.
        with pytest.raises(ValueError):
            pipeline.record_send_result(outbox_id=outbox_id, owner="reliable-sender", lease_id=lease_a,
                                        confirmed=True, now=210.0, db_path=db)
        with pipeline._connect(db) as con:
            row = con.execute("SELECT status, lease_id FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_SENDING
        assert row["lease_id"] == lease_b
        # The current claim B can finalize its own send.
        final = pipeline.record_send_result(outbox_id=outbox_id, owner="reliable-sender", lease_id=lease_b,
                                            confirmed=True, detail={"confirmed": True}, now=220.0, db_path=db)
        assert final["status"] == pipeline.OUTBOX_SENT


def test_requeue_dead_letter_rejects_non_dead_letter():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        outbox_id = _make_outbox(db)  # pending, not dead_letter
        with pytest.raises(ValueError):
            pipeline.requeue_dead_letter(outbox_id=outbox_id, reason="x", actor="tester", db_path=db)


def test_requeue_dead_letter_recovers_never_sent_and_audits():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        outbox_id = _make_dead_outbox(db, send_started_at=None,
                                      error="test_mode_target_rejected: x",
                                      result={"skipped": True, "reason": "x"})
        updated = pipeline.requeue_dead_letter(outbox_id=outbox_id,
                                               reason="gate rejection, never sent",
                                               actor="tester", db_path=db, now=200.0)
        assert updated["status"] == pipeline.OUTBOX_RETRY
        assert updated["attempts"] == 0
        assert updated["requeue_count"] == 1
        assert updated["error"] is None
        assert updated["next_attempt_at"] == 200.0
        assert updated["send_started_at"] is None
        with pipeline._connect(db) as con:
            audit = con.execute("SELECT * FROM send_outbox_recovery_audit WHERE outbox_id=?",
                                (outbox_id,)).fetchone()
        assert audit["prior_status"] == pipeline.OUTBOX_DEAD
        assert audit["prior_attempts"] == 5
        assert audit["legacy_override"] == 0
        assert audit["actor"] == "tester"
        assert audit["reason"] == "gate rejection, never sent"
        assert audit["verification_evidence"] is None


def test_recover_legacy_gate_rejection_recovers_with_correct_pins():
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        error = "test_mode_target_rejected: target name 'family' is not in ['bot\u7fa4\u804a\u6d4b\u8bd5']"
        outbox_id = _make_dead_outbox(db, send_started_at=11.0, error=error,
                                      result={"skipped": True, "reason": error[:200]},
                                      reply_text="E2E family canary test")
        sha = hashlib.sha256("E2E family canary test".encode("utf-8")).hexdigest()
        updated = pipeline.recover_legacy_gate_rejection(
            outbox_id=outbox_id, expected_error=error, expected_reply_text_sha256=sha,
            verification_evidence="dead-letter error is test_mode_target_rejected and result.skipped=true; sender never invoked",
            actor="operator", db_path=db, now=300.0)
        assert updated["status"] == pipeline.OUTBOX_RETRY
        assert updated["attempts"] == 0
        assert updated["requeue_count"] == 1
        with pipeline._connect(db) as con:
            audit = con.execute("SELECT * FROM send_outbox_recovery_audit WHERE outbox_id=?",
                                (outbox_id,)).fetchone()
        assert audit["legacy_override"] == 1
        assert "test_mode_target_rejected" in (audit["verification_evidence"] or "")
        assert audit["prior_send_started_at"] == 11.0


def test_recover_legacy_gate_rejection_rejects_wrong_pins():
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        error = "test_mode_target_rejected: x"
        outbox_id = _make_dead_outbox(db, send_started_at=11.0, error=error,
                                      result={"skipped": True}, reply_text="msg")
        sha = hashlib.sha256("msg".encode("utf-8")).hexdigest()
        base = dict(outbox_id=outbox_id, expected_error=error, expected_reply_text_sha256=sha,
                    verification_evidence="v", actor="op", db_path=db)
        with pytest.raises(ValueError):  # wrong error pin
            pipeline.recover_legacy_gate_rejection(**{**base, "expected_error": "different"})
        with pytest.raises(ValueError):  # wrong reply hash
            pipeline.recover_legacy_gate_rejection(**{**base, "expected_reply_text_sha256": "0" * 64})
        with pytest.raises(ValueError):  # missing evidence
            pipeline.recover_legacy_gate_rejection(**{**base, "verification_evidence": ""})


def test_recover_legacy_gate_rejection_rejects_non_gate_and_null_sentinel():
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        # Non-gate dead letter (result not skipped, error not a gate prefix).
        outbox_id = _make_dead_outbox(db, send_started_at=11.0, error="send confirmation failed",
                                      result={"confirmed": False}, reply_text="msg")
        sha = hashlib.sha256("msg".encode("utf-8")).hexdigest()
        with pytest.raises(ValueError):
            pipeline.recover_legacy_gate_rejection(
                outbox_id=outbox_id, expected_error="send confirmation failed",
                expected_reply_text_sha256=sha, verification_evidence="v", actor="op", db_path=db)
        # NULL sentinel -> must use the safe path, not the legacy override.
        outbox_id2 = _make_dead_outbox(db, send_started_at=None, error="test_mode_target_rejected: x",
                                       result={"skipped": True}, reply_text="m2", key="t2")
        sha2 = hashlib.sha256("m2".encode("utf-8")).hexdigest()
        with pytest.raises(ValueError):
            pipeline.recover_legacy_gate_rejection(
                outbox_id=outbox_id2, expected_error="test_mode_target_rejected: x",
                expected_reply_text_sha256=sha2, verification_evidence="v", actor="op", db_path=db)
