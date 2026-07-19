"""Regression tests for target discovery scan hardening (Stage 5 fix)."""
from __future__ import annotations

import json
import sqlite3
import sys
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import target_registry as reg
import control_api


def test_is_canonical_message_db():
    assert reg._is_canonical_message_db("message_0.db") is True
    assert reg._is_canonical_message_db("message_12.db") is True
    assert reg._is_canonical_message_db("message_0-first.material.db") is False
    assert reg._is_canonical_message_db("message_0.check.db") is False
    assert reg._is_canonical_message_db("message_0.latest.db") is False
    assert reg._is_canonical_message_db("message_fts.db") is False


def test_discover_from_message_db_skips_corrupt(tmp_path):
    bad = tmp_path / "message_0-first.material.db"
    bad.write_bytes(b"not a sqlite database")
    assert reg.discover_from_message_db(bad) == []


def test_discover_all_ignores_material_and_reads_shard(tmp_path):
    msg_dir = tmp_path / "message"
    msg_dir.mkdir()
    # junk
    (msg_dir / "message_0-first.material.db").write_bytes(b"nope")
    # good shard with Name2Id + Msg table
    good = msg_dir / "message_0.db"
    username = "999@chatroom"
    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
    con = sqlite3.connect(good)
    con.execute("create table Name2Id (user_name text)")
    con.execute("insert into Name2Id values (?)", (username,))
    con.execute(f'create table "{table}" (local_id integer, create_time integer)')
    con.execute(f'insert into "{table}" values (3, 100)')
    con.commit(); con.close()
    # contact for display name
    contact_dir = tmp_path / "contact"
    contact_dir.mkdir()
    contact = contact_dir / "contact.db"
    con = sqlite3.connect(contact)
    con.execute("create table contact (username text, remark text, nick_name text, alias text)")
    con.execute("insert into contact values (?,?,?,?)", (username, "", "超级卡投诉处理-存量中心", ""))
    con.commit(); con.close()

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            reg,
            "_resolved_contact_db",
            lambda contact_db=None: contact if contact_db is None else Path(contact_db),
        )
        items = reg.discover_all(msg_dir)
    finally:
        monkeypatch.undo()
    assert len(items) == 1
    assert items[0]["username"] == username
    assert items[0]["name"] == "超级卡投诉处理-存量中心"
    assert items[0]["last_local_id"] == 3


def test_call_cli_preserves_include_contacts(monkeypatch):
    seen = {}

    def fake_main(argv=None):
        seen["argv"] = list(argv or [])
        print(json.dumps({"discovered": 1, "added": 1, "include_contacts": "--include-contacts" in (argv or [])}))
        return 0

    monkeypatch.setattr(control_api.mt, "main", fake_main)
    body, status, _ = control_api._call_cli(["scan", "--include-contacts", "--json"])
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload.get("include_contacts") is True
    assert seen["argv"] == ["scan", "--include-contacts", "--json"]
    assert payload.get("added") == 1
