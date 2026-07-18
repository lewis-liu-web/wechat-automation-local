"""Tests for the Stage 1 reliable-pipeline scheduler.

These tests exercise the dedicated scheduler thread, its start/stop/status
controls, and the quarantine action.  They do NOT dispatch real WeChat
messages; worker / sender behavior is either mocked or driven against an
empty pipeline so no outbound traffic is produced.
"""

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import control_api  # noqa: E402
import reliable_pipeline as pipeline  # noqa: E402
import reliable_worker  # noqa: E402


# --- Helpers ----------------------------------------------------------------


def _route(method: str, path: str, body: dict | None = None):
    """Dispatch a control_api route directly without starting a socket."""
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _patch_config(monkeypatch, *, enabled: bool = False, db_path: str | None = None,
                  test_target_only: bool = False, scheduler_interval_seconds: float | None = None) -> dict:
    """Replace ``control_api.reg.load_config`` with a stub returning a fixed
    in-memory config."""
    cfg = {
        "targets": [],
        "default_triggers": [],
        "knowledge_bases": {},
        "reliable_pipeline": {
            "enabled": enabled,
            "test_target_only": test_target_only,
        },
    }
    if db_path is not None:
        cfg["reliable_pipeline"]["db_path"] = db_path
    if scheduler_interval_seconds is not None:
        cfg["reliable_pipeline"]["scheduler_interval_seconds"] = scheduler_interval_seconds
    monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: cfg)
    return cfg


def _make_queued_turn_job(db_path: Path, *, target_id: str = "wxid_t",
                          group_key: str = "wxid_t", sender_id: str = "wxid_s",
                          local_id: int = 1) -> dict:
    """Create and return a single queued turn job in a temp pipeline DB."""
    event_id = "src:%s:%s" % (target_id, local_id)
    payload = {"message": "hello"}
    pipeline.persist_inbound_event(
        event_id=event_id, target_id=target_id, group_key=group_key,
        sender_id=sender_id, local_id=local_id, payload=payload,
        db_path=db_path, received_at=1.0,
    )
    pipeline.add_event_to_window(
        event_id, db_path=db_path, now=1.0,
        debounce_seconds=1.0, max_window_seconds=2.0,
    )
    pipeline.close_due_windows(db_path=db_path, now=10.0)
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db_path)
    assert jobs, "no job was created"
    assert jobs[0]["status"] == pipeline.JOB_QUEUED
    return jobs[0]


@pytest.fixture(autouse=True)
def _stop_scheduler_after_test():
    """Ensure the scheduler thread is stopped between tests."""
    yield
    control_api._reliable_scheduler_stop()


# --- Status / disabled behavior ---------------------------------------------


def test_scheduler_status_initially_not_running(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=False)
    status, resp = _route("GET", "/reliable-pipeline/scheduler/status")
    assert status == 200
    assert resp["ok"] is True
    assert resp["running"] is False
    assert "iterations" in resp
    assert "interval" in resp


def test_start_disabled_returns_action_disabled(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=False)
    status, resp = _route("POST", "/reliable-pipeline/scheduler/start", {})
    assert status == 200
    assert resp["ok"] is True
    assert resp["action"] == "disabled"
    assert resp["enabled"] is False


def test_auto_start_disabled_pipeline_does_nothing(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=False)
    result = control_api._reliable_scheduler_auto_start()
    status = control_api._reliable_scheduler_status()
    assert result is None
    assert status["running"] is False


def test_auto_start_enabled_pipeline_starts_scheduler(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True, scheduler_interval_seconds=2.0)
    result = control_api._reliable_scheduler_auto_start()
    status = control_api._reliable_scheduler_status()
    assert result is not None
    assert result["action"] == "started"
    assert status["running"] is True


# --- Scheduler tick ordering and resilience ---------------------------------


