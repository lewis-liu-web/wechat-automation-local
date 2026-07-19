"""Post-Stage-4 monitor loop structural regression."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wechat_bot_monitor as monitor  # noqa: E402
MONITOR_PATH = Path(monitor.__file__)


def _src():
    return MONITOR_PATH.read_text(encoding="utf-8")


class TestMonitorPostStage4(unittest.TestCase):
    def test_strict_helper_present(self):
        s = _src()
        self.assertIn("def _reliable_pipeline_globally_enabled(", s)
        self.assertIn("return section.get('enabled') is True", s)

    def test_legacy_gate_and_thin_handler_removed(self):
        s = _src()
        for sym in ("def _is_reliable_pipeline_target(",
                    "def _handle_thin_monitor_target(",
                    "def _build_thin_monitor_aggregated_message("):
            self.assertNotIn(sym, s)

    def test_monitor_source_has_no_generate_reply_call_sites(self):
        self.assertEqual(_src().count("generate_reply("), 0)

    def test_per_cycle_drain_uses_strict_helper(self):
        s = _src()
        idx = s.find("Reliable pipeline per-cycle drain")
        self.assertGreater(idx, 0)
        self.assertIn("_reliable_pipeline_globally_enabled(cfg)", s[idx:idx + 400])
        self.assertNotIn(
            "(cfg or {}).get('reliable_pipeline', {}).get('enabled')", s
        )


if __name__ == "__main__":
    unittest.main()
