"""Unit tests for the mtime-cached loaders and wait_sent_confirmation.

These guard three contracts:
- load_config and load_contact_name_map skip work when file mtime is stable.
- save_config invalidates the config cache; external mtime change forces
  a reload of contact names.
- wait_sent_confirmation skips run_fast_refresh when the raw DB mtime has
  not advanced since the last refresh, and re-refreshes when it moves.

The tests are hermetic: each test writes its own temp files in a temp dir
and uses module-level cache invalidation to avoid bleeding state between
tests.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / 'wechat_bot_monitor.py'

spec = importlib.util.spec_from_file_location('wechat_bot_monitor_under_test', MONITOR_PATH)
assert spec is not None and spec.loader is not None
monitor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = monitor
spec.loader.exec_module(monitor)


class LoadConfigCacheTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / 'targets.json'
        self.path.write_text(json.dumps({'targets': []}), encoding='utf-8')
        monitor._CONFIG_CACHE.clear()

    def test_returns_cached_dict_when_mtime_unchanged(self):
        cfg1 = monitor.load_config(self.path)
        cfg1['targets'].append({'name': 'X', 'username': 'x', 'table': 'Msg_x', 'db': 'message_0.db', 'last_local_id': 0})
        cfg2 = monitor.load_config(self.path)
        self.assertIs(cfg1, cfg2)

    def test_reloads_when_mtime_changes(self):
        cfg1 = monitor.load_config(self.path)
        cfg1['targets'].append({'name': 'X', 'username': 'x', 'table': 'Msg_x', 'db': 'message_0.db', 'last_local_id': 0})
        time.sleep(0.01)
        self.path.write_text(json.dumps({'targets': [{'name': 'Y', 'username': 'y'}]}), encoding='utf-8')
        cfg2 = monitor.load_config(self.path)
        self.assertIsNot(cfg1, cfg2)
        self.assertEqual(cfg2['targets'][0]['name'], 'Y')
        self.assertEqual(len(monitor._CONFIG_CACHE), 1)
        cached = next(iter(monitor._CONFIG_CACHE.values()))
        self.assertIs(cached[1], cfg2)

    def test_save_config_invalidates_cache(self):
        cfg1 = monitor.load_config(self.path)
        cfg1['targets'].append({'name': 'Z', 'username': 'z', 'table': 'Msg_z', 'db': 'message_0.db', 'last_local_id': 0})
        monitor.save_config(cfg1, self.path)
        cfg2 = monitor.load_config(self.path)
        self.assertIsNot(cfg1, cfg2)
        self.assertEqual(cfg2['targets'][0]['name'], 'Z')


class LoadContactCacheTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.db_path = Path(self.td.name) / 'contact.db'
        self._write_contact([('alice', 'Alice备注', '', '')])
        monitor._CONTACT_CACHE.clear()

    def _write_contact(self, rows):
        if self.db_path.exists():
            self.db_path.unlink()
        con = sqlite3.connect(self.db_path)
        con.execute('create table contact (username text, remark text, nick_name text, alias text)')
        con.executemany('insert into contact values (?, ?, ?, ?)', rows)
        con.commit()
        con.close()

    def test_returns_dict_when_mtime_unchanged(self):
        names1 = monitor.load_contact_name_map(self.db_path)
        self.assertEqual(names1, {'alice': 'Alice备注'})
        names2 = monitor.load_contact_name_map(self.db_path)
        self.assertEqual(names2, {'alice': 'Alice备注'})
        names1['poison'] = 'X'
        names3 = monitor.load_contact_name_map(self.db_path)
        self.assertNotIn('poison', names3)

    def test_reloads_when_mtime_changes(self):
        monitor.load_contact_name_map(self.db_path)
        time.sleep(0.01)
        self._write_contact([('bob', 'Bob备注', '', '')])
        names = monitor.load_contact_name_map(self.db_path)
        self.assertEqual(names, {'bob': 'Bob备注'})

    def test_missing_db_returns_empty_without_caching_error(self):
        path = Path(self.td.name) / 'nope.db'
        names = monitor.load_contact_name_map(path)
        self.assertEqual(names, {})

    def test_no_contact_table_returns_empty(self):
        con = sqlite3.connect(self.db_path)
        con.execute('drop table contact')
        con.commit()
        con.close()
        time.sleep(0.01)
        names = monitor.load_contact_name_map(self.db_path)
        self.assertEqual(names, {})


class WaitSentConfirmationRefreshTests(unittest.TestCase):
    """Verify wait_sent_confirmation skips run_fast_refresh when raw DB mtime is stable."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.db_dir = Path(self.td.name) / 'db_storage' / 'message'
        self.db_dir.mkdir(parents=True)
        self.raw_db = self.db_dir / 'message_0.db'
        self.raw_db.write_bytes(b'')
        monitor._CONFIG_CACHE.clear()
        monitor._CONTACT_CACHE.clear()

    def _target(self):
        return {'name': 'T', 'username': 'x', 'table': 'Msg_test', 'db': 'message_0.db'}

    def test_raw_db_dir_unresolvable_still_refreshes(self):
        calls = []

        def fake_run_fast_refresh(config_path):
            calls.append(1)
            return (0, 0.0, 0, 0, 'fake')

        monitor.run_fast_refresh = fake_run_fast_refresh
        monitor.has_self_sent_after = lambda *a, **k: None
        with patch.object(monitor, '_read_db_dir', return_value=None):
            ok = monitor.wait_sent_confirmation(self._target(), 0, 'hi', timeout=0.6)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(calls), 1)

    def test_skips_refresh_when_raw_db_mtime_stable(self):
        calls = []

        def fake_run_fast_refresh(config_path):
            calls.append(1)
            return (0, 0.0, 0, 0, 'fake')

        monitor.run_fast_refresh = fake_run_fast_refresh
        monitor.has_self_sent_after = lambda *a, **k: None
        with patch.object(monitor, '_read_db_dir', return_value=str(self.db_dir.parent)):
            ok = monitor.wait_sent_confirmation(self._target(), 0, 'hi', timeout=1.2)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)

    def test_refreshes_again_when_raw_db_mtime_moves(self):
        calls = []

        def fake_run_fast_refresh(config_path):
            calls.append(1)
            return (0, 0.0, 0, 0, 'fake')

        monitor.run_fast_refresh = fake_run_fast_refresh
        monitor.has_self_sent_after = lambda *a, **k: None
        with patch.object(monitor, '_read_db_dir', return_value=str(self.db_dir.parent)):
            monitor.wait_sent_confirmation(self._target(), 0, 'hi', timeout=0.6)
            time.sleep(0.01)
            os.utime(self.raw_db, None)
            monitor.wait_sent_confirmation(self._target(), 0, 'hi', timeout=0.6)
        self.assertEqual(len(calls), 2)

    def test_target_raw_db_path_normalizes(self):
        cfg = {'db_dir': str(self.db_dir.parent)}
        p = monitor._target_raw_db_path({'db': 'message_0.db'}, cfg)
        self.assertEqual(p, self.raw_db)
        p2 = monitor._target_raw_db_path({'db': 'bizchat/biz_message_0.db'}, cfg)
        self.assertTrue(str(p2).endswith('bizchat' + os.sep + 'biz_message_0.db'))
        self.assertIsNone(monitor._target_raw_db_path({'db': 'x.db'}, {}))
        self.assertIsNone(monitor._target_raw_db_path({'db': '/etc/passwd'}, cfg))
        self.assertIsNone(monitor._target_raw_db_path({'db': '../x.db'}, cfg))
