"""Tests for the durable ingress helper in wechat_bot_monitor.

These tests exercise the thin-monitor entry point that feeds the durable
pipeline. They deliberately do not exercise the legacy aggregator, KB
retrieval, or any LLM call. The new path must:

- return the persistable local_id only after persist_inbound_event succeeds;
- return None / an "advanced": False marker on persist failure so the caller
  can refuse to advance its cursor;
- treat the same source_event_id as idempotent (re-read returns the same row
  without a second persist);
- allow an open window to materialize a single turn_job after a crash/restart;
- never call generate_reply or send_reply while the new path is in effect.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reliable_pipeline as pipeline

import wechat_bot_monitor as monitor  # noqa: E402


def _event(local_id: int, *, sender="wxid_sender", sender_username=None,
           text="hello", local_type=1, status=0):
    msg = {
        "local_id": local_id,
        "message_content": text,
        "real_sender_id": sender,
        "sender": sender,
        "sender_username": sender_username or sender,
        "sender_display_name": "Sender",
        "mention_name": "Sender",
        "local_type": local_type,
        "status": status,
    }
    return msg


def _target(*, username="wxid_target@chatroom", db="message_0.db",
            table="Msg_target", reliable_pipeline_target=True, mode=None):
    t = {
        "name": username,
        "username": username,
        "db": db,
        "table": table,
        "enabled": True,
        "last_local_id": 0,
        "reliable_pipeline_target": reliable_pipeline_target,
    }
    if mode is not None:
        t["mode"] = mode
    return t


def _cfg(*, enabled=True, db_path=None):
    cfg = {"reliable_pipeline": {"enabled": enabled}}
    if db_path is not None:
        cfg["reliable_pipeline"]["db_path"] = str(db_path)
    return cfg


class TestReliablePipelineTargetEligibility(unittest.TestCase):
    def test_default_disabled_blocks_even_explicit_target_opt_in(self):
        cfg = {"reliable_pipeline": {"enabled": False}}
        t = _target()
        self.assertFalse(monitor._is_reliable_pipeline_target(t, cfg))

    def test_enabled_requires_target_opt_in(self):
        cfg = _cfg(enabled=True)
        t = _target(reliable_pipeline_target=False)
        self.assertFalse(monitor._is_reliable_pipeline_target(t, cfg))

    def test_thin_monitor_mode_is_equivalent_opt_in(self):
        cfg = _cfg(enabled=True)
        t = _target(reliable_pipeline_target=False, mode="thin_monitor")
        self.assertTrue(monitor._is_reliable_pipeline_target(t, cfg))

    def test_thin_monitor_type_is_equivalent_opt_in(self):
        cfg = _cfg(enabled=True)
        t = _target(reliable_pipeline_target=False, mode=None)
        t["type"] = "thin-monitor"
        self.assertTrue(monitor._is_reliable_pipeline_target(t, cfg))

    def test_thin_monitor_type_alone_with_unrelated_mode_is_eligible(self):
        cfg = _cfg(enabled=True)
        t = _target(reliable_pipeline_target=False, mode="group_assistant")
        t["type"] = "thin-monitor"
        self.assertTrue(monitor._is_reliable_pipeline_target(t, cfg))

    def test_enabled_plus_explicit_target_opt_in_is_eligible(self):
        cfg = _cfg(enabled=True)
        t = _target(reliable_pipeline_target=True)
        self.assertTrue(monitor._is_reliable_pipeline_target(t, cfg))


class TestResolvedIdentifiers(unittest.TestCase):
    def test_missing_sender_returns_none(self):
        t = _target()
        m = _event(1, sender="")
        self.assertIsNone(monitor._resolve_pipeline_identifiers(t, m))

    def test_missing_local_id_returns_none(self):
        t = _target()
        m = _event(0)
        self.assertIsNone(monitor._resolve_pipeline_identifiers(t, m))

    def test_sender_falls_back_to_real_sender_id_then_sender(self):
        t = _target()
        m = {
            "local_id": 5,
            "real_sender_id": "wxid_real",
            "sender_username": "",
            "sender": "",
        }
        ident = monitor._resolve_pipeline_identifiers(t, m)
        self.assertIsNotNone(ident)
        self.assertEqual(ident["sender_id"], "wxid_real")

        m2 = {
            "local_id": 5,
            "real_sender_id": None,
            "sender_username": "",
            "sender": "wxid_legacy",
        }
        ident2 = monitor._resolve_pipeline_identifiers(t, m2)
        self.assertIsNotNone(ident2)
        self.assertEqual(ident2["sender_id"], "wxid_legacy")

    def test_event_id_matches_pipeline_helper(self):
        t = _target(db="message_0.db", table="Msg_target")
        m = _event(42)
        ident = monitor._resolve_pipeline_identifiers(t, m)
        self.assertEqual(ident["event_id"], pipeline.source_event_id(t, m))


class TestPayloadBuilder(unittest.TestCase):
    def test_payload_contains_message_target_event_context_without_kb(self):
        t = _target()
        m = _event(7)
        policy = {"reply_policy": "balanced", "session_policy": {"timeout_seconds": 60}}
        payload = monitor.build_event_payload(t, m, event_context={
            "trigger_matched": True,
            "in_session": False,
            "sender_username": "wxid_sender",
        }, target_policy=policy)
        self.assertEqual(payload["message"]["local_id"], 7)
        self.assertEqual(payload["target"]["username"], t["username"])
        self.assertEqual(payload["event_context"]["trigger_matched"], True)
        self.assertEqual(payload["target_policy"]["reply_policy"], "balanced")
        # No KB pre-fetching performed at ingress.
        self.assertNotIn("knowledge_bases", payload)
        self.assertNotIn("kb_results", payload)
        self.assertNotIn("retrieved_chunks", payload)

    def test_payload_target_includes_dedicated_agent_instance_id(self):
        t = _target()
        t["dedicated_agent_instance_id"] = "hermes-worker-3"
        m = _event(7)
        payload = monitor.build_event_payload(t, m)
        self.assertEqual(payload["target"]["dedicated_agent_instance_id"], "hermes-worker-3")

    def test_payload_target_missing_binding_is_empty_string(self):
        t = _target()
        m = _event(7)
        payload = monitor.build_event_payload(t, m)
        self.assertEqual(payload["target"]["dedicated_agent_instance_id"], "")


class TestDurableIngressEvent(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "pipeline.sqlite"
        self.cfg = _cfg(enabled=True, db_path=self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_once(self, t, m, cfg, **kw):
        return monitor.durable_ingress_event(t, m, cfg=cfg, db_path=self.db, **kw)

    def test_persist_success_advances_cursor(self):
        t = _target()
        m = _event(11)
        result = self._run_once(t, m, self.cfg)
        self.assertTrue(result["advanced"])
        self.assertEqual(result["local_id"], 11)
        self.assertTrue(result["persisted"])
        # Caller can advance last_local_id because persistence succeeded.
        self.assertEqual(pipeline.counts(db_path=self.db)["inbound_events"], {"pending": 1})

    def test_persist_failure_does_not_advance(self):
        t = _target()
        m = _event(12)
        # Force persist_inbound_event to raise to simulate a durable write failure.
        with patch.object(
            monitor.pipeline, "persist_inbound_event",
            side_effect=RuntimeError("disk full"),
        ):
            result = self._run_once(t, m, self.cfg)
        self.assertFalse(result["advanced"])
        self.assertFalse(result["persisted"])
        self.assertIn("disk full", result["error"])
    def test_duplicate_replay_does_not_extend_due_at(self):
        t = _target()
        m = _event(13)
        # Attach at now=1.0; default debounce=5.0 → due_at=6.0. Replay at
        # now=5.0 — still inside the same open window so add_event_to_window
        # must NOT mutate due_at on replay.
        first = self._run_once(t, m, self.cfg, now=1.0)
        self.assertTrue(first["advanced"])

        with pipeline._connect(self.db) as con:
            row = con.execute("SELECT due_at FROM turn_windows LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        due_before = float(row["due_at"])
        self.assertAlmostEqual(due_before, 6.0, places=3)

        second = self._run_once(t, m, self.cfg, now=5.0)
        self.assertTrue(second["advanced"])
        self.assertFalse(second["inserted"])

        with pipeline._connect(self.db) as con:
            row = con.execute("SELECT due_at FROM turn_windows LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        due_after = float(row["due_at"])
        # Idempotence contract: the membership guard must prevent
        # add_event_to_window from mutating due_at on replay.
        self.assertEqual(due_after, due_before,
                         "duplicate replay must not extend the open window's due_at")

        # Later drain still materializes exactly one job.
        out = monitor.drain_due_pipeline(cfg=self.cfg, db_path=self.db, now=20.0)
        self.assertEqual(out["closed_turns"], 1)
        self.assertEqual(out["created_jobs"], 1)
        self.assertEqual(pipeline.counts(db_path=self.db)["turn_jobs"]["queued"], 1)

    def test_open_window_materializes_one_job_after_close(self):
        t = _target()
        self._run_once(t, _event(1), self.cfg, now=1.0)
        self._run_once(t, _event(2), self.cfg, now=2.0)
        # Window is still open — no turn yet.
        self.assertEqual(pipeline.counts(db_path=self.db)["turns"], {})
        # Simulate crash + restart: only the per-cycle drain runs, no new messages.
        out = monitor.drain_due_pipeline(cfg=self.cfg, db_path=self.db, now=20.0)
        self.assertEqual(out["closed_turns"], 1)
        self.assertEqual(out["created_jobs"], 1)
        jobs = pipeline.create_jobs_for_ready_turns(db_path=self.db)
        self.assertEqual(jobs, [])
        # Exactly one queued job for the materialized turn.
        self.assertEqual(pipeline.counts(db_path=self.db)["turn_jobs"]["queued"], 1)

    def test_new_path_does_not_invoke_generate_reply_or_send_reply(self):
        t = _target()
        m = _event(99)
        # Stub the symbols to blow up if the new path would call them.
        gen_reply = MagicMock(side_effect=AssertionError("generate_reply must not be called"))
        send_reply = MagicMock(side_effect=AssertionError("send_reply must not be called"))
        with patch.object(monitor, "generate_reply", gen_reply, create=True), \
                patch.object(monitor, "send_reply", send_reply, create=True):
            self._run_once(t, m, self.cfg)
            monitor.drain_due_pipeline(cfg=self.cfg, db_path=self.db, now=5.0)
        gen_reply.assert_not_called()
        send_reply.assert_not_called()

    def test_drain_failure_does_not_advance_cursor(self):
        t = _target()
        m = _event(77)
        # Persist succeeds, window-attach succeeds; the drain itself fails.
        with patch.object(
            monitor.pipeline, "close_due_windows",
            side_effect=RuntimeError("disk full on close"),
        ):
            result = self._run_once(t, m, self.cfg, now=1.0)
        # Cursor must NOT advance: caller retries the entire ingress.
        self.assertFalse(result["advanced"])
        self.assertTrue(result["persisted"])
        self.assertIn("drain failed", result["error"])
        # The event is durable; the open window is durable; only the drain
        # failed. A future cycle's drain must still be able to close it.
        out = monitor.drain_due_pipeline(cfg=self.cfg, db_path=self.db, now=20.0)
        self.assertEqual(out["closed_turns"], 1)
        self.assertEqual(out["created_jobs"], 1)

    def test_create_jobs_failure_does_not_advance_cursor(self):
        t = _target()
        m = _event(78)
        with patch.object(
            monitor.pipeline, "create_jobs_for_ready_turns",
            side_effect=RuntimeError("disk full on materialize"),
        ):
            result = self._run_once(t, m, self.cfg, now=1.0)
        self.assertFalse(result["advanced"])
        self.assertTrue(result["persisted"])
        self.assertIn("drain failed", result["error"])

    def test_loop_early_fence_runs_before_legacy_advancing_branches(self):
        """The per-cycle thin-target fence MUST short-circuit BEFORE image,
        admin, self-sent, and admin_muted branches can advance the cursor.
        This guards against a later self-sent/admin-muted row cursor-pasting
        a durable failure earlier in the cycle.
        """
        import re
        src = Path(monitor.__file__).read_text(encoding='utf-8')
        # Locate the per-message for-loop region.
        loop_start = src.find("for db_name, db_targets in group_targets_by_db(targets).items():")
        self.assertGreater(loop_start, 0)
        loop_end = src.find("# Close conversation windows whose debounce", loop_start)
        body = src[loop_start:loop_end]
        fence_pos = body.find("_rl_failed_targets")
        admin_pos = body.find("_try_handle_admin_command")
        self_sent_pos = body.find("self_sent_skip")
        muted_pos = body.find("target_muted")
        self.assertGreater(fence_pos, 0)
        self.assertGreater(admin_pos, 0)
        self.assertGreater(self_sent_pos, 0)
        self.assertGreater(muted_pos, 0)
        # The early fence must appear strictly before every legacy branch.
        self.assertLess(fence_pos, admin_pos,
                        "fence must run before _try_handle_admin_command")
        self.assertLess(fence_pos, self_sent_pos,
                        "fence must run before self_sent_skip")
        self.assertLess(fence_pos, muted_pos,
                        "fence must run before target_muted branch")

    def test_thin_target_does_not_populate_legacy_pending_images(self):
        """The thin-monitor / durable path must NOT touch the legacy in-memory
        _pending_images dict; image_path on the message itself is the only
        surface that crosses the ingress boundary.
        """
        import re
        src = Path(monitor.__file__).read_text(encoding='utf-8')
        loop_start = src.find("for db_name, db_targets in group_targets_by_db(targets).items():")
        loop_end = src.find("# Close conversation windows whose debounce", loop_start)
        body = src[loop_start:loop_end]
        pending_pos = body.find("_pending_images.setdefault")
        gate_pos = body.find("if not _is_reliable_pipeline_target(t, cfg):")
        self.assertGreater(pending_pos, 0)
        self.assertGreater(gate_pos, 0)
        # The gate must appear at or before the _pending_images mutation.
        self.assertLessEqual(gate_pos, pending_pos,
                             "thin-target gate must precede _pending_images mutation")

    def test_duplicate_after_turned_does_not_reopen_window(self):
        t = _target()
        m = _event(80)
        # First ingress: persist + attach.
        first = self._run_once(t, m, self.cfg, now=1.0)
        self.assertTrue(first["advanced"])
        # Drain closes the window and marks the event as INBOUND_TURNED.
        monitor.drain_due_pipeline(cfg=self.cfg, db_path=self.db, now=20.0)
        with pipeline._connect(self.db) as con:
            row = con.execute(
                "SELECT status FROM inbound_events WHERE local_id=80"
            ).fetchone()
        self.assertEqual(row["status"], pipeline.INBOUND_TURNED)
        # Wipe any leftover window rows so the assertion is unambiguous.
        with pipeline._connect(self.db) as con:
            con.execute("DELETE FROM turn_windows")
            con.commit()
        # Replay: persist is a no-op, event already TURNED. The duplicate
        # branch must NOT call add_event_to_window, so turn_windows stays empty.
        attach_calls = {"n": 0}
        original_attach = monitor.pipeline.add_event_to_window

        def counting_attach(*a, **kw):
            attach_calls["n"] += 1
            return original_attach(*a, **kw)

        with patch.object(
            monitor.pipeline, "add_event_to_window", side_effect=counting_attach,
        ):
            second = self._run_once(t, m, self.cfg, now=30.0)
        self.assertTrue(second["advanced"])
        self.assertFalse(second["inserted"])
        self.assertFalse(second["window_attached"])
        self.assertEqual(attach_calls["n"], 0,
                         "duplicate-after-turned must not invoke add_event_to_window")
        with pipeline._connect(self.db) as con:
            windows = con.execute("SELECT * FROM turn_windows").fetchall()
        self.assertEqual(windows, [],
                         "duplicate-after-turned must not reopen a window")

    def test_missing_sender_returns_not_advanced(self):
        t = _target()
        m = _event(33, sender="", sender_username="")
        result = self._run_once(t, m, self.cfg)
        self.assertFalse(result["advanced"])
        self.assertFalse(result["persisted"])
        self.assertEqual(pipeline.counts(db_path=self.db)["inbound_events"], {})

    def test_window_add_failure_does_not_advance_and_recovers_on_retry(self):
        t = _target()
        m = _event(50)
        call_count = {"n": 0}
        original = monitor.pipeline.add_event_to_window

        def flaky(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("sqlite locked")
            return original(*a, **kw)

        with patch.object(
            monitor.pipeline, "add_event_to_window", side_effect=flaky,
        ):
            first = self._run_once(t, m, self.cfg, now=1.0)
        self.assertFalse(first["advanced"])
        # Persisted row is durably there; no turn materialized yet.
        self.assertEqual(pipeline.counts(db_path=self.db)["inbound_events"], {"pending": 1})
        self.assertEqual(pipeline.counts(db_path=self.db)["turns"], {})

        # Retry: add_event_to_window now succeeds. The persisted event's
        # integer id must be attached to the open window.
        retry = self._run_once(t, m, self.cfg, now=2.0)
        self.assertTrue(retry["advanced"])
        self.assertFalse(retry["inserted"])  # duplicate persist read; window attached
        with pipeline._connect(self.db) as con:
            row = con.execute("SELECT first_event_id, last_event_id FROM turn_windows LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        # The window must span exactly the persisted event id, not a phantom row.
        self.assertEqual(int(row["first_event_id"]), int(row["last_event_id"]))


class TestDrainDuePipeline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "pipeline.sqlite"
        self.cfg = _cfg(enabled=True, db_path=self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def test_drain_is_safe_when_no_events(self):
        out = monitor.drain_due_pipeline(cfg=self.cfg, db_path=self.db, now=10.0)
        self.assertEqual(out["closed_turns"], 0)
        self.assertEqual(out["created_jobs"], 0)

    def test_drain_recovers_open_window_without_new_messages(self):
        t = _target()
        monitor.durable_ingress_event(t, _event(1), cfg=self.cfg, db_path=self.db, now=1.0)
        monitor.durable_ingress_event(t, _event(2), cfg=self.cfg, db_path=self.db, now=2.0)
        # No new message this cycle; the open window must still be flushed.
        out = monitor.drain_due_pipeline(cfg=self.cfg, db_path=self.db, now=20.0)
        self.assertEqual(out["closed_turns"], 1)
        self.assertEqual(out["created_jobs"], 1)
        self.assertEqual(pipeline.counts(db_path=self.db)["turn_jobs"]["queued"], 1)




class TestPipelineSenderAllowlist(unittest.TestCase):
    """Unit tests for the per-target reliable-pipeline sender allowlist helper."""

    def _cfg(self, test_target_only=False):
        return {"reliable_pipeline": {"enabled": True, "test_target_only": test_target_only}}

    def _target(self, allowed=None):
        t = _target()
        if allowed is not None:
            t["reliable_pipeline_allowed_senders"] = allowed
        return t

    def _msg(self, sender_username=None, real_sender_id=None, sender=None):
        m = {"local_id": 1}
        if sender_username is not None:
            m["sender_username"] = sender_username
        if real_sender_id is not None:
            m["real_sender_id"] = real_sender_id
        if sender is not None:
            m["sender"] = sender
        return m

    def test_allowlist_ignored_when_test_target_only_off(self):
        t = self._target(allowed=["wxid_test"])
        m = self._msg(sender_username="wxid_other")
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=False)))

    def test_empty_or_missing_list_allows_all(self):
        t = self._target(allowed=[])
        m = self._msg(sender_username="wxid_any")
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=True)))
        t2 = _target()
        self.assertTrue(monitor._is_allowed_pipeline_sender(t2, m, self._cfg(test_target_only=True)))

    def test_sender_username_allowed(self):
        t = self._target(allowed=["wxid_test"])
        m = self._msg(sender_username="wxid_test")
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=True)))

    def test_case_insensitive_match(self):
        t = self._target(allowed=["WXID_TEST"])
        m = self._msg(sender_username="wxid_test")
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=True)))

    def test_sender_username_takes_priority_over_real_sender_id(self):
        t = self._target(allowed=["wxid_real"])
        m = self._msg(sender_username="wxid_other", real_sender_id="wxid_real")
        self.assertFalse(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=True)))

    def test_real_sender_id_fallback(self):
        t = self._target(allowed=["wxid_real"])
        m = self._msg(sender_username="", real_sender_id="wxid_real")
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=True)))

    def test_sender_fallback(self):
        t = self._target(allowed=["wxid_legacy"])
        m = self._msg(sender_username="", real_sender_id="", sender="wxid_legacy")
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=True)))

    def test_blocked_sender_returns_false(self):
        t = self._target(allowed=["wxid_allowed"])
        m = self._msg(sender_username="wxid_other")
        self.assertFalse(monitor._is_allowed_pipeline_sender(t, m, self._cfg(test_target_only=True)))

    def test_non_dict_inputs_are_safe(self):
        t = self._target(allowed=["wxid_test"])
        # Non-dict m in test mode with a non-empty allowlist is rejected (fail-closed).
        self.assertFalse(monitor._is_allowed_pipeline_sender(t, "not a dict", self._cfg(test_target_only=True)))
        # Non-dict cfg/t disable the allowlist and return True.
        self.assertTrue(monitor._is_allowed_pipeline_sender("not a dict", {}, self._cfg(test_target_only=True)))
        self.assertTrue(monitor._is_allowed_pipeline_sender(t, {}, "not a dict"))


class TestPipelineAllowlistMainLoop(unittest.TestCase):
    """Integration test: the monitor loop rejects non-allowed senders before durable ingress."""

    def _blocked_event(self):
        return {
            "local_id": 42,
            "message_content": "probe message",
            "real_sender_id": "wxid_blocked",
            "sender": "wxid_blocked",
            "sender_username": "wxid_blocked",
            "sender_display_name": "Blocked",
            "mention_name": "Blocked",
            "local_type": 1,
            "status": 0,
            "create_time": 1234567890,
        }

    def _build_target(self):
        return {
            "name": "bot群聊测试",
            "username": "wxid_target@chatroom",
            "db": "message_0.db",
            "table": "Msg_target",
            "enabled": True,
            "last_local_id": 0,
            "reliable_pipeline_target": True,
            "reliable_pipeline_allowed_senders": ["wxid_allowed"],
        }

    def _build_config(self, tmp_stop):
        return {
            "reliable_pipeline": {
                "enabled": True,
                "test_target_only": True,
            },
            "stop_file": str(tmp_stop),
            "poll_interval": 3,
        }

    @patch.object(monitor, '_record_event')
    @patch.object(monitor, 'durable_ingress_event')
    @patch.object(monitor, 'flush_due', return_value=[])
    @patch.object(monitor, 'drain_due_pipeline', return_value={'closed_turns': 0, 'created_jobs': 0})
    @patch.object(monitor, 'enrich_sender_display_name', side_effect=lambda m, names: m)
    @patch.object(monitor, 'fetch_new_for_db')
    @patch.object(monitor, 'group_targets_by_db')
    @patch.object(monitor, 'load_contact_name_map', return_value={})
    @patch.object(monitor, 'fetch_latest_for_target', return_value=None)
    @patch.object(monitor, 'enabled_targets')
    @patch.object(monitor, 'load_config')
    @patch('sys.argv', ['wechat_bot_monitor.py', '--once', '--no-fast-refresh', '--no-save-state', '--config', 'C:\\fake_config.json'])
    def test_blocked_sender_advances_cursor_and_skips_durable_ingress(
        self, mock_load_config, mock_enabled_targets, mock_fetch_latest,
        mock_load_contact, mock_group, mock_fetch_new, mock_enrich,
        mock_drain, mock_flush_due, mock_durable, mock_record
    ):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        tmp_stop = tmp_path / 'monitor.stop'
        target = self._build_target()
        cfg = self._build_config(tmp_stop)
        def _load_config(path):
            return cfg
        mock_load_config.side_effect = _load_config

        def _enabled_targets(c):
            return [target]
        mock_enabled_targets.side_effect = _enabled_targets

        def group_by_db(targets):
            return {t['db']: [t] for t in targets}
        mock_group.side_effect = group_by_db

        blocked = self._blocked_event()
        mock_fetch_new.return_value = [(target, blocked)]

        monitor.main()

        mock_durable.assert_not_called()
        mock_record.assert_called_once()
        args, kwargs = mock_record.call_args
        self.assertEqual(args[0], 'reliable_pipeline_sender_not_allowed')
        self.assertEqual(kwargs['target'], 'bot群聊测试')
        self.assertEqual(kwargs['payload']['local_id'], 42)
        self.assertEqual(target['last_local_id'], 42)

if __name__ == "__main__":
    unittest.main()