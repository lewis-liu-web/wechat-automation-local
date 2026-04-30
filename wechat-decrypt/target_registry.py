#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Target discovery and configuration registry for the WeChat bot.

Design goals:
- Discover chats from decrypted local WeChat DBs.
- Keep newly discovered chats in a pending candidate pool by default.
- Provide safe, atomic config updates for CLI/UI callers.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "wechat_bot_targets.json"
CANDIDATES_PATH = ROOT / "wechat_bot_candidates.json"
DECRYPTED_DIR = ROOT / "decrypted"
DECRYPTED_MESSAGE_DIR = DECRYPTED_DIR / "message"
DECRYPTED_CONTACT_DB = DECRYPTED_DIR / "contact" / "contact.db"
METADATA_DBS = ("contact/contact.db", "session/session.db")


def _load_runtime_paths():
    try:
        from config import load_config as _load_config
        cfg = _load_config()
        return Path(cfg.get("db_dir", "")), Path(cfg.get("decrypted_dir", str(DECRYPTED_DIR)))
    except Exception:
        return Path(""), DECRYPTED_DIR


def _needs_refresh(raw_db, decrypted_db):
    raw_db = Path(raw_db)
    decrypted_db = Path(decrypted_db)
    if not raw_db.exists():
        return False
    if not decrypted_db.exists():
        return True
    try:
        return raw_db.stat().st_mtime > decrypted_db.stat().st_mtime + 0.5
    except OSError:
        return False


def refresh_metadata_dbs(metadata_dbs=METADATA_DBS):
    """Refresh small contact/session DBs before discovery so chatroom names are current.

    Best-effort: failures are reported to the caller but do not block message discovery.
    """
    db_dir, decrypted_dir = _load_runtime_paths()
    results = []
    for rel in metadata_dbs:
        raw = db_dir / rel if db_dir else Path("")
        out = decrypted_dir / rel
        item = {"db": rel, "refreshed": False, "skipped": False, "ok": True, "message": ""}
        if not _needs_refresh(raw, out):
            item["skipped"] = True
            item["message"] = "up_to_date_or_missing_raw"
            results.append(item)
            continue
        cmd = [sys.executable, str(ROOT / "decrypt_db.py"), "--db", rel]
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
            item["refreshed"] = (r.returncode == 0)
            item["ok"] = (r.returncode == 0)
            tail = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[-1000:]
            item["message"] = tail
        except Exception as e:
            item["ok"] = False
            item["message"] = str(e)
        results.append(item)
    return results


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def msg_table(username):
    return "Msg_" + hashlib.md5(str(username).encode("utf-8")).hexdigest()


def safe_json_load(path, default):
    path = Path(path)
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def connect_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)


def table_exists(con, name):
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


def load_config(path=CONFIG_PATH):
    cfg = safe_json_load(path, {})
    cfg.setdefault("targets", [])
    cfg.setdefault("default_triggers", [])
    cfg.setdefault("default_reply_template", "")
    return cfg


def load_candidates(path=CANDIDATES_PATH):
    data = safe_json_load(path, {"version": 1, "updated_at": "", "candidates": []})
    data.setdefault("version", 1)
    data.setdefault("updated_at", "")
    data.setdefault("candidates", [])
    return data


def display_name_from_contact(username, contact_db=DECRYPTED_CONTACT_DB):
    p = Path(contact_db)
    if not p.exists() or not username:
        return ""
    con = connect_ro(p)
    con.row_factory = sqlite3.Row
    try:
        if table_exists(con, "contact"):
            row = con.execute(
                'select remark, nick_name, alias, username from contact where username=? limit 1',
                (username,),
            ).fetchone()
            if row:
                return row["remark"] or row["nick_name"] or row["alias"] or row["username"] or ""
        if username.endswith("@chatroom") and table_exists(con, "chat_room_info_detail"):
            cols = [r[1] for r in con.execute('pragma table_info("chat_room_info_detail")')]
            if "username_" in cols:
                row = con.execute(
                    'select username_ from chat_room_info_detail where username_=? limit 1',
                    (username,),
                ).fetchone()
                if row:
                    return row["username_"]
    finally:
        con.close()
    return ""


