"""Tests for new control_api routes added for the Streamlit console optimization."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs
import control_api


def _route(method, path, params=None, body=None):
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, params or {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def test_get_agent_job_returns_detail_and_missing_returns_404(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = agent_jobs.DEFAULT_DB_PATH
        try:
            agent_jobs.DEFAULT_DB_PATH = db
            job = agent_jobs.enqueue_job(
                job_key="test-job",
                group_key="test-group",
                target_name="测试群",
                sender="tester",
                task_type="deep_free",
                provider="echo",
                payload={"prompt": "hello"},
            )
            job_id = job["id"]
            status, payload = _route("GET", "/agent/jobs/%d" % job_id, {}, {})
            assert status == 200
            assert payload.get("ok") is True
            assert payload["job"]["id"] == job_id

            status2, payload2 = _route("GET", "/agent/jobs/999999", {}, {})
            assert status2 == 404
            assert payload2.get("ok") is False
        finally:
            agent_jobs.DEFAULT_DB_PATH = orig_default


def test_post_target_mode_writes_customer_service_bundle(monkeypatch, tmp_path):
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(
        json.dumps({"targets": [{"name": "群A", "username": "wxid_a", "table": "Msg_a"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    status, payload = _route("POST", "/targets/群A/mode", {}, {"mode": "customer_service"})
    assert status == 200
    t = payload["target"]
    assert t["mode"] == "customer_service"
    assert t["reply_policy"] == "knowledge_grounded"
    assert t["session_policy"]["timeout_seconds"] == 120
    assert t["context_policy"]["max_messages"] == 40


def test_post_target_mode_normalizes_legacy_personal_assistant(monkeypatch, tmp_path):
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(
        json.dumps({"targets": [{"name": "群A", "username": "wxid_a", "table": "Msg_a", "mode": "personal_assistant"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    status, payload = _route("POST", "/targets/群A/mode", {}, {"mode": "personal_assistant"})
    assert status == 200
    t = payload["target"]
    assert t["mode"] == "group_assistant"
    assert t["reply_policy"] == "balanced"
    assert t["session_policy"]["timeout_seconds"] == 60
    assert t["context_policy"]["max_messages"] == 30
