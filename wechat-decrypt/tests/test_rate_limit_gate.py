"""P0 rate-limit gate after trigger/free/session gate and before durable ingress."""

from __future__ import annotations

import unittest
from pathlib import Path

import wechat_bot_monitor as monitor


class TestRateLimitGate(unittest.TestCase):
    def setUp(self):
        monitor.reset_rate_limit_state()

    def tearDown(self):
        monitor.reset_rate_limit_state()

    def _target(self, **kw):
        t = {
            "name": "bot群聊测试",
            "username": "100001@chatroom",
            "db": "message_0.db",
            "table": "Msg_abc123",
        }
        t.update(kw)
        return t

    def _msg(self, sender="wxid_user", local_id=10):
        return {
            "local_id": local_id,
            "message_content": "hello",
            "status": 0,
            "sender_username": sender,
            "real_sender_id": sender,
        }

    def test_default_allows_first_three_denies_fourth(self):
        cfg = {}
        t = self._target()
        m = self._msg()
        for i in range(3):
            ok, info = monitor.check_rate_limit(cfg, t, m, now=1000.0 + i)
            self.assertTrue(ok, f"hit {i} should be allowed")
            self.assertEqual(info["max_enters"], 3)
            self.assertEqual(info["window_seconds"], 60)
            self.assertEqual(info["count"], i + 1)
        ok, info = monitor.check_rate_limit(cfg, t, m, now=1003.0)
        self.assertFalse(ok)
        self.assertEqual(info["count"], 3)

    def test_window_expiry_allows_again(self):
        cfg = {"rate_limit": {"window_seconds": 10, "max_enters": 2}}
        t = self._target()
        m = self._msg()
        ok, _ = monitor.check_rate_limit(cfg, t, m, now=1000.0)
        self.assertTrue(ok)
        ok, _ = monitor.check_rate_limit(cfg, t, m, now=1005.0)
        self.assertTrue(ok)
        ok, info = monitor.check_rate_limit(cfg, t, m, now=1006.0)
        self.assertFalse(ok)
        # First hit has now expired, so the window only contains the second hit.
        ok, info = monitor.check_rate_limit(cfg, t, m, now=1011.0)
        self.assertTrue(ok)
        self.assertEqual(info["count"], 2)

    def test_enabled_false_always_allows(self):
        cfg = {"rate_limit": {"enabled": False, "window_seconds": 10, "max_enters": 1}}
        t = self._target()
        m = self._msg()
        for i in range(5):
            ok, info = monitor.check_rate_limit(cfg, t, m, now=1000.0 + i)
            self.assertTrue(ok)
            self.assertEqual(info["count"], 0)
            self.assertIs(info.get("enabled"), False)

    def test_per_sender_isolation(self):
        cfg = {"rate_limit": {"window_seconds": 60, "max_enters": 2}}
        t = self._target()
        m1 = self._msg(sender="wxid_a")
        m2 = self._msg(sender="wxid_b")
        ok, _ = monitor.check_rate_limit(cfg, t, m1, now=1000.0)
        self.assertTrue(ok)
        ok, _ = monitor.check_rate_limit(cfg, t, m1, now=1001.0)
        self.assertTrue(ok)
        # Sender b is independent even though a is at the limit.
        ok, _ = monitor.check_rate_limit(cfg, t, m2, now=1002.0)
        self.assertTrue(ok)
        ok, info = monitor.check_rate_limit(cfg, t, m1, now=1003.0)
        self.assertFalse(ok)
        self.assertEqual(info["count"], 2)

    def test_target_rate_limit_overrides_cfg(self):
        cfg = {"rate_limit": {"window_seconds": 60, "max_enters": 5}}
        t = self._target(rate_limit={"window_seconds": 10, "max_enters": 1})
        m = self._msg()
        ok, _ = monitor.check_rate_limit(cfg, t, m, now=1000.0)
        self.assertTrue(ok)
        ok, info = monitor.check_rate_limit(cfg, t, m, now=1001.0)
        self.assertFalse(ok)
        self.assertEqual(info["max_enters"], 1)
        self.assertEqual(info["window_seconds"], 10)

    def test_rate_limit_skip_is_per_message_reason(self):
        self.assertIn("rate_limit_skip", monitor._PER_MESSAGE_REASONS)

    def test_source_order_after_gate_before_durable_ingress(self):
        text = Path(monitor.__file__).read_text(encoding="utf-8")
        gate = text.find("_enter, _trigger_hit, _session_active = should_enter_durable(cfg, t, m)")
        rate_limit = text.find("_rl_ok, _rl_info = check_rate_limit(cfg, t, m)")
        durable = text.find("_rl_result = durable_ingress_event(")
        self.assertGreater(gate, 0)
        self.assertGreater(rate_limit, gate)
        self.assertGreater(durable, rate_limit)
        self.assertIn("'rate_limit_skip'", text)
        self.assertIn("rate_limit_skip", monitor._PER_MESSAGE_REASONS)


if __name__ == "__main__":
    unittest.main()
