"""Tests for control_api /overview/topics routes."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_api


def _route(method, path, params=None, body=None):
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, params or {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _patch_cfg(monkeypatch, cfg):
    monkeypatch.setattr(control_api.reg, "load_config", lambda _path=None, cfg=cfg: cfg)


def test_get_overview_topics_returns_state_shape(monkeypatch, tmp_path):
    cfg = {
        "targets": [{"name": "群A", "username": "t_a", "enabled": True}],
    }
    _patch_cfg(monkeypatch, cfg)

    sample = {
        "ttl_seconds": 600,
        "targets": [
            {
                "target_id": "t_a",
                "name": "群A",
                "date": "2026-07-22",
                "generated_at": 1753104000.0,
                "status": "ok",
                "topics": [
                    {"title": "话题", "summary": "摘要", "keywords": ["k1"]}
                ],
                "message_count": 3,
            }
        ],
    }
    monkeypatch.setattr(
        control_api.digest_service, "get_topics_state", lambda _cfg, **_kw: sample
    )

    status, payload = _route("GET", "/overview/topics")
    assert status == 200
    assert payload["ok"] is True
    assert payload["ttl_seconds"] == 600
    assert len(payload["targets"]) == 1
    assert payload["targets"][0]["target_id"] == "t_a"
    assert payload["targets"][0]["topics"][0]["title"] == "话题"


def test_post_overview_topics_refresh_all(monkeypatch, tmp_path):
    cfg = {
        "targets": [
            {"name": "群A", "username": "t_a", "enabled": True},
            {"name": "群B", "username": "t_b", "enabled": True},
        ],
    }
    _patch_cfg(monkeypatch, cfg)
    monkeypatch.setattr(
        control_api.digest_service,
        "refresh_now",
        lambda _cfg, target_id=None, **_kw: {"building": ([target_id] if target_id else ["t_a", "t_b"])},
    )

    status, payload = _route("POST", "/overview/topics/refresh", body={})
    assert status == 200
    assert payload["ok"] is True
    assert "building" in payload
    assert set(payload["building"]) == {"t_a", "t_b"}


def test_post_overview_topics_refresh_single_target(monkeypatch, tmp_path):
    cfg = {
        "targets": [
            {"name": "群A", "username": "t_a", "enabled": True},
            {"name": "群B", "username": "t_b", "enabled": True},
        ],
    }
    _patch_cfg(monkeypatch, cfg)
    captured = {}

    def _fake_refresh(_cfg, target_id=None, **_kw):
        captured["target_id"] = target_id
        return {"building": [target_id]}

    monkeypatch.setattr(control_api.digest_service, "refresh_now", _fake_refresh)

    status, payload = _route(
        "POST", "/overview/topics/refresh", body={"target": "t_b"}
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["building"] == ["t_b"]
    assert captured["target_id"] == "t_b"


def test_post_overview_topics_refresh_unknown_target_returns_404(monkeypatch, tmp_path):
    cfg = {"targets": [{"name": "群A", "username": "t_a", "enabled": True}]}
    _patch_cfg(monkeypatch, cfg)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("refresh_now should not be called for an unknown target")

    monkeypatch.setattr(control_api.digest_service, "refresh_now", _should_not_be_called)

    status, payload = _route(
        "POST", "/overview/topics/refresh", body={"target": "ghost_t"}
    )
    assert status == 404
    assert payload["ok"] is False
    assert "ghost_t" in payload["error"]