def test_scheduler_tick_runs_worker_before_sender(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True)
    calls = []

    def fake_worker(body):
        calls.append("worker")
        return {"action": "processed"}

    def fake_sender(body):
        calls.append("sender")
        return {"action": "processed"}

    monkeypatch.setattr(control_api, "_reliable_worker_run_once", fake_worker)
    monkeypatch.setattr(control_api, "_reliable_sender_run_once", fake_sender)

    control_api._reliable_scheduler_tick()
    assert calls == ["worker", "sender"]


def test_scheduler_tick_sender_runs_after_worker_error(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True)
    calls = []

    monkeypatch.setattr(
        control_api, "_reliable_worker_run_once",
        lambda body: {"ok": False, "action": "error", "error": "worker boom"},
    )
    monkeypatch.setattr(
        control_api, "_reliable_sender_run_once",
        lambda body: calls.append("sender") or {"action": "processed"},
    )

    control_api._reliable_scheduler_tick()
    assert "sender" in calls


def test_duplicate_start_is_idempotent(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True, scheduler_interval_seconds=2.0)
    first = control_api._reliable_scheduler_start({})
    second = control_api._reliable_scheduler_start({})
    control_api._reliable_scheduler_stop()

    assert first["action"] == "started"
    assert second["action"] == "already_running"
    assert first["running"] is True
    assert second["running"] is True


def test_iteration_error_is_recorded_and_loop_continues(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True, scheduler_interval_seconds=0.05)

    monkeypatch.setattr(
        control_api, "_reliable_worker_run_once",
        lambda body: {"ok": False, "action": "error", "error": "worker boom: /private/secret"},
    )
    monkeypatch.setattr(
        control_api, "_reliable_sender_run_once",
        lambda body: {"action": "processed"},
    )

    control_api._reliable_scheduler_start({})
    # Let the scheduler iterate several times; the first tick records the error.
    time.sleep(0.25)
    control_api._reliable_scheduler_stop()

    status = control_api._reliable_scheduler_status()
    assert status["iterations"] >= 2
    # last_error is a fixed safe marker; raw helper text is never surfaced.
    assert status["last_error"] == "worker returned error"
    body = json.dumps(status)
    assert "worker boom" not in body
    assert "/private/secret" not in body


def test_status_route_never_leaks_helper_error_text(monkeypatch, tmp_path):
    """The public HTTP status route must only expose fixed safe markers, never
    helper-provided error strings that could contain paths or user content."""
    _patch_config(monkeypatch, enabled=True)

    monkeypatch.setattr(
        control_api, "_reliable_worker_run_once",
        lambda body: {"ok": False, "action": "error", "error": "disk on fire: /secret/path"},
    )
    monkeypatch.setattr(
        control_api, "_reliable_sender_run_once",
        lambda body: {"ok": False, "action": "error", "error": "send failed: user content"},
    )

    # Drive one tick deterministically (no thread race), then read via HTTP route.
    control_api._reliable_scheduler_tick()

    status, resp = _route("GET", "/reliable-pipeline/scheduler/status")
    assert status == 200
    body = json.dumps(resp)
    # No raw helper error text, paths, or exception reprs leak in the response.
    assert "disk on fire" not in body
    assert "/secret/path" not in body
    assert "send failed" not in body
    assert "user content" not in body
    assert resp["last_error"] in ("worker returned error", "sender returned error")
    assert resp["last_worker"] == {"action": "error"}
    assert resp["last_sender"] == {"action": "error"}


def test_stop_route_is_idempotent_and_safe(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True, scheduler_interval_seconds=2.0)
    control_api._reliable_scheduler_start({})

    # First stop returns stopped.
    status, resp = _route("POST", "/reliable-pipeline/scheduler/stop", {})
    assert status == 200
    assert resp["ok"] is True
    assert resp["running"] is False
    assert resp["action"] in ("stopped", "stop_requested")

    # Subsequent stop is safe and returns the same non-running state.
    status, resp = _route("POST", "/reliable-pipeline/scheduler/stop", {})
    assert status == 200
    assert resp["ok"] is True
    assert resp["running"] is False


