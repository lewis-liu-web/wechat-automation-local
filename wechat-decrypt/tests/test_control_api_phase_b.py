"""Tests for Stage 2 Phase B control_api endpoints.

Covers the manual migration controls and the isolated mocked preflight helper
introduced in Phase B. These endpoints are not part of the reliable-pipeline
or M5 async-loop routes; they are explicit, manual-only controls.
"""

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_jobs
import control_api


def _route(method: str, path: str, body: dict | None = None):
    """Dispatch a control_api route directly without starting a socket."""
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _contract(action: str = "reply", reply_text: str = "answer") -> str:
    return json.dumps({
        "schema_version": 1,
        "action": action,
        "reply_text": reply_text,
        "reason_code": "test",
        "risk_level": "low",
    })


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_preflight_hermes_profile_returns_safe_metadata():
    with mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout=_contract(), stderr="")
        result = control_api.preflight_hermes_profile("hello", cli_path="hermes-test")
    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["action"] == "reply"
    assert result["schema_version"] == 1
    assert result["reason"] == "preflight_ok"
    assert result["stage"] == "parsed"
    assert "stdout" not in result
    assert "stderr" not in result
    assert "reply_text" not in result


def test_preflight_hermes_profile_rejects_legacy_display_text():
    invalid_outputs = [
        "plain text reply",
        "╭─ ⚕ Hermes ─╮\nanswer\n╰─╯",
        "\x1b[31merror\x1b[0m",
        '{"tool":"leann_search","query":"x"}',
        _contract() + "\nextra trailing prose",
    ]
    for stdout in invalid_outputs:
        with mock.patch("agent_provider.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=stdout, stderr="")
            result = control_api.preflight_hermes_profile("hello", cli_path="hermes-test")
        assert result["ok"] is False, stdout
        assert result["status"] == "fail"
        assert result["stage"] == "contract_violation"
        assert "stdout" not in result


def test_preflight_hermes_profile_no_persistence(tmp_path):
    """Preflight must not create a session dir or persist raw provider stdout."""
    home = str(tmp_path)
    with mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout=_contract(), stderr="")
        result = control_api.preflight_hermes_profile(
            "hello", cli_path="hermes-test", hermes_home=home
        )
    assert result["ok"] is True
    jobs_dir = tmp_path / ".wechat_agent_jobs"
    assert not jobs_dir.exists()
    assert "stdout" not in result
    assert "stderr" not in result


def test_preflight_hermes_profile_no_enqueue_or_popen():
    with mock.patch("agent_provider.subprocess.run") as mock_run, \
         mock.patch("agent_provider.subprocess.Popen") as mock_popen:
        mock_run.return_value = mock.Mock(returncode=0, stdout=_contract(), stderr="")
        result = control_api.preflight_hermes_profile("hello", cli_path="hermes-test")
    assert result["ok"] is True
    mock_popen.assert_not_called()


def test_preflight_endpoint_routes_use_configured_provider_only():
    configured = control_api.HermesProvider(
        cli_path="configured-hermes",
        profile="configured-profile",
        hermes_home="configured-home",
    )
    with mock.patch.object(control_api, "_build_agent_provider", return_value=configured), \
         mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout=_contract(), stderr="")
        status, resp = _route("POST", "/agent/preflight/run", {
            "prompt": "hello",
            "cli_path": "untrusted-command",
            "profile": "untrusted-profile",
            "hermes_home": "untrusted-home",
        })
    assert status == 200
    assert resp["ok"] is True
    command = mock_run.call_args.args[0]
    assert command[0] == "configured-hermes"
    assert "configured-profile" in command
    assert "untrusted-command" not in command
    assert "stdout" not in resp
    assert "stderr" not in resp


# ---------------------------------------------------------------------------
# Legacy async job expiration
# ---------------------------------------------------------------------------


