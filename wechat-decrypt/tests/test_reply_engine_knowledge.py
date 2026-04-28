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
from reply_engine import retrieve_knowledge, generate_reply

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
        hits=retrieve_knowledge('完全无关问题', cfg, {'knowledge_bases': []})
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
        hits=retrieve_knowledge('苹果', cfg, {'knowledge_bases':['scene.a']})
        labels='\n'.join(h.label for h in hits)
        self.assertIn('scene.a', labels)
        self.assertNotIn('scene.b', labels)
        hits2=retrieve_knowledge('香蕉', cfg, {'knowledge_bases':['scene.a','scene.b']})
        self.assertIn('scene.b', '\n'.join(h.label for h in hits2))

    def test_ima_without_key_is_safe_no_hit(self):
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg={'wiki_dir': str(root), 'knowledge_bases': {'online.ima.x': {'type':'ima','api_key_env':'NON_EXISTENT_IMA_KEY_FOR_TEST'}}}
        hits=retrieve_knowledge('whatever', cfg, {'knowledge_bases':['online.ima.x']})
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
            hits=retrieve_knowledge('苹果', cfg, {'knowledge_bases':['online.ima.x']})
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
            hits=retrieve_knowledge(q, cfg, {'knowledge_bases':['online.ima.x']})
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
        cfg={'wiki_dir': str(root), 'knowledge_bases': {'scene.a': {'type':'local','path':'scenes/a'}}, 'reply_engine': {'use_subagent': False}}
        d=generate_reply('小助手 苹果是什么', {'knowledge_bases':['scene.a']}, cfg)
        self.assertTrue(d.should_reply)
        self.assertTrue(any('scene.a' in x for x in d.wiki_hits))

if __name__ == '__main__':
    unittest.main()
