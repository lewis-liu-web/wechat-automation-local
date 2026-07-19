"""Stage 4 retired thin_monitor handler — legacy tests replaced with smoke."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wechat_bot_monitor as monitor  # noqa: E402
MONITOR = Path(monitor.__file__)


class TestThinMonitorRetired(unittest.TestCase):
    def test_handle_thin_monitor_target_removed(self):
        self.assertNotIn(
            "def _handle_thin_monitor_target(",
            MONITOR.read_text(encoding="utf-8"),
        )

    def test_thin_monitor_enabled_branch_removed(self):
        self.assertNotIn(
            "_thin_monitor_enabled(cfg",
            MONITOR.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
