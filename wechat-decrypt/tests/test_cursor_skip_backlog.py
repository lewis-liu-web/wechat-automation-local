"""Cursor snap on enable / hot-resume: skip backlog to avoid group reply storms."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import target_registry as reg
import wechat_bot_monitor as mon


def _make_msg_db(message_dir: Path, table: str, max_local_id: int) -> Path:
    message_dir.mkdir(parents=True, exist_ok=True)
    db_path = message_dir / "message_0.db"
    con = sqlite3.connect(db_path)
    try:
        con.execute('create table if not exists "%s" (local_id integer primary key)' % table)
        con.execute('delete from "%s"' % table)
        for i in range(1, max_local_id + 1):
            con.execute('insert into "%s"(local_id) values (?)' % table, (i,))
        con.commit()
    finally:
        con.close()
    return db_path


class TestSnapOnEnable(TestCase):
    def test_enable_candidate_snaps_cursor_to_db_max(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        msg_dir = root / "message"
        table = reg.msg_table("room@chatroom")
        _make_msg_db(msg_dir, table, 50)

        cfg_path = root / "wechat_bot_targets.json"
        cand_path = root / "wechat_bot_candidates.json"
        cfg_path.write_text(json.dumps({"version": 1, "targets": []}), encoding="utf-8")
        cand_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "candidates": [
                        {
                            "username": "room@chatroom",
                            "name": "Group Chat",
                            "db": "message_0.db",
                            "table": table,
                            "last_local_id": 10,
                            "status": "pending",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch.object(reg, "DECRYPTED_MESSAGE_DIR", msg_dir), patch.object(
            reg, "_load_runtime_paths", return_value=(Path(""), root)
        ):
            target = reg.enable_candidate(
                "room@chatroom", config_path=cfg_path, candidates_path=cand_path
            )
        self.assertEqual(int(target.get("last_local_id") or 0), 50)

    def test_set_enabled_on_snaps_when_was_disabled(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        msg_dir = root / "message"
        table = reg.msg_table("room@chatroom")
        _make_msg_db(msg_dir, table, 77)

        cfg_path = root / "wechat_bot_targets.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": [
                        {
                            "name": "Group Chat",
                            "username": "room@chatroom",
                            "db": "message_0.db",
                            "table": table,
                            "last_local_id": 12,
                            "enabled": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch.object(reg, "DECRYPTED_MESSAGE_DIR", msg_dir), patch.object(
            reg, "_load_runtime_paths", return_value=(Path(""), root)
        ):
            target = reg.set_enabled("room@chatroom", True, config_path=cfg_path)
        self.assertEqual(int(target.get("last_local_id") or 0), 77)

    def test_set_enabled_off_does_not_snap(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        msg_dir = root / "message"
        table = reg.msg_table("room@chatroom")
        _make_msg_db(msg_dir, table, 99)

        cfg_path = root / "wechat_bot_targets.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": [
                        {
                            "name": "Group Chat",
                            "username": "room@chatroom",
                            "db": "message_0.db",
                            "table": table,
                            "last_local_id": 20,
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch.object(reg, "DECRYPTED_MESSAGE_DIR", msg_dir), patch.object(
            reg, "_load_runtime_paths", return_value=(Path(""), root)
        ):
            target = reg.set_enabled("room@chatroom", False, config_path=cfg_path)
        self.assertEqual(int(target.get("last_local_id") or 0), 20)
        self.assertFalse(target.get("enabled"))


class TestStartupAndHotResume(TestCase):
    def setUp(self):
        mon._CURSOR_AUDIT.clear()
        mon._PREV_ENABLED_TARGET_KEYS = None

    def test_startup_advance_runs_even_when_pipeline_disabled(self):
        t = {
            "name": "g",
            "db": "message_0.db",
            "table": "Msg_x",
            "username": "room@chatroom",
            "last_local_id": 5,
        }
        with patch.object(mon, "fetch_latest_for_target", return_value={"local_id": 40}):
            runtime = mon._advance_startup_cursors(
                [t], {"monitor": {"advance_cursor_on_start": True}, "reliable_pipeline": {"enabled": False}}
            )
        self.assertEqual(int(t["last_local_id"]), 40)
        key = "%s|%s|%s" % (t["db"], t["table"], t["username"])
        self.assertEqual(runtime[key], 40)

    def test_startup_respects_advance_flag_false(self):
        t = {
            "name": "g",
            "db": "message_0.db",
            "table": "Msg_x",
            "username": "room@chatroom",
            "last_local_id": 5,
        }
        with patch.object(mon, "fetch_latest_for_target", return_value={"local_id": 40}):
            mon._advance_startup_cursors(
                [t], {"monitor": {"advance_cursor_on_start": False}}
            )
        self.assertEqual(int(t["last_local_id"]), 5)
