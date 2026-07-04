"""Tests for pure UI helper functions in wechat-control-ui/app.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "wechat-control-ui"))

import app


def test_label_passthrough_for_unknown_value():
    assert app._label({"known": "已知"}, "unknown") == "unknown"


def test_job_status_label_maps_known_status():
    assert app._job_status_label("queued") == "排队中"
    assert app._job_status_label("sent") == "已发送"
    assert app._job_status_label("custom_state") == "custom_state"


def test_send_status_label_maps_known_status():
    assert app._send_status_label("sent") == "已发送"
    assert app._send_status_label("pending") == "待发送"
    assert app._send_status_label("other") == "other"


def test_provider_label_maps_known_providers():
    assert app._provider_label("echo") == "安全测试（不调用真实模型）"
    assert app._provider_label("hermes") == "Hermes 本地 Agent"
    assert app._provider_label("unknown") == "unknown"


class _MockSessionState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


def test_load_data_preserves_existing_cache_on_api_error(monkeypatch):
    """Regression: binding KB calls _clear_data_cache + st.rerun(); if a
    subsequent API call fails, the existing targets/kbs cache must not
    be wiped to empty lists.
    """
    from api import ControlAPIError

    state = _MockSessionState({"base_url": "http://localhost:18590"})
    monkeypatch.setattr(app.st, "session_state", state)

    def _raise(*args, **kwargs):
        raise ControlAPIError("unreachable")

    monkeypatch.setattr(app, "health", _raise)
    monkeypatch.setattr(app, "status", _raise)
    monkeypatch.setattr(app, "events_stats", _raise)
    monkeypatch.setattr(app, "events_recent", _raise)
    monkeypatch.setattr(app, "list_targets", _raise)
    monkeypatch.setattr(app, "list_kbs", _raise)
    monkeypatch.setattr(app, "get_default_triggers", _raise)

    existing_targets = [{"name": "bot群聊测试"}]
    existing_kbs = [{"id": "bus_index"}]
    state["targets_all"] = existing_targets
    state["kbs_result"] = existing_kbs

    app._load_data()

    assert state["targets_all"] is existing_targets
    assert state["kbs_result"] is existing_kbs
    assert state["data_loaded"] is True
