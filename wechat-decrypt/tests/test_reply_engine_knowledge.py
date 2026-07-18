import tempfile
import unittest
from pathlib import Path
from unittest import mock
import io
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import knowledge_retrieval as kr
from reply_engine import generate_reply, _tool_agent_available_tools, _tool_agent_allowed_leann_indexes, _resolve_skill_name, _wechat_side_payload, _thin_monitor_enabled
import agent_provider as ap


def _tool_agent_result_mock(reply_text="工具查询结果回复", action="reply", status="done", risk_level="low"):
    return mock.Mock(ok=True, status=status, reply_text=reply_text, raw={"agent_result": {"action": action, "reply_text": reply_text, "reason_code": "test", "risk_level": risk_level}})


class KnowledgeArchitectureTests(unittest.TestCase):
    def make_wiki(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root/'core').mkdir()
        (root/'core'/'reply_boundary.md').write_text('边界 不能承诺 不能泄露密钥', encoding='utf-8')
        (root/'scenes'/'a').mkdir(parents=True)
        (root/'scenes'/'a'/'faq.md').write_text('苹果 场景A 专用资料', encoding='utf-8')
        (root/'scenes'/'b').mkdir(parents=True)
        (root/'scenes'/'b'/'faq.md').write_text('香蕉 场景B 专用资料', encoding='utf-8')
        return td, root

    def test_core_always_loaded_and_zero_scene_allowed(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg={'wiki_dir': str(root), 'knowledge_bases': {}}
        hits=kr.retrieve_knowledge('完全无关问题', cfg, {'knowledge_bases': []})
        labels=[h.label for h in hits]
        self.assertTrue(any('core' in x for x in labels))
        self.assertFalse(any('scene' in x for x in labels))

    def test_target_selects_multiple_scene_kbs_without_cross_leak(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg={'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type':'local','path':'scenes/a'},
            'scene.b': {'type':'local','path':'scenes/b'},
        }}
        hits=kr.retrieve_knowledge('苹果', cfg, {'knowledge_bases':['scene.a']})
        labels='\n'.join(h.label for h in hits)
        self.assertIn('scene.a', labels)
        self.assertNotIn('scene.b', labels)
        hits2=kr.retrieve_knowledge('香蕉', cfg, {'knowledge_bases':['scene.a','scene.b']})
        self.assertIn('scene.b', '\n'.join(h.label for h in hits2))

    def test_scene_limit_is_independent_from_core_limit(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {
                'scene.a': {'type': 'local', 'path': 'scenes/a'},
                'scene.b': {'type': 'local', 'path': 'scenes/b'},
            },
            'reply_engine': {'core_limit': 1, 'scene_limit': 2},
        }
        layers = kr.retrieve_knowledge_layers('苹果 香蕉', cfg, {'knowledge_bases': ['scene.a', 'scene.b']})
        self.assertLessEqual(len(layers['core']), 1, 'core_limit should cap core hits')
        self.assertLessEqual(len(layers['scene']), 2, 'scene_limit should cap scene hits')
        self.assertEqual(len(layers['scene']), 2, 'both scene KBs should appear')

    def test_scene_hits_dedup_by_source_kb_rel_path(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {
                'scene.a': {'type': 'local', 'path': 'scenes/a'},
            },
            'reply_engine': {'scene_limit': 5},
        }
        # Mock _retrieve_local_kb to return two hits for the same rel_path.
        from knowledge_retrieval import _retrieve_local_kb as real_local
        def fake_local(query, root, spec, limit):
            from knowledge_retrieval import KnowledgeHit
            kb_id = str(spec.get('id') or spec.get('path') or 'local')
            return [
                KnowledgeHit('local', kb_id, 'scene', 'scenes/a/faq.md', '片段1', 3),
                KnowledgeHit('local', kb_id, 'scene', 'scenes/a/faq.md', '片段2', 2),
            ]
        with mock.patch('knowledge_retrieval._retrieve_local_kb', fake_local):
            layers = kr.retrieve_knowledge_layers('苹果', cfg, {'knowledge_bases': ['scene.a']})
        self.assertEqual(len(layers['scene']), 1, 'duplicate rel_path should be deduped')


    def test_local_kb_matches_body_content(self):
        """Body-only keywords should be retrievable from local KB via fallback scoring."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('工作号真实号用于号码认证场景', encoding='utf-8')
        cfg={'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type':'local','path':'scenes/a'},
        }}
        hits=kr.retrieve_knowledge('工作号真实号', cfg, {'knowledge_bases':['scene.a']})
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)

    def test_local_kb_fts_matches_body_content(self):
        """FTS5 path should be the one that returns body-only hits."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('工作号真实号用于号码认证场景', encoding='utf-8')
        from knowledge_retrieval import _retrieve_local_kb_fts
        hits=_retrieve_local_kb_fts('工作号真实号', root, {'id':'scene.a','type':'local','path':str(root/'scenes'/'a'),'scope':'scene'}, 5)
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)

    def test_leann_kb_retrieved_as_scene_hits(self):
        """Regression: type=leann knowledge bases must be searched and returned
        as scene hits so raw_agent prompt includes them.
        """
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {
                'bus_index': {'type': 'leann', 'index_name': 'work_kb_bus'},
            },
        }

        def fake_search(spec, query, limit=5, config_path=None, cfg=None):
            return {
                "hits": [
                    {"rel_path": "公交卡充值.md", "score": 0.9, "snippet": "请检查 NFC 是否开启。"},
                    {"rel_path": "刷卡失败.md", "score": 0.8, "snippet": "确认超级 SIM 卡已启用。"},
                ],
                "matched_files": 2,
                "total_files": 2,
                "query": query,
            }

        with mock.patch('knowledge_retrieval._target_registry.search_leann_kb', fake_search):
            layers = kr.retrieve_knowledge_layers('公交卡充值失败', cfg, {'knowledge_bases': ['bus_index']})
        contents = '\n'.join(h.content for h in layers['scene'])
        self.assertIn('NFC', contents)
        self.assertIn('超级 SIM', contents)

    def test_local_kb_fts_matches_body_content_with_punctuation(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('工作号真实号用于号码认证场景。', encoding='utf-8')
        from knowledge_retrieval import _retrieve_local_kb_fts
        hits=_retrieve_local_kb_fts('工作号真实号', root, {'id':'scene.a','type':'local','path':str(root/'scenes'/'a'),'scope':'scene'}, 5)
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)

    def test_local_kb_fts_matches_body_content_long_sentence(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('简单介绍一下，工作号真实号是一款号码认证产品。', encoding='utf-8')
        from knowledge_retrieval import _retrieve_local_kb_fts
        hits=_retrieve_local_kb_fts('介绍一下工作号真实号产品', root, {'id':'scene.a','type':'local','path':str(root/'scenes'/'a'),'scope':'scene'}, 5)
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)

    def test_local_kb_fts_matches_body_content_after_cleaning_mentions(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('工作号真实号用于号码认证场景。', encoding='utf-8')
        from knowledge_retrieval import _retrieve_local_kb_fts, _clean_query_for_fts
        raw = 'lewis4438136:\n@飞扬的跟屁虫\u2005简单介绍一下工作号真实号产品呢'
        cleaned = _clean_query_for_fts(raw)
        self.assertIn('工作号真实号', cleaned)
        hits=_retrieve_local_kb_fts(cleaned, root, {'id':'scene.a','type':'local','path':str(root/'scenes'/'a'),'scope':'scene'}, 5)
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)

    def test_local_kb_matches_body_content_with_punctuation(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('工作号真实号用于号码认证场景。', encoding='utf-8')
        cfg={'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type':'local','path':'scenes/a'},
        }}
        hits=kr.retrieve_knowledge('工作号真实号', cfg, {'knowledge_bases':['scene.a']})
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)

    def test_local_kb_matches_body_content_long_sentence(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('简单介绍一下，工作号真实号是一款号码认证产品。', encoding='utf-8')
        cfg={'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type':'local','path':'scenes/a'},
        }}
        hits=kr.retrieve_knowledge('介绍一下工作号真实号产品', cfg, {'knowledge_bases':['scene.a']})
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)

    def test_local_kb_matches_body_content_with_mention_prefix(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        (root/'scenes'/'a'/'body_only.md').write_text('工作号真实号用于号码认证场景。', encoding='utf-8')
        cfg={'wiki_dir': str(root), 'knowledge_bases': {
            'scene.a': {'type':'local','path':'scenes/a'},
        }}
        hits=kr.retrieve_knowledge('lewis4438136:\n@飞扬的跟屁虫\u2005简单介绍一下工作号真实号产品呢', cfg, {'knowledge_bases':['scene.a']})
        contents='\n'.join(h.content for h in hits)
        self.assertIn('工作号真实号', contents)
    def test_ima_without_key_is_safe_no_hit(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg={'wiki_dir': str(root), 'knowledge_bases': {'online.ima.x': {'type':'ima','api_key_env':'NON_EXISTENT_IMA_KEY_FOR_TEST'}}}
        hits=kr.retrieve_knowledge('whatever', cfg, {'knowledge_bases':['online.ima.x']})
        self.assertTrue(all(h.source == 'local' for h in hits))

    def test_ima_search_uses_official_contract_and_parses_hits(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        payload = {
            'data': {
                'info_list': [
                    {'title': 'IMA标题', 'highlight_content': 'IMA命中内容', 'media_id': 'm1', 'parent_folder_id': 'f1'},
                    {'title': '其他目录', 'highlight_content': '不应命中', 'media_id': 'm2', 'parent_folder_id': 'other'},
                    {'title': '目录本身', 'highlight_content': '不应作为资料', 'media_id': 'folder_1', 'parent_folder_id': 'f1', 'media_type': 99},
                ]
            }
        }
        class FakeResp:
            def __init__(self, response_payload, is_json=True):
                self.response_payload = response_payload
                self.is_json = is_json
                self.headers = mock.Mock()
                self.headers.get_content_charset.return_value = 'utf-8'
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def read(self, *args):
                if self.is_json:
                    return json.dumps(self.response_payload, ensure_ascii=False).encode('utf-8')
                return str(self.response_payload).encode('utf-8')
        requests = []
        def fake_urlopen(req, timeout=0):
            item = {
                'url': req.full_url,
                'headers': dict(req.header_items()),
                'body': json.loads(req.data.decode('utf-8')) if getattr(req, 'data', None) else None,
                'timeout': timeout,
            }
            requests.append(item)
            if req.full_url == 'https://example.test/m1.txt':
                return FakeResp('IMA正文内容', is_json=False)
            if '/openapi/wiki/v1/get_media_info' in req.full_url:
                return FakeResp({'data': {'url_info': {'url': 'https://example.test/m1.txt', 'headers': []}}})
            return FakeResp(payload)
        cfg={'wiki_dir': str(root), 'knowledge_bases': {'online.ima.x': {
            'type':'ima', 'knowledge_base_id':'kb123', 'folder_id':'f1', 'client_id_env':'TEST_IMA_CLIENT', 'api_key_env':'TEST_IMA_KEY', 'timeout':3
        }}}
        with mock.patch.dict(os.environ, {'TEST_IMA_CLIENT':'cid', 'TEST_IMA_KEY':'akey'}), \
             mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            hits=kr.retrieve_knowledge('苹果', cfg, {'knowledge_bases':['online.ima.x']})
        ima_hits=[h for h in hits if h.source == 'ima']
        self.assertEqual(len(ima_hits), 1)
        search_req = next(r for r in requests if '/openapi/wiki/v1/search_knowledge' in r['url'])
        detail_req = next(r for r in requests if '/openapi/wiki/v1/get_media_info' in r['url'])
        self.assertEqual(search_req['body'], {'query':'苹果', 'cursor':'', 'knowledge_base_id':'kb123'})
        self.assertEqual(detail_req['body'], {'media_id':'m1'})
        self.assertEqual(search_req['headers'].get('Ima-openapi-clientid'), 'cid')
        self.assertEqual(search_req['headers'].get('Ima-openapi-apikey'), 'akey')
        self.assertEqual(search_req['timeout'], 3)
        self.assertIn('IMA正文内容', ima_hits[0].content)
        self.assertNotIn('不应命中', ima_hits[0].content)
        self.assertEqual(ima_hits[0].kb_id, 'kb123')
        self.assertIn('f1/m1', ima_hits[0].label)

    def test_ima_search_retries_cleaned_chat_query(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        calls = []
        def make_payload(req):
            body = json.loads(req.data.decode('utf-8'))
            calls.append(body['query'])
            if '工作号' in body['query'] and '真实号' in body['query'] and '@' not in body['query']:
                return {'data': {'info_list': [
                    {'title': '工作号真实号产品', 'highlight_content': '工作号真实号用于号码认证场景', 'media_id': 'm-work', 'parent_folder_id': 'f1'},
                    {'title': '其他目录', 'highlight_content': '不应命中', 'media_id': 'm-other', 'parent_folder_id': 'other'},
                ]}}
            return {'data': {'info_list': []}}
        class FakeResp:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def read(self): return json.dumps(self.payload, ensure_ascii=False).encode('utf-8')
        def fake_urlopen(req, timeout=0):
            return FakeResp(make_payload(req))
        cfg={'wiki_dir': str(root), 'knowledge_bases': {'online.ima.x': {
            'type':'ima', 'knowledge_base_id':'kb123', 'folder_id':'f1', 'client_id_env':'TEST_IMA_CLIENT', 'api_key_env':'TEST_IMA_KEY', 'timeout':3
        }}}
        q='lewis4438136:\n@飞扬的跟屁虫\u2005简单介绍一下工作号真实号产品呢'
        with mock.patch.dict(os.environ, {'TEST_IMA_CLIENT':'cid', 'TEST_IMA_KEY':'akey'}), \
             mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            hits=kr.retrieve_knowledge(q, cfg, {'knowledge_bases':['online.ima.x']})
        ima_hits=[h for h in hits if h.source == 'ima']
        self.assertTrue(any('工作号真实号用于号码认证场景' in h.content for h in ima_hits))
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn('lewis4438136:', calls[0])
        self.assertIn('@飞扬的跟屁虫', calls[0])
        self.assertTrue(any('@' not in c and '工作号' in c and '真实号' in c for c in calls[1:]))
        self.assertNotIn('m-other', '\n'.join(h.label for h in ima_hits))

    def test_generate_reply_reports_hits(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg={'wiki_dir': str(root), 'knowledge_bases': {'scene.a': {'type':'local','path':'scenes/a'}}, 'reply_engine': {}}
        d=generate_reply('小助手 苹果是什么', {'knowledge_bases':['scene.a']}, cfg)
        self.assertTrue(d.should_reply)
        self.assertTrue(any('scene.a' in x for x in d.wiki_hits))

    def test_generate_reply_tool_agent_sync_uses_deep_agent_provider(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg={
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type':'local','path':'scenes/a'}},
            'reply_engine': {'agent_mode': 'tool_agent'},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = _tool_agent_result_mock()
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases':['scene.a']}, cfg)

        self.assertTrue(d.should_reply)
        self.assertIn('工具查询结果回复', d.reply_text)
        self.assertEqual(d.retrieval_debug.get('agent_mode'), 'tool_agent')
        self.assertEqual(d.retrieval_debug.get('route'), 'tool_agent_sync')
        instance.run.assert_called_once()
        job = instance.run.call_args.args[0]
        payload = job.get('payload') or job
        self.assertEqual(payload.get('agent_mode'), 'tool_agent')
        self.assertTrue(payload.get('reliable_result_contract'))
        self.assertEqual(payload.get('_allowed_kb_ids'), ['scene.a'])
        self.assertTrue(any(t.get('name') == 'leann_search' for t in payload.get('available_tools', [])))

    def test_generate_reply_tool_agent_sync_falls_back_when_provider_fails(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg={
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type':'local','path':'scenes/a'}},
            'reply_engine': {'agent_mode': 'tool_agent'},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = mock.Mock(ok=False, status='failed', reply_text='', raw={})
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases':['scene.a']}, cfg)

        self.assertTrue(d.should_reply)
        # When the deep-agent provider fails, we should fall back to standard retrieval.
        self.assertTrue(any('scene.a' in x for x in d.wiki_hits))
        self.assertIn('tool_agent', d.retrieval_debug.get('agent_mode', ''))

    def test_generate_reply_tool_agent_sync_silent_decision(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'agent_mode': 'tool_agent'},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = _tool_agent_result_mock(reply_text="", action="silent", status="silent")
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases': ['scene.a']}, cfg)
        self.assertFalse(d.should_reply)
        self.assertEqual(d.reply_text, "")
        self.assertEqual(d.intent, "tool_agent_silent")
        self.assertEqual(d.risk_level, "low")

    def test_generate_reply_tool_agent_sync_escalate_decision(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'agent_mode': 'tool_agent'},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = _tool_agent_result_mock(reply_text="", action="escalate", status="escalated", risk_level="high")
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases': ['scene.a']}, cfg)
        self.assertFalse(d.should_reply)
        self.assertEqual(d.reply_text, "")
        self.assertEqual(d.intent, "tool_agent_escalate")
        self.assertEqual(d.risk_level, "high")
        self.assertTrue(d.need_human)

    def test_generate_reply_tool_agent_sync_no_display_text_fallback(self):
        """A raw display-text reply_text must not be used when action is not reply."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'agent_mode': 'tool_agent'},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = mock.Mock(
                ok=True, status="done", reply_text="display text fallback",
                raw={"agent_result": {"action": "silent", "reply_text": "", "reason_code": "test", "risk_level": "low"}}
            )
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases': ['scene.a']}, cfg)
        self.assertFalse(d.should_reply)
        self.assertEqual(d.reply_text, "")
        self.assertEqual(d.intent, "tool_agent_silent")

    def test_generate_reply_tool_agent_sync_reply_requires_contract_reply_text(self):
        """Strict reply action cannot fall back to AgentResult.reply_text."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'agent_mode': 'tool_agent'},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = mock.Mock(
                ok=True,
                status="done",
                reply_text="display text fallback",
                raw={"agent_result": {"action": "reply", "reply_text": "", "reason_code": "test", "risk_level": "low"}},
            )
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases': ['scene.a']}, cfg)
        self.assertNotIn("display text fallback", d.reply_text)

    def test_provider_from_config_reads_max_tool_rounds_and_leann_cli_path(self):
        cfg = {
            'agent_provider': {
                'default': 'hermes',
                'providers': {
                    'hermes': {
                        'cli_path': 'hermes',
                        'max_tool_rounds': 5,
                        'leann_cli_path': 'custom-leann',
                    }
                },
                'instances': [
                    {
                        'id': 'deep-1',
                        'provider': 'hermes',
                        'cli_path': 'hermes',
                        'max_tool_rounds': 7,
                        'leann_cli_path': 'instance-leann',
                    }
                ],
            }
        }
        legacy = ap.provider_from_config(cfg)
        self.assertEqual(legacy.max_tool_rounds, 5)
        self.assertEqual(legacy.leann_cli_path, 'custom-leann')

        inst = ap.provider_from_config(cfg, instance_id='deep-1')
        self.assertEqual(inst.max_tool_rounds, 7)
        self.assertEqual(inst.leann_cli_path, 'instance-leann')

    def test_tool_agent_available_tools_default_to_leann_only(self):
        cfg = {'reply_engine': {'agent_mode': 'tool_agent'}}
        tools = _tool_agent_available_tools(cfg)
        self.assertEqual([t['name'] for t in tools], ['leann_search'])

    def test_tool_agent_available_tools_is_leann_only_even_when_mcp_enabled(self):
        cfg = {'reply_engine': {'agent_mode': 'tool_agent', 'enable_wechat_mcp_tool': True}}
        tools = _tool_agent_available_tools(cfg)
        self.assertEqual([t['name'] for t in tools], ['leann_search'])

    def test_generate_reply_tool_agent_passes_mcp_hint_flag_when_enabled(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'agent_mode': 'tool_agent', 'enable_wechat_mcp_tool': True},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = _tool_agent_result_mock()
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases': ['scene.a']}, cfg)

        self.assertTrue(d.should_reply)
        self.assertIn('工具查询结果回复', d.reply_text)
        instance.run.assert_called_once()
        job = instance.run.call_args.args[0]
        payload = job.get('payload') or job
        tool_names = [t.get('name') for t in payload.get('available_tools', [])]
        self.assertEqual(tool_names, ['leann_search'])
        self.assertTrue(payload.get('enable_wechat_mcp_tool'))

    def test_async_raw_agent_propagates_tool_agent_when_delegate_to_agent(self):
        """Queued deep-agent jobs must keep tool_agent mode; Hermes MCP handles tool loop."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'mode': 'raw_agent', 'agent_mode': 'tool_agent', 'wechat_auto_skill': 'wechat_auto'},
        }
        message = {
            'content': '小助手 分析一下苹果',
            'local_id': 1,
            'local_type': 1,
        }
        with mock.patch('reply_engine._agent_jobs.enqueue_job') as mock_enqueue:
            d = generate_reply(message, {'knowledge_bases': ['scene.a'], 'mode': 'customer_service'}, cfg)

        self.assertTrue(d.should_reply)
        self.assertIn('处理', d.reply_text)
        mock_enqueue.assert_called_once()
        payload = mock_enqueue.call_args.kwargs['payload']
        self.assertEqual(payload['agent_mode'], 'tool_agent')
        self.assertEqual(payload['skill_name'], 'wechat_auto')
        self.assertTrue(len(payload.get('available_tools', [])) > 0)
        self.assertEqual(payload['available_tools'][0].get('name'), 'leann_search')
        self.assertIn('cli_path', payload.get('leann', {}))
        self.assertEqual(payload.get('knowledge_hits', []), [])
        self.assertEqual(payload.get('image_descriptions', []), [])
        self.assertIn('wechat_side', payload)
        self.assertEqual(payload['wechat_side'].get('responsibilities'), ['listen', 'trigger', 'session', 'image_extract', 'send'])
        self.assertIn('reply_generation', payload['wechat_side'].get('delegated_to_agent', []))
        self.assertIn('scene.a', payload['retrieval_debug']['selected_kbs'])

    def test_tool_agent_allowed_leann_indexes_from_bound_kbs(self):
        cfg = {
            'knowledge_bases': {
                'leann.a': {'type': 'leann', 'index_name': 'idx_a'},
                'leann.b': {'type': 'leann', 'index_name': 'idx_b'},
                'scene.a': {'type': 'local', 'path': 'scenes/a'},
            }
        }
        target = {'knowledge_bases': ['leann.a', 'leann.b', 'scene.a']}
        self.assertEqual(sorted(_tool_agent_allowed_leann_indexes(cfg, target)), ['idx_a', 'idx_b'])

    def test_tool_agent_available_tools_restricts_to_allowed_indexes(self):
        cfg = {
            'knowledge_bases': {
                'leann.a': {'type': 'leann', 'index_name': 'idx_a'},
                'leann.b': {'type': 'leann', 'index_name': 'idx_b'},
            }
        }
        target = {'knowledge_bases': ['leann.a', 'leann.b']}
        tools = _tool_agent_available_tools(cfg, target)
        self.assertEqual([t['name'] for t in tools], ['leann_search'])
        self.assertEqual(sorted(tools[0]['allowed_index_names']), ['idx_a', 'idx_b'])
        self.assertEqual(tools[0]['default_index_name'], 'idx_a')

    def test_generate_reply_tool_agent_passes_allowed_index_names(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {
                'leann.a': {'type': 'leann', 'index_name': 'idx_a'},
            },
            'reply_engine': {'agent_mode': 'tool_agent'},
        }
        with mock.patch('agent_provider.HermesProvider') as MockProvider:
            instance = MockProvider.return_value
            instance.run.return_value = _tool_agent_result_mock()
            d = generate_reply('小助手 苹果是什么', {'knowledge_bases': ['leann.a']}, cfg)

        self.assertTrue(d.should_reply)
        job = instance.run.call_args.args[0]
        payload = job.get('payload') or job
        self.assertEqual(payload.get('_allowed_kb_ids'), ['leann.a'])
        tool = payload['available_tools'][0]
        self.assertEqual(tool['allowed_index_names'], ['idx_a'])
        self.assertEqual(tool['default_index_name'], 'idx_a')

    def test_thin_monitor_delegates_vision_to_agent(self):
        """Thin-monitor must not pre-describe images; vision is delegated to the agent."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'mode': 'raw_agent', 'agent_mode': 'tool_agent', 'thin_monitor': True},
        }
        img = Path(td.name) / 'img.jpg'
        img.write_bytes(b'fake')
        message = {
            'content': '[图片]',
            'local_id': 1,
            'local_type': 3,
            'image_path': str(img),
        }
        with mock.patch('reply_engine._local_image_descriptions') as mock_desc, \
             mock.patch('reply_engine._agent_jobs.enqueue_job') as mock_enqueue:
            d = generate_reply(message, {'knowledge_bases': ['scene.a'], 'mode': 'customer_service'}, cfg)

        self.assertTrue(d.should_reply)
        self.assertIn('正在处理', d.reply_text)
        mock_desc.assert_not_called()
        mock_enqueue.assert_called_once()
        payload = mock_enqueue.call_args.kwargs['payload']
        self.assertEqual(payload['image_paths'], [str(img)])
        self.assertEqual(payload['image_descriptions'], [])

    def test_tool_agent_non_thin_keeps_agent_side_vision(self):
        """Non-thin tool_agent should not pre-describe images; vision is delegated."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            'wiki_dir': str(root),
            'knowledge_bases': {'scene.a': {'type': 'local', 'path': 'scenes/a'}},
            'reply_engine': {'mode': 'raw_agent', 'agent_mode': 'tool_agent'},
        }
        img = Path(td.name) / 'img.jpg'
        img.write_bytes(b'fake')
        message = {
            'content': '[图片]',
            'local_id': 1,
            'local_type': 3,
            'image_path': str(img),
        }
        with mock.patch('reply_engine._local_image_descriptions') as mock_desc, \
             mock.patch('reply_engine._agent_jobs.enqueue_job') as mock_enqueue:
            d = generate_reply(message, {'knowledge_bases': ['scene.a'], 'mode': 'customer_service'}, cfg)

        self.assertTrue(d.should_reply)
        mock_desc.assert_not_called()
        mock_enqueue.assert_called_once()
        payload = mock_enqueue.call_args.kwargs['payload']
        self.assertEqual(payload['image_paths'], [str(img)])
        self.assertEqual(payload['image_descriptions'], [])

    def test_describe_images_for_agent_records_source_and_error(self):
        """_describe_images_for_agent must include vision_source and vision_error."""
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        img = Path(td.name) / 'img.jpg'
        img.write_bytes(b'fake')
        from reply_engine import _describe_images_for_agent
        with mock.patch('image_handler.recognize_image_with_fallback', return_value=('a cat', 'mmx', None)):
            descs = _describe_images_for_agent([str(img)], 'describe', target={}, config={})
        self.assertEqual(len(descs), 1)
        self.assertEqual(descs[0]['description'], 'a cat')
        self.assertEqual(descs[0]['vision_source'], 'mmx')
        self.assertEqual(descs[0]['vision_error'], '')

        with mock.patch('image_handler.recognize_image_with_fallback', return_value=('[VLM Error] all vision sources failed', None, 'all failed')):
            descs = _describe_images_for_agent([str(img)], 'describe', target={}, config={})
        self.assertEqual(descs[0]['vision_source'], '')
        self.assertEqual(descs[0]['vision_error'], 'all failed')

if __name__ == '__main__':
    unittest.main()
