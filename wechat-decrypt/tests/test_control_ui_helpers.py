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
