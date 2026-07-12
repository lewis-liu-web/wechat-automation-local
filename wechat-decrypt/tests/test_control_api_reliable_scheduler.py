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


# --- Quarantine -------------------------------------------------------------


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
