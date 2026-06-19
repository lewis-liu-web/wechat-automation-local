"""Tests for wechat-control-ui/api.py HTTP client wrappers."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "wechat-control-ui"))

import api


def _mock_urlopen(body_dict, status=200):
    """Return a MagicMock that patches urllib.request.urlopen and records calls."""
    mock = MagicMock()

    def _side_effect(req, **kw):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(body_dict).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    mock.side_effect = _side_effect
    return mock


def test_agent_provider_health_passes_instance_id():
    mock = _mock_urlopen({"health": {"ok": True}})
    with patch("urllib.request.urlopen", mock):
        api.agent_provider_health(provider="hermes", instance_id="hermes-worker1")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/agent/provider/health?provider=hermes&instance_id=hermes-worker1"


def test_create_agent_test_job_includes_target_fields():
    mock = _mock_urlopen({"job": {"id": 1}})
    with patch("urllib.request.urlopen", mock):
        api.create_agent_test_job(
            "prompt",
            provider="hermes",
            group_key="g",
            sender="s",
            target={"name": "群A", "username": "wxid_a", "table": "Msg_a"},
            mention_name="@小助理",
            priority=3,
            task_type="deep_free",
        )
    req = mock.call_args[0][0]
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["target_username"] == "wxid_a"
    assert sent["target_name"] == "群A"
    assert sent["target_table"] == "Msg_a"
    assert sent["mention_name"] == "@小助理"
    assert sent["priority"] == 3
    assert sent["task_type"] == "deep_free"


def test_diagnose_kb_calls_diagnose_endpoint():
    mock = _mock_urlopen({"ok": True})
    with patch("urllib.request.urlopen", mock):
        api.diagnose_kb("kb1", "query text")
    req = mock.call_args[0][0]
    assert req.full_url.startswith("http://127.0.0.1:18590/kbs/kb1/diagnose")
    assert "?q=query%20text" in req.full_url


def test_set_target_mode_calls_mode_endpoint():
    mock = _mock_urlopen({"target": {"mode": "customer_service"}})
    with patch("urllib.request.urlopen", mock):
        api.set_target_mode("群A", "customer_service")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/targets/%E7%BE%A4A/mode"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["mode"] == "customer_service"


def test_get_agent_job_calls_job_detail_endpoint():
    mock = _mock_urlopen({"job": {"id": 7}})
    with patch("urllib.request.urlopen", mock):
        api.get_agent_job(7)
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/agent/jobs/7"

def test_api_shim_forwards_to_control_client():
    """The compatibility wrapper must actually delegate to control_client._request."""
    mock = _mock_urlopen({"targets": [{"name": "群A"}], "count": 1})
    with patch("control_client._request", lambda *a, **kw: {"targets": [{"name": "群A"}]}):
        result = api.list_targets(kind="all")
    assert result == [{"name": "群A"}]


def test_api_shim_forwards_get_agent_job_to_control_client():
    mock = _mock_urlopen({"job": {"id": 9}})
    with patch("control_client._request", lambda *a, **kw: {"job": {"id": 9}}):
        result = api.get_agent_job(9)
    assert result == {"id": 9}