def discover_from_message_db(db_path):
    """Return candidates visible in one decrypted message_N.db.

    A chat has a message table named md5(username) and usually appears in Name2Id.
    We require the target Msg_xxx table to exist so enabling can work immediately.
    """
    p = Path(db_path)
    if not p.exists() or p.name == "message_fts.db" or p.name == "message_resource.db":
        return []
    con = connect_ro(p)
    con.row_factory = sqlite3.Row
    try:
        if not table_exists(con, "Name2Id"):
            return []
        tables = {r["name"] for r in con.execute("select name from sqlite_master where type='table'")}
        out = []
        for row in con.execute('select user_name from Name2Id where coalesce(user_name, "") != ""'):
            username = row["user_name"]
            table = msg_table(username)
            if table not in tables:
                continue
            last_local_id = 0
            last_message_time = 0
            try:
                r2 = con.execute(
                    'select max(local_id) as last_local_id, max(create_time) as last_message_time from "%s"' % table
                ).fetchone()
                if r2:
                    last_local_id = int(r2["last_local_id"] or 0)
                    last_message_time = int(r2["last_message_time"] or 0)
            except Exception:
                pass
            out.append({
                "username": username,
                "type": "group" if username.endswith("@chatroom") else "contact",
                "db": p.name,
                "table": table,
                "last_local_id": last_local_id,
                "last_message_time": last_message_time,
                "last_message_time_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_message_time)) if last_message_time else "",
            })
        return out
    finally:
        con.close()


def discover_all(message_dir=DECRYPTED_MESSAGE_DIR):
    items = []
    for db in sorted(Path(message_dir).glob("message_*.db")):
        items.extend(discover_from_message_db(db))
    seen = set()
    deduped = []
    for item in sorted(items, key=lambda x: (x.get("last_message_time") or 0), reverse=True):
        if item["username"] in seen:
            continue
        seen.add(item["username"])
        item["name"] = display_name_from_contact(item["username"]) or item["username"]
        deduped.append(item)
    return deduped


def target_usernames(cfg):
    return {t.get("username") for t in cfg.get("targets", []) if t.get("username")}


def candidate_by_username(data):
    return {c.get("username"): c for c in data.get("candidates", []) if c.get("username")}


def discover_candidates(config_path=CONFIG_PATH, candidates_path=CANDIDATES_PATH, include_contacts=False, refresh_metadata=True):
    metadata_refresh = refresh_metadata_dbs() if refresh_metadata else []
    cfg = load_config(config_path)
    configured = target_usernames(cfg)
    data = load_candidates(candidates_path)
    by_user = candidate_by_username(data)
    found = discover_all()
    added = 0
    updated = 0
    for item in found:
        if not include_contacts and item.get("type") != "group":
            continue
        username = item["username"]
        if username in configured:
            continue
        old = by_user.get(username)
        if old:
            old.update({k: item[k] for k in ["name", "type", "db", "table", "last_local_id", "last_message_time", "last_message_time_text"]})
            old.setdefault("status", "pending")
            old["updated_at"] = now_text()
            updated += 1
        else:
            cand = dict(item)
            cand.update({
                "status": "pending",
                "first_seen": now_text(),
                "updated_at": now_text(),
                "suggested_knowledge_bases": [],
                "source": "decrypted_message_db",
            })
            data["candidates"].append(cand)
            by_user[username] = cand
            added += 1
    data["updated_at"] = now_text()
    save_json_atomic(candidates_path, data)
    return {"discovered": len(found), "added": added, "updated": updated, "pending": sum(1 for c in data["candidates"] if c.get("status") == "pending"), "metadata_refresh": metadata_refresh}


def find_target(cfg, key):
    for t in cfg.get("targets", []):
        if key in (t.get("username"), t.get("name")):
            return t
    return None