def _claim_and_submit_legacy(db, payload):
    job = agent_jobs.enqueue_job(
        job_key=f"legacy-{time.time()}", group_key="g1", task_type="deep_agent",
        payload=payload, db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    ok = agent_jobs.mark_submitted(
        job["id"], external_session_id=f"sess-{job['id']}", external_user_msg_id=1,
        external_provider="hermes", db_path=db,
    )
    assert ok, "mark_submitted failed; job must be dispatching"
    return job


def test_expire_legacy_async_jobs_expires_active_legacy_jobs(tmp_path):
    db = tmp_path / "jobs.sqlite"
    job = _claim_and_submit_legacy(db, {"prompt": "q"})
    result = control_api._expire_legacy_async_jobs(db_path=db)
    assert result["ok"] is True
    assert result["action"] == "expired"
    assert result["count"] == 1
    assert result["job_ids"] == [job["id"]]
    updated = agent_jobs.get_job(job["id"], db_path=db)
    assert updated["status"] == agent_jobs.STATUS_EXPIRED
    assert updated["error"] == "legacy protocol job expired before strict cutover"


def test_expire_legacy_async_jobs_expires_dispatching_legacy_job(tmp_path):
    db = tmp_path / "jobs.sqlite"
    job = agent_jobs.enqueue_job(
        job_key="legacy-dispatching", group_key="g1", task_type="deep_agent",
        payload={"prompt": "q"}, db_path=db,
    )
    with agent_jobs.open_db(db) as con:
        con.execute(
            "UPDATE agent_jobs SET status=?, external_session_id=? WHERE id=?",
            (agent_jobs.STATUS_DISPATCHING, "sess-dispatching", job["id"]),
        )
        con.commit()
    result = control_api._expire_legacy_async_jobs(db_path=db)
    assert result["count"] == 1
    assert result["job_ids"] == [job["id"]]
    updated = agent_jobs.get_job(job["id"], db_path=db)
    assert updated["status"] == agent_jobs.STATUS_EXPIRED
    assert "payload" not in result
    assert "payload_json" not in result


def test_expire_legacy_async_jobs_does_not_touch_strict_jobs(tmp_path):
    db = tmp_path / "jobs.sqlite"
    legacy_job = _claim_and_submit_legacy(db, {"prompt": "q"})
    strict_job = agent_jobs.enqueue_job(
        job_key="strict-1", group_key="g2", task_type="deep_agent",
        payload={"prompt": "q", "reliable_result_contract": True}, db_path=db,
    )
    claimed = agent_jobs.claim_dispatchable(worker_id="w2", db_path=db, max_global_dispatching=2)
    assert claimed is not None
    agent_jobs.mark_submitted(
        strict_job["id"], external_session_id="sess-strict-1", external_user_msg_id=1,
        external_provider="hermes", db_path=db,
    )
    result = control_api._expire_legacy_async_jobs(db_path=db)
    assert result["count"] == 1
    assert result["job_ids"] == [legacy_job["id"]]
    strict_updated = agent_jobs.get_job(strict_job["id"], db_path=db)
    assert strict_updated["status"] != agent_jobs.STATUS_EXPIRED


def test_expire_legacy_async_jobs_does_not_touch_queued_jobs(tmp_path):
    db = tmp_path / "jobs.sqlite"
    job = agent_jobs.enqueue_job(
        job_key="queued-1", group_key="g1", task_type="deep_agent",
        payload={"prompt": "q"}, db_path=db,
    )
    result = control_api._expire_legacy_async_jobs(db_path=db)
    assert result["count"] == 0
    assert result["job_ids"] == []
    updated = agent_jobs.get_job(job["id"], db_path=db)
    assert updated["status"] == agent_jobs.STATUS_QUEUED


def test_expire_legacy_async_jobs_is_idempotent(tmp_path):
    db = tmp_path / "jobs.sqlite"
    job = _claim_and_submit_legacy(db, {"prompt": "q"})
    result1 = control_api._expire_legacy_async_jobs(db_path=db)
    result2 = control_api._expire_legacy_async_jobs(db_path=db)
    assert result1["count"] == 1
    assert result2["count"] == 0
    assert result2["job_ids"] == []


def test_expire_legacy_endpoint_routes():
    with mock.patch.object(control_api, "_expire_legacy_async_jobs", return_value={
        "ok": True, "action": "expired", "count": 0, "job_ids": []
    }):
        status, resp = _route("POST", "/agent/async-jobs/expire-legacy", {})
    assert status == 200
    assert resp["ok"] is True
    assert resp["action"] == "expired"
    assert resp["count"] == 0
    assert resp["job_ids"] == []
