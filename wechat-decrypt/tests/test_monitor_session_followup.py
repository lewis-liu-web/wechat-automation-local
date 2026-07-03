#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Follow-up intent session tests after removing the keyword whitelist."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / 'wechat_bot_monitor.py'

spec = importlib.util.spec_from_file_location('wechat_bot_monitor_followup_test', MONITOR_PATH)
assert spec is not None and spec.loader is not None
monitor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = monitor
spec.loader.exec_module(monitor)


class SessionFollowupTests(unittest.TestCase):
    """Session gate: bare statements, acks, close hints, images."""

    def setUp(self):
        monitor._active_sessions.clear()

    def tearDown(self):
        monitor._active_sessions.clear()

    def _make_target(self, mode="group_assistant", session_policy=None):
        policy = session_policy or {}
        return {
            "name": "bot群聊测试",
            "username": "47965620946@chatroom",
            "db": "message_0.db",
            "table": "Msg_abc",
            "mode": mode,
            "session_policy": policy,
        }

    def _make_msg(self, text, sender_id=7, image_path=None):
        msg = {
            "message_content": "lewis4438136:\n%s" % text,
            "real_sender_id": sender_id,
            "sender_username": "lewis4438136",
        }
        if image_path:
            msg["image_path"] = image_path
        return msg

    def _cfg(self, target):
        return {"targets": [target]}

    def test_bare_statement_is_followup(self):
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        monitor._activate_session(t, trigger, cfg)
        key = monitor._session_key(t, trigger)
        followup = self._make_msg("公交卡充值失败")
        self.assertTrue(monitor._is_in_session(t, followup, cfg))
        self.assertIn(key, monitor._active_sessions)
    def test_ack_only_stays_in_session(self):
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        for text in ("好的", "嗯嗯", "OK", "👌"):
            with self.subTest(text=text):
                monitor._active_sessions.clear()
                monitor._activate_session(t, trigger, cfg)
                key = monitor._session_key(t, trigger)
                followup = self._make_msg(text)
                self.assertTrue(monitor._is_in_session(t, followup, cfg))
                self.assertIn(key, monitor._active_sessions)

    def test_close_hint_expires_session(self):
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        for text in ("谢谢", "好了", "没事了", "不需要", "算了"):
            with self.subTest(text=text):
                monitor._active_sessions.clear()
                monitor._activate_session(t, trigger, cfg)
                key = monitor._session_key(t, trigger)
                followup = self._make_msg(text)
                self.assertFalse(monitor._is_in_session(t, followup, cfg))
                self.assertNotIn(key, monitor._active_sessions)

    def test_close_hint_不需要_and_算了_expire_session(self):
        """Regression: cues recognized by reply_decision must also expire the monitor session."""
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        for text in ("不需要", "算了"):
            with self.subTest(text=text):
                monitor._active_sessions.clear()
                monitor._activate_session(t, trigger, cfg)
                key = monitor._session_key(t, trigger)
                followup = self._make_msg(text)
                self.assertFalse(monitor._is_in_session(t, followup, cfg))
                self.assertNotIn(key, monitor._active_sessions)

    def test_image_only_is_followup(self):
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 看下这张图")
        monitor._activate_session(t, trigger, cfg)
        key = monitor._session_key(t, trigger)
        followup = self._make_msg("", image_path="/tmp/screen.png")
        self.assertTrue(monitor._is_in_session(t, followup, cfg))
        self.assertIn(key, monitor._active_sessions)

    def test_empty_text_not_a_followup(self):
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        monitor._activate_session(t, trigger, cfg)
        key = monitor._session_key(t, trigger)
        followup = {
            "message_content": "",
            "real_sender_id": 7,
            "sender_username": "lewis4438136",
        }
        self.assertFalse(monitor._is_in_session(t, followup, cfg))
        self.assertIn(key, monitor._active_sessions)
        t = self._make_target(session_policy={"require_followup_intent": False})
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        monitor._activate_session(t, trigger, cfg)
        followup = self._make_msg("公交卡充值失败")
        self.assertFalse(monitor._is_in_session(t, followup, cfg))

    def test_close_cue_with_trigger_expires_session(self):
        """A message that matches a trigger AND is a close cue must close the session."""
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        monitor._activate_session(t, trigger, cfg)
        key = monitor._session_key(t, trigger)
        close_msg = self._make_msg("好的，谢谢")
        plan = type('Plan', (), {'reply_mode': 'silent', 'should_reply': False, 'reason': 'session_close_or_non_followup'})()
        monitor._maybe_expire_session_on_close_cue(t, close_msg, plan, {'session_active': True})
        self.assertNotIn(key, monitor._active_sessions)

    def test_ack_only_with_trigger_keeps_session(self):
        """A pure ack that happens to match a trigger should not expire the session."""
        t = self._make_target()
        cfg = self._cfg(t)
        trigger = self._make_msg("@飞扬的跟屁虫 怎么退押金")
        monitor._activate_session(t, trigger, cfg)
        key = monitor._session_key(t, trigger)
        ack_msg = self._make_msg("好的")
        plan = type('Plan', (), {'reply_mode': 'silent', 'should_reply': False, 'reason': 'ack_only'})()
        monitor._maybe_expire_session_on_close_cue(t, ack_msg, plan, {'session_active': True})
        self.assertIn(key, monitor._active_sessions)


if __name__ == '__main__':
    unittest.main()
