"""Durable ingress + post-Stage-4 single-durable integration tests.
Asserts the durable worker + helper work with the legacy gate/thin_handler removed.
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reliable_pipeline as pipeline
import wechat_bot_monitor as monitor  # noqa: E402


def _event(local_id, *, sender="wxid_sender", text="hello", local_type=1, status=0):
    return {
        "local_id": local_id,
        "sender": sender,
        "sender_username": sender,
        "real_sender_id": "",
        "message_content": text,
        "local_type": local_type,
        "status": status,
        "talker": "wxid_target@chatroom",
        "create_time": 1700000000 + local_id,
        "packed_info_data": b"",
    }


def _target(*, username="wxid_target@chatroom", db="message_0.db", table="Msg_target"):
    return {
        "name": "bot_test",
        "username": username,
        "db": db,
        "table": table,
        "last_local_id": 0,
        "reliable_pipeline_target": True,
    }


def _cfg(*, enabled=True, db_path=None):
    cfg = {"reliable_pipeline": {"enabled": enabled}}
    if db_path is not None:
        cfg["reliable_pipeline"]["db_path"] = str(db_path)
    return cfg


class TestResolvedIdentifiers(unittest.TestCase):
    def test_event_id_matches_pipeline_helper(self):
        t = _target()
        m = _event(42)
        ident = monitor._resolve_pipeline_identifiers(t, m)
        self.assertEqual(ident["event_id"], pipeline.source_event_id(t, m))


class TestDurableIngressEvent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "reliable_pipeline.sqlite"
        self.cfg = _cfg(enabled=True, db_path=self.db)
        self.t = _target()
        self.m = _event(42)
        self.event_ctx = {"mode": "durable_ingress", "local_id": 42, "sender": "wxid_sender"}

    def test_persist_success_advances_cursor(self):
        first = monitor.durable_ingress_event(
            self.t, self.m, cfg=self.cfg, config_path=None,
            db_path=self.db, now=1.0, event_context=self.event_ctx,
            target_policy=[],
        )
        self.assertTrue(first["advanced"])
        self.assertTrue(first["persisted"])

    def test_durable_event_helper_fail_closed_when_disabled(self):
        disabled = _cfg(enabled=False, db_path=self.db)
        out = monitor.durable_ingress_event(
            self.t, self.m, cfg=disabled, config_path=None,
            db_path=self.db, now=1.0, event_context=self.event_ctx,
            target_policy=[],
        )
        self.assertFalse(out["advanced"])
        self.assertIn("globally disabled", out["error"])
        # DB file was not touched.
        self.assertFalse(self.db.exists())


class TestDrainDuePipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "reliable_pipeline.sqlite"

    def test_drain_when_disabled_returns_neutral_summary(self):
        cfg = _cfg(enabled=False, db_path=self.db)
        out = monitor.drain_due_pipeline(cfg=cfg, db_path=self.db, now=1.0)
        self.assertIn("error", out)


class TestPipelineSenderAllowlist(unittest.TestCase):
    def test_empty_or_missing_list_allows_all(self):
        cfg = _cfg(enabled=True, db_path=None)
        t = _target()
        m = _event(1)
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, m, cfg))


class TestMonolithicDeleteGuard(unittest.TestCase):
    """Static check: monitor.py must no longer reference legacy single-durable
    gate symbols or aggregator call sites.
    """
    def test_monitor_no_longer_calls_legacy_aggregation(self):
        text = Path(monitor.__file__).read_text(encoding="utf-8")
        for sym in ("_is_reliable_pipeline_target",
                    "_handle_thin_monitor_target",
                    "_build_thin_monitor_aggregated_message",
                    "_thin_monitor_enabled(cfg"):
            self.assertNotIn(sym, text)
        self.assertEqual(text.count("generate_reply("), 0)

    def test_strict_helper_present_and_strict(self):
        text = Path(monitor.__file__).read_text(encoding="utf-8")
        self.assertIn("def _reliable_pipeline_globally_enabled(", text)
        self.assertIn("return section.get('enabled') is True", text)


class TestSingleDurableOrder(unittest.TestCase):
    def test_admin_before_per_message_global_guard(self):
        text = Path(monitor.__file__).read_text(encoding="utf-8")
        admin_idx = text.find("_try_handle_admin_command(")
        # Find the FIRST occurrence of the per-message guard (after admin).
        # All occurrences: at top-level helper def + several usages. We take the
        # first one strictly after admin.
        guard_indices = []
        i = 0
        while True:
            j = text.find("_reliable_pipeline_globally_enabled(cfg)", i)
            if j < 0:
                break
            guard_indices.append(j)
            i = j + 1
        per_msg_guard = next((g for g in guard_indices if g > admin_idx), None)
        self.assertIsNotNone(per_msg_guard)
        self.assertGreater(per_msg_guard, admin_idx)

    def test_durable_ingress_called_after_global_guard(self):
        text = Path(monitor.__file__).read_text(encoding="utf-8")
        guard = text.find("if not _reliable_pipeline_globally_enabled(cfg):")
        durable = text.find("durable_ingress_event(")
        self.assertGreater(guard, 0)
        self.assertGreater(durable, guard)


if __name__ == "__main__":
    unittest.main()