def test_worker_exception_still_runs_sender_and_continues(monkeypatch, tmp_path):
    """A real worker exception must not abort the sender or the loop, and
    the public state must never carry an exception repr or filesystem path."""
    _patch_config(monkeypatch, enabled=True, scheduler_interval_seconds=0.05)
    sender_calls = []

    def fake_worker(body):
        raise RuntimeError("db crashed: /private/secret")

    def fake_sender(body):
        sender_calls.append("sender")
        return {"action": "processed"}

    monkeypatch.setattr(control_api, "_reliable_worker_run_once", fake_worker)
    monkeypatch.setattr(control_api, "_reliable_sender_run_once", fake_sender)

    control_api._reliable_scheduler_start({})
    time.sleep(0.25)
    control_api._reliable_scheduler_stop()

    status = control_api._reliable_scheduler_status()
    assert len(sender_calls) >= 2
    assert status["iterations"] >= 2
    # Safe public marker, not an exception repr or path.
    assert status["last_error"] == "worker iteration error"
    body = json.dumps(status)
    assert "db crashed" not in body
    assert "/private/secret" not in body
    assert "RuntimeError" not in body


def test_scheduler_runs_general_then_healthy_on_duty_instances(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True)
    calls = []

    def fake_worker(body):
        calls.append(dict(body))
        return {"action": "processed"}

    monkeypatch.setattr(control_api, "_reliable_worker_run_once", fake_worker)
    monkeypatch.setattr(control_api, "_reliable_sender_run_once", lambda body: {"action": "processed"})
    monkeypatch.setattr(
        control_api, "_agent_pool_status",
        lambda: {
            "instances": [
                {"id": "hermes-wechat-bot-worker3", "on_duty": True, "health": {"ok": True, "ready": True}},
            ]
        },
    )

    control_api._reliable_scheduler_tick()
    assert len(calls) == 2
    assert calls[0] == {}
    assert calls[1] == {"instance_id": "hermes-wechat-bot-worker3"}


def test_scheduler_skips_available_but_not_on_duty_instance(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True)
    calls = []

    def fake_worker(body):
        calls.append(dict(body))
        return {"action": "processed"}

    monkeypatch.setattr(control_api, "_reliable_worker_run_once", fake_worker)
    monkeypatch.setattr(control_api, "_reliable_sender_run_once", lambda body: {"action": "processed"})
    monkeypatch.setattr(
        control_api, "_agent_pool_status",
        lambda: {
            "instances": [
                # Healthy but not on-duty should be skipped.
                {"id": "hermes-wechat-bot-worker3", "on_duty": False, "health": {"ok": True, "ready": True}},
            ]
        },
    )

    control_api._reliable_scheduler_tick()
    # Only the general run; not-on-duty instance is not invoked.
    assert calls == [{}]


def test_scheduler_skips_unhealthy_on_duty_instance(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True)
    calls = []

    def fake_worker(body):
        calls.append(dict(body))
        return {"action": "processed"}

    monkeypatch.setattr(control_api, "_reliable_worker_run_once", fake_worker)
    monkeypatch.setattr(control_api, "_reliable_sender_run_once", lambda body: {"action": "processed"})
    monkeypatch.setattr(
        control_api, "_agent_pool_status",
        lambda: {
            "instances": [
                {"id": "hermes-wechat-bot-worker3", "on_duty": True, "health": {"ok": False, "ready": False}},
            ]
        },
    )

    control_api._reliable_scheduler_tick()
    # Only the general run; no dedicated call for unhealthy worker3.
    assert calls == [{}]


    _patch_config(monkeypatch, enabled=True)
    calls = []

    def fake_worker(body):
        calls.append(dict(body))
        return {"action": "processed"}

    monkeypatch.setattr(control_api, "_reliable_worker_run_once", fake_worker)
    monkeypatch.setattr(control_api, "_reliable_sender_run_once", lambda body: {"action": "processed"})
    monkeypatch.setattr(
        control_api, "_agent_pool_status",
        lambda: {
            "instances": [
                {"id": "hermes-wechat-bot-worker3", "on_duty": True, "health": {"ok": False, "ready": False}},
            ]
        },
    )

    control_api._reliable_scheduler_tick()
    # Only the general run; no dedicated call for unhealthy worker3.
    assert calls == [{}]




