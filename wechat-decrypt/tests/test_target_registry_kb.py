#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for knowledge-base validation and single-source binding."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


class TestLeannKb(unittest.TestCase):
    def _tmp_config(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cand_path = Path(td.name) / "candidates.json"
        cfg_path.write_text("{}", encoding="utf-8")
        cand_path.write_text('{"version": 1, "candidates": []}', encoding="utf-8")
        return td, cfg_path, cand_path

    def test_add_leann_kb_requires_index_name(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.add_knowledge_base("leann.x", kb_type="leann", config_path=cfg_path)
        self.assertIn("knowledge-base-id", str(ctx.exception).lower())

    def test_add_leann_kb_accepts_index_name(self):
        _, cfg_path, _ = self._tmp_config()
        out = reg.add_knowledge_base("leann.x", kb_type="leann", knowledge_base_id="idx_x", config_path=cfg_path)
        self.assertEqual(out["type"], "leann")
        self.assertEqual(out["index_name"], "idx_x")

    def test_validate_kb_rejects_leann_without_index_name(self):
        _, cfg_path, _ = self._tmp_config()
        with self.assertRaises(ValueError) as ctx:
            reg.add_knowledge_base("leann.x", kb_type="leann", knowledge_base_id="", config_path=cfg_path)
        self.assertIn("index_name", str(ctx.exception).lower())

    def test_bind_wiki_accepts_multiple_leann_indexes(self):
        _, cfg_path, _ = self._tmp_config()
        cfg = reg.load_config(cfg_path)
        cfg["knowledge_bases"] = {
            "leann.a": {"type": "leann", "index_name": "idx_a", "enabled": True},
            "leann.b": {"type": "leann", "index_name": "idx_b", "enabled": True},
        }
        cfg["targets"] = [{"name": "群A", "username": "wxid_a", "knowledge_bases": ["leann.a"]}]
        reg.save_json_atomic(cfg_path, cfg)
        t = reg.bind_wiki("wxid_a", ["leann.a", "leann.b"], replace=True, config_path=cfg_path)
        self.assertEqual(sorted(t["knowledge_bases"]), ["leann.a", "leann.b"])

    def test_list_knowledge_bases_includes_index_name(self):
        _, cfg_path, _ = self._tmp_config()
        reg.add_knowledge_base("leann.x", kb_type="leann", knowledge_base_id="idx_x", description="desc", config_path=cfg_path)
        rows = reg.list_knowledge_bases(config_path=cfg_path)
        row = next((r for r in rows if r["id"] == "leann.x"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["type"], "leann")
        self.assertEqual(row["index_name"], "idx_x")

    def test_search_kb_dispatches_to_leann_search(self):
        _, cfg_path, _ = self._tmp_config()
        cfg = reg.load_config(cfg_path)
        cfg["knowledge_bases"] = {"leann.x": {"type": "leann", "index_name": "idx_x", "enabled": True}}
        reg.save_json_atomic(cfg_path, cfg)
        with mock.patch("target_registry.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout='{"results": [{"title": "T", "content": "C"}]}', stderr="")
            res = reg.search_kb("leann.x", "query", limit=3, config_path=cfg_path)
        self.assertEqual(res.get("matched_files"), 1)
        self.assertEqual(res["hits"][0]["rel_path"], "T")


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


class TestWikiKbScanActions(unittest.TestCase):
    """Regression tests for read-only actions on auto-discovered wiki KBs.

    The wiki scanner exposes ``desktop_pdf`` and ``scene.workdocs`` even when
    they are NOT registered in ``cfg["knowledge_bases"]``.  These actions must
    succeed for scanned ids:
      * open_kb_dir / open_kb_obsidian (resolve path)
      * rebuild_kb_index (delete stale .kb_index.sqlite)
      * diagnose_local_kb / search_local_kb / import_kb_file (read & write)
    while ``set_knowledge_base_enabled`` and ``delete_knowledge_base`` must
    refuse to mutate a KB that has no configured alias.
    """

    def _setup_wiki(self):
        """Build a temp dir containing a wiki/ subtree with two scanned KBs.

        Returns (td, cfg_path, wiki_root, desktop_dir, workdocs_dir).  No
        entries are written to ``cfg["knowledge_bases"]`` so every id below
        resolves only through the wiki scanner.
        """
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cfg_path.write_text(json.dumps({}), encoding="utf-8")

        wiki = Path(td.name) / "wiki"
        desktop_dir = wiki / "desktop_pdf"
        desktop_dir.mkdir(parents=True)
        (desktop_dir / "intro.md").write_text(
            "欢迎语 工作号 用于号码认证场景\n", encoding="utf-8",
        )

        scenes = wiki / "scenes"
        workdocs_dir = scenes / "workdocs"
        workdocs_dir.mkdir(parents=True)
        (workdocs_dir / "guide.md").write_text(
            "工作号真实号用于号码认证场景的常见问题\n", encoding="utf-8",
        )
        return td, cfg_path, wiki, desktop_dir, workdocs_dir

    def test_scanned_rows_mark_managed_false(self):
        _, cfg_path, _, _, _ = self._setup_wiki()
        rows = reg.list_knowledge_bases(config_path=cfg_path)
        by_id = {r["id"]: r for r in rows}
        self.assertIn("desktop_pdf", by_id)
        self.assertIn("scene.workdocs", by_id)
        self.assertFalse(by_id["desktop_pdf"]["managed"])
        self.assertFalse(by_id["scene.workdocs"]["managed"])
        # Configured KBs (none here) would still be marked managed=True.
        for r in rows:
            if r["id"] in ("desktop_pdf", "scene.workdocs"):
                self.assertFalse(r["managed"])

    def test_open_kb_dir_resolves_scanned_id(self):
        _, cfg_path, _, desktop_dir, _ = self._setup_wiki()
        called = {}
        real_popen = reg.subprocess.Popen

        def fake_popen(args, *a, **kw):
            called.setdefault("args", []).append(list(args))
            class _Dummy:
                pass
            return _Dummy()

        reg.subprocess.Popen = fake_popen
        try:
            result = reg.open_kb_dir("desktop_pdf", config_path=cfg_path)
        finally:
            reg.subprocess.Popen = real_popen

        self.assertEqual(Path(result).resolve(), desktop_dir.resolve())
        self.assertEqual(len(called.get("args", [])), 1)
        cmd = called["args"][0]
        self.assertEqual(Path(cmd[-1]).resolve(), desktop_dir.resolve())

    def test_open_kb_obsidian_resolves_scanned_id(self):
        _, cfg_path, _, _, workdocs_dir = self._setup_wiki()
        called = {}
        real_popen = reg.subprocess.Popen

        def fake_popen(args, *a, **kw):
            called.setdefault("args", []).append(list(args))
            class _Dummy:
                pass
            return _Dummy()

        reg.subprocess.Popen = fake_popen
        try:
            out = reg.open_kb_obsidian("scene.workdocs", config_path=cfg_path)
        finally:
            reg.subprocess.Popen = real_popen

        self.assertEqual(Path(out["path"]).resolve(), workdocs_dir.resolve())
        self.assertTrue(out.get("executable"))
        self.assertEqual(len(called.get("args", [])), 1)
        cmd = called["args"][0]
        self.assertEqual(Path(cmd[-1]).resolve(), workdocs_dir.resolve())

    def test_rebuild_kb_index_removes_stale_index_on_scanned_id(self):
        _, cfg_path, _, _, workdocs_dir = self._setup_wiki()
        db_path = workdocs_dir / ".kb_index.sqlite"
        db_path.write_text("old index blob", encoding="utf-8")
        self.assertTrue(db_path.exists())
        out = reg.rebuild_kb_index("scene.workdocs", config_path=cfg_path)
        self.assertTrue(out["index_removed"])
        self.assertFalse(db_path.exists())
        self.assertEqual(out["id"], "scene.workdocs")
        self.assertGreaterEqual(out["doc_count"], 1)

    def test_diagnose_local_kb_works_on_scanned_id(self):
        _, cfg_path, _, _, workdocs_dir = self._setup_wiki()
        info = reg.diagnose_local_kb(
            "scene.workdocs", query="工作号", config_path=cfg_path,
        )
        # The implementation may or may not have produced a fresh FTS index,
        # but the keys MUST be present and the path must point at the scanned
        # directory.  Doc count is non-negative.
        self.assertIn("index_path", info)
        self.assertEqual(Path(info["index_path"]).parent.resolve(), workdocs_dir.resolve())
        self.assertIn("doc_count", info)
        self.assertIsInstance(info["doc_count"], int)
        self.assertGreaterEqual(info["doc_count"], 0)

    def test_search_local_kb_works_on_scanned_id(self):
        _, cfg_path, _, _, _ = self._setup_wiki()
        result = reg.search_local_kb(
            "scene.workdocs", "工作号", limit=5, config_path=cfg_path,
        )
        self.assertGreaterEqual(result.get("total_files", 0), 1)
        hits = result.get("hits") or []
        self.assertTrue(hits, "expected at least one hit for '工作号'")
        # Token must appear in the hit snippet of the matching file.
        self.assertTrue(
            any("工作号" in (h.get("snippet") or "") for h in hits),
            "expected query token to appear in at least one hit snippet",
        )

    def test_import_kb_file_works_on_scanned_id(self):
        _, cfg_path, _, desktop_dir, _ = self._setup_wiki()
        # Prepare a source markdown outside the KB dir to import.
        src_dir = desktop_dir.parent.parent / "import_src"
        src_dir.mkdir(exist_ok=True)
        src_file = src_dir / "imported.md"
        src_file.write_text("# imported\n\n用于号码认证的导入条目\n", encoding="utf-8")
        try:
            out = reg.import_kb_file(
                "desktop_pdf", str(src_file), config_path=cfg_path,
            )
            copied = out.get("copied") or []
            self.assertTrue(copied, "expected at least one copied entry, got %r" % (out,))
            # The copied path must live inside the scanned KB dir.
            copied_path = Path(copied[0]).resolve()
            self.assertTrue(
                str(copied_path).startswith(str(desktop_dir.resolve())),
                "expected copy under %s, got %s" % (desktop_dir.resolve(), copied_path),
            )
            self.assertTrue(copied_path.exists())
        finally:
            try:
                src_file.unlink()
            except OSError:
                pass

    def test_set_knowledge_base_enabled_rejects_scanned_id(self):
        _, cfg_path, _, _, _ = self._setup_wiki()
        with self.assertRaises(ValueError) as ctx:
            reg.set_knowledge_base_enabled("scene.workdocs", True, config_path=cfg_path)
        # The KB map MUST remain untouched for a scanned id.
        cfg_after = reg.load_config(cfg_path)
        self.assertNotIn("scene.workdocs", cfg_after.get("knowledge_bases") or {})
        self.assertIn("未在配置中注册", str(ctx.exception))

    def test_delete_knowledge_base_rejects_scanned_id(self):
        _, cfg_path, _, _, workdocs_dir = self._setup_wiki()
        # Capture mtime so we can prove the directory is not deleted either.
        mtime_before = workdocs_dir.stat().st_mtime
        with self.assertRaises(ValueError) as ctx:
            reg.delete_knowledge_base(
                "scene.workdocs", remove_files=True, config_path=cfg_path,
            )
        cfg_after = reg.load_config(cfg_path)
        self.assertNotIn("scene.workdocs", cfg_after.get("knowledge_bases") or {})
        self.assertTrue(workdocs_dir.exists(), "scanned dir must not be removed")
        self.assertEqual(workdocs_dir.stat().st_mtime, mtime_before)
        self.assertIn("未在配置中注册", str(ctx.exception))

    def test_configured_kb_still_managed_after_scan(self):
        """Adding an explicit alias for a scanned folder must keep managed=True."""
        td, cfg_path, _, desktop_dir, _ = self._setup_wiki()
        cfg = reg.load_config(cfg_path)
        cfg["knowledge_bases"] = {
            "scene.desktop": {"type": "local", "path": str(desktop_dir), "enabled": True},
        }
        reg.save_json_atomic(cfg_path, cfg)
        rows = reg.list_knowledge_bases(config_path=cfg_path)
        by_id = {r["id"]: r for r in rows}
        # Configured alias stays managed.
        self.assertIn("scene.desktop", by_id)
        self.assertTrue(by_id["scene.desktop"]["managed"])
        # Unregistered scanned id remains unmanaged.
        self.assertIn("scene.workdocs", by_id)
        self.assertFalse(by_id["scene.workdocs"]["managed"])


class TestBindWikiAutoRegister(unittest.TestCase):
    """Auto-register scanned wiki KBs when binding them to a target."""

    def _setup_wiki_with_target(self):
        """Build a temp dir with a wiki/scenes/workdocs folder and a target."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cfg_path = Path(td.name) / "targets.json"
        cfg = {
            "knowledge_bases": {},
            "targets": [{
                "name": "群A",
                "username": "wxid_a",
                "knowledge_bases": [],
            }],
        }
        reg.save_json_atomic(cfg_path, cfg)
        cand_path = Path(td.name) / "candidates.json"
        reg.save_json_atomic(cand_path, {"version": 1, "candidates": []})

        wiki = Path(td.name) / "wiki"
        scenes = wiki / "scenes"
        workdocs_dir = scenes / "workdocs"
        workdocs_dir.mkdir(parents=True)
        (workdocs_dir / "guide.md").write_text(
            "工作号真实号用于号码认证场景的常见问题\n", encoding="utf-8",
        )
        return td, cfg_path, cand_path, workdocs_dir

    def test_bind_wiki_auto_registers_scanned_kb(self):
        _, cfg_path, _, workdocs_dir = self._setup_wiki_with_target()
        t = reg.bind_wiki(
            "wxid_a", ["scene.workdocs"], replace=True, config_path=cfg_path,
        )
        self.assertEqual(t["knowledge_bases"], ["scene.workdocs"])
        cfg = reg.load_config(cfg_path)
        self.assertIn("scene.workdocs", cfg["knowledge_bases"])
        spec = cfg["knowledge_bases"]["scene.workdocs"]
        self.assertEqual(spec.get("path"), "scenes/workdocs")
        self.assertEqual(spec.get("type"), "local")
        self.assertEqual(spec.get("source"), "local_folder")
        self.assertEqual(spec.get("scope"), "scene")
        self.assertTrue(spec.get("enabled", False))

    def test_bind_wiki_still_rejects_truly_unknown_kb(self):
        _, cfg_path, _, _ = self._setup_wiki_with_target()
        with self.assertRaises(ValueError) as ctx:
            reg.bind_wiki(
                "wxid_a", ["scene.nonexistent"], replace=True, config_path=cfg_path,
            )
        self.assertIn("unknown knowledge base", str(ctx.exception).lower())
        cfg = reg.load_config(cfg_path)
        self.assertNotIn("scene.nonexistent", cfg.get("knowledge_bases", {}))

    def test_bind_wiki_partial_failure_does_not_persist_registration(self):
        """If one KB is valid scanned but another is unknown, no KB is persisted."""
        _, cfg_path, _, _ = self._setup_wiki_with_target()
        with self.assertRaises(ValueError):
            reg.bind_wiki(
                "wxid_a",
                ["scene.workdocs", "scene.nonexistent"],
                replace=True,
                config_path=cfg_path,
            )
        cfg = reg.load_config(cfg_path)
        self.assertEqual(cfg.get("knowledge_bases", {}), {})

    def test_enable_candidate_auto_registers_scanned_kb(self):
        td, cfg_path, cand_path, workdocs_dir = self._setup_wiki_with_target()
        reg.save_json_atomic(cand_path, {
            "version": 1,
            "candidates": [{"username": "wxid_b", "name": "群B", "status": "pending"}],
        })
        t = reg.enable_candidate(
            "wxid_b",
            knowledge_bases=["scene.workdocs"],
            config_path=cfg_path,
            candidates_path=cand_path,
        )
        self.assertEqual(t["knowledge_bases"], ["scene.workdocs"])
        cfg = reg.load_config(cfg_path)
        self.assertIn("scene.workdocs", cfg["knowledge_bases"])
        spec = cfg["knowledge_bases"]["scene.workdocs"]
        self.assertEqual(spec.get("path"), "scenes/workdocs")
        self.assertEqual(spec.get("type"), "local")


class TestLeannEnv(unittest.TestCase):
    def test_leann_env_uses_configured_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"reply_engine": {"leann": {"cache_dir": tmp}}}
            env = reg._leann_env(cfg)
            self.assertEqual(env["HF_HOME"], os.path.join(tmp, "huggingface"))
            self.assertEqual(env["SENTENCE_TRANSFORMERS_HOME"], os.path.join(tmp, "sentence_transformers"))
            self.assertEqual(env["HF_ENDPOINT"], "https://hf-mirror.com")

    def test_leann_env_defaults_to_d_drive(self):
        env = reg._leann_env({})
        self.assertIn("D:", env["HF_HOME"].upper())
        self.assertIn(r"cache\leann\huggingface", env["HF_HOME"])
        self.assertEqual(env["HF_ENDPOINT"], "https://hf-mirror.com")


class TestMigrateLeannCache(unittest.TestCase):
    def test_migrate_copies_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dst = Path(tmp) / "dst"
            (src / "hub" / "models--x").mkdir(parents=True)
            (src / "hub" / "models--x" / "file.txt").write_text("model")
            result = reg.migrate_leann_cache(str(src), str(dst))
            self.assertTrue(result["copied"])
            self.assertEqual(result["files"], 1)
            self.assertTrue((dst / "hub" / "models--x" / "file.txt").exists())
            self.assertEqual((dst / "hub" / "models--x" / "file.txt").read_text(), "model")

    def test_migrate_skips_nonexistent_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = reg.migrate_leann_cache(str(Path(tmp) / "missing"), str(Path(tmp) / "dst"))
            self.assertFalse(result["copied"])
            self.assertEqual(result["reason"], "source does not exist")

    def test_migrate_skips_nonempty_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dst = Path(tmp) / "dst"
            src.mkdir()
            dst.mkdir()
            (src / "a.txt").write_text("a")
            (dst / "b.txt").write_text("b")
            result = reg.migrate_leann_cache(str(src), str(dst))
            self.assertFalse(result["copied"])
            self.assertEqual(result["reason"], "destination not empty")


class TestLeannBuild(unittest.TestCase):
    def setUp(self):
        reg._LEANN_BUILD_JOBS.clear()

    @mock.patch("target_registry._run_leann_build_async")
    def test_build_leann_kb_starts_job(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {"wk": {"type": "leann", "index_name": "work", "docs_dir": tmp, "enabled": True}}}))
            result = reg.build_leann_kb("wk", config_path=str(cfg_path))
            self.assertEqual(result["status"], "started")
            self.assertEqual(result["index_name"], "work")
            self.assertEqual(result["docs_dir"], tmp)
            self.assertTrue(result["build_id"].startswith("leann_build_wk_"))
            self.assertGreater(len(result["build_id"]), len("leann_build_wk_"))
            mock_run.assert_called_once()

    @mock.patch("target_registry._run_leann_build_async")
    def test_build_leann_kb_passes_docs_dir_and_force(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {"wk": {"type": "leann", "index_name": "work", "enabled": True}}}))
            reg.build_leann_kb("wk", docs_dir=str(docs), force=True, config_path=str(cfg_path))
            call_args = mock_run.call_args
            self.assertIn("build", call_args.args[1])
            self.assertIn("--docs", call_args.args[1])
            self.assertIn(str(docs), call_args.args[1])
            self.assertIn("--force", call_args.args[1])

    def test_build_leann_kb_rejects_missing_docs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {"wk": {"type": "leann", "index_name": "work", "enabled": True}}}))
            with self.assertRaises(ValueError) as ctx:
                reg.build_leann_kb("wk", config_path=str(cfg_path))
            self.assertIn("docs_dir", str(ctx.exception).lower())

    def test_build_leann_kb_rejects_non_leann(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {"local": {"type": "local", "path": tmp, "enabled": True}}}))
            with self.assertRaises(ValueError) as ctx:
                reg.build_leann_kb("local", config_path=str(cfg_path))
            self.assertIn("not leann", str(ctx.exception).lower())

    def test_build_leann_kb_rejects_missing_kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {}}))
            with self.assertRaises(ValueError) as ctx:
                reg.build_leann_kb("missing", config_path=str(cfg_path))
            self.assertIn("not found", str(ctx.exception).lower())

    def test_get_leann_build_status_unknown(self):
        result = reg.get_leann_build_status("nonexistent")
        self.assertEqual(result["status"], "unknown")

    @mock.patch("target_registry.subprocess.Popen")
    def test_run_leann_build_async_logs_and_updates_status(self, mock_popen):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "build.log"
            dummy_proc = mock.Mock()
            dummy_proc.returncode = 0
            dummy_proc.wait.return_value = 0
            mock_popen.return_value = dummy_proc
            reg._run_leann_build_async("bid_123", ["leann", "build", "idx"], {"X": "1"}, str(log_path))
            # wait briefly for daemon thread to finish
            import time as _time
            for _ in range(50):
                if reg.get_leann_build_status("bid_123").get("status") in ("done", "failed"):
                    break
                _time.sleep(0.01)
            status = reg.get_leann_build_status("bid_123")
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["returncode"], 0)
            self.assertEqual(status["log_path"], str(log_path))
            self.assertIn("START", log_path.read_text(encoding="utf-8"))
            self.assertIn("END rc=0", log_path.read_text(encoding="utf-8"))

    @mock.patch("target_registry.subprocess.Popen")
    def test_run_leann_build_async_records_popen_failure(self, mock_popen):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "build.log"
            mock_popen.side_effect = FileNotFoundError("leann not found")
            reg._run_leann_build_async("bid_fail", ["leann", "build", "idx"], {"X": "1"}, str(log_path))
            import time as _time
            for _ in range(50):
                if reg.get_leann_build_status("bid_fail").get("status") == "failed":
                    break
                _time.sleep(0.01)
            status = reg.get_leann_build_status("bid_fail")
            self.assertEqual(status["status"], "failed")
            self.assertIn("leann not found", status.get("error", ""))
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("START", log_text)
            self.assertIn("ERROR", log_text)
            self.assertIn("leann not found", log_text)

    @mock.patch("target_registry.subprocess.Popen")
    def test_run_leann_build_async_handles_timeout(self, mock_popen):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "build.log"
            dummy_proc = mock.Mock()
            # First wait() raises timeout; the reaper then calls wait(timeout=5).
            dummy_proc.wait.side_effect = [
                subprocess.TimeoutExpired(cmd=["leann"], timeout=0.1),
                -1,
            ]
            mock_popen.return_value = dummy_proc
            reg._run_leann_build_async("bid_timeout", ["leann", "build", "idx"], {"X": "1"}, str(log_path), timeout=0.1)
            import time as _time
            for _ in range(50):
                if reg.get_leann_build_status("bid_timeout").get("status") == "failed":
                    break
                _time.sleep(0.01)
            status = reg.get_leann_build_status("bid_timeout")
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["returncode"], -1)
            self.assertIn("timed out", status.get("error", "").lower())
            dummy_proc.kill.assert_called_once()
            self.assertGreaterEqual(dummy_proc.wait.call_count, 2)
            self.assertNotIn("proc", reg._LEANN_BUILD_JOBS.get("bid_timeout", {}))

    @mock.patch("target_registry._run_leann_build_async")
    def test_build_leann_kb_uses_docs_dir_from_spec(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {"wk": {"type": "leann", "index_name": "work", "docs_dir": str(docs), "enabled": True}}}))
            reg.build_leann_kb("wk", config_path=str(cfg_path))
            call_args = mock_run.call_args
            cmd = call_args.args[1]
            self.assertIn("--docs", cmd)
            self.assertIn(str(docs), cmd)

    @mock.patch("target_registry._run_leann_build_async")
    def test_build_leann_kb_preregisters_started_status(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {"wk": {"type": "leann", "index_name": "work", "docs_dir": tmp, "enabled": True}}}))
            result = reg.build_leann_kb("wk", config_path=str(cfg_path))
            build_id = result["build_id"]
            status = reg.get_leann_build_status(build_id)
            self.assertEqual(status["status"], "started")
            self.assertEqual(status["log_path"], result["log_path"])
            self.assertEqual(status["docs_dir"], tmp)
            self.assertIn(build_id, reg._LEANN_BUILD_JOBS)

    def test_trim_leann_build_jobs_caps_terminal_jobs(self):
        reg._LEANN_BUILD_JOBS.clear()
        for i in range(55):
            bid = "bid_%03d" % i
            reg._LEANN_BUILD_JOBS[bid] = {
                "status": "done" if i % 2 == 0 else "failed",
                "created_at": time.time() + i,
                "log_path": "/tmp/%s.log" % bid,
            }
        reg._LEANN_BUILD_JOBS["running"] = {
            "status": "running",
            "created_at": 0,
            "log_path": "/tmp/running.log",
        }
        reg._trim_leann_build_jobs(max_jobs=50)
        self.assertLessEqual(len(reg._LEANN_BUILD_JOBS), 51)
        self.assertIn("running", reg._LEANN_BUILD_JOBS)
        self.assertNotIn("bid_000", reg._LEANN_BUILD_JOBS)

    def test_list_leann_indexes_scans_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text("{}")
            indexes_dir = Path(tmp) / ".leann" / "indexes"
            (indexes_dir / "idx_a").mkdir(parents=True)
            (indexes_dir / "idx_b").mkdir(parents=True)
            result = reg.list_leann_indexes(config_path=str(cfg_path))
        self.assertEqual([i["name"] for i in result["indexes"]], ["idx_a", "idx_b"])
        self.assertEqual(result["error"], "")
        self.assertIn("cwd", result)

    def test_list_leann_indexes_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text("{}")
            result = reg.list_leann_indexes(config_path=str(cfg_path))
        self.assertEqual(result["indexes"], [])
        self.assertEqual(result["error"], "")

    def test_get_leann_index_info_reads_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text("{}")
            index_dir = Path(tmp) / ".leann" / "indexes" / "my_idx"
            index_dir.mkdir(parents=True)
            meta = index_dir / "documents.leann.meta.json"
            meta.write_text(json.dumps({"document_count": 42, "created_at": "2026-07-04T10:00:00"}))
            info = reg.get_leann_index_info("my_idx", config_path=str(cfg_path))
        self.assertTrue(info["exists"])
        self.assertEqual(info["document_count"], 42)
        self.assertEqual(info["created_at"], "2026-07-04T10:00:00")

    def test_get_leann_index_info_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text("{}")
            info = reg.get_leann_index_info("missing", config_path=str(cfg_path))
        self.assertFalse(info["exists"])

    @mock.patch("target_registry._run_leann_build_async")
    def test_build_state_persists_and_reloads(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.json"
            cfg_path.write_text(json.dumps({"knowledge_bases": {"wk": {"type": "leann", "index_name": "work", "docs_dir": tmp, "enabled": True}}}))
            reg._LEANN_BUILD_JOBS.clear()
            reg.build_leann_kb("wk", config_path=str(cfg_path))
            state_path = Path(tmp) / "logs" / "leann_build_state.json"
            self.assertTrue(state_path.exists())
            # Simulate a fresh module load by clearing and reloading.
            reg._LEANN_BUILD_JOBS.clear()
            loaded = reg._load_leann_build_state(config_path=str(cfg_path))
            self.assertEqual(len(loaded), 1)
            bid = list(loaded.keys())[0]
            self.assertEqual(loaded[bid]["status"], "started")
            self.assertEqual(loaded[bid]["kb_id"], "wk")


if __name__ == "__main__":
    unittest.main()
