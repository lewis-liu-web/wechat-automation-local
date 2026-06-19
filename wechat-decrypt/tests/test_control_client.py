"""Tests for wechat-decrypt/control_client.py HTTP client wrappers."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_client


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


def test_list_targets_builds_url():
    mock = _mock_urlopen({"targets": [], "count": 0})
    with patch("urllib.request.urlopen", mock):
        control_client.list_targets(kind="all")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/targets?kind=all"


def test_get_agent_job_builds_url():
    mock = _mock_urlopen({"job": {"id": 42}})
    with patch("urllib.request.urlopen", mock):
        control_client.get_agent_job(42)
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/agent/jobs/42"


def test_start_async_loop_body():
    mock = _mock_urlopen({"running": True, "action": "started"})
    with patch("urllib.request.urlopen", mock):
        control_client.start_async_loop(instance_id="hermes-a")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/agent/async-loop/start"
    sent = json.loads(req.data.decode("utf-8"))
    assert sent["instance_id"] == "hermes-a"


def test_target_info_builds_url():
    mock = _mock_urlopen({"kind": "target"})
    with patch("urllib.request.urlopen", mock):
        control_client.target_info("群A")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/targets/%E7%BE%A4A"


def test_kb_info_builds_url():
    mock = _mock_urlopen({"knowledge_base": {"id": "kb1"}})
    with patch("urllib.request.urlopen", mock):
        control_client.kb_info("kb1")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/kbs/kb1/info"


def test_clear_default_triggers_builds_url():
    mock = _mock_urlopen({"ok": True, "default_triggers": []})
    with patch("urllib.request.urlopen", mock):
        control_client.clear_default_triggers()
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/triggers/default/clear"


def test_agent_instance_on_duty_builds_url():
    mock = _mock_urlopen({"running": True, "action": "joined"})
    with patch("urllib.request.urlopen", mock):
        control_client.agent_instance_on_duty("hermes-a")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/agent/instance/hermes-a/on-duty"


def test_agent_instance_off_duty_builds_url():
    mock = _mock_urlopen({"running": False, "action": "stopping"})
    with patch("urllib.request.urlopen", mock):
        control_client.agent_instance_off_duty("hermes-a")
    req = mock.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:18590/agent/instance/hermes-a/off-duty"