def find_candidate(data, key):
    for c in data.get("candidates", []):
        if key in (c.get("username"), c.get("name")):
            return c
    return None


def enable_candidate(key, knowledge_bases=None, config_path=CONFIG_PATH, candidates_path=CANDIDATES_PATH):
    cfg = load_config(config_path)
    if find_target(cfg, key):
        raise ValueError("target already configured: %s" % key)
    data = load_candidates(candidates_path)
    cand = find_candidate(data, key)
    if not cand:
        raise ValueError("candidate not found: %s (run discover first)" % key)
    username = cand["username"]
    if find_target(cfg, username):
        raise ValueError("target already configured: %s" % username)
    target = {
        "name": cand.get("name") or username,
        "username": username,
        "db": cand.get("db") or "message_0.db",
        "table": cand.get("table") or msg_table(username),
        "last_local_id": int(cand.get("last_local_id") or 0),
        "enabled": True,
        "triggers": [],
        "reply_template": "",
        "knowledge_bases": knowledge_bases or cand.get("suggested_knowledge_bases") or [],
    }
    cfg.setdefault("targets", []).append(target)
    cand["status"] = "enabled"
    cand["enabled_at"] = now_text()
    cand["updated_at"] = now_text()
    data["updated_at"] = now_text()
    save_json_atomic(config_path, cfg)
    save_json_atomic(candidates_path, data)
    return target


def set_enabled(key, enabled, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    t["enabled"] = bool(enabled)
    save_json_atomic(config_path, cfg)
    return t


def bind_wiki(key, knowledge_bases, replace=False, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    if replace:
        t["knowledge_bases"] = list(knowledge_bases)
    else:
        cur = list(t.get("knowledge_bases") or [])
        for kb in knowledge_bases:
            if kb not in cur:
                cur.append(kb)
        t["knowledge_bases"] = cur
    save_json_atomic(config_path, cfg)
    return t


def list_items(kind="all", config_path=CONFIG_PATH, candidates_path=CANDIDATES_PATH):
    cfg = load_config(config_path)
    data = load_candidates(candidates_path)
    return {"targets": cfg.get("targets", []), "candidates": data.get("candidates", [])}


def list_groups(config_path=CONFIG_PATH, candidates_path=CANDIDATES_PATH):
    """Return a merged group view for CLI display.

    Candidates and configured targets are merged by username.  Pending groups remain
    visible after a target is configured, with an explicit listen_enabled column.
    """
    cfg = load_config(config_path)
    data = load_candidates(candidates_path)
    by_user = {}

    def ensure(username):
        if username not in by_user:
            by_user[username] = {
                "status": "",
                "listen_enabled": False,
                "name": username,
                "username": username,
                "db": "",
                "last_local_id": "",
                "last_message_time": "",
                "knowledge_bases": "",
            }
        return by_user[username]

    for c in data.get("candidates", []):
        username = c.get("username")
        if not username or c.get("type") != "group":
            continue
        row = ensure(username)
        row.update({
            "status": c.get("status") or "pending",
            "name": c.get("name") or username,
            "db": c.get("db") or "",
            "last_local_id": c.get("last_local_id") or "",
            "last_message_time": c.get("last_message_time_text") or "",
        })

    for t in cfg.get("targets", []):
        username = t.get("username")
        if not username or not str(username).endswith("@chatroom"):
            continue
        row = ensure(username)
        row.update({
            "status": "enabled" if t.get("enabled", True) else "disabled",
            "listen_enabled": bool(t.get("enabled", True)),
            "name": t.get("name") or row.get("name") or username,
            "db": t.get("db") or row.get("db") or "",
            "last_local_id": t.get("last_local_id") or row.get("last_local_id") or "",
            "knowledge_bases": ",".join(t.get("knowledge_bases") or []),
        })

    rows = list(by_user.values())
    rows.sort(key=lambda r: (0 if r.get("status") == "pending" else 1, str(r.get("last_message_time") or "")), reverse=False)
    return rows

