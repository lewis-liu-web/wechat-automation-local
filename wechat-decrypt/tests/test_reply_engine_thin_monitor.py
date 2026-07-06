import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reply_engine import (
    _resolve_skill_name,
    _wechat_side_payload,
    _thin_monitor_enabled,
    generate_reply,
)


class ThinMonitorHelperTests(unittest.TestCase):
    def test_resolve_skill_name_priority_target(self):
        cfg = {"reply_engine": {"skill_name": "wechat_task", "wechat_auto_skill": "wechat_auto"}}
        target = {"skill_name": "custom_skill"}
        self.assertEqual(_resolve_skill_name(cfg, target), "custom_skill")

    def test_resolve_skill_name_thin_mode(self):
        cfg = {"reply_engine": {"wechat_auto_skill": "wechat_auto"}}
        target = {}
        self.assertEqual(_resolve_skill_name(cfg, target, thin_monitor=True), "wechat_auto")

    def test_resolve_skill_name_tool_agent_mode(self):
        cfg = {"reply_engine": {"wechat_auto_skill": "wechat_auto"}}
        target = {}
        self.assertEqual(_resolve_skill_name(cfg, target, is_tool_agent=True), "wechat_auto")

    def test_resolve_skill_name_default(self):
        cfg = {}
        target = {}
        self.assertEqual(_resolve_skill_name(cfg, target), "wechat_task")

    def test_resolve_skill_name_global_default(self):
        cfg = {"reply_engine": {"skill_name": "my_default"}}
        target = {}
        self.assertEqual(_resolve_skill_name(cfg, target), "my_default")

    def test_thin_monitor_enabled_target_overrides(self):
        cfg = {"reply_engine": {"thin_monitor": True}}
        target = {"thin_monitor": False}
        self.assertFalse(_thin_monitor_enabled(cfg, target))

    def test_thin_monitor_enabled_global(self):
        cfg = {"reply_engine": {"thin_monitor": True}}
        target = {}
        self.assertTrue(_thin_monitor_enabled(cfg, target))

    def test_wechat_side_payload_thin(self):
        side = _wechat_side_payload(True)
        self.assertEqual(side["responsibilities"], ["listen", "send"])
        self.assertIn("session_management", side["delegated_to_agent"])
        self.assertIn("reply_generation", side["delegated_to_agent"])

    def test_wechat_side_payload_legacy(self):
        side = _wechat_side_payload(False)
        self.assertEqual(side["responsibilities"], ["listen", "trigger", "session", "image_extract", "send"])
        self.assertNotIn("session_management", side["delegated_to_agent"])
        self.assertIn("reply_generation", side["delegated_to_agent"])


class ThinMonitorGenerateReplyTests(unittest.TestCase):
    def make_wiki(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "core").mkdir()
        (root / "core" / "reply_boundary.md").write_text("边界 不能承诺 不能泄露密钥", encoding="utf-8")
        (root / "scenes" / "a").mkdir(parents=True)
        (root / "scenes" / "a" / "faq.md").write_text("苹果 场景A 专用资料", encoding="utf-8")
        return td, root

    def test_thin_monitor_skips_local_preprocessing(self):
        import tempfile
        td, root = self.make_wiki()
        self.addCleanup(td.cleanup)
        cfg = {
            "wiki_dir": str(root),
            "knowledge_bases": {"scene.a": {"type": "local", "path": "scenes/a"}},
            "reply_engine": {
                "mode": "raw_agent",
                "agent_mode": "standard",
                "thin_monitor": True,
                "wechat_auto_skill": "wechat_auto",
            },
        }
        message = {
            "content": "小助手 分析一下苹果",
            "local_id": 1,
            "local_type": 1,
        }
        with mock.patch("reply_engine._agent_jobs.enqueue_job") as mock_enqueue:
            d = generate_reply(message, {"knowledge_bases": ["scene.a"], "mode": "customer_service"}, cfg)

        self.assertTrue(d.should_reply)
        self.assertIn("处理", d.reply_text)
        mock_enqueue.assert_called_once()
        payload = mock_enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["skill_name"], "wechat_auto")
        self.assertTrue(len(payload.get("available_tools", [])) > 0)
        self.assertIn("leann_search", [t.get("name") for t in payload.get("available_tools", [])])
        self.assertIn("cli_path", payload.get("leann", {}))
        self.assertEqual(payload.get("knowledge_hits", []), [])
        self.assertEqual(payload.get("image_descriptions", []), [])
        self.assertEqual(payload.get("context_messages", []), [])
        self.assertIn("wechat_side", payload)
        self.assertIn("session_management", payload["wechat_side"].get("delegated_to_agent", []))


if __name__ == "__main__":
    unittest.main()
