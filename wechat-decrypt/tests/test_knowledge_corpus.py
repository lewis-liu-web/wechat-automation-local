# Editing intent (per project AGENTS.md):
# - Add an auditable annotated regression corpus fixture and a thin consumer
#   that drives the existing knowledge_retrieval facade with it.
# - This does not change production bot logic; it only adds Stage 3
#   regression coverage for lexical match, semantic match (fake LEANN),
#   cross-KB isolation, no-hit, and provider outage.
# - Worker-level behaviors (image, multi-message turn, silent, escalation)
#   are already covered by tests/test_reliable_worker.py and are only
#   cross-referenced in the fixture, not duplicated here.
# - No target feature flags are enabled; no runtime behavior changes.

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Consume the annotated knowledge corpus fixture for Stage 3 regression.

The fixture records business-level input, authorized KBs, and expected
provenance/decision. This module only exercises the facade-level cases
through ``knowledge_retrieval.search_knowledge``; durable worker cases are
owned by ``tests/test_reliable_worker.py`` and referenced here for audit.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import knowledge_retrieval as kr

CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "knowledge_corpus.json"


def _load_corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _case(cases: list, case_id: str) -> dict:
    for case in cases:
        if case.get("id") == case_id:
            return case
    raise KeyError(f"corpus case {case_id!r} not found")


class _CorpusAdapter:
    """Set up local wiki documents and fake LEANN outcomes per case id."""

    def __init__(self, tmp_root: Path):
        self.root = tmp_root
        (self.root / "core").mkdir()
        (self.root / "core" / "reply_boundary.md").write_text(
            "边界 不能承诺 不能泄露密钥", encoding="utf-8"
        )
        (self.root / "scenes" / "a").mkdir(parents=True)
        (self.root / "scenes" / "a" / "faq.md").write_text(
            "苹果 场景A 专用资料\n购买方式：在线下单", encoding="utf-8"
        )
        (self.root / "scenes" / "b").mkdir(parents=True)
        (self.root / "scenes" / "b" / "faq.md").write_text(
            "香蕉 场景B 专用资料", encoding="utf-8"
        )

    def build_config(self, case: dict) -> dict:
        allowed = case["allowed_kb_ids"]
        cfg: dict = {"wiki_dir": str(self.root), "knowledge_bases": {}}
        for kb_id in allowed:
            if kb_id == "scene.a":
                cfg["knowledge_bases"][kb_id] = {"type": "local", "path": "scenes/a"}
            elif kb_id == "scene.b":
                cfg["knowledge_bases"][kb_id] = {"type": "local", "path": "scenes/b"}
            elif kb_id == "leann.bus":
                cfg["knowledge_bases"][kb_id] = {
                    "type": "leann",
                    "index_name": "work_kb_bus",
                }
            elif kb_id == "online.ima.work":
                cfg["knowledge_bases"][kb_id] = {
                    "type": "ima",
                    "knowledge_base_id": "kb_work",
                    "api_key_env": "NON_EXISTENT_IMA_KEY_FOR_TEST",
                }
        return cfg

    def run_facade(self, case: dict) -> kr.KnowledgeSearchResult:
        cfg = self.build_config(case)
        allowed = case["allowed_kb_ids"]
        query = case["query"]
        if case["id"] == "semantic_match":
            fake_result = {
                "hits": [
                    {
                        "rel_path": "公交卡充值.md",
                        "score": 0.9,
                        "snippet": "公交卡充值失败可申请退款，请检查 NFC 是否开启。",
                    }
                ]
            }
            with mock.patch.object(
                kr._target_registry, "search_leann_kb", return_value=fake_result
            ):
                return kr.search_knowledge(query, cfg, allowed, limit=5)
        return kr.search_knowledge(query, cfg, allowed, limit=5)


