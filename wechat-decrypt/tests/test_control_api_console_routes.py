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


# ---------------------------------------------------------------------------
# Regression tests for send/retry-send paths that sanitize and persist result
# ---------------------------------------------------------------------------

def test_retry_send_sanitizes_and_updates_result_text(monkeypatch, tmp_path):
    """/agent/jobs/{id}/retry-send must sanitize the persisted result before sending."""
    import agent_worker
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({"targets": [{"name": "群A", "username": "wxid_a", "table": "Msg_a"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = agent_jobs.DEFAULT_DB_PATH
        try:
            agent_jobs.DEFAULT_DB_PATH = db
            job = agent_jobs.enqueue_job(
                job_key="retry-1",
                group_key="g1",
                target_name="群A",
                sender="user",
                task_type="deep_free",
                provider="echo",
                payload={"max_reply_chars": 120, "agent_mode": "raw_agent"},
            )
            job_id = job["id"]
            # Move job through the real worker lifecycle so complete_job is allowed.
            agent_jobs.claim_next_job(provider="echo", worker_id="w1", db_path=db)
            assert agent_jobs.complete_job(job_id, "中" * 1200) is True
            assert len(agent_jobs.get_job(job_id).get("result_text") or "") == 1200

            sent = []
            def _fake_send(job, result_text, config_path):
                sent.append(result_text)
                return {"sent": True, "reason": "ok"}
            monkeypatch.setattr(agent_worker, "_send_result_back", _fake_send)
            monkeypatch.setattr(agent_worker, "_HAS_SENDER", True)

            status, payload = _route("POST", "/agent/jobs/%d/retry-send" % job_id, {}, {})
            assert status == 200
            assert payload.get("ok") is True
            assert payload["action"] == "sent"
            assert len(sent) == 1
            assert len(sent[0]) == 120
            stored = agent_jobs.get_job(job_id).get("result_text") or ""
            assert len(stored) == 120
        finally:
            agent_jobs.DEFAULT_DB_PATH = orig_default


def test_retry_send_preserves_full_text_when_cap_is_high(monkeypatch, tmp_path):
    """retry-send with a high cap must not truncate the full reply."""
    import agent_worker
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({"targets": [{"name": "群A", "username": "wxid_a", "table": "Msg_a"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = agent_jobs.DEFAULT_DB_PATH
        try:
            agent_jobs.DEFAULT_DB_PATH = db
            job = agent_jobs.enqueue_job(
                job_key="retry-2",
                group_key="g1",
                target_name="群A",
                sender="user",
                task_type="deep_free",
                provider="echo",
                payload={"max_reply_chars": 2000, "agent_mode": "raw_agent"},
            )
            job_id = job["id"]
            agent_jobs.claim_next_job(provider="echo", worker_id="w1", db_path=db)
            assert agent_jobs.complete_job(job_id, "中" * 1200) is True
            assert len(agent_jobs.get_job(job_id).get("result_text") or "") == 1200

            sent = []
            def _fake_send(job, result_text, config_path):
                sent.append(result_text)
                return {"sent": True, "reason": "ok"}
            monkeypatch.setattr(agent_worker, "_send_result_back", _fake_send)
            monkeypatch.setattr(agent_worker, "_HAS_SENDER", True)

            status, payload = _route("POST", "/agent/jobs/%d/retry-send" % job_id, {}, {})
            assert status == 200
            assert payload.get("ok") is True
            assert len(sent[0]) == 1200
            stored = agent_jobs.get_job(job_id).get("result_text") or ""
            assert len(stored) == 1200
        finally:
            agent_jobs.DEFAULT_DB_PATH = orig_default


def test_retry_send_updates_result_text_for_already_sent_job(monkeypatch, tmp_path):
    """A retry on a status=sent job must still persist the sanitized (truncated) text."""
    import agent_worker
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({"targets": [{"name": "群A", "username": "wxid_a", "table": "Msg_a"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = agent_jobs.DEFAULT_DB_PATH
        try:
            agent_jobs.DEFAULT_DB_PATH = db
            job = agent_jobs.enqueue_job(
                job_key="retry-sent-1",
                group_key="g1",
                target_name="群A",
                sender="user",
                task_type="deep_free",
                provider="echo",
                payload={"max_reply_chars": 120, "agent_mode": "raw_agent"},
            )
            job_id = job["id"]
            agent_jobs.claim_next_job(provider="echo", worker_id="w1", db_path=db)
            assert agent_jobs.complete_job(job_id, "中" * 1200) is True
            assert len(agent_jobs.get_job(job_id).get("result_text") or "") == 1200
            agent_jobs.mark_sent(job_id)
            assert agent_jobs.get_job(job_id)["status"] == "sent"

            sent = []
            def _fake_send(job, result_text, config_path):
                sent.append(result_text)
                return {"sent": True, "reason": "ok"}
            monkeypatch.setattr(agent_worker, "_send_result_back", _fake_send)
            monkeypatch.setattr(agent_worker, "_HAS_SENDER", True)

            status, payload = _route("POST", "/agent/jobs/%d/retry-send" % job_id, {}, {})
            assert status == 200
            assert payload.get("ok") is True
            assert payload["action"] == "sent"
            assert len(sent) == 1
            assert len(sent[0]) == 120
            stored = agent_jobs.get_job(job_id)
            assert stored["status"] == "sent"
            assert len(stored.get("result_text") or "") == 120
        finally:
            agent_jobs.DEFAULT_DB_PATH = orig_default


def test_async_sender_run_once_sanitizes_and_sends_long_replies(monkeypatch, tmp_path):
    """/agent/sender/run-once must sanitize and send done jobs with cap > 600."""
    import agent_worker
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({"targets": [{"name": "群A", "username": "wxid_a", "table": "Msg_a"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = agent_jobs.DEFAULT_DB_PATH
        try:
            agent_jobs.DEFAULT_DB_PATH = db
            job = agent_jobs.enqueue_job(
                job_key="async-1",
                group_key="g1",
                target_name="群A",
                sender="user",
                task_type="deep_free",
                provider="echo",
                payload={"max_reply_chars": 120, "agent_mode": "raw_agent"},
            )
            job_id = job["id"]
            agent_jobs.claim_next_job(provider="echo", worker_id="w1", db_path=db)
            assert agent_jobs.complete_job(job_id, "中" * 1200) is True
            assert len(agent_jobs.get_job(job_id).get("result_text") or "") == 1200
            assert agent_jobs.get_job(job_id)["status"] == "done"

            sent = []
            def _fake_send(job, result_text, config_path):
                sent.append(result_text)
                return {"sent": True, "reason": "ok"}
            monkeypatch.setattr(agent_worker, "_send_result_back", _fake_send)
            monkeypatch.setattr(agent_worker, "_HAS_SENDER", True)

            result = control_api._async_sender_run_once({"limit": 5})
            assert result["sent"] == 1
            assert len(sent) == 1
            assert len(sent[0]) == 120
            stored = agent_jobs.get_job(job_id).get("result_text") or ""
            assert len(stored) == 120
        finally:
            agent_jobs.DEFAULT_DB_PATH = orig_default


def test_async_sender_preserves_full_text_when_cap_is_high(monkeypatch, tmp_path):
    """async sender with a high cap must send the full reply."""
    import agent_worker
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({"targets": [{"name": "群A", "username": "wxid_a", "table": "Msg_a"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = agent_jobs.DEFAULT_DB_PATH
        try:
            agent_jobs.DEFAULT_DB_PATH = db
            job = agent_jobs.enqueue_job(
                job_key="async-2",
                group_key="g1",
                target_name="群A",
                sender="user",
                task_type="deep_free",
                provider="echo",
                payload={"max_reply_chars": 2000, "agent_mode": "raw_agent"},
            )
            job_id = job["id"]
            agent_jobs.claim_next_job(provider="echo", worker_id="w1", db_path=db)
            assert agent_jobs.complete_job(job_id, "中" * 1200) is True
            assert len(agent_jobs.get_job(job_id).get("result_text") or "") == 1200

            sent = []
            def _fake_send(job, result_text, config_path):
                sent.append(result_text)
                return {"sent": True, "reason": "ok"}
            monkeypatch.setattr(agent_worker, "_send_result_back", _fake_send)
            monkeypatch.setattr(agent_worker, "_HAS_SENDER", True)

            result = control_api._async_sender_run_once({"limit": 5})
            assert result["sent"] == 1
            assert len(sent) == 1
            assert len(sent[0]) == 1200
            stored = agent_jobs.get_job(job_id).get("result_text") or ""
            assert len(stored) == 1200
        finally:
            agent_jobs.DEFAULT_DB_PATH = orig_default