def test_quarantine_queued_turn_job(monkeypatch, tmp_path):
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    job = _make_queued_turn_job(db_path)

    status, resp = _route(
        "POST", "/reliable-pipeline/turn-jobs/%d/quarantine" % job["id"],
        {"reason": "test quarantine"},
    )
    assert status == 200
    assert resp["ok"] is True
    assert resp["action"] == "quarantined"
    assert resp["job_id"] == job["id"]
    assert resp["quarantined"] is True
    assert resp["retryable"] is False

    # Verify the job moved to a terminal failed state and no outbox was created.
    with pipeline._connect(db_path) as con:
        row = con.execute("SELECT status, error FROM turn_jobs WHERE id=?", (job["id"],)).fetchone()
        outbox = con.execute("SELECT 1 FROM send_outbox WHERE job_id=?", (job["id"],)).fetchone()
    assert row["status"] == pipeline.JOB_FAILED
    assert row["error"] == "test quarantine"
    assert outbox is None


def test_quarantine_works_when_pipeline_disabled(monkeypatch, tmp_path):
    """Quarantine must be available even when the pipeline is globally
    disabled so legacy queued jobs can be failed before enabling the scheduler.
    """
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=False, db_path=str(db_path))
    job = _make_queued_turn_job(db_path)

    status, resp = _route(
        "POST", "/reliable-pipeline/turn-jobs/%d/quarantine" % job["id"],
        {"reason": "pre-enable quarantine"},
    )
    assert status == 200
    assert resp["ok"] is True
    assert resp["action"] == "quarantined"
    assert resp["quarantined"] is True
    assert resp["retryable"] is False

    with pipeline._connect(db_path) as con:
        row = con.execute("SELECT status FROM turn_jobs WHERE id=?", (job["id"],)).fetchone()
        outbox = con.execute("SELECT 1 FROM send_outbox WHERE job_id=?", (job["id"],)).fetchone()
    assert row["status"] == pipeline.JOB_FAILED
    assert outbox is None


def test_quarantine_invalid_job_id_returns_error(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True)
    for bad_id in ("abc", "0", "-1"):
        status, resp = _route("POST", "/reliable-pipeline/turn-jobs/%s/quarantine" % bad_id, {})
        assert status == 200, bad_id
        assert resp["ok"] is False, bad_id
        assert resp["action"] == "error", bad_id
        assert "positive integer" in resp["error"], bad_id


def test_quarantine_missing_job_returns_not_quarantined(monkeypatch, tmp_path):
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    # Database exists but has no job with id=999.
    status, resp = _route("POST", "/reliable-pipeline/turn-jobs/999/quarantine", {})
    assert status == 200
    assert resp["ok"] is True
    assert resp["action"] == "not_quarantined"
    assert resp["quarantined"] is False


def test_quarantine_does_not_invoke_sender(monkeypatch, tmp_path):
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    job = _make_queued_turn_job(db_path)

    sender = MagicMock()
    monkeypatch.setattr(control_api.reliable_worker, "send_once", sender)

    _route("POST", "/reliable-pipeline/turn-jobs/%d/quarantine" % job["id"], {})
    sender.assert_not_called()


# --- Outbox dead-letter requeue endpoint (safe path only) --------------------

def _make_dead_outbox(db_path, *, send_started_at, error="test_mode_target_rejected: x",
                      result=None):
    """Create a reply outbox and force it to dead_letter with the given marker."""
    job = _make_queued_turn_job(db_path)
    applied = pipeline.apply_agent_result(
        job_id=job["id"],
        result={"schema_version": 1, "action": "reply", "reply_text": "hi",
                "reason_code": "ok", "risk_level": "low"},
        final_filter=lambda t: t,
        db_path=db_path,
        now=11.0,
    )
    outbox_id = applied["outbox"]["id"]
    with pipeline._connect(db_path) as con:
        con.execute(
            "UPDATE send_outbox SET status=?, attempts=5, error=?, result_json=?, "
            "send_started_at=?, dead_at=50.0, lease_owner=NULL, lease_until=NULL WHERE id=?",
            (pipeline.OUTBOX_DEAD, error, json.dumps(result) if result is not None else None,
             send_started_at, outbox_id),
        )
    return outbox_id