class KnowledgeCorpusTests(unittest.TestCase):
    """Facade-level regression over the annotated corpus."""

    def _load(self):
        return _load_corpus()["cases"]

    def _assert_facade(self, case: dict, result: kr.KnowledgeSearchResult) -> None:
        expected = case["expected"]
        self.assertEqual(result.status, expected["facade_status"], case["id"])
        for want in expected.get("provenance", []):
            matches = [
                p
                for p in result.provenance
                if p.get("kb_id") == want["kb_id"]
                and ("status" not in want or p.get("status") == want["status"])
                and ("count" not in want or p.get("count") == want["count"])
            ]
            self.assertTrue(matches, f"case={case['id']} missing provenance {want}")

    def test_lexical_match(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = _CorpusAdapter(Path(td))
            case = _case(self._load(), "lexical_match")
            result = adapter.run_facade(case)
            self._assert_facade(case, result)
            self.assertTrue(any("场景A" in h["content"] for h in result.hits))

    def test_semantic_match(self):
        """Semantic match is validated through a controlled fake LEANN outcome.

        The fixture records expected provenance/action; the adapter maps the
        case to a fake search result so the test is stable across environments
        and does not depend on a real LEANN index or embedding version.
        """
        with tempfile.TemporaryDirectory() as td:
            adapter = _CorpusAdapter(Path(td))
            case = _case(self._load(), "semantic_match")
            result = adapter.run_facade(case)
            self._assert_facade(case, result)
            self.assertTrue(any("公交卡" in h["content"] for h in result.hits))

    def test_cross_kb_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = _CorpusAdapter(Path(td))
            case = _case(self._load(), "cross_kb_isolation")
            # scene.b is configured but not authorized.
            cfg = adapter.build_config(case)
            cfg["knowledge_bases"]["scene.b"] = {"type": "local", "path": "scenes/b"}
            result = kr.search_knowledge(case["query"], cfg, case["allowed_kb_ids"], limit=5)
            self._assert_facade(case, result)
            kb_ids = {h["kb_id"] for h in result.hits}
            self.assertIn("scene.a", kb_ids)
            self.assertNotIn("scene.b", kb_ids)

    def test_no_hit(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = _CorpusAdapter(Path(td))
            case = _case(self._load(), "no_hit")
            result = adapter.run_facade(case)
            self._assert_facade(case, result)
            self.assertEqual(result.hits, [])

    def test_provider_outage(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = _CorpusAdapter(Path(td))
            case = _case(self._load(), "provider_outage")
            result = adapter.run_facade(case)
            self._assert_facade(case, result)
            self.assertTrue(result.error, case["id"])

    def test_corpus_coverage_references_are_valid(self):
        """Audit check: every corpus case names a real, machine-resolvable test.

        Facade cases (owned by this module) must be class nodes under
        KnowledgeCorpusTests. Worker cases must reference module-level
        functions in tests/test_reliable_worker.py.
        """
        cases = {c["id"]: c for c in self._load()}
        for case_id, case in cases.items():
            covered_by = case.get("covered_by", "")
            self.assertTrue(covered_by, f"case {case_id} missing covered_by")
            self.assertIn("::", covered_by, f"case {case_id} covered_by is not a pytest node id")
            parts = covered_by.split("::")
            rel_path = parts[0]
            test_name = parts[-1]
            full_path = ROOT / rel_path
            self.assertTrue(full_path.is_file(), f"case {case_id} covered_by file missing: {full_path}")

            if case_id in ("image_message", "multi_message_turn", "silent_chat", "escalation"):
                self.assertEqual(
                    rel_path, "tests/test_reliable_worker.py",
                    f"case {case_id} must be covered by tests/test_reliable_worker.py",
                )
                self.assertEqual(len(parts), 2, f"case {case_id} worker covered_by must be file::function")
                source = full_path.read_text(encoding="utf-8")
                self.assertIn(f"def {test_name}(", source, f"case {case_id} covered_by function missing: {test_name}")
            else:
                self.assertEqual(
                    rel_path, "tests/test_knowledge_corpus.py",
                    f"case {case_id} must be covered by tests/test_knowledge_corpus.py",
                )
                self.assertEqual(len(parts), 3, f"case {case_id} facade covered_by must be file::class::function")
                self.assertEqual(parts[1], "KnowledgeCorpusTests", f"case {case_id} facade covered_by class must be KnowledgeCorpusTests")
                self.assertTrue(hasattr(self.__class__, test_name), f"case {case_id} covered_by method missing: {test_name}")



if __name__ == "__main__":
    unittest.main()
