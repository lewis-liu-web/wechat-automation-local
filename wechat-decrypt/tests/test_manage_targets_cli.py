"""Tests for new manage_targets CLI commands (no external processes)."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manage_targets as mt
import target_registry as reg


# Capture original registry functions before monkeypatching.
_ORIGINSPECT_TARGET = reg.inspect_target
_ORIGDELETE_TARGET = reg.delete_target
_ORIGSET_TARGET_FIELD = reg.set_target_field
_ORIGSET_TARGET_MODE_BUNDLE = reg.set_target_mode_bundle
_ORIGSET_CATEGORY = reg.set_category
_ORIGSET_DEFAULT_TRIGGERS = reg.set_default_triggers
_ORIGSET_KB_ENABLED = reg.set_knowledge_base_enabled
_ORIGDELETE_KB = reg.delete_knowledge_base


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
                "mode": "group_assistant",
                "category": "user",
            }
        ],
        "default_triggers": ["@小助理"],
        "knowledge_bases": {
            "kb1": {
                "type": "getnote",
                "knowledge_base_id": "kid1",
                "enabled": False,
                "description": "测试 KB",
            }
        },
    }
    reg.save_json_atomic(cfg_path, cfg)
    reg.save_json_atomic(cand_path, {"version": 1, "candidates": [
        {"name": "群A", "username": "wxid_a", "status": "enabled"},
        {"name": "群B", "username": "wxid_b", "status": "pending"},
    ]})
    return td, cfg_path, cand_path


def _pathify(monkeypatch, cfg_path, cand_path):
    """Patch manage_targets.reg functions to use explicit temp paths."""
    def inspect(key, **kw):
        return _ORIGINSPECT_TARGET(key, config_path=str(cfg_path), candidates_path=str(cand_path), **kw)
    def delete(key, **kw):
        return _ORIGDELETE_TARGET(key, config_path=str(cfg_path), candidates_path=str(cand_path), **kw)
    def field(key, f, value, **kw):
        return _ORIGSET_TARGET_FIELD(key, f, value, config_path=str(cfg_path), **kw)
    def mode(key, m, **kw):
        return _ORIGSET_TARGET_MODE_BUNDLE(key, m, config_path=str(cfg_path), **kw)
    def category(key, c, **kw):
        return _ORIGSET_CATEGORY(key, c, config_path=str(cfg_path), **kw)
    def default(words, **kw):
        return _ORIGSET_DEFAULT_TRIGGERS(words, config_path=str(cfg_path), **kw)
    def kb_enabled(kb_id, enabled, **kw):
        return _ORIGSET_KB_ENABLED(kb_id, enabled, config_path=str(cfg_path), **kw)
    def kb_delete(kb_id, **kw):
        return _ORIGDELETE_KB(kb_id, config_path=str(cfg_path), **kw)

    for name, fn in [
        ("inspect_target", inspect),
        ("delete_target", delete),
        ("set_target_field", field),
        ("set_target_mode_bundle", mode),
        ("set_category", category),
        ("set_default_triggers", default),
        ("set_knowledge_base_enabled", kb_enabled),
        ("delete_knowledge_base", kb_delete),
    ]:
        monkeypatch.setattr(mt.reg, name, fn)


class TestTargetShow:
    def test_target_show_json(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["target-show", "wxid_a", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert data["kind"] == "target"
        assert data["target"]["name"] == "群A"
        assert data["effective_triggers"] == ["#ask"]


class TestTargetDelete:
    def test_target_delete_requires_yes(self, monkeypatch, capsys):
        _tmp_config()
        monkeypatch.setattr(mt.reg, "delete_target", lambda key, **kw: None)
        code = mt.main(["target-delete", "wxid_a"])
        captured = capsys.readouterr()
        assert code == 2
        assert "--yes" in captured.out or "--yes" in captured.err

    def test_target_delete_with_yes(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["target-delete", "wxid_a", "--yes", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert data["ok"] is True
        cfg = reg.load_config(str(cfg_path))
        assert not any(t["username"] == "wxid_a" for t in cfg["targets"])


class TestTargetField:
    def test_target_field_json(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["target-field", "wxid_a", "note", "hello", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert data["action"] == "set"
        cfg = reg.load_config(str(cfg_path))
        assert cfg["targets"][0].get("note") == "hello"

    def test_target_field_json_value(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["target-field", "wxid_a", "tags", '["a", "b"]', "--json"])
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert data["action"] == "set"
        cfg = reg.load_config(str(cfg_path))
        assert cfg["targets"][0].get("tags") == ["a", "b"]


class TestTargetModeAndCategory:
    def test_target_mode(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["target-mode", "wxid_a", "customer_service", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        cfg = reg.load_config(str(cfg_path))
        assert cfg["targets"][0]["mode"] == "customer_service"

    def test_target_category(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["target-category", "wxid_a", "admin", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        cfg = reg.load_config(str(cfg_path))
        assert cfg["targets"][0]["category"] == "admin"


class TestDefaultTriggers:
    def test_trigger_default_replace_and_clear(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["trigger-default-replace", "#bot", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        cfg = reg.load_config(str(cfg_path))
        assert cfg["default_triggers"] == ["#bot"]

        code = mt.main(["trigger-default-clear", "--json"])
        assert code == 0
        cfg = reg.load_config(str(cfg_path))
        assert cfg["default_triggers"] == []


class TestKbEnableDelete:
    def test_kb_enable_disable(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["kb-enable", "kb1", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        cfg = reg.load_config(str(cfg_path))
        assert cfg["knowledge_bases"]["kb1"]["enabled"] is True

        code = mt.main(["kb-disable", "kb1", "--json"])
        assert code == 0
        cfg = reg.load_config(str(cfg_path))
        assert cfg["knowledge_bases"]["kb1"]["enabled"] is False

    def test_kb_delete_requires_yes(self, monkeypatch, capsys):
        _tmp_config()
        monkeypatch.setattr(mt.reg, "delete_knowledge_base", lambda kb_id, **kw: None)
        code = mt.main(["kb-delete", "kb1", "--json"])
        captured = capsys.readouterr()
        assert code == 2

    def test_kb_delete_with_yes(self, monkeypatch, capsys):
        td, cfg_path, cand_path = _tmp_config()
        _pathify(monkeypatch, cfg_path, cand_path)
        code = mt.main(["kb-delete", "kb1", "--yes", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert data["ok"] is True
        cfg = reg.load_config(str(cfg_path))
        assert "kb1" not in cfg["knowledge_bases"]


class TestDecryptStatus:
    def test_decrypt_status_missing_config(self, monkeypatch, capsys, tmp_path):
        import config as _config
        missing_path = str(tmp_path / "no_such_config.json")
        monkeypatch.setattr(_config, "CONFIG_FILE", missing_path)
        code = mt.main(["decrypt-status", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert data["ok"] is False
        assert data["reason"] == "config_missing"

    def test_decrypt_status_present_config_no_keys(self, monkeypatch, capsys, tmp_path):
        import config as _config
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "db_dir": str(tmp_path / "db"),
            "decrypted_dir": str(tmp_path / "dec"),
            "keys_file": str(tmp_path / "keys.json"),
        }), encoding="utf-8")
        (tmp_path / "dec").mkdir()
        (tmp_path / "dec" / "a.db").write_text("data", encoding="utf-8")
        monkeypatch.setattr(_config, "CONFIG_FILE", str(cfg_path))
        def _load(*a, **k):
            return {
                "db_dir": str(tmp_path / "db"),
                "decrypted_dir": str(tmp_path / "dec"),
                "keys_file": str(tmp_path / "keys.json"),
            }
        monkeypatch.setattr(_config, "load_config", _load)
        code = mt.main(["decrypt-status", "--json"])
        captured = capsys.readouterr()
        assert code == 0
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert data["keys_count"] == 0
        assert data["decrypted_db_count"] == 1
        assert data["decrypted_total_bytes"] == 4
        assert "enc_key" not in captured.out