def test_requeue_outbox_recovers_never_sent_dead_letter(monkeypatch, tmp_path):
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    outbox_id = _make_dead_outbox(db_path, send_started_at=None,
                                  result={"skipped": True, "reason": "x"})
    status, resp = _route("POST", "/reliable-pipeline/outbox/%d/requeue" % outbox_id,
                          {"reason": "gate rejection, never sent"})
    assert status == 200
    assert resp["ok"] is True
    assert resp["action"] == "requeued"
    assert resp["status"] == pipeline.OUTBOX_RETRY
    assert resp["requeue_count"] == 1
    with pipeline._connect(db_path) as con:
        row = con.execute("SELECT status, attempts, send_started_at FROM send_outbox WHERE id=?",
                          (outbox_id,)).fetchone()
        audit = con.execute("SELECT actor, legacy_override FROM send_outbox_recovery_audit WHERE outbox_id=?",
                            (outbox_id,)).fetchone()
    assert row["status"] == pipeline.OUTBOX_RETRY
    assert row["attempts"] == 0
    assert row["send_started_at"] is None
    assert audit["actor"] == "control_api"
    assert audit["legacy_override"] == 0


def test_requeue_outbox_rejects_legacy_sentinel_row(monkeypatch, tmp_path):
    """A pre-tracking (non-NULL send_started_at) dead letter is refused over HTTP."""
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    outbox_id = _make_dead_outbox(db_path, send_started_at=42.0, result={"skipped": True})
    status, resp = _route("POST", "/reliable-pipeline/outbox/%d/requeue" % outbox_id,
                          {"reason": "attempt legacy recovery"})
    assert status == 200
    assert resp["ok"] is False
    assert resp["action"] == "error"
    with pipeline._connect(db_path) as con:
        row = con.execute("SELECT status FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        n = con.execute("SELECT COUNT(*) AS c FROM send_outbox_recovery_audit WHERE outbox_id=?",
                        (outbox_id,)).fetchone()["c"]
    assert row["status"] == pipeline.OUTBOX_DEAD  # unchanged
    assert n == 0  # no audit written on rejection


def test_requeue_outbox_rejects_non_dead_letter(monkeypatch, tmp_path):
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    job = _make_queued_turn_job(db_path)
    applied = pipeline.apply_agent_result(
        job_id=job["id"],
        result={"schema_version": 1, "action": "reply", "reply_text": "hi",
                "reason_code": "ok", "risk_level": "low"},
        final_filter=lambda t: t, db_path=db_path, now=11.0,
    )
    outbox_id = applied["outbox"]["id"]  # pending, not dead_letter
    status, resp = _route("POST", "/reliable-pipeline/outbox/%d/requeue" % outbox_id,
                          {"reason": "not dead"})
    assert status == 200
    assert resp["ok"] is False
    assert resp["action"] == "error"


def test_requeue_outbox_requires_reason(monkeypatch, tmp_path):
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    outbox_id = _make_dead_outbox(db_path, send_started_at=None, result={"skipped": True})
    for body in ({}, {"reason": "   "}):
        status, resp = _route("POST", "/reliable-pipeline/outbox/%d/requeue" % outbox_id, body)
        assert status == 200
        assert resp["ok"] is False
        assert resp["action"] == "error"


def test_requeue_outbox_rejects_non_object_body(monkeypatch, tmp_path):
    db_path = tmp_path / "rp.sqlite"
    _patch_config(monkeypatch, enabled=True, db_path=str(db_path))
    outbox_id = _make_dead_outbox(db_path, send_started_at=None, result={"skipped": True})
    status, resp = _route("POST", "/reliable-pipeline/outbox/%d/requeue" % outbox_id, [1, 2])
    assert status == 200
    assert resp["ok"] is False
    assert resp["action"] == "error"


