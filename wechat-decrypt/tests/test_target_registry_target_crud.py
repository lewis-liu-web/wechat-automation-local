"""Tests for target_registry inspect_target / delete_target helpers."""

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import target_registry as reg


class TestInspectTarget(TestCase):
    def _tmp_config(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cand_path = Path(td.name) / "candidates.json"
        cfg = {
            "targets": [
                {
                    "name": "群A",
                    "username": "wxid_a",
                    "enabled": True,
                    "category": "user",
                    "mode": "group_assistant",
                    "triggers": ["#ask"],
                    "knowledge_bases": ["scene.a"],
                },
                {
                    "name": "群B",
                    "username": "wxid_b",
                    "enabled": False,
                    "knowledge_bases": [],
                },
            ],
            "default_triggers": ["@小助理"],
            "knowledge_bases": {
                "scene.a": {"type": "local", "enabled": True, "description": "QA KB"},
                "scene.b": {"type": "getnote", "enabled": False},
            },
        }
        reg.save_json_atomic(cfg_path, cfg)
        reg.save_json_atomic(cand_path, {"version": 1, "candidates": []})
        return td, cfg_path, cand_path

    def test_inspect_target_returns_resolved_kb(self):
        _, cfg_path, cand_path = self._tmp_config()
        info = reg.inspect_target("wxid_a", config_path=cfg_path, candidates_path=cand_path)
        assert info["kind"] == "target"
        assert info["target"]["name"] == "群A"
        assert info["effective_triggers"] == ["#ask"]
        assert len(info["knowledge_bases"]) == 1
        kb = info["knowledge_bases"][0]
        assert kb["id"] == "scene.a"
        assert kb["exists"] is True
        assert kb["type"] == "local"
        assert kb["source"] == "local_folder"
        assert kb["enabled"] is True
        assert kb["description"] == "QA KB"

    def test_inspect_target_uses_default_triggers_when_empty(self):
        _, cfg_path, cand_path = self._tmp_config()
        info = reg.inspect_target("wxid_b", config_path=cfg_path, candidates_path=cand_path)
        assert info["effective_triggers"] == ["@小助理"]
        assert info["knowledge_bases"] == []

    def test_inspect_target_resolves_missing_kb_as_not_exists(self):
        _, cfg_path, cand_path = self._tmp_config()
        cfg = reg.load_config(cfg_path)
        cfg["targets"][0]["knowledge_bases"].append("missing_kb")
        reg.save_json_atomic(cfg_path, cfg)
        info = reg.inspect_target("wxid_a", config_path=cfg_path, candidates_path=cand_path)
        missing = [kb for kb in info["knowledge_bases"] if kb["id"] == "missing_kb"][0]
        assert missing["exists"] is False
        assert missing["enabled"] is False

    def test_inspect_target_by_name(self):
        _, cfg_path, cand_path = self._tmp_config()
        info = reg.inspect_target("群A", config_path=cfg_path, candidates_path=cand_path)
        assert info["kind"] == "target"

    def test_inspect_target_returns_candidate(self):
        _, cfg_path, cand_path = self._tmp_config()
        reg.save_json_atomic(
            cand_path,
            {
                "version": 1,
                "candidates": [
                    {"name": "群C", "username": "wxid_c", "status": "pending"}
                ],
            },
        )
        info = reg.inspect_target("wxid_c", config_path=cfg_path, candidates_path=cand_path)
        assert info["kind"] == "candidate"
        assert info["effective_triggers"] == ["@小助理"]

    def test_inspect_target_missing_raises(self):
        _, cfg_path, cand_path = self._tmp_config()
        try:
            reg.inspect_target("nosuch", config_path=cfg_path, candidates_path=cand_path)
        except ValueError as e:
            assert "nosuch" in str(e)
        else:
            raise AssertionError("expected ValueError")


class TestDeleteTarget(TestCase):
    def _tmp_config(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cand_path = Path(td.name) / "candidates.json"
        cfg = {
            "targets": [
                {
                    "name": "群A",
                    "username": "wxid_a",
                    "enabled": True,
                    "knowledge_bases": ["scene.a"],
                }
            ],
            "knowledge_bases": {"scene.a": {"type": "local"}},
        }
        reg.save_json_atomic(cfg_path, cfg)
        reg.save_json_atomic(
            cand_path,
            {
                "version": 1,
                "candidates": [
                    {
                        "name": "群A",
                        "username": "wxid_a",
                        "status": "enabled",
                        "enabled_at": "2026-01-01",
                    }
                ],
            },
        )
        return td, cfg_path, cand_path

    def test_delete_target_removes_target_and_resets_candidate(self):
        _, cfg_path, cand_path = self._tmp_config()
        old_t = reg.delete_target("wxid_a", config_path=cfg_path, candidates_path=cand_path)
        assert old_t["username"] == "wxid_a"
        cfg = reg.load_config(cfg_path)
        assert len(cfg["targets"]) == 0
        cdata = reg.load_candidates(cand_path)
        c = cdata["candidates"][0]
        assert c["status"] == "pending"
        assert "enabled_at" not in c
        assert cdata["updated_at"]

    def test_delete_target_missing_raises(self):
        _, cfg_path, cand_path = self._tmp_config()
        try:
            reg.delete_target("nosuch", config_path=cfg_path, candidates_path=cand_path)
        except ValueError as e:
            assert "nosuch" in str(e)
        else:
            raise AssertionError("expected ValueError")
