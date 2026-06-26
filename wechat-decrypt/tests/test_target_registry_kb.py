#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for knowledge-base validation and single-source binding."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import target_registry as reg


class TestKbValidation(unittest.TestCase):
    def _tmp_config(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cand_path = Path(td.name) / "candidates.json"
        cfg_path.write_text("{}", encoding="utf-8")
        cand_path.write_text('{"version": 1, "candidates": []}', encoding="utf-8")
        return td, cfg_path, cand_path

    def test_add_local_kb_requires_existing_path(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.add_knowledge_base("scene.x", kb_type="local", path="/does/not/exist", config_path=cfg_path)
        self.assertIn("path does not exist", str(ctx.exception).lower())

    def test_add_local_kb_accepts_valid_path(self):
        td, cfg_path, _ = self._tmp_config()
        p = Path(td.name) / "kb"
        p.mkdir()
        out = reg.add_knowledge_base("scene.x", kb_type="local", path=str(p), config_path=cfg_path)
        self.assertEqual(out["type"], "local")
        self.assertEqual(out["path"], str(p))

    def test_add_getnote_kb_requires_knowledge_base_id(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.add_knowledge_base("online.g", kb_type="getnote", config_path=cfg_path)
        self.assertIn("knowledge-base-id", str(ctx.exception).lower())

    def test_add_hook_kb_requires_executable(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.add_knowledge_base("hook.x", kb_type="hook", config_path=cfg_path)
        self.assertIn("executable", str(ctx.exception).lower())

    def test_add_kb_rejects_duplicate_alias(self):
        td, cfg_path, _ = self._tmp_config()
        p = Path(td.name) / "kb"
        p.mkdir()
        reg.add_knowledge_base("scene.x", kb_type="local", path=str(p), config_path=cfg_path)
        with self.assertRaises(ValueError) as ctx:
            reg.add_knowledge_base("scene.x", kb_type="local", path=str(p), config_path=cfg_path)
        self.assertIn("already exists", str(ctx.exception).lower())

    def test_add_kb_replace_overwrites_existing(self):
        td, cfg_path, _ = self._tmp_config()
        p = Path(td.name) / "kb"
        p.mkdir()
        reg.add_knowledge_base("scene.x", kb_type="local", path=str(p), config_path=cfg_path)
        p2 = Path(td.name) / "kb2"
        p2.mkdir()
        out = reg.add_knowledge_base("scene.x", kb_type="local", path=str(p2), replace=True, config_path=cfg_path)
        self.assertEqual(out["path"], str(p2))


class TestSingleSourceBinding(unittest.TestCase):
    def _tmp_config(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cand_path = Path(td.name) / "candidates.json"
        p = Path(td.name) / "kb"
        p.mkdir()
        cfg = {
            "knowledge_bases": {
                "scene.a": {"type": "local", "path": str(p), "enabled": True},
                "online.g": {"type": "getnote", "knowledge_base_id": "g1", "enabled": True},
                "disabled.kb": {"type": "local", "path": str(p), "enabled": False},
            },
            "targets": [{"name": "群A", "username": "wxid_a", "knowledge_bases": ["scene.a"]}],
        }
        reg.save_json_atomic(cfg_path, cfg)
        reg.save_json_atomic(cand_path, {"version": 1, "candidates": []})
        return td, cfg_path, cand_path

    def test_bind_wiki_rejects_mixed_source_kbs(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.bind_wiki("wxid_a", ["scene.a", "online.g"], replace=True, config_path=cfg_path)
        self.assertIn("同源", str(ctx.exception))

    def test_bind_wiki_accepts_same_source_multiple_kbs(self):
        td, cfg_path, _ = self._tmp_config()
        p = Path(td.name) / "kb2"
        p.mkdir()
        cfg = reg.load_config(cfg_path)
        cfg["knowledge_bases"]["scene.b"] = {"type": "local", "path": str(p), "enabled": True}
        reg.save_json_atomic(cfg_path, cfg)
        t = reg.bind_wiki("wxid_a", ["scene.a", "scene.b"], replace=True, config_path=cfg_path)
        self.assertEqual(sorted(t["knowledge_bases"]), ["scene.a", "scene.b"])

    def test_bind_wiki_rejects_unknown_kb(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.bind_wiki("wxid_a", ["scene.unknown"], replace=True, config_path=cfg_path)
        self.assertIn("unknown knowledge base", str(ctx.exception).lower())

    def test_bind_wiki_rejects_disabled_kb(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.bind_wiki("wxid_a", ["disabled.kb"], replace=True, config_path=cfg_path)
        self.assertIn("disabled", str(ctx.exception).lower())

    def test_bind_wiki_replace_sets_single_kb(self):
        _, cfg_path, _ = self._tmp_config()
        t = reg.bind_wiki("wxid_a", ["online.g"], replace=True, config_path=cfg_path)
        self.assertEqual(t["knowledge_bases"], ["online.g"])
    def test_bind_wiki_append_to_existing_raises_on_mixed_source(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.bind_wiki("wxid_a", ["online.g"], replace=False, config_path=cfg_path)
        self.assertIn("同源", str(ctx.exception))

    def test_bind_wiki_append_to_existing_accepts_same_source(self):
        td, cfg_path, _ = self._tmp_config()
        p = Path(td.name) / "kb2"
        p.mkdir()
        cfg = reg.load_config(cfg_path)
        cfg["knowledge_bases"]["scene.b"] = {"type": "local", "path": str(p), "enabled": True}
        reg.save_json_atomic(cfg_path, cfg)
        t = reg.bind_wiki("wxid_a", ["scene.b"], replace=False, config_path=cfg_path)
        self.assertEqual(sorted(t["knowledge_bases"]), ["scene.a", "scene.b"])
    def test_enable_candidate_rejects_mixed_source_kbs(self):
        _, cfg_path, cand_path = self._tmp_config()
        reg.save_json_atomic(cand_path, {"version": 1, "candidates": [{"username": "wxid_b", "name": "群B", "status": "pending"}]})
        with self.assertRaises(ValueError) as ctx:
            reg.enable_candidate("wxid_b", knowledge_bases=["scene.a", "online.g"], config_path=cfg_path, candidates_path=cand_path)
        self.assertIn("同源", str(ctx.exception))

    def test_enable_candidate_accepts_same_source_multiple_kbs(self):
        td, cfg_path, cand_path = self._tmp_config()
        p = Path(td.name) / "kb2"
        p.mkdir()
        cfg = reg.load_config(cfg_path)
        cfg["knowledge_bases"]["scene.b"] = {"type": "local", "path": str(p), "enabled": True}
        reg.save_json_atomic(cfg_path, cfg)
        reg.save_json_atomic(cand_path, {"version": 1, "candidates": [{"username": "wxid_b", "name": "群B", "status": "pending"}]})
        t = reg.enable_candidate("wxid_b", knowledge_bases=["scene.a", "scene.b"], config_path=cfg_path, candidates_path=cand_path)
        self.assertEqual(sorted(t["knowledge_bases"]), ["scene.a", "scene.b"])

class TestDisableLocalKb(unittest.TestCase):
    def test_disable_local_kb_skips_local_scene_retrieval(self):
        import tempfile
        from reply_engine import retrieve_knowledge
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root/'core').mkdir()
        (root/'core'/'reply_boundary.md').write_text('边界 不能承诺', encoding='utf-8')
        (root/'scenes'/'a').mkdir(parents=True)
        (root/'scenes'/'a'/'faq.md').write_text('工作号真实号用于号码认证场景', encoding='utf-8')
        cfg={'wiki_dir': str(root), 'knowledge_bases': {'scene.a': {'type':'local','path':'scenes/a'}}, 'reply_engine': {'disable_local_kb': True}}
        hits=retrieve_knowledge('工作号真实号', cfg, {'knowledge_bases':['scene.a']})
        # Core boundaries still apply; scene local KB is skipped.
        self.assertTrue(any('core' in h.label for h in hits))
        self.assertFalse(any('scene.a' in h.label for h in hits))
    def test_diagnose_local_kb_reports_index_state(self):
        import tempfile
        from reply_engine import diagnose_local_kb, _ensure_local_kb_fts
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        kb_dir = root/'kb'
        kb_dir.mkdir()
        (kb_dir/'faq.md').write_text('工作号真实号用于号码认证场景', encoding='utf-8')
        spec = {'id': 'scene.a', 'type': 'local', 'path': str(kb_dir), 'scope': 'scene'}
        con = _ensure_local_kb_fts(root, spec)
        if con:
            con.close()
        info = diagnose_local_kb(root, spec, query='工作号真实号')
        self.assertTrue(info.get('index_exists'))
        self.assertEqual(info.get('doc_count'), 1)
        self.assertIn('工作号真实号', info.get('sample_fts_query', ''))
        self.assertEqual(len(info.get('sample_hits', [])), 1)

    def test_rebuild_kb_index_removes_existing_index(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name)/'targets.json'
        cfg_path.write_text('{}', encoding='utf-8')
        kb_dir = Path(td.name)/'kb'
        kb_dir.mkdir()
        db_path = kb_dir/'.kb_index.sqlite'
        db_path.write_text('old index', encoding='utf-8')
        cfg = {
            'knowledge_bases': {
                'scene.a': {'type': 'local', 'path': str(kb_dir), 'enabled': True},
            },
        }
        reg.save_json_atomic(cfg_path, cfg)
        out = reg.rebuild_kb_index('scene.a', config_path=cfg_path)
        self.assertTrue(out.get('index_removed'))
        self.assertFalse(db_path.exists())



class TestWikiKbScan(unittest.TestCase):
    def test_list_knowledge_bases_includes_wiki_dirs(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cand_path = Path(td.name) / "candidates.json"
        cfg_path.write_text(
            json.dumps({
                "knowledge_bases": {
                    "scene.existing": {"type": "local", "path": "scenes/existing"}
                }
            }),
            encoding="utf-8",
        )
        cand_path.write_text('{"version": 1, "candidates": []}', encoding="utf-8")
        wiki = Path(td.name) / "wiki"
        (wiki / "desktop_pdf").mkdir(parents=True)
        (wiki / "scenes" / "workdocs").mkdir(parents=True)
        # create a file so the folder is non-empty (not required by impl, but clearer)
        (wiki / "desktop_pdf" / "a.md").write_text("x", encoding="utf-8")
        (wiki / "scenes" / "workdocs" / "b.md").write_text("y", encoding="utf-8")

        rows = reg.list_knowledge_bases(config_path=cfg_path)
        ids = {r["id"] for r in rows}
        self.assertIn("desktop_pdf", ids)
        self.assertIn("scene.workdocs", ids)
        self.assertIn("scene.existing", ids)

        desktop = next(r for r in rows if r["id"] == "desktop_pdf")
        self.assertEqual(desktop["path"], "desktop_pdf")
        self.assertEqual(desktop["source"], "local_folder")

        workdocs = next(r for r in rows if r["id"] == "scene.workdocs")
        self.assertEqual(workdocs["path"], "scenes/workdocs")
if __name__ == "__main__":
    unittest.main()
