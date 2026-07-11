"""Tests for CLI-oriented control_api routes added by the CLI completion plan."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_api
import target_registry as reg


def _route(method, path, params=None, body=None):
    """Dispatch a control_api route directly without starting a socket."""
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, params or {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _tmp_config():
    td = tempfile.TemporaryDirectory()
    cfg_path = Path(td.name) / "targets.json"
    cand_path = Path(td.name) / "candidates.json"
    cfg = {
        "targets": [
            {
                "name": "群A",
                "username": "wxid_a",
                "enabled": True,
                "triggers": ["#ask"],
                "knowledge_bases": [],
            }
        ],
        "default_triggers": ["@小助理"],
        "knowledge_bases": {},
    }
    reg.save_json_atomic(cfg_path, cfg)
    reg.save_json_atomic(cand_path, {"version": 1, "candidates": []})
    return td, cfg_path, cand_path


def _fake_running_thread():
    return type("T", (), {"is_alive": lambda self: True, "join": lambda *a, **k: None})()


class TestInspectTargetRoute:
    def test_get_target_returns_kind_target(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(control_api.reg, "CANDIDATES_PATH", str(cand_path))
        status, resp = _route("GET", "/targets/wxid_a")
        assert status == 200
        assert resp.get("ok") is True
        assert resp.get("kind") == "target"
        assert resp["target"]["name"] == "群A"
        assert resp["effective_triggers"] == ["#ask"]

    def test_get_target_missing_returns_404(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(control_api.reg, "CANDIDATES_PATH", str(cand_path))
        status, resp = _route("GET", "/targets/nosuch")
        assert status == 404
        assert resp.get("ok") is False


class TestKbInfoRoute:
    def test_get_kb_info_missing_returns_404(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        status, resp = _route("GET", "/kbs/missing/info")
        assert status == 404
        assert resp.get("ok") is False


class TestAgentOnOffDutyRoute:
    def setup_method(self):
        control_api._ASYNC_LOOP_STATE.clear()
        control_api._ASYNC_LOOP_STOP.set()

    def test_on_duty_starts_loop_for_instance(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        calls = []
        def _fake_start(body):
            calls.append(body)
            return {"action": "started", "running": True}
        monkeypatch.setattr(control_api, "_async_loop_start", _fake_start)
        status, resp = _route("POST", "/agent/instance/hermes-a/on-duty")
        assert status == 200
        assert resp.get("ok") is True
        assert resp.get("action") == "started"
        assert calls == [{"instance_id": "hermes-a"}]

    def test_off_duty_removes_instance_from_loop_state(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(control_api, "_ASYNC_LOOP_THREAD", _fake_running_thread())
        control_api._ASYNC_LOOP_STATE.update({
            "config": {
                "instance_ids": ["hermes-a", "hermes-b"],
                "instance_id": "hermes-a,hermes-b",
                "max_global_dispatching": 2,
            }
        })
        control_api._ASYNC_LOOP_STOP.clear()
        status, resp = _route("POST", "/agent/instance/hermes-a/off-duty")
        assert status == 200
        assert resp.get("ok") is True
        assert resp.get("action") == "removed"
        assert resp.get("instance_id") == "hermes-a"
        assert resp["config"]["instance_ids"] == ["hermes-b"]
        assert resp["config"]["instance_id"] == "hermes-b"

    def test_off_duty_last_instance_stops_loop(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(control_api, "_ASYNC_LOOP_THREAD", _fake_running_thread())
        control_api._ASYNC_LOOP_STATE.update({
            "config": {"instance_ids": ["hermes-a"], "instance_id": "hermes-a"}
        })
        control_api._ASYNC_LOOP_STOP.clear()
        status, resp = _route("POST", "/agent/instance/hermes-a/off-duty")
        assert status == 200
        assert resp.get("ok") is True
        assert resp.get("action") == "stopping"

    def test_off_duty_not_on_duty(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(control_api, "_ASYNC_LOOP_THREAD", _fake_running_thread())
        control_api._ASYNC_LOOP_STATE.update({
            "config": {"instance_ids": ["hermes-a"], "instance_id": "hermes-a"}
        })
        control_api._ASYNC_LOOP_STOP.clear()
        status, resp = _route("POST", "/agent/instance/hermes-z/off-duty")
        assert status == 200
        assert resp.get("ok") is True
        assert resp.get("action") == "not_on_duty"


class TestLeannRoutes:
    def test_list_leann_indexes_route(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        called = []
        def _fake_list(config_path):
            called.append(config_path)
            return {"indexes": [{"name": "idx_a"}], "error": "", "cwd": "/tmp"}
        monkeypatch.setattr(control_api.reg, "list_leann_indexes", _fake_list)
        status, resp = _route("GET", "/kbs/leann/indexes")
        assert status == 200
        assert resp.get("ok") is True
        assert resp["indexes"] == [{"name": "idx_a"}]
        assert called

    def test_leann_index_info_route(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        called = []
        def _fake_info(name, config_path):
            called.append((name, config_path))
            return {"name": name, "exists": True}
        monkeypatch.setattr(control_api.reg, "get_leann_index_info", _fake_info)
        status, resp = _route("GET", "/kbs/leann/indexes/my_idx/info")
        assert status == 200
        assert resp["exists"] is True
        assert called[0][0] == "my_idx"

    def test_leann_build_requires_docs_dir(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        cfg = reg.load_config(str(cfg_path))
        cfg["knowledge_bases"] = {"wk": {"type": "leann", "index_name": "work", "enabled": True}}
        reg.save_json_atomic(str(cfg_path), cfg)
        status, resp = _route("POST", "/kbs/wk/leann/build", body={"force": True})
        assert status == 400
        assert "docs_dir" in resp.get("error", "").lower()

    def test_save_leann_kb_can_replace_docs_dir(self, monkeypatch):
        td, cfg_path, cand_path = _tmp_config()
        monkeypatch.setattr(control_api.reg, "CONFIG_PATH", str(cfg_path))
        docs = Path(td.name) / "docs"
        docs.mkdir()
        cfg = reg.load_config(str(cfg_path))
        cfg["knowledge_bases"] = {"wk": {"type": "leann", "index_name": "work", "enabled": True}}
        reg.save_json_atomic(str(cfg_path), cfg)
        status, resp = _route("POST", "/kbs", body={
            "id": "wk",
            "type": "leann",
            "knowledge_base_id": "work",
            "docs_dir": str(docs),
            "replace": True,
        })
        assert status == 200
        assert resp.get("ok") is True
        updated = reg.load_config(str(cfg_path))
        assert updated["knowledge_bases"]["wk"]["docs_dir"] == str(docs)
