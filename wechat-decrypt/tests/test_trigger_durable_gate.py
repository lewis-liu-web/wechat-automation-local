"""P0-1: trigger / free / session gate before durable ingress."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import wechat_bot_monitor as monitor


class TestIsTrigger(unittest.TestCase):
    def _msg(self, text, **kw):
        m = {
            "local_id": 10,
            "message_content": text,
            "status": 0,
            "sender_username": "wxid_user",
            "real_sender_id": "wxid_user",
        }
        m.update(kw)
        return m

    def test_free_mode_accepts_any_text(self):
        cfg = {"default_response_mode": "trigger", "default_triggers": ["飞扬"]}
        t = {"response_mode": "free", "triggers": []}
        self.assertTrue(monitor.is_trigger(cfg, t, self._msg("随便聊聊")))

    def test_trigger_mode_keyword_match(self):
        cfg = {"default_response_mode": "trigger", "default_triggers": ["飞扬"]}
        t = {"response_mode": "trigger", "triggers": ["飞扬的跟屁虫"]}
        self.assertTrue(monitor.is_trigger(cfg, t, self._msg("飞扬的跟屁虫 怎么退押金")))
        self.assertFalse(monitor.is_trigger(cfg, t, self._msg("今天天气怎么样")))

    def test_empty_triggers_silent(self):
        cfg = {"default_response_mode": "trigger", "default_triggers": ["飞扬"]}
        t = {"response_mode": "trigger", "triggers": []}
        self.assertFalse(monitor.is_trigger(cfg, t, self._msg("飞扬 帮我看看")))

    def test_none_triggers_falls_back_to_default(self):
        cfg = {"default_response_mode": "trigger", "default_triggers": ["飞扬"]}
        t = {"response_mode": "trigger"}  # triggers missing
        self.assertTrue(monitor.is_trigger(cfg, t, self._msg("飞扬 你好")))
        self.assertFalse(monitor.is_trigger(cfg, t, self._msg("无关消息")))

    def test_self_sent_never_triggers(self):
        cfg = {"default_response_mode": "trigger", "default_triggers": ["飞扬"]}
        t = {"response_mode": "free", "triggers": []}
        self.assertFalse(monitor.is_trigger(cfg, t, self._msg("飞扬", status=2)))


class TestShouldEnterDurable(unittest.TestCase):
    def setUp(self):
        monitor._active_sessions.clear()

    def tearDown(self):
        monitor._active_sessions.clear()

    def _cfg(self, target):
        return {
            "default_response_mode": "trigger",
            "default_triggers": ["飞扬"],
            "targets": [target],
            "session_window": 120,
        }

    def _target(self, **kw):
        t = {
            "name": "bot群聊测试",
            "username": "100001@chatroom",
            "response_mode": "trigger",
            "triggers": ["飞扬的跟屁虫"],
            "mode": "group_assistant",
            "session_policy": {"timeout_seconds": 120, "max_turns": 5, "require_followup_intent": True},
        }
        t.update(kw)
        return t

    def _msg(self, text, sender="wxid_user", **kw):
        m = {
            "local_id": 10,
            "message_content": text,
            "status": 0,
            "sender_username": sender,
            "real_sender_id": sender,
        }
        m.update(kw)
        return m

    def test_free_always_enters(self):
        t = self._target(response_mode="free", triggers=[])
        enter, hit, sess = monitor.should_enter_durable(self._cfg(t), t, self._msg("闲聊一句"))
        self.assertTrue(enter)
        self.assertTrue(hit)
        self.assertFalse(sess)

    def test_trigger_miss_skips(self):
        t = self._target()
        enter, hit, sess = monitor.should_enter_durable(self._cfg(t), t, self._msg("今天天气怎么样"))
        self.assertFalse(enter)
        self.assertFalse(hit)
        self.assertFalse(sess)

    def test_trigger_hit_enters(self):
        t = self._target()
        enter, hit, sess = monitor.should_enter_durable(
            self._cfg(t), t, self._msg("飞扬的跟屁虫 怎么退押金")
        )
        self.assertTrue(enter)
        self.assertTrue(hit)
        self.assertFalse(sess)

    def test_empty_triggers_skip(self):
        t = self._target(triggers=[])
        enter, hit, sess = monitor.should_enter_durable(self._cfg(t), t, self._msg("飞扬 帮我"))
        self.assertFalse(enter)
        self.assertFalse(hit)

    def test_active_session_followup_enters_without_keyword(self):
        t = self._target()
        cfg = self._cfg(t)
        trigger = self._msg("飞扬的跟屁虫 怎么退押金")
        monitor._activate_session(t, trigger, cfg)
        enter, hit, sess = monitor.should_enter_durable(cfg, t, self._msg("还是失败了"))
        self.assertTrue(enter)
        self.assertFalse(hit)
        self.assertTrue(sess)

    def test_source_order_gate_before_durable(self):
        text = Path(monitor.__file__).read_text(encoding="utf-8")
        # Compare call sites in the main loop, not the function definitions.
        gate = text.find("_enter, _trigger_hit, _session_active = should_enter_durable(cfg, t, m)")
        durable = text.find("_rl_result = durable_ingress_event(")
        self.assertGreater(gate, 0)
        self.assertGreater(durable, gate)
        # after precheck, before durable
        precheck = text.find("# --- precheck boundary")
        self.assertGreater(gate, precheck)
        self.assertIn("'trigger_skip'", text)
        self.assertIn("trigger_skip", monitor._PER_MESSAGE_REASONS)


class TestTriggerSkipCursorReason(unittest.TestCase):
    def test_trigger_skip_is_per_message_reason(self):
        self.assertIn("trigger_skip", monitor._PER_MESSAGE_REASONS)


if __name__ == "__main__":
    unittest.main()