class SessionCloseModeTests(unittest.TestCase):
    """Session keyword-close should be disabled in personal_assistant/free mode."""

    def setUp(self):
        monitor._active_sessions.clear()

    def tearDown(self):
        monitor._active_sessions.clear()

    def _make_target(self, mode="personal_assistant"):
        return {
            "name": "bot群聊测试",
            "username": "47965620946@chatroom",
            "db": "message_0.db",
            "table": "Msg_abc",
            "mode": mode,
        }

    def _make_msg(self, text, sender_id=7):
        return {
            "message_content": "lewis4438136:\n%s" % text,
            "real_sender_id": sender_id,
            "sender_username": "lewis4438136",
        }

    def test_free_mode_keeps_session_on_casual_close_keyword(self):
        cfg = {"targets": [self._make_target("personal_assistant")]}
        t = cfg["targets"][0]
        msg = self._make_msg("@飞扬的跟屁虫 聊聊天")
        monitor._activate_session(t, msg, cfg)
        key = monitor._session_key(t, msg)
        self.assertIn(key, monitor._active_sessions)
        # Casual phrase containing "好啦" should NOT close session in free mode
        followup = self._make_msg("无所谓好不好啦")
        self.assertTrue(monitor._is_in_session(t, followup, cfg))
        self.assertIn(key, monitor._active_sessions)

    def test_balanced_mode_closes_session_on_close_keyword(self):
        cfg = {"targets": [self._make_target("group_assistant")]}
        t = cfg["targets"][0]
        msg = self._make_msg("@飞扬的跟屁虫 介绍一下")
        monitor._activate_session(t, msg, cfg)
        key = monitor._session_key(t, msg)
        self.assertIn(key, monitor._active_sessions)
        followup = self._make_msg("好了")
        self.assertFalse(monitor._is_in_session(t, followup, cfg))
        self.assertNotIn(key, monitor._active_sessions)


if __name__ == '__main__':
    unittest.main()
class SessionCloseModeTests(unittest.TestCase):
    """Session keyword-close should be disabled in personal_assistant/free mode."""

    def setUp(self):
        monitor._active_sessions.clear()

    def tearDown(self):
        monitor._active_sessions.clear()

    def _make_target(self, mode="personal_assistant"):
        return {
            "name": "bot群聊测试",
            "username": "47965620946@chatroom",
            "db": "message_0.db",
            "table": "Msg_abc",
            "mode": mode,
        }

    def _make_msg(self, text, sender_id=7):
        return {
            "message_content": "lewis4438136:\n%s" % text,
            "real_sender_id": sender_id,
            "sender_username": "lewis4438136",
        }

    def test_free_mode_keeps_session_on_casual_close_keyword(self):
        cfg = {"targets": [self._make_target("personal_assistant")]}
        t = cfg["targets"][0]
        msg = self._make_msg("@飞扬的跟屁虫 聊聊天")
        monitor._activate_session(t, msg, cfg)
        key = monitor._session_key(t, msg)
        self.assertIn(key, monitor._active_sessions)
        # Casual phrase containing "好啦" should NOT close session in free mode
        followup = self._make_msg("无所谓好不好啦")
        self.assertTrue(monitor._is_in_session(t, followup, cfg))
        self.assertIn(key, monitor._active_sessions)

    def test_balanced_mode_closes_session_on_close_keyword(self):
        cfg = {"targets": [self._make_target("group_assistant")]}
        t = cfg["targets"][0]
        msg = self._make_msg("@飞扬的跟屁虫 介绍一下")
        monitor._activate_session(t, msg, cfg)
        key = monitor._session_key(t, msg)
        self.assertIn(key, monitor._active_sessions)
        followup = self._make_msg("好了")
        self.assertFalse(monitor._is_in_session(t, followup, cfg))
        self.assertNotIn(key, monitor._active_sessions)
