#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for fast_refresh cross-process lock and replace retry logic."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from file_lock import InterProcessLock, LockTimeout, lock_path_for_db
import fast_refresh_targets as frt


class InterProcessLockTests(unittest.TestCase):
    def test_lock_serializes_threads(self):
        tmp = Path(tempfile.mkdtemp())
        lock_path = tmp / "test.lock"
        counter = [0]
        def incr():
            with InterProcessLock(lock_path, timeout=5.0):
                v = counter[0]
                time.sleep(0.01)
                counter[0] = v + 1
        threads = [threading.Thread(target=incr) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(counter[0], 10)

    def test_lock_timeout_raises(self):
        tmp = Path(tempfile.mkdtemp())
        lock_path = tmp / "timeout.lock"
        with InterProcessLock(lock_path, timeout=5.0):
            with self.assertRaises(LockTimeout):
                with InterProcessLock(lock_path, timeout=0.1):
                    pass


class ReplaceRetryTests(unittest.TestCase):
    def test_replace_retry_succeeds_after_transient_failure(self):
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "src.txt"
        dst = tmp / "dst.txt"
        src.write_text("source", encoding="utf-8")
        dst.write_text("dest", encoding="utf-8")

        attempts = [0]
        path_type = type(src)
        real_replace = path_type.replace
        def flaky_replace(self, target):
            attempts[0] += 1
            if attempts[0] < 3:
                raise OSError("simulated contention")
            return real_replace(self, target)

        with mock.patch.object(path_type, 'replace', flaky_replace):
            frt._replace_with_retry(src, dst, max_attempts=5, backoff=0.01)

        self.assertEqual(attempts[0], 3)
        self.assertEqual(dst.read_text(encoding="utf-8"), "source")

    def test_replace_retry_exhausts(self):
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "src.txt"
        dst = tmp / "dst.txt"
        src.write_text("source", encoding="utf-8")

        def always_fail(self, target):
            raise OSError("simulated contention")

        path_type = type(src)
        with mock.patch.object(path_type, 'replace', always_fail):
            with self.assertRaises(OSError):
                frt._replace_with_retry(src, dst, max_attempts=2, backoff=0.01)


class StateConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.raw = self.tmp / "raw" / "message"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.raw_db = self.raw / "message_0.db"
        con = __import__("sqlite3").connect(str(self.raw_db))
        con.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")
        con.execute("INSERT INTO test VALUES (1)")
        con.commit()
        con.close()
        (self.tmp / "targets.json").write_text(json.dumps({
            "targets": [{"enabled": True, "db": "message_0.db"}]
        }), encoding="utf-8")
        (self.tmp / "keys.json").write_text(json.dumps({
            "message/message_0.db": {
                "enc_key": "00" * 32,
                "server_salt": "00" * 16,
            }
        }), encoding="utf-8")
        self.path_cfg = {
            "db_dir": str(self.raw.parent),
            "decrypted_dir": str(self.tmp / "decrypted"),
            "keys_file": str(self.tmp / "keys.json"),
        }
        self._saved_state = {
            "STATE_FILE": frt.STATE_FILE,
            "TMP_DIR": frt.TMP_DIR,
            "LOG": frt.LOG,
            "decrypt_database": frt.decrypt_database,
            "load_path_config": frt.load_path_config,
        }
        frt.STATE_FILE = self.tmp / "state.json"
        frt.TMP_DIR = self.tmp / "tmp"
        frt.LOG = self.tmp / "log"
        frt.decrypt_database = self._copy_decrypt
        frt.load_path_config = lambda: self.path_cfg

    def tearDown(self):
        frt.STATE_FILE = self._saved_state["STATE_FILE"]
        frt.TMP_DIR = self._saved_state["TMP_DIR"]
        frt.LOG = self._saved_state["LOG"]
        frt.decrypt_database = self._saved_state["decrypt_database"]
        frt.load_path_config = self._saved_state["load_path_config"]

    @staticmethod
    def _copy_decrypt(src, dst, key):
        import shutil
        shutil.copy(src, dst)
        return True

    def test_concurrent_refreshers_update_state_once(self):
        results = []
        errors = []
        def run():
            try:
                r = frt.refresh_targets(str(self.tmp / "targets.json"), force=True)
                results.extend(r)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2, "both threads must return a result")
        refreshed = [r for r in results if r.get("status") == "refreshed"]
        self.assertTrue(len(refreshed) >= 1)
        state = json.loads(frt.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn("message/message_0.db", state)


class StableAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {
            "STATE_FILE": frt.STATE_FILE,
            "load_path_config": frt.load_path_config,
            "get_key_info": frt.get_key_info,
            "decrypt_database": frt.decrypt_database,
            "verify_sqlite": frt.verify_sqlite,
            "raw_fingerprint": frt.raw_fingerprint,
            "time_time": frt.time.time,
            "os_getpid": frt.os.getpid,
        }
        frt.STATE_FILE = self.tmp / "state.json"
        frt.load_path_config = lambda: {
            "db_dir": str(self.tmp / "raw"),
            "decrypted_dir": str(self.tmp / "decrypted"),
            "keys_file": str(self.tmp / "keys.json"),
        }
        (self.tmp / "raw" / "message").mkdir(parents=True)
        (self.tmp / "decrypted" / "message").mkdir(parents=True)
        (self.tmp / "keys.json").write_text(json.dumps({"message_0": {"enc_key": "00" * 32}}), encoding="utf-8")
        self.raw_db = self.tmp / "raw" / "message" / "message_0.db"
        self.raw_db.write_bytes(b"raw_v1")
        self.final_db = self.tmp / "decrypted" / "message" / "message_0.db"
        self.final_db.write_bytes(b"old_v1")
        frt.get_key_info = lambda keys, rel: {"enc_key": "00" * 32}
        self.decrypt_calls = 0
        frt.decrypt_database = lambda src, dst, key: self._copy(src, dst)
        frt.verify_sqlite = lambda path: None
        frt.time.time = lambda: 1234567890.0
        frt.os.getpid = lambda: 42

    def tearDown(self):
        frt.STATE_FILE = self._saved["STATE_FILE"]
        frt.load_path_config = self._saved["load_path_config"]
        frt.get_key_info = self._saved["get_key_info"]
        frt.decrypt_database = self._saved["decrypt_database"]
        frt.verify_sqlite = self._saved["verify_sqlite"]
        frt.raw_fingerprint = self._saved["raw_fingerprint"]
        frt.time.time = self._saved["time_time"]
        frt.os.getpid = self._saved["os_getpid"]

    def _copy(self, src, dst):
        self.decrypt_calls += 1
        Path(dst).write_bytes(("attempt-%d" % self.decrypt_calls).encode("ascii"))
        return True

    def _seq_fp(self, values):
        it = iter(values)
        def _fn(db_dir, rel):
            return next(it)
        frt.raw_fingerprint = _fn

    def test_second_attempt_stable_publishes_with_attempt_count(self):
        state = {"message/message_0.db": {"fingerprint": {"size": 0, "mtime_ns": 0}}}
        self._seq_fp([
            {"size": 1, "mtime_ns": 1},  # pre-check -> mismatch old
            {"size": 2, "mtime_ns": 2},  # attempt 1 pre
            {"size": 3, "mtime_ns": 3},  # attempt 1 post -> race
            {"size": 3, "mtime_ns": 3},  # attempt 2 pre
            {"size": 3, "mtime_ns": 3},  # attempt 2 post -> stable
            {"size": 3, "mtime_ns": 3},  # publish lock re-check
        ])
        result = frt.refresh_one("message/message_0.db", frt.load_path_config(), {}, state, force=False)
        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(self.final_db.read_bytes(), b"attempt-2")
        self.assertEqual(state["message/message_0.db"]["attempts"], 2)
        self.assertEqual(state["message/message_0.db"]["fingerprint"]["size"], 3)

    def test_three_races_keep_existing_output_and_state(self):
        state = {"message/message_0.db": {"fingerprint": {"size": 0, "mtime_ns": 0}}}
        self._seq_fp([
            {"size": 1, "mtime_ns": 1},  # pre-check -> mismatch old
            {"size": 2, "mtime_ns": 2},  # attempt 1 pre
            {"size": 3, "mtime_ns": 3},  # attempt 1 post -> race
            {"size": 4, "mtime_ns": 4},  # attempt 2 pre
            {"size": 5, "mtime_ns": 5},  # attempt 2 post -> race
            {"size": 6, "mtime_ns": 6},  # attempt 3 pre
            {"size": 7, "mtime_ns": 7},  # attempt 3 post -> race
        ])
        result = frt.refresh_one("message/message_0.db", frt.load_path_config(), {}, state, force=False)
        self.assertEqual(result["status"], "raced")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(result["attempt_details"]), 3)
        self.assertEqual(self.final_db.read_bytes(), b"old_v1")
        self.assertEqual(state["message/message_0.db"]["fingerprint"]["size"], 0)


if __name__ == "__main__":
    unittest.main()
