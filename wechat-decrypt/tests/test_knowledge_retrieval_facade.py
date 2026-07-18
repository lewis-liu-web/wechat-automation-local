#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Focused tests for the knowledge_retrieval facade.

Covers the target-authorized search_knowledge facade, provenance, and the
explicit distinction between no-hit and provider failure.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import knowledge_retrieval as kr
from knowledge_retrieval import KnowledgeHit, search_knowledge


class KnowledgeRetrievalFacadeTests(unittest.TestCase):
    def make_wiki(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / 'core').mkdir()
        (root / 'core' / 'reply_boundary.md').write_text('边界 不能承诺 不能泄露密钥', encoding='utf-8')
        (root / 'scenes' / 'a').mkdir(parents=True)
        (root / 'scenes' / 'a' / 'faq.md').write_text('苹果 场景A 专用资料', encoding='utf-8')
        return td, root

    def test_search_empty_allowed_list_is_invalid(self):
        result = search_knowledge('hello', {}, [])
        self.assertEqual(result.status, 'invalid')
        self.assertEqual(result.hits, [])
        self.assertEqual(result.provenance, [])
        self.assertIn('no authorized', result.error or '')

    def test_search_returns_ok_and_provenance_for_hits(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type': 'local', 'path': 'scenes/a'},
        }}
        result = search_knowledge('苹果', cfg, ['scene.a'])
        self.assertEqual(result.status, 'ok')
        self.assertTrue(len(result.hits) > 0)
        self.assertEqual(result.hits[0]['kb_id'], 'scene.a')
        self.assertTrue(any(p['kb_id'] == 'scene.a' and p['status'] == 'hit' for p in result.provenance))
        self.assertIsNone(result.error)

    def test_search_no_hit_when_local_kb_has_no_match(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type': 'local', 'path': 'scenes/a'},
        }}
        result = search_knowledge(' completely unrelated phrase ', cfg, ['scene.a'])
        self.assertEqual(result.status, 'no_hit')
        self.assertEqual(result.hits, [])
        prov = result.provenance
        self.assertEqual(len(prov), 1)
        self.assertEqual(prov[0]['kb_id'], 'scene.a')
        self.assertEqual(prov[0]['status'], 'no_hit')
        self.assertEqual(prov[0]['count'], 0)
        self.assertIsNone(prov[0]['error'])

    def test_search_provider_failure_for_missing_credentials(self):
        cfg = {'wiki_dir': '', 'knowledge_bases': {
            'online.ima.x': {'type': 'ima', 'api_key_env': 'NON_EXISTENT_IMA_KEY_FOR_TEST'},
        }}
        result = search_knowledge('whatever', cfg, ['online.ima.x'])
        self.assertEqual(result.status, 'provider_failure')
        self.assertEqual(result.hits, [])
        prov = result.provenance
        self.assertEqual(len(prov), 1)
        self.assertEqual(prov[0]['kb_id'], 'online.ima.x')
        self.assertEqual(prov[0]['status'], 'failure')
        self.assertEqual(prov[0]['count'], 0)
        self.assertTrue(prov[0]['error'] and 'missing ima credentials' in prov[0]['error'])
        self.assertTrue('provider failed' in result.error or 'failed' in (result.error or ''))

    def test_search_only_authorized_kbs_are_used(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root / 'scenes' / 'b').mkdir(parents=True)
        (root / 'scenes' / 'b' / 'faq.md').write_text('香蕉 场景B', encoding='utf-8')
        cfg = {'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type': 'local', 'path': 'scenes/a'},
            'scene.b': {'type': 'local', 'path': 'scenes/b'},
        }}
        result = search_knowledge('苹果', cfg, ['scene.a'])
        self.assertEqual(result.status, 'ok')
        kb_ids = {h['kb_id'] for h in result.hits}
        self.assertIn('scene.a', kb_ids)
        self.assertNotIn('scene.b', kb_ids)
        prov_ids = {p['kb_id'] for p in result.provenance}
        self.assertIn('scene.a', prov_ids)
        self.assertNotIn('scene.b', prov_ids)

    def test_search_hits_include_payload_fields(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type': 'local', 'path': 'scenes/a'},
        }}
        result = search_knowledge('苹果', cfg, ['scene.a'], limit=5)
        self.assertEqual(result.status, 'ok')
        for hit in result.hits:
            self.assertIn('label', hit)
            self.assertIn('source', hit)
            self.assertIn('kb_id', hit)
            self.assertIn('rel_path', hit)
            self.assertIn('content', hit)
            self.assertIn('score', hit)

    def test_search_local_kb_path_missing_is_failure(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type': 'local', 'path': 'does_not_exist'},
        }}
        result = search_knowledge('苹果', cfg, ['scene.a'])
        self.assertEqual(result.status, 'provider_failure')
        self.assertEqual(result.hits, [])
        self.assertEqual(result.provenance[0]['status'], 'failure')
        self.assertTrue('does not exist' in (result.provenance[0]['error'] or ''))

    def test_search_leann_registry_failure(self):
        cfg = {'wiki_dir': '', 'knowledge_bases': {
            'leann_idx': {'type': 'leann', 'index_name': 'missing'},
        }}
        with mock.patch('knowledge_retrieval._target_registry', None):
            result = search_knowledge('query', cfg, ['leann_idx'])
        self.assertEqual(result.status, 'provider_failure')
        self.assertEqual(result.hits, [])
        prov = result.provenance[0]
        self.assertEqual(prov['kb_id'], 'leann_idx')
        self.assertEqual(prov['status'], 'failure')
        self.assertTrue('target_registry unavailable' in (prov['error'] or ''))

    def test_search_leann_error_is_provider_failure(self):
        cfg = {'wiki_dir': '', 'knowledge_bases': {
            'leann_idx': {'type': 'leann', 'index_name': 'work'},
        }}
        with mock.patch.object(kr._target_registry, 'search_leann_kb', return_value={
            'hits': [], 'error': 'direct search timed out',
        }) as fake:
            result = search_knowledge('query', cfg, ['leann_idx'])
        self.assertEqual(result.status, 'provider_failure')
        self.assertEqual(result.provenance[0]['status'], 'failure')
        self.assertEqual(result.provenance[0]['error'], 'direct search timed out')
        self.assertNotIn('config_path', fake.call_args.kwargs)

    def test_search_leann_config_path_is_passed_when_given(self):
        cfg = {'wiki_dir': '', 'knowledge_bases': {
            'leann_idx': {'type': 'leann', 'index_name': 'work'},
        }}
        fake = mock.Mock(return_value={'hits': [], 'error': None})
        with mock.patch.object(kr._target_registry, 'search_leann_kb', fake):
            search_knowledge('query', cfg, ['leann_idx'], config_path=r'C:\fake\config.json')
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs.get('config_path'), r'C:\fake\config.json')

    def test_search_leann_config_path_omitted_when_not_given(self):
        cfg = {'wiki_dir': '', 'knowledge_bases': {
            'leann_idx': {'type': 'leann', 'index_name': 'work'},
        }}
        fake = mock.Mock(return_value={'hits': [], 'error': None})
        with mock.patch.object(kr._target_registry, 'search_leann_kb', fake):
            search_knowledge('query', cfg, ['leann_idx'])
        kwargs = fake.call_args.kwargs
        self.assertNotIn('config_path', kwargs)

    def test_knowledge_hit_label(self):
        h = KnowledgeHit('local', 'core', 'first_principles', 'core/boundary.md', 'content', 5)
        self.assertEqual(h.label, 'local:core:core/boundary.md')

    def test_knowledge_hits_to_payload(self):
        hits = [KnowledgeHit('local', 'k', 'scene', 'r.md', 'content', 3)]
        payload = kr._knowledge_hits_to_payload(hits)
        self.assertEqual(payload[0]['kb_id'], 'k')
        self.assertEqual(payload[0]['content'], 'content')
        self.assertEqual(payload[0]['score'], 3)


if __name__ == '__main__':
    unittest.main()
