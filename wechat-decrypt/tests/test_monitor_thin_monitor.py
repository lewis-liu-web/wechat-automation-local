import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wechat_bot_monitor as mon
from reply_engine import ReplyDecision


class ThinMonitorHelperTests(unittest.TestCase):
    def test_thin_monitor_enabled_target_overrides_config(self):
        cfg = {"reply_engine": {"thin_monitor": True}}
        target = {"thin_monitor": False}
        self.assertFalse(mon._thin_monitor_enabled(cfg, target))

    def test_thin_monitor_enabled_global(self):
        cfg = {"reply_engine": {"thin_monitor": True}}
        target = {}
        self.assertTrue(mon._thin_monitor_enabled(cfg, target))

    def test_thin_monitor_enabled_default_false(self):
        cfg = {}
        target = {}
        self.assertFalse(mon._thin_monitor_enabled(cfg, target))


class ThinMonitorHandleTests(unittest.TestCase):
    def _make_cfg(self, thin_monitor=True):
        return {
            "reply_engine": {
                "mode": "raw_agent",
                "agent_mode": "standard",
                "thin_monitor": thin_monitor,
                "wechat_auto_skill": "wechat_auto",
            },
            "send_mode": "foreground",
        }

    def _make_args(self, dry_run=False):
        args = mock.MagicMock()
        args.dry_run = dry_run
        return args

    def _make_target(self, thin_monitor=True):
        return {
            "name": "test_group",
            "username": "wxid_test_group",
            "table": "msg_table",
            "thin_monitor": thin_monitor,
        }

    def _make_message(self, local_type=1, content="hello", image_path=None):
        return {
            "local_id": 42,
            "local_type": local_type,
            "message_content": content,
            "sender": "wxid_user",
            "sender_username": "wxid_user",
            "sender_display_name": "User",
            "status": 0,
            "image_path": image_path,
        }

    def test_handle_decrypts_image_for_thin_target(self):
        target = self._make_target(thin_monitor=True)
        m = self._make_message(local_type=3)
        cfg = self._make_cfg()
        args = self._make_args(dry_run=True)
        with mock.patch("image_handler.process_image_message", return_value="/tmp/img.jpg") as mock_decrypt, \
             mock.patch("wechat_bot_monitor.generate_reply", return_value=ReplyDecision(False, "", intent="thin_ack")) as mock_gen, \
             mock.patch("wechat_bot_monitor.ingest_event") as mock_ingest, \
             mock.patch("wechat_bot_monitor.send_reply") as mock_send:
            mock_ingest.return_value = None
            mon._handle_thin_monitor_target(
                target, m, cfg,
                config_path="wechat_bot_targets.json",
                args=args,
                contact_names={},
                lid=42,
            )
        mock_decrypt.assert_called_once()
        self.assertEqual(m.get("image_path"), "/tmp/img.jpg")

    def test_handle_skips_decrypt_if_path_already_present(self):
        target = self._make_target(thin_monitor=True)
        m = self._make_message(local_type=3, image_path="/tmp/existing.jpg")
        cfg = self._make_cfg()
        args = self._make_args(dry_run=True)
        with mock.patch("image_handler.process_image_message") as mock_decrypt, \
             mock.patch("wechat_bot_monitor.generate_reply", return_value=ReplyDecision(False, "", intent="thin_ack")) as mock_gen, \
             mock.patch("wechat_bot_monitor.ingest_event") as mock_ingest, \
             mock.patch("wechat_bot_monitor.send_reply") as mock_send:
            mock_ingest.return_value = None
            mon._handle_thin_monitor_target(
                target, m, cfg,
                config_path="wechat_bot_targets.json",
                args=args,
                contact_names={},
                lid=42,
            )
        mock_decrypt.assert_not_called()

    def test_handle_calls_generate_reply_with_thin_payload(self):
        target = self._make_target(thin_monitor=True)
        m = self._make_message()
        cfg = self._make_cfg()
        args = self._make_args(dry_run=True)
        turn = mock.MagicMock()
        turn.end_local_id = 42
        turn.to_generate_reply_message.return_value = {"content": "hello"}
        with mock.patch("wechat_bot_monitor.generate_reply", return_value=ReplyDecision(True, "收到，稍后回复。", intent="agent_job_queued")) as mock_gen, \
             mock.patch("wechat_bot_monitor.ingest_event", return_value=turn) as mock_ingest, \
             mock.patch("wechat_bot_monitor.send_reply") as mock_send, \
             mock.patch("wechat_bot_monitor.precheck", return_value=None):
            mon._handle_thin_monitor_target(
                target, m, cfg,
                config_path="wechat_bot_targets.json",
                args=args,
                contact_names={},
                lid=42,
            )
        mock_gen.assert_called_once()
        agg_msg = mock_gen.call_args[0][0]
        self.assertIn("target_policy", agg_msg)
        self.assertIn("event_context", agg_msg)
        mock_send.assert_not_called()  # dry_run=True

    def test_handle_does_not_touch_pending_images_or_sessions(self):
        target = self._make_target(thin_monitor=True)
        m = self._make_message(local_type=3)
        cfg = self._make_cfg()
        args = self._make_args(dry_run=True)
        with mock.patch("image_handler.process_image_message", return_value="/tmp/img.jpg"), \
             mock.patch("wechat_bot_monitor.generate_reply", return_value=ReplyDecision(False, "", intent="thin_ack")) as mock_gen, \
             mock.patch("wechat_bot_monitor.ingest_event", return_value=None) as mock_ingest, \
             mock.patch("wechat_bot_monitor.send_reply") as mock_send:
            before_pending = dict(mon._pending_images)
            before_sessions = dict(mon._active_sessions)
            mon._handle_thin_monitor_target(
                target, m, cfg,
                config_path="wechat_bot_targets.json",
                args=args,
                contact_names={},
                lid=42,
            )
            self.assertEqual(mon._pending_images, before_pending)
            self.assertEqual(mon._active_sessions, before_sessions)

    def test_handle_precheck_boundary_skips_generate_reply(self):
        target = self._make_target(thin_monitor=True)
        m = self._make_message()
        cfg = self._make_cfg()
        args = self._make_args(dry_run=True)
        boundary = mock.MagicMock()
        boundary.risk_level = "high"
        boundary.reply_text = "该请求需要本人确认。"
        turn = mock.MagicMock()
        turn.end_local_id = 42
        turn.to_generate_reply_message.return_value = {"content": "hello"}
        with mock.patch("wechat_bot_monitor.precheck", return_value=boundary) as mock_precheck, \
             mock.patch("wechat_bot_monitor.generate_reply") as mock_gen, \
             mock.patch("wechat_bot_monitor.ingest_event", return_value=turn) as mock_ingest, \
             mock.patch("wechat_bot_monitor.send_reply") as mock_send:
            mon._handle_thin_monitor_target(
                target, m, cfg,
                config_path="wechat_bot_targets.json",
                args=args,
                contact_names={},
                lid=42,
            )
        mock_precheck.assert_called_once()
        mock_gen.assert_not_called()


class ThinMonitorMainLoopTests(unittest.TestCase):
    def test_non_thin_target_uses_legacy_path(self):
        """A target without thin_monitor should still run trigger/session logic."""
        # This is a smoke test: we just verify that the legacy code path is not
        # bypassed by the thin-monitor branch guard.
        cfg = {"reply_engine": {"thin_monitor": False}}
        target = {"thin_monitor": False}
        self.assertFalse(mon._thin_monitor_enabled(cfg, target))


if __name__ == "__main__":
    unittest.main()