def test_requeue_outbox_invalid_id(monkeypatch, tmp_path):
    _patch_config(monkeypatch, enabled=True)
    for bad_id in ("abc", "0", "-1"):
        status, resp = _route("POST", "/reliable-pipeline/outbox/%s/requeue" % bad_id,
                              {"reason": "x"})
        assert status == 200, bad_id
        assert resp["ok"] is False, bad_id
        assert resp["action"] == "error", bad_id


def test_active_on_duty_instance_ids_picks_persistent_config_flag(monkeypatch):
    """Instances with ``reliable_on_duty: true`` are active across control_api restarts
    only when they are also healthy in the pool status."""
    monkeypatch.setattr(control_api, "_agent_pool_status", lambda: {"instances": [
        {"id": "hermes-a", "on_duty": False, "health": {"ok": True}},
        {"id": "hermes-b", "on_duty": False, "health": {"ok": True}},
        {"id": "hermes-c", "on_duty": False, "health": {"ok": False}},
    ]})
    cfg = {
        "targets": [],
        "agent_provider": {
            "instances": [
                {"id": "hermes-b", "reliable_on_duty": True},
                {"id": "hermes-c", "reliable_on_duty": True},
            ]
        },
    }
    monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: cfg)
    ids = control_api._active_on_duty_instance_ids()
    assert ids == ["hermes-b"]


def test_active_on_duty_instance_ids_merges_runtime_and_persistent(monkeypatch):
    """Runtime on-duty instances and persistent config flags are merged; health is required."""
    monkeypatch.setattr(control_api, "_agent_pool_status", lambda: {"instances": [
        {"id": "hermes-a", "on_duty": True, "health": {"ok": True}},
        {"id": "hermes-b", "on_duty": False, "health": {"ok": True}},
    ]})
    cfg = {
        "targets": [],
        "agent_provider": {
            "instances": [{"id": "hermes-b", "reliable_on_duty": True}]
        },
    }
    monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: cfg)
    ids = sorted(control_api._active_on_duty_instance_ids())
    assert ids == ["hermes-a", "hermes-b"]


def test_scheduler_routes_persistent_on_duty_after_restart(monkeypatch, tmp_path):
    """Restart-level: after a full scheduler state reset + startup, the reliable
    scheduler routes dedicated jobs to a healthy instance flagged in config,
    independent of the M5 async-loop."""
    _patch_config(monkeypatch, enabled=True)
    control_api._reliable_scheduler_stop()

    # Dormant Thread fake: lets _reliable_scheduler_start initialize state
    # without a real background loop running, so assertions are deterministic.
    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._alive = False
        def start(self):
            self._alive = True
        def is_alive(self):
            return self._alive
        def join(self, timeout=None):
            self._alive = False
    monkeypatch.setattr(control_api.threading, "Thread", FakeThread)

    monkeypatch.setattr(
        control_api, "_agent_pool_status",
        lambda: {
            "instances": [
                # M5 async-loop is off-duty, but health is good.
                {"id": "hermes-wechat-bot-worker3", "on_duty": False, "health": {"ok": True, "ready": True}},
            ]
        },
    )
    cfg = control_api.reg.load_config()
    cfg["agent_provider"] = {
        "instances": [{"id": "hermes-wechat-bot-worker3", "reliable_on_duty": True}]
    }
    monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: cfg)

    calls = []
    def fake_worker(body):
        calls.append(dict(body))
        return {"action": "processed"}
    monkeypatch.setattr(control_api, "_reliable_worker_run_once", fake_worker)
    monkeypatch.setattr(control_api, "_reliable_sender_run_once", lambda body: {"action": "processed"})

    # Simulate startup auto-start.
    start_result = control_api._reliable_scheduler_start({})
    assert start_result["action"] == "started"
    assert control_api._reliable_scheduler_status()["running"] is True
    # Drive one deterministic tick; the only calls come from this tick.
    control_api._reliable_scheduler_tick()
    assert len(calls) == 2
    assert calls[0] == {}
    assert calls[1] == {"instance_id": "hermes-wechat-bot-worker3"}
