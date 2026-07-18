"""Tests for the target-scoped rollback runbook (Stage 4).

Rollback procedure (per target, existing mechanisms only):
  1. Disable new durable ingress: set ``reliable_pipeline_target=False`` in the
     target config. The monitor reloads config every cycle, so new messages
     fall through to the legacy path from the next cycle.
  2. Drain in-flight decisions: quarantine each non-terminal ``turn_jobs``
     row via the control-plane API (terminal ``failed``, audited).
  3. Drain-to-send: decided ``send_outbox`` rows keep flowing to ``sent``.
     Rollback never cancels an authorized reply — no global test-gate abuse,
     no hand-edited rows. (Emergency per-target outbox cancel is a known
     gap and would require a new audited API.)
  4. Verify scope: other targets' jobs/outbox are unaffected.
  5. Roll forward: re-enable the flag; durable ingress resumes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import control_api
import reliable_pipeline as pipeline
import reliable_worker as worker
import wechat_bot_monitor as monitor


# --- helpers -----------------------------------------------------------------

def _route(method: str, path: str, body: dict | None = None):
    """Dispatch a control_api route directly without starting a socket."""
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _patch_config(monkeypatch, *, enabled: bool = True, db_path: str | None = None) -> dict:
    cfg = {
        "targets": [],
        "default_triggers": [],
        "knowledge_bases": {},
        "reliable_pipeline": {"enabled": enabled, "test_target_only": False},
    }
    if db_path is not None:
        cfg["reliable_pipeline"]["db_path"] = db_path
    monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: cfg)
    return cfg


def _event(local_id: int, *, sender="wxid_sender", text="hello"):
    return {
        "local_id": local_id,
        "message_content": text,
        "real_sender_id": sender,
        "sender": sender,
        "sender_username": sender,
        "sender_display_name": "Sender",
        "mention_name": "Sender",
        "local_type": 1,
        "status": 0,
    }


def _target(username: str, *, opted_in: bool = True):
    return {
        "name": username,
        "username": username,
        "db": "message_0.db",
        "table": "Msg_%s" % username,
        "enabled": True,
        "last_local_id": 0,
        "reliable_pipeline_target": opted_in,
    }


def _seed_job(db: Path, *, target_id: str = "wxid_target", local_id: int = 1) -> dict:
    """Create one queued turn job for ``target_id`` in a temp pipeline DB."""
    event_id = "wx:message_0.db:Msg_%s:%d" % (target_id, local_id)
    pipeline.persist_inbound_event(
        event_id=event_id, target_id=target_id, group_key=target_id,
        sender_id="wxid_sender", local_id=local_id, payload=_event(local_id),
        received_at=float(local_id), db_path=db,
    )
    pipeline.add_event_to_window(
        event_id, debounce_seconds=1, max_window_seconds=10,
        now=float(local_id), db_path=db,
    )
    pipeline.close_due_windows(now=float(local_id) + 1, db_path=db)
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    assert jobs[0]["status"] == pipeline.JOB_QUEUED
    return jobs[0]


def _contract_dict(action: str, text: str = "", reason: str = "ok", risk: str = "low") -> dict:
    return {
        "schema_version": pipeline.AGENT_RESULT_VERSION,
        "action": action,
        "reply_text": text,
        "reason_code": reason,
        "risk_level": risk,
    }


def _pass_filter(text: str) -> str:
    return text


def _fake_send_result(*, ok: bool, confirmed=None) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, reason="confirmed" if confirmed else "", mode="foreground",
                           attempted=["uia_send"], confirmed=confirmed, error="")


@pytest.fixture(autouse=True)
def _stop_scheduler_after_test():
    yield
    control_api._reliable_scheduler_stop()


# --- 1. quarantine drain is target-scoped ------------------------------------

def test_quarantine_drains_only_rollback_target_jobs(monkeypatch, tmp_path):
    db = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, db_path=str(db))
    job_a1 = _seed_job(db, target_id="wxid_a", local_id=1)
    job_a2 = _seed_job(db, target_id="wxid_a", local_id=2)
    job_b = _seed_job(db, target_id="wxid_b", local_id=1)

    for job in (job_a1, job_a2):
        status, resp = _route(
            "POST", "/reliable-pipeline/turn-jobs/%d/quarantine" % job["id"],
            {"reason": "rollback drain"},
        )
        assert status == 200
        assert resp["quarantined"] is True

    with pipeline._connect(db) as con:
        rows = con.execute("SELECT id, status FROM turn_jobs").fetchall()
        turns_a = con.execute("SELECT status FROM turns WHERE target_id='wxid_a'").fetchall()
    by_id = {r["id"]: r["status"] for r in rows}
    assert by_id[job_a1["id"]] == pipeline.JOB_FAILED
    assert by_id[job_a2["id"]] == pipeline.JOB_FAILED
    assert by_id[job_b["id"]] == pipeline.JOB_QUEUED
    assert all(t["status"] == pipeline.TURN_FAILED for t in turns_a)

    # Drained rows never interfere: the next claimed job is B's.
    claimed = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
    assert claimed["id"] == job_b["id"]


# --- 2. decided outbox rows drain-to-send despite ingress opt-out ------------

def test_outbox_drains_to_send_after_ingress_opt_out(tmp_path):
    db = tmp_path / "rp.sqlite"
    _seed_job(db)
    job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
    applied = pipeline.apply_agent_result(
        job_id=job["id"], result=_contract_dict("reply", "ok"),
        final_filter=_pass_filter, db_path=db, now=11.0,
    )
    assert applied["outbox"] is not None

    # Target opts OUT of new ingress but remains present in config['targets'].
    target = _target("wxid_target", opted_in=False)
    cfg = {"targets": [target]}
    with mock.patch.object(worker, "send_reply_detailed",
                           return_value=_fake_send_result(ok=True, confirmed=True)) as sender:
        out = worker.send_once("sender", cfg, db_path=db, now=12.0)
    assert out["status"] == "processed"
    sender.assert_called_once()
    assert out["processed"][0]["row"]["status"] == pipeline.OUTBOX_SENT

    with pipeline._connect(db) as con:
        row = con.execute("SELECT status, result_json FROM send_outbox").fetchone()
    assert row["status"] == pipeline.OUTBOX_SENT
    assert json.loads(row["result_json"]).get("confirmed") is True


# --- 3. other targets keep flowing during a rollback --------------------------

def test_other_target_pipeline_unaffected_during_rollback(monkeypatch, tmp_path):
    db = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, db_path=str(db))
    job_a = _seed_job(db, target_id="wxid_a", local_id=1)
    job_b = _seed_job(db, target_id="wxid_b", local_id=1)

    # Roll back A.
    status, resp = _route(
        "POST", "/reliable-pipeline/turn-jobs/%d/quarantine" % job_a["id"],
        {"reason": "rollback"},
    )
    assert status == 200 and resp["quarantined"] is True

    # B continues end-to-end: claim -> reply decision -> outbox -> confirmed send.
    claimed = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
    assert claimed["id"] == job_b["id"]
    applied = pipeline.apply_agent_result(
        job_id=claimed["id"], result=_contract_dict("reply", "ok"),
        final_filter=_pass_filter, db_path=db, now=11.0,
    )
    assert applied["outbox"] is not None
    target_b = _target("wxid_b")
    with mock.patch.object(worker, "send_reply_detailed",
                           return_value=_fake_send_result(ok=True, confirmed=True)):
        out = worker.send_once("sender", {"targets": [target_b]}, db_path=db, now=12.0)
    assert out["processed"][0]["row"]["status"] == pipeline.OUTBOX_SENT

    with pipeline._connect(db) as con:
        a_row = con.execute("SELECT status FROM turn_jobs WHERE id=?", (job_a["id"],)).fetchone()
        a_outbox = con.execute("SELECT 1 FROM send_outbox WHERE job_id=?", (job_a["id"],)).fetchone()
    assert a_row["status"] == pipeline.JOB_FAILED
    assert a_outbox is None


# --- 4. ingress flag toggle sequence (rollback + roll forward) ----------------

def test_ingress_toggle_sequence_opt_out_and_roll_forward(tmp_path):
    db = tmp_path / "rp.sqlite"
    cfg = {"reliable_pipeline": {"enabled": True}}
    t = _target("wxid_target")

    out1 = monitor.durable_ingress_event(t, _event(1), cfg=cfg, db_path=db, now=1.0)
    assert out1["error"] == ""
    assert out1["persisted"] is True

    # Roll back: durable fence refuses; the monitor falls through to legacy.
    t["reliable_pipeline_target"] = False
    out2 = monitor.durable_ingress_event(t, _event(2), cfg=cfg, db_path=db, now=2.0)
    assert out2["error"] == "target not opted into reliable pipeline"
    assert out2["persisted"] is False

    # Roll forward: re-enable; persistence resumes.
    t["reliable_pipeline_target"] = True
    out3 = monitor.durable_ingress_event(t, _event(3), cfg=cfg, db_path=db, now=3.0)
    assert out3["error"] == ""
    assert out3["persisted"] is True

    with pipeline._connect(db) as con:
        ids = [r["local_id"] for r in con.execute("SELECT local_id FROM inbound_events ORDER BY local_id")]
    assert ids == [1, 3]
