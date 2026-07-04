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
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows: suppress console window for subprocess calls
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# In-memory tracking for asynchronous LEANN index builds.
_LEANN_BUILD_JOBS: Dict[str, Dict[str, Any]] = {}
_LEANN_BUILD_LOCK = threading.RLock()

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
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180,
                               creationflags=_NO_WINDOW_FLAGS)
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


def _audit_event(kind, target=None, payload=None):
    """Best-effort audit write; never raises into the CLI flow."""
    try:
        import event_log
        event_log.log_event(kind, target=target, payload=payload or {})
    except Exception:
        pass


VALID_CATEGORIES = ("user", "admin")


def _normalize_category(value, default="user"):
    """Coerce a category string to one of the supported values.

    Anything not in ``VALID_CATEGORIES`` (including empty / None) falls
    back to ``default``. Used both when writing a new target and when
    reading legacy rows that were saved before the field existed.
    """
    v = str(value or "").strip().lower()
    if v in VALID_CATEGORIES:
        return v
    return default


def get_target_category(target):
    """Return the effective category for a target dict (legacy rows = 'user')."""
    return _normalize_category((target or {}).get("category"))



def _is_private_chat(username):
    """Return True for 1-on-1 contacts (anything not a WeChat chatroom)."""
    return not str(username or "").endswith("@chatroom")

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
    # Audit every config/candidate write so unexpected emptying can be traced.
    try:
        import event_log
        name = path.name
        if name == "wechat_bot_targets.json":
            event_log.log_event(
                "config_write",
                target=None,
                payload={
                    "path": str(path),
                    "n_targets": len(data.get("targets") or []),
                    "n_kbs": len(data.get("knowledge_bases") or {}),
                    "default_triggers": list(data.get("default_triggers") or []),
                },
            )
        elif name == "wechat_bot_candidates.json":
            event_log.log_event(
                "candidates_write",
                target=None,
                payload={
                    "path": str(path),
                    "enabled_count": sum(
                        1 for c in (data.get("candidates") or []) if (c.get("status") or "") == "enabled"
                    ),
                    "n_candidates": len(data.get("candidates") or []),
                },
            )
    except Exception:
        pass


def connect_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)


def table_exists(con, name):
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


def load_config(path=CONFIG_PATH):
    cfg = safe_json_load(path, {})
    cfg.setdefault("targets", [])
    cfg.setdefault("default_triggers", [])
    cfg.setdefault("default_reply_template", "")
    cfg.setdefault("reply_engine", {})
    # Default to raw-agent mode so deep tasks are pre-acked and queued asynchronously.
    reply_engine = cfg.get("reply_engine") or {}
    if isinstance(reply_engine, dict) and not reply_engine.get("mode"):
        reply_engine["mode"] = "raw_agent"
        cfg["reply_engine"] = reply_engine
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




def inspect_target(key, config_path=CONFIG_PATH, candidates_path=CANDIDATES_PATH):
    """Return target or pending candidate detail by name/username without mutating files."""
    cfg = load_config(config_path)
    data = load_candidates(candidates_path)
    default_triggers = list(cfg.get("default_triggers") or [])

    def _resolve_kb(kb_id):
        spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
        if not spec:
            return {"id": kb_id, "exists": False, "type": None, "source": None,
                    "enabled": False, "description": ""}
        return {
            "id": kb_id,
            "exists": True,
            "type": spec.get("type") or "local",
            "source": _kb_source(spec),
            "enabled": bool(spec.get("enabled", True)),
            "description": spec.get("description") or "",
        }

    t = find_target(cfg, key)
    if t:
        return {
            "kind": "target",
            "target": dict(t),
            "effective_triggers": list(t.get("triggers") or default_triggers),
            "default_triggers": default_triggers,
            "knowledge_bases": [_resolve_kb(kb_id) for kb_id in (t.get("knowledge_bases") or [])],
        }
    c = find_candidate(data, key)
    if c:
        return {
            "kind": "candidate",
            "candidate": dict(c),
            "effective_triggers": default_triggers,
            "default_triggers": default_triggers,
            "knowledge_bases": [],
        }
    raise ValueError("target or candidate not found: %s" % key)


def delete_target(key, config_path=CONFIG_PATH, candidates_path=CANDIDATES_PATH):
    """Hard delete configured target and reset matching candidate to pending if present."""
    cfg = load_config(config_path)
    data = load_candidates(candidates_path)
    target = find_target(cfg, key)
    if not target:
        raise ValueError("target not found: %s" % key)
    cfg["targets"] = [t for t in cfg.get("targets", []) if t is not target]
    save_json_atomic(config_path, cfg)
    username = target.get("username") or ""
    name = target.get("name") or ""
    touched = False
    for c in data.get("candidates", []):
        if c.get("username") == username or c.get("name") == name:
            c["status"] = "pending"
            c.pop("enabled_at", None)
            c["updated_at"] = now_text()
            touched = True
    if touched:
        data["updated_at"] = now_text()
    save_json_atomic(candidates_path, data)
    return dict(target)


def enable_candidate(key, knowledge_bases=None, category=None, config_path=CONFIG_PATH, candidates_path=CANDIDATES_PATH):
    cfg = load_config(config_path)
    data = load_candidates(candidates_path)

    def _merge_and_validate(cur, new_kbs):
        merged = list(cur)
        for kb in new_kbs or []:
            if kb not in merged:
                merged.append(kb)
        for kb in merged:
            _ensure_kb_registered(kb, cfg=cfg, config_path=config_path)
        validate_knowledge_bases(merged, cfg=cfg)
        return merged

    existing = find_target(cfg, key)
    if existing:
        existing["enabled"] = True
        existing["category"] = _normalize_category(category, default=existing.get("category"))
        if knowledge_bases:
            cur = list(existing.get("knowledge_bases") or [])
            final_kbs = _merge_and_validate(cur, knowledge_bases)
            existing["knowledge_bases"] = final_kbs
        cand = find_candidate(data, existing.get("username") or key)
        if cand:
            cand["status"] = "enabled"
            cand["updated_at"] = now_text()
            data["updated_at"] = now_text()
            save_json_atomic(candidates_path, data)
        save_json_atomic(config_path, cfg)
        return existing

    cand = find_candidate(data, key)
    if not cand:
        raise ValueError("candidate not found: %s (run discover first)" % key)
    username = cand["username"]
    existing = find_target(cfg, username)
    if existing:
        existing["enabled"] = True
        existing["category"] = _normalize_category(category, default=existing.get("category"))
        if knowledge_bases:
            cur = list(existing.get("knowledge_bases") or [])
            final_kbs = _merge_and_validate(cur, knowledge_bases)
            existing["knowledge_bases"] = final_kbs
        cand["status"] = "enabled"
        cand["updated_at"] = now_text()
        data["updated_at"] = now_text()
        save_json_atomic(config_path, cfg)
        save_json_atomic(candidates_path, data)
        return existing
    selected_kbs = knowledge_bases or cand.get("suggested_knowledge_bases") or []
    for kb in selected_kbs:
        _ensure_kb_registered(kb, cfg=cfg, config_path=config_path)
    validate_knowledge_bases(selected_kbs, cfg=cfg)
    target = {
        "name": cand.get("name") or username,
        "username": username,
        "db": cand.get("db") or "message_0.db",
        "table": cand.get("table") or msg_table(username),
        "last_local_id": int(cand.get("last_local_id") or 0),
        "enabled": True,
        "triggers": [],
        "reply_template": "",
        "knowledge_bases": selected_kbs,
        "category": _normalize_category(category),
    }
    if _is_private_chat(username):
        target["response_mode"] = "free"
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


def set_category(key, category, config_path=CONFIG_PATH):
    """Update the business category of an existing target (admin / user)."""
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    t["category"] = _normalize_category(category)
    save_json_atomic(config_path, cfg)
    return t

def normalize_admin_senders(value) -> List[str]:
    """Coerce admin_senders field to a clean list of trimmed strings."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[\n,]", value)
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if item is None:
                continue
            parts.extend(re.split(r"[\n,]", str(item)))
    else:
        parts = [str(value)]
    cleaned: List[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if text:
            cleaned.append(text)
    return cleaned


def get_target_admin_senders(target) -> List[str]:
    """Return the effective admin_senders list for a target dict."""
    return normalize_admin_senders((target or {}).get("admin_senders"))


def is_admin_sender(target, sender_username, sender_display_name=None) -> bool:
    """Return True if sender_username (or sender_display_name) is allowed to issue admin commands for target.

    Matching is case-insensitive and whitespace-trimmed. Entries in admin_senders may be wxids or nicknames/remarks.
    """
    if get_target_category(target) != "admin":
        return False
    whitelist = get_target_admin_senders(target)
    if not whitelist:
        return True
    candidates = {str(sender_username or "").strip().lower()}
    if sender_display_name:
        candidates.add(str(sender_display_name).strip().lower())
    normalized = {entry.strip().lower() for entry in whitelist if entry.strip()}
    return bool(candidates & normalized)


def mode_bundle(mode: str) -> Dict[str, Any]:
    """Return the full reply/session/context policy bundle for a response mode."""
    mode = str(mode or "").strip().lower()
    if mode == "customer_service":
        return {
            "mode": "customer_service",
            "reply_policy": "knowledge_grounded",
            # require_followup_intent: True/unset = session messages go to reply_decision.decide();
            # False = session follow-ups are silent without calling decide().
            "session_policy": {"timeout_seconds": 120, "max_turns": 5, "require_followup_intent": True},
            "context_policy": {"time_window_seconds": 120, "max_messages": 40, "sender_recent_limit": 6, "include_bot_recent": True},
        }
    return {
        "mode": "group_assistant",
        "reply_policy": "balanced",
            # require_followup_intent: True/unset = session messages go to reply_decision.decide();
            # False = session follow-ups are silent without calling decide().
        "session_policy": {"timeout_seconds": 60, "max_turns": 3, "require_followup_intent": True},
        "context_policy": {"time_window_seconds": 90, "max_messages": 30, "sender_recent_limit": 5, "include_bot_recent": True},
    }


def get_registered_agent_instance(cfg, instance_id):
    """Return the registered agent_provider instance dict matching instance_id, or None."""
    if not instance_id:
        return None
    instances = ((cfg or {}).get("agent_provider") or {}).get("instances") or []
    for inst in instances:
        if isinstance(inst, dict) and inst.get("id") == instance_id:
            return inst
    return None


def get_target_dedicated_instance_id(target, cfg) -> Optional[str]:
    """Return the target's dedicated_agent_instance_id if it resolves to a registered instance, else None."""
    if not target:
        return None
    candidate = target.get("dedicated_agent_instance_id")
    if not candidate:
        return None
    candidate = str(candidate).strip()
    if not candidate:
        return None
    if get_registered_agent_instance(cfg, candidate) is None:
        return None
    return candidate


def set_target_dedicated_agent_instance_id(key, instance_id, config_path=CONFIG_PATH):
    """Bind a target to a dedicated agent_provider instance (empty clears the binding)."""
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    cleaned = str(instance_id or "").strip()
    if cleaned and get_registered_agent_instance(cfg, cleaned) is None:
        raise ValueError("agent_provider instance not registered: %s" % cleaned)
    t["dedicated_agent_instance_id"] = cleaned or None
    save_json_atomic(config_path, cfg)
    return t


def set_target_field(key, field, value, config_path=CONFIG_PATH):
    """Update an arbitrary scalar field on an existing target."""
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    t[field] = value
    save_json_atomic(config_path, cfg)
    return t


def set_target_mode_bundle(key, mode, config_path=CONFIG_PATH):
    """Update target response mode and derived policy fields atomically."""
    bundle = mode_bundle(mode)
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    t.update(bundle)
    save_json_atomic(config_path, cfg)
    return t


def get_triggers(key, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    return {
        "name": t.get("name"),
        "username": t.get("username"),
        "triggers": list(t.get("triggers") or []),
        "default_triggers": list(cfg.get("default_triggers") or []),
    }


def get_default_triggers(config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    return list(cfg.get("default_triggers") or [])


def set_default_triggers(words, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    clean = []
    for item in words or []:
        item = str(item).strip()
        if item and item not in clean:
            clean.append(item)
    cfg["default_triggers"] = clean
    save_json_atomic(config_path, cfg)
    return clean


def set_triggers(key, triggers, replace=False, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    clean = []
    for item in triggers or []:
        item = str(item).strip()
        if item and item not in clean:
            clean.append(item)
    if replace:
        t["triggers"] = clean
    else:
        cur = list(t.get("triggers") or [])
        for item in clean:
            if item not in cur:
                cur.append(item)
        t["triggers"] = cur
    save_json_atomic(config_path, cfg)
    return get_triggers(key, config_path=config_path)


def remove_triggers(key, triggers, clear=False, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    if clear:
        t["triggers"] = []
    else:
        rm = {str(x).strip() for x in (triggers or []) if str(x).strip()}
        t["triggers"] = [x for x in list(t.get("triggers") or []) if x not in rm]
    save_json_atomic(config_path, cfg)
    return get_triggers(key, config_path=config_path)


def _resolve_wiki_root(config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    base = Path(config_path).resolve().parent
    wiki_dir = cfg.get("wiki_dir")
    if wiki_dir:
        wiki_root = Path(wiki_dir)
        if not wiki_root.is_absolute():
            wiki_root = base / wiki_root
    else:
        wiki_root = base / "wiki"
    return wiki_root


def _scan_wiki_kbs(config_path=CONFIG_PATH):
    """Return auto-discovered local KB rows for folders under wiki_dir.

    Direct subdirectories of wiki_dir become KBs named after the folder.
    Subdirectories of wiki_dir/scenes/ become scene KBs with id 'scene.<name>'.
    System directories (core, scenes, providers, __pycache__, etc.) are skipped.
    """
    wiki_root = _resolve_wiki_root(config_path)
    if not wiki_root.exists():
        return []
    excluded = {"core", "scenes", "providers", "__pycache__", ".git", "temp", "tmp"}
    rows = []
    seen_paths = set()
    # Direct children of wiki_dir
    for p in sorted(wiki_root.iterdir()):
        if p.is_dir() and p.name not in excluded:
            rel = p.name
            rows.append({
                "id": p.name,
                "type": "local",
                "enabled": True,
                "knowledge_base_id": "",
                "path": rel,
                "source": "local_folder",
                "scope": "scene",
                "limit": None,
                "timeout": None,
                "description": "自动识别：%s" % p.name,
            })
            seen_paths.add(rel)
    # Scene KBs under wiki_dir/scenes/
    scenes_root = wiki_root / "scenes"
    if scenes_root.exists() and scenes_root.is_dir():
        for p in sorted(scenes_root.iterdir()):
            if p.is_dir() and p.name not in excluded:
                rel = "scenes/%s" % p.name
                kb_id = "scene.%s" % p.name
                if rel not in seen_paths:
                    rows.append({
                        "id": kb_id,
                        "type": "local",
                        "enabled": True,
                        "knowledge_base_id": "",
                        "path": rel,
                        "source": "local_folder",
                        "scope": "scene",
                        "limit": None,
                        "timeout": None,
                        "description": "自动识别：%s" % kb_id,
                    })
                    seen_paths.add(rel)
    return rows
def _get_kb_spec(kb_id, cfg=None, config_path=CONFIG_PATH):
    """Return a knowledge-base spec for kb_id, checking the configured KB map first.

    Order of lookup:
      1. ``cfg["knowledge_bases"][kb_id]`` when present — the returned dict is
         a copy of the configured spec with ``_from_scan=False``.
      2. Otherwise, scan wiki subdirectories via :func:`_scan_wiki_kbs` and
         synthesize a local-only spec (``type="local"``, ``source="local_folder"``,
         ``scope="scene"``) with ``_from_scan=True`` when a folder matches the
         requested ``kb_id``.
      3. Returns ``None`` if neither lookup yields a match.
    """
    if not kb_id:
        return None
    cfg = cfg if cfg is not None else load_config(config_path)
    configured = (cfg.get("knowledge_bases") or {}).get(kb_id)
    if configured:
        out = dict(configured or {})
        out["_from_scan"] = False
        return out
    for row in _scan_wiki_kbs(config_path):
        if row.get("id") == kb_id:
            out = {
                "type": "local",
                "path": row.get("path") or "",
                "enabled": True,
                "source": "local_folder",
                "scope": "scene",
                "_from_scan": True,
            }
            # Preserve any extra metadata the scanner exposed (description, etc.).
            for key in ("description", "knowledge_base_id", "limit", "timeout"):
                if row.get(key) is not None and key not in out:
                    out[key] = row.get(key)
            return out
    return None


def _is_scanned_kb(kb_id, cfg=None, config_path=CONFIG_PATH):
    """Return True when kb_id resolves via the wiki scanner rather than config."""
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    return bool(spec and spec.get("_from_scan"))



def _ensure_kb_registered(kb_id, cfg=None, config_path=CONFIG_PATH):
    """Auto-register a scanned wiki KB into cfg["knowledge_bases"] when needed.

    Behavior:
      * ``legacy:<id>`` aliases pass through and return None (validation skips them).
      * If the KB is already configured, return the existing spec untouched.
      * Otherwise consult :func:`_get_kb_spec`. When that yields a spec with
        ``_from_scan=True``, copy the relevant fields into
        ``cfg["knowledge_bases"][kb_id]`` and return the freshly registered
        spec. The caller must save ``cfg`` after the overall operation validates.
      * Returns None for KB ids that neither match a configured entry nor a
        scanned wiki folder — the caller is expected to raise the standard
        "unknown knowledge base" error.
    """
    if not kb_id or str(kb_id).startswith("legacy:"):
        return None
    cfg = cfg if cfg is not None else load_config(config_path)
    cfg.setdefault("knowledge_bases", {})
    if kb_id in cfg["knowledge_bases"]:
        return cfg["knowledge_bases"][kb_id]
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec or not spec.get("_from_scan"):
        return None
    registered = {k: spec.get(k) for k in
                  ("id", "type", "path", "source", "scope", "description", "enabled")
                  if spec.get(k) is not None}
    registered.setdefault("id", kb_id)
    cfg["knowledge_bases"][kb_id] = registered
    # Caller is responsible for saving config after validating the full operation.
    return registered


def list_knowledge_bases(config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    rows = []
    existing_ids = set()
    existing_paths = set()
    for kb_id, spec in sorted((cfg.get("knowledge_bases") or {}).items()):
        spec = spec or {}
        row = {
            "id": kb_id,
            "type": spec.get("type") or "local",
            "enabled": spec.get("enabled", True),
            "knowledge_base_id": spec.get("knowledge_base_id") or "",
            "index_name": spec.get("index_name") or "",
            "path": spec.get("path") or spec.get("dir") or "",
            "source": _kb_source(spec),
            "scope": spec.get("scope") or "scene",
            "limit": spec.get("limit"),
            "timeout": spec.get("timeout"),
            "description": spec.get("description") or "",
            "managed": True,
        }
        rows.append(row)
        existing_ids.add(kb_id)
        existing_paths.add(row["path"])
    for row in _scan_wiki_kbs(config_path):
        if row["id"] not in existing_ids and row["path"] not in existing_paths:
            row["managed"] = False
            rows.append(row)
    return rows


def _resolve_kb_path(path, config_path=CONFIG_PATH, cfg=None):
    """Resolve a local KB path, honoring wiki_dir from config.

    - Returns None for empty path.
    - Absolute paths are returned only if they exist.
    - Relative paths resolve against cfg['wiki_dir'] when present,
      using the same logic as reply_engine._resolve_wiki_path.
    - Falls back to the config file's parent directory.
    - Returns None if the resolved path does not exist.
    """
    if not path:
        return None
    p = Path(path)
    if p.is_absolute():
        return p if p.exists() else None
    cfg = cfg or load_config(config_path)
    base = Path(config_path).resolve().parent
    wiki_dir = cfg.get("wiki_dir")
    if wiki_dir:
        wiki_root = Path(wiki_dir)
        if not wiki_root.is_absolute():
            wiki_root = base / wiki_root
    else:
        wiki_root = base / "wiki"
    resolved = wiki_root / p
    return resolved if resolved.exists() else None


def validate_kb_config(kb_id, spec, config_path=CONFIG_PATH):
    """Validate a knowledge-base spec before it is persisted.

    Raises ValueError with a clear message for invalid configurations.
    """
    cfg = load_config(config_path)
    kb_type = str(spec.get("type") or "").strip().lower()
    if not kb_type:
        raise ValueError("知识库 '%s' 缺少 type 字段" % kb_id)
    if kb_type not in ("local", "getnote", "ima", "hook", "leann"):
        raise ValueError("知识库 '%s' 不支持的类型: %s" % (kb_id, kb_type))
    if not isinstance(spec.get("enabled", True), bool):
        raise ValueError("知识库 '%s' enabled 必须是布尔值" % kb_id)
    if kb_type == "local":
        kb_path = _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)
        if not kb_path or not kb_path.exists() or not kb_path.is_dir():
            raise ValueError("知识库 '%s' path does not exist or is not a directory: %s" % (kb_id, spec.get("path")))
    elif kb_type in ("getnote", "ima"):
        if not spec.get("knowledge_base_id"):
            raise ValueError("知识库 '%s' 必须提供 knowledge_base_id" % kb_id)
    elif kb_type == "hook":
        if not spec.get("executable"):
            raise ValueError("知识库 '%s' 必须提供 executable" % kb_id)
    elif kb_type == "leann":
        if not spec.get("index_name"):
            raise ValueError("知识库 '%s' 必须提供 index_name" % kb_id)


def _local_kb_dir(kb_id, spec, config_path=CONFIG_PATH):
    """Return the resolved local directory for a local KB spec."""
    cfg = load_config(config_path)
    return _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)


def add_knowledge_base(kb_id, kb_type="getnote", knowledge_base_id=None, path=None, description="",
                        executable=None, scope="scene", limit=None, timeout=None,
                       enabled=True, replace=False, source=None, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    cfg.setdefault("knowledge_bases", {})
    if kb_id in cfg["knowledge_bases"] and not replace:
        raise ValueError("knowledge base already exists: %s (use --replace to update)" % kb_id)
    spec = {"type": kb_type, "enabled": bool(enabled)}
    if source:
        spec["source"] = str(source).strip()
    if kb_type == "getnote":
        if not knowledge_base_id:
            raise ValueError("--kid/--knowledge-base-id is required for getnote knowledge base")
        spec["knowledge_base_id"] = knowledge_base_id
        if executable:
            spec["executable"] = executable
        spec["scope"] = scope or "scene"
        if limit is not None:
            spec["limit"] = int(limit)
        if timeout is not None:
            spec["timeout"] = int(timeout)
    elif kb_type == "hook":
        if not executable:
            raise ValueError("--executable is required for hook knowledge base")
        spec["executable"] = executable
        if knowledge_base_id:
            spec["knowledge_base_id"] = knowledge_base_id
        spec["scope"] = scope or "scene"
        if limit is not None:
            spec["limit"] = int(limit)
        if timeout is not None:
            spec["timeout"] = int(timeout)
    elif kb_type == "local":
        if not path:
            raise ValueError("--path is required for local knowledge base")
        spec["path"] = path
        spec["scope"] = scope or "scene"
    elif kb_type == "leann":
        if not knowledge_base_id:
            raise ValueError("--kid/--knowledge-base-id (用作 LEANN index_name) 是 leann 类型必填")
        spec["index_name"] = str(knowledge_base_id).strip()
        spec["scope"] = scope or "scene"
        if executable:
            spec["executable"] = str(executable).strip()
        if limit is not None:
            spec["limit"] = int(limit)
        if timeout is not None:
            spec["timeout"] = int(timeout)
    else:
        raise ValueError("unsupported knowledge base type: %s" % kb_type)
    if description:
        spec["description"] = description
    validate_kb_config(kb_id, spec, config_path=config_path)
    cfg["knowledge_bases"][kb_id] = spec
    save_json_atomic(config_path, cfg)
    out = dict(spec)
    out["id"] = kb_id
    return out


def set_knowledge_base_enabled(kb_id, enabled=True, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("_from_scan"):
        raise ValueError("该知识库 '%s' 来自 wiki 扫描，未在配置中注册，请先在配置中显式注册后再管理" % kb_id)
    configured = (cfg.get("knowledge_bases") or {}).get(kb_id) or {}
    configured["enabled"] = bool(enabled)
    save_json_atomic(config_path, cfg)
    out = dict(configured)
    out["id"] = kb_id
    return out

def delete_knowledge_base(kb_id, remove_files=False, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    kbs = cfg.setdefault("knowledge_bases", {})
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("_from_scan"):
        raise ValueError("该知识库 '%s' 来自 wiki 扫描，未在配置中注册，请先在配置中显式注册后再管理" % kb_id)
    configured = kbs.get(kb_id)
    if not configured:
        raise ValueError("知识库不存在: %s" % kb_id)
    for t in cfg.get("targets") or []:
        t["knowledge_bases"] = [x for x in (t.get("knowledge_bases") or []) if x != kb_id]
    removed = kbs.pop(kb_id)
    save_json_atomic(config_path, cfg)
    if remove_files and (removed or {}).get("type") == "local":
        kb_dir = _resolve_kb_path((removed or {}).get("path") or "", config_path=config_path, cfg=cfg)
        if kb_dir and kb_dir.exists() and kb_dir.is_dir() and _wiki_root() in kb_dir.resolve().parents:
            shutil.rmtree(str(kb_dir), ignore_errors=True)
    out = dict(removed or {})
    out["id"] = kb_id
    return out


def _unknown_kb_message(kb, cfg):
    for known_id, spec in (cfg.get("knowledge_bases") or {}).items():
        if str((spec or {}).get("knowledge_base_id") or "") == str(kb):
            return "unknown knowledge base id: %s. Did you mean configured alias '%s'? Use: python manage_targets.py kb <群名> %s" % (kb, known_id, known_id)
    return "unknown knowledge base id: %s. Run 'python manage_targets.py kb-list' or create one with 'python manage_targets.py kb-add <别名> --kid <外部知识库ID>'" % kb


def _kb_source(spec):
    spec = spec or {}
    explicit = str(spec.get("source") or "").strip().lower()
    if explicit:
        return explicit
    typ = str(spec.get("type") or "local").strip().lower()
    if typ == "local":
        return "local_folder"
    if typ == "leann":
        return "leann"
    return typ


def validate_knowledge_bases(knowledge_bases, config_path=CONFIG_PATH, cfg=None):
    cfg = cfg or load_config(config_path)
    sources = set()
    for kb in knowledge_bases or []:
        if str(kb).startswith("legacy:"):
            continue
        spec = _get_kb_spec(kb, cfg=cfg, config_path=config_path)
        if not spec:
            raise ValueError(_unknown_kb_message(kb, cfg))
        if spec.get("enabled", True) is False:
            raise ValueError("知识库 '%s' 已被禁用，无法绑定。请先启用或选择其他知识库。" % kb)
        sources.add(_kb_source(spec))
    if len(sources) > 1:
        raise ValueError("一个监听目标只能绑定同源知识库，当前混用了: %s。请只选择 obsidian/local_folder/getnote/ima/hook 中的一种。" % ", ".join(sorted(sources)))
    return True



def bind_wiki(key, knowledge_bases, replace=False, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    if replace:
        final_kbs = list(knowledge_bases)
    else:
        cur = list(t.get("knowledge_bases") or [])
        for kb in knowledge_bases:
            if kb not in cur:
                cur.append(kb)
        final_kbs = cur
    for kb in final_kbs:
        _ensure_kb_registered(kb, cfg=cfg, config_path=config_path)
    validate_knowledge_bases(final_kbs, cfg=cfg)
    t["knowledge_bases"] = final_kbs
    save_json_atomic(config_path, cfg)
    return t


def _wiki_root():
    return ROOT / "wiki"


def create_local_kb_dir(kb_id, description="", replace=False, source="local_folder", config_path=CONFIG_PATH):
    """Create a local directory-based knowledge base and register it."""
    if not kb_id:
        raise ValueError("知识库名称不能为空")
    kb_id = str(kb_id).strip()
    cfg = load_config(config_path)
    kbs = cfg.setdefault("knowledge_bases", {})
    if kb_id in kbs and not replace:
        raise ValueError("知识库别名已存在: %s (使用 --replace 覆盖)" % kb_id)
    base_dir = _wiki_root() / kb_id
    base_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "id": kb_id,
        "type": "local",
        "source": source or "local_folder",
        "path": str(base_dir.resolve()),
        "description": description or "本地知识库: %s" % kb_id,
        "enabled": True,
    }
    kbs[kb_id] = spec
    save_json_atomic(config_path, cfg)
    return spec


def get_kb_info(kb_id, config_path=CONFIG_PATH):
    """Return knowledge base info including file stats for local dirs
    and resolved name/counts for online providers (getnote, ima)."""
    cfg = load_config(config_path)
    spec = (cfg.get("knowledge_bases") or {}).get(kb_id)
    if not spec:
        return None
    info = dict(spec)
    if spec.get("type") == "local":
        p = Path(spec.get("path") or "")
        if not p.exists() or not p.is_dir():
            info["file_count"] = 0
            info["exists"] = False
        else:
            files = list(p.rglob("*"))
            docs = [f for f in files if f.is_file() and f.suffix.lower() == ".md"]
            info["file_count"] = len(docs)
            info["total_files"] = len([f for f in files if f.is_file()])
            info["exists"] = True
    elif spec.get("type") == "getnote":
        info.update(_resolve_getnote_kb_info(spec))
    elif spec.get("type") == "ima":
        info.update(_resolve_ima_kb_info(spec))
    elif spec.get("type") == "leann":
        diag = diagnose_leann_kb(spec, config_path=config_path, cfg=cfg)
        info["exists"] = bool(diag.get("ok"))
        info["online_error"] = diag.get("error") or ""
    return info


def rebuild_kb_index(kb_id, config_path=CONFIG_PATH):
    """Force-delete the FTS index for a local knowledge base.

    The index will be rebuilt lazily on the next query via reply_engine.
    """
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持重建本地知识库索引: %s" % kb_id)
    kb_dir = _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)
    if not kb_dir or not kb_dir.exists() or not kb_dir.is_dir():
        raise ValueError("知识库路径不存在或不是目录: %s" % spec.get("path"))
    db_path = kb_dir / ".kb_index.sqlite"
    index_removed = False
    if db_path.exists():
        db_path.unlink()
        index_removed = True
    doc_count = sum(1 for p in kb_dir.rglob("*.md") if p.name != ".kb_index.sqlite")
    return {
        "id": kb_id,
        "index_path": str(db_path),
        "index_removed": index_removed,
        "doc_count": doc_count,
    }


def diagnose_local_kb(kb_id, query='', config_path=CONFIG_PATH):
    """Diagnose a local KB index and run a sample query."""
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持诊断本地知识库: %s" % kb_id)
    import reply_engine
    root = reply_engine._kb_root(cfg)
    resolved = _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)
    if resolved is not None:
        spec = dict(spec)
        spec["path"] = str(resolved)
    # Ensure the index exists so diagnosis reflects current KB contents.
    con = reply_engine._ensure_local_kb_fts(root, spec)
    if con:
        con.close()
    return reply_engine.diagnose_local_kb(root, spec, query=query)


def _getnote_exe(spec):
    return str(spec.get("executable") or spec.get("cli") or "getnote").strip() or "getnote"


def _resolve_getnote_kb_info(spec):
    """Call `getnote kbs -o json` once and return the matching entry's name and counts."""
    out = {"online_name": "", "online_note_count": None, "online_file_count": None, "online_error": ""}
    kb_id = str(spec.get("knowledge_base_id") or spec.get("kb_id") or "").strip()
    if not kb_id:
        out["online_error"] = "未配置 knowledge_base_id"
        return out
    exe = _getnote_exe(spec)
    try:
        proc = subprocess.run([exe, "kbs", "-o", "json"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20,
                              creationflags=_NO_WINDOW_FLAGS)
    except Exception as e:
        out["online_error"] = "调用 getnote 失败: %s" % (e,)
        return out
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        out["online_error"] = "getnote 退出码 %s: %s" % (proc.returncode, (proc.stderr or "").strip()[:200])
        return out
    try:
        payload = json.loads(proc.stdout)
    except Exception as e:
        out["online_error"] = "getnote 返回非 JSON: %s" % (e,)
        return out
    items = ((payload.get("data") or {}).get("topics") or [])
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or item.get("topic_id") or "") == kb_id:
            out["online_name"] = str(item.get("name") or "").strip()
            stats = item.get("stats") or {}
            try:
                out["online_note_count"] = int(stats.get("note_count") or 0)
            except Exception:
                pass
            try:
                out["online_file_count"] = int(stats.get("file_count") or 0)
            except Exception:
                pass
            out["online_description"] = str(item.get("description") or "").strip()
            return out
    out["online_error"] = "未在 getnote 列表中找到 ID %s" % kb_id
    return out


def _resolve_ima_kb_info(spec):
    """Placeholder for IMA. Currently the user has IMA disabled, so this returns
    a stub without making any network call."""
    return {
        "online_name": "",
        "online_note_count": None,
        "online_file_count": None,
        "online_error": "IMA 在线名称解析未实现，可在 wechat_bot_targets.json 手动填 description。",
    }


def list_kbs_extended(config_path=CONFIG_PATH):
    """List KBs with extended info (file count, online name) per entry."""
    rows = list_knowledge_bases(config_path=config_path)
    for row in rows:
        try:
            info = get_kb_info(row.get("id"), config_path=config_path) or {}
        except Exception:
            info = {}
        row["file_count"] = int(info.get("file_count") or 0) if row.get("type") == "local" else None
        row["online_name"] = info.get("online_name") or ""
        row["online_note_count"] = info.get("online_note_count")
        row["online_file_count"] = info.get("online_file_count")
        row["online_error"] = info.get("online_error") or ""
    return rows


def import_kb_file(kb_id, source_path, config_path=CONFIG_PATH, allow_empty=False):
    """Copy a file or directory into a local knowledge base.

    Non-Markdown files are converted to Markdown with Microsoft MarkItDown when
    available.  The local KB stays Markdown-only as the source of truth, which
    keeps Obsidian and the retrieval layer simple.

    Files that fail to convert are skipped by default and reported in the
    ``failed`` list.  Set ``allow_empty=True`` only for the legacy behavior
    (write a stub markdown) and the result is still non-fatal either way.
    """
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持导入本地知识库: %s" % kb_id)
    resolved_dst = _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)
    dst = Path(str(resolved_dst)) if resolved_dst else Path(spec.get("path") or "")
    if not dst.exists():
        dst.mkdir(parents=True, exist_ok=True)
    src = Path(source_path)
    if not src.exists():
        raise ValueError("源路径不存在: %s" % source_path)
    copied = []
    failed = []

    def safe_stem(name):
        stem = Path(name).stem.strip() or "document"
        stem = re.sub(r"[<>:\\|?*\x00-\x1f]+", "_", stem)
        return stem[:120] or "document"

    def copy_or_convert_file(file_path, rel_parent=Path("")):
        file_path = Path(file_path)
        if file_path.suffix.lower() == ".md":
            tgt = dst / rel_parent / file_path.name
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(file_path), str(tgt))
            return str(tgt)
        tgt = dst / rel_parent / (safe_stem(file_path.name) + ".md")
        if tgt.exists():
            digest = hashlib.sha1(str(file_path).encode("utf-8", errors="ignore")).hexdigest()[:8]
            tgt = dst / rel_parent / (safe_stem(file_path.name) + "-" + digest + ".md")
        tgt.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = _convert_file_to_markdown(file_path)
        except ValueError as e:
            # Default: skip the file and report. No placeholder is written so
            # the KB directory stays clean of unusable markdown.
            return {"skipped": True, "path": str(file_path), "error": str(e)}
        if allow_empty and not text.strip():
            tgt.write_text(
                "# %s\n\n无法用 MarkItDown 提取正文：empty content\n" % safe_stem(file_path.name),
                encoding="utf-8",
            )
            return str(tgt)
        if not text.strip():
            return {"skipped": True, "path": str(file_path),
                    "error": "MarkItDown returned empty content"}
        tgt.write_text(text, encoding="utf-8")
        return str(tgt)

    def _process(item, rel_parent):
        result = copy_or_convert_file(item, rel_parent)
        if isinstance(result, dict) and result.get("skipped"):
            failed.append({"path": result["path"], "error": result["error"]})
        else:
            copied.append(result)

    if src.is_file():
        _process(src, Path(""))
    elif src.is_dir():
        for item in src.rglob("*"):
            if item.is_file():
                _process(item, item.relative_to(src).parent)
    else:
        raise ValueError("不支持的源路径类型: %s" % source_path)
    return {"copied": copied, "failed": failed, "skipped": []}


def search_local_kb(kb_id, query, limit=5, config_path=CONFIG_PATH):
    """Lightweight local-KB retrieval for the UI test-search button.

    Walks the KB directory, scores each .md file by query-token overlap, and
    returns the top ``limit`` matches.  This intentionally bypasses the FTS5
    index in reply_engine so the UI can preview results without a full
    monitor setup.
    """
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持检索本地知识库: %s" % kb_id)
    resolved_base = _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)
    base = Path(str(resolved_base)) if resolved_base else Path(spec.get("path") or "")
    if not base.exists() or not base.is_dir():
        return {"hits": [], "matched_files": 0, "total_files": 0, "query": query}
    toks = re.findall(r"[\u4e00-\u9fff]{2,}|\w+", query or "")
    toks = [t for t in toks if len(t) >= 1]
    if not toks:
        return {"hits": [], "matched_files": 0, "total_files": 0, "query": query}
    scored = []
    total = 0
    for p in base.rglob("*.md"):
        if p.name == ".kb_index.sqlite":
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        total += 1
        rel = str(p.relative_to(base)).replace("\\", "/")
        score = 0
        for tok in toks:
            tl = tok.lower()
            score += body.lower().count(tl)
            score += rel.lower().count(tl) * 3
        if score:
            snippet = body.strip().splitlines()
            preview = " ".join(snippet)[:240]
            scored.append({
                "rel_path": rel,
                "score": score,
                "snippet": preview,
            })
    scored.sort(key=lambda x: (-x["score"], x["rel_path"]))
    return {
        "hits": scored[:max(1, int(limit))],
        "matched_files": len(scored),
        "total_files": total,
        "query": query,
    }


def _leann_exe(spec):
    return str(spec.get("executable") or spec.get("cli") or "leann").strip() or "leann"


def _leann_env(cfg=None, config_path=CONFIG_PATH):
    """Return an environment dict for LEANN subprocesses.

    Defaults embedding model caches to D:\\cache\\leann and sets the HF mirror
    endpoint so model downloads work reliably on Chinese networks.
    """
    if cfg is None:
        cfg = load_config(config_path)
    cache_root = Path(cfg.get("reply_engine", {}).get("leann", {}).get("cache_dir", r"D:\cache\leann"))
    env = dict(os.environ)
    env["HF_HOME"] = str(cache_root / "huggingface")
    env["SENTENCE_TRANSFORMERS_HOME"] = str(cache_root / "sentence_transformers")
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    return env


def migrate_leann_cache(src_dir, dst_dir):
    """Copy an existing HuggingFace / sentence-transformers cache tree to a new location."""
    src = Path(src_dir)
    dst = Path(dst_dir)
    if not src.exists():
        return {"copied": False, "reason": "source does not exist", "src": str(src), "dst": str(dst)}
    if dst.exists() and any(dst.iterdir()):
        return {"copied": False, "reason": "destination not empty", "src": str(src), "dst": str(dst)}
    dst.mkdir(parents=True, exist_ok=True)
    copied_files = 0
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied_files += 1
    return {"copied": True, "src": str(src), "dst": str(dst), "files": copied_files}


def search_leann_kb(spec, query, limit=5, config_path=CONFIG_PATH, cfg=None):
    """Run leann search against the configured LEANN index."""
    if cfg is None:
        cfg = load_config(config_path)
    index_name = str(spec.get("index_name") or "").strip()
    if not index_name:
        raise ValueError("LEANN 知识库缺少 index_name")
    exe = _leann_exe(spec)
    env = _leann_env(cfg=cfg)
    cmd = [
        exe,
        "search",
        index_name,
        str(query or ""),
        "--top-k",
        str(int(limit or 5)),
        "--json",
        "--non-interactive",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(spec.get("timeout") or 120)),
            env=env,
            creationflags=_NO_WINDOW_FLAGS,
        )
    except Exception as e:
        return {"hits": [], "matched_files": 0, "total_files": 0, "query": query, "error": str(e)}
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return {"hits": [], "matched_files": 0, "total_files": 0, "query": query,
                "error": "leann search failed rc=%s stderr=%s" % (proc.returncode, (proc.stderr or "")[:200])}
    try:
        payload = json.loads(proc.stdout)
    except Exception as e:
        return {"hits": [], "matched_files": 0, "total_files": 0, "query": query, "error": str(e)}
    if isinstance(payload, dict):
        results = payload.get("results") or payload.get("data") or payload.get("hits") or []
    elif isinstance(payload, list):
        results = payload
    else:
        results = []
    hits = []
    for idx, item in enumerate(results[:max(1, int(limit or 5))], start=1):
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("id") or "").strip()
            content = str(item.get("content") or item.get("text") or item.get("summary") or "").strip()
            rel = title or str(idx)
        else:
            rel = str(idx)
            content = str(item)
        hits.append({
            "rel_path": rel,
            "score": None,
            "snippet": content[:320],
        })
    return {"hits": hits, "matched_files": len(hits), "total_files": len(results), "query": query}


def search_kb(kb_id, query, limit=5, config_path=CONFIG_PATH):
    """Dispatch search by KB type."""
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    kb_type = str(spec.get("type") or "local").lower()
    if kb_type == "leann":
        return search_leann_kb(spec, query, limit=limit, config_path=config_path, cfg=cfg)
    return search_local_kb(kb_id, query, limit=limit, config_path=config_path)


def diagnose_leann_kb(spec, config_path=CONFIG_PATH, cfg=None):
    """Check that the configured LEANN index exists."""
    if cfg is None:
        cfg = load_config(config_path)
    index_name = str(spec.get("index_name") or "").strip()
    if not index_name:
        raise ValueError("LEANN 知识库缺少 index_name")
    exe = _leann_exe(spec)
    env = _leann_env(cfg=cfg)
    try:
        proc = subprocess.run(
            [exe, "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
            creationflags=_NO_WINDOW_FLAGS,
        )
    except Exception as e:
        return {"ok": False, "index_name": index_name, "error": str(e)}
    if proc.returncode != 0:
        return {"ok": False, "index_name": index_name,
                "error": "leann list failed rc=%s stderr=%s" % (proc.returncode, (proc.stderr or "")[:200])}
    output = proc.stdout or ""
    exists = index_name in output.split()
    return {"ok": exists, "index_name": index_name, "listed": exists, "error": "" if exists else "索引不存在"}


def diagnose_kb(kb_id, query='', config_path=CONFIG_PATH):
    """Dispatch diagnosis by KB type."""
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    kb_type = str(spec.get("type") or "local").lower()
    if kb_type == "leann":
        return diagnose_leann_kb(spec, config_path=config_path, cfg=cfg)
    return diagnose_local_kb(kb_id, query=query, config_path=config_path)


def _trim_leann_build_jobs(max_jobs=50):
    """Cap the number of terminal LEANN build jobs kept in memory."""
    with _LEANN_BUILD_LOCK:
        terminal = [
            (bid, job)
            for bid, job in _LEANN_BUILD_JOBS.items()
            if job.get("status") in ("done", "failed")
        ]
        if len(terminal) <= max_jobs:
            return
        terminal_sorted = sorted(terminal, key=lambda x: x[1].get("created_at", 0))
        for bid, _ in terminal_sorted[: len(terminal) - max_jobs]:
            _LEANN_BUILD_JOBS.pop(bid, None)


def _run_leann_build_async(build_id, cmd, env, log_path, timeout=120):
    def _build():
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write("[%s] START %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd)))
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=f,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=env,
                        creationflags=_NO_WINDOW_FLAGS,
                    )
                except Exception as e:
                    err = "failed to start leann build: %s" % e
                    f.write("[%s] ERROR %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), err))
                    with _LEANN_BUILD_LOCK:
                        _LEANN_BUILD_JOBS.setdefault(build_id, {}).update(
                            {"log_path": log_path, "status": "failed", "error": err}
                        )
                    _trim_leann_build_jobs()
                    return
                with _LEANN_BUILD_LOCK:
                    _LEANN_BUILD_JOBS.setdefault(build_id, {}).update(
                        {"proc": proc, "log_path": log_path, "status": "running"}
                    )
                try:
                    rc = proc.wait(timeout=float(timeout))
                    status = "done" if rc == 0 else "failed"
                    err = "" if rc == 0 else "leann build exited with code %s" % rc
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    rc = -1
                    status = "failed"
                    err = "leann build timed out after %ss" % timeout
                with _LEANN_BUILD_LOCK:
                    job = _LEANN_BUILD_JOBS.setdefault(build_id, {})
                    job.update({"status": status, "returncode": rc, "error": err, "log_path": log_path})
                    job.pop("proc", None)
                _trim_leann_build_jobs()
                f.write("[%s] END rc=%s status=%s%s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    rc,
                    status,
                    (" error=%s" % err) if err else "",
                ))
        except Exception as e:
            # Last-resort catch for unexpected worker errors
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                    f.write("[%s] WORKER ERROR %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
            except Exception:
                pass
            with _LEANN_BUILD_LOCK:
                _LEANN_BUILD_JOBS.setdefault(build_id, {}).update(
                    {"log_path": log_path, "status": "failed", "error": str(e)}
                )
            _trim_leann_build_jobs()
    t = threading.Thread(target=_build, daemon=True)
    t.start()


def build_leann_kb(kb_id, docs=None, force=False, config_path=CONFIG_PATH):
    """Start an asynchronous LEANN build for the configured index.

    Returns a dict with build_id, log_path, index_name, status='started'.
    The build runs in a background thread and writes stdout/stderr to log_path.
    """
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("knowledge base not found: %s" % kb_id)
    if str(spec.get("type") or "").lower() != "leann":
        raise ValueError("knowledge base is not leann type: %s" % kb_id)
    index_name = str(spec.get("index_name") or "").strip()
    if not index_name:
        raise ValueError("LEANN 知识库缺少 index_name")
    exe = _leann_exe(spec)
    env = _leann_env(cfg=cfg, config_path=config_path)
    doc_paths = list(docs) if docs else ["."]
    if not doc_paths:
        doc_paths = ["."]
    cmd = [exe, "build", index_name, "--docs"] + doc_paths
    if force:
        cmd.append("--force")
    timeout = float((cfg.get("reply_engine") or {}).get("leann", {}).get("timeout", 120))
    log_dir = Path(config_path).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    build_id = "leann_build_%s_%s" % (kb_id, uuid.uuid4().hex[:12])
    log_path = log_dir / ("%s.log" % build_id)
    log_path_str = str(log_path)
    with _LEANN_BUILD_LOCK:
        _LEANN_BUILD_JOBS[build_id] = {
            "log_path": log_path_str,
            "status": "started",
            "created_at": time.time(),
        }
    _run_leann_build_async(build_id, cmd, env, log_path_str, timeout=timeout)
    return {
        "build_id": build_id,
        "log_path": log_path_str,
        "index_name": index_name,
        "status": "started",
    }


def get_leann_build_status(build_id):
    with _LEANN_BUILD_LOCK:
        job = _LEANN_BUILD_JOBS.get(build_id)
    if not job:
        return {"build_id": build_id, "status": "unknown"}
    return {
        "build_id": build_id,
        "status": job.get("status", "unknown"),
        "returncode": job.get("returncode"),
        "log_path": job.get("log_path"),
        "error": job.get("error", ""),
    }


def _convert_file_to_markdown(path):
    path = Path(path)
    notes = []
    text = ""
    try:
        from markitdown import MarkItDown  # type: ignore
        result = MarkItDown().convert(str(path))
        text = getattr(result, "text_content", "") or ""
        if text.strip():
            return text
        notes.append("python_markitdown_returned_empty")
    except Exception as e:
        notes.append("python_markitdown_error: %s: %s" % (type(e).__name__, e or "no_message"))
    markitdown_cmd = _find_markitdown_exe()
    try:
        proc = subprocess.run([markitdown_cmd, str(path)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              creationflags=_NO_WINDOW_FLAGS)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return proc.stdout
        stderr_tail = ((proc.stderr or "").strip().splitlines() or [""])[-1][:400]
        stdout_head = (proc.stdout or "")[:400]
        notes.append("cli_exit=%s stderr_tail=%r stdout_head=%r" % (
            proc.returncode, stderr_tail, stdout_head,
        ))
    except Exception as e:
        notes.append("cli_markitdown_error: %s: %s" % (type(e).__name__, e or "no_message"))
    try:
        size = path.stat().st_size
        notes.append("file_size=%d" % size)
    except Exception:
        pass
    raise ValueError(
        "非 Markdown 文件需要 MarkItDown 转换，但转换失败: %s [%s]" % (path, " | ".join(notes))
    )


def _find_markitdown_exe():
    env = os.environ.get("MARKITDOWN_EXE")
    if env and Path(env).exists():
        return env
    candidates = [
        Path(r"D:\programs\wechat-kb-tools\Scripts\markitdown.exe"),
        ROOT / ".venv" / "Scripts" / "markitdown.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return "markitdown"


def open_kb_dir(kb_id, config_path=CONFIG_PATH):
    """Open local knowledge base directory in file manager."""
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持打开本地知识库目录: %s" % kb_id)
    resolved = _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)
    p = Path(str(resolved)) if resolved else Path(spec.get("path") or "")
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    import platform
    import subprocess
    system = platform.system()
    if system == "Windows":
        subprocess.Popen(["explorer", str(p)], creationflags=_NO_WINDOW_FLAGS)
    elif system == "Darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return str(p)

def open_kb_obsidian(kb_id, config_path=CONFIG_PATH):
    """Open a local knowledge base directory as an Obsidian vault."""
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持用 Obsidian 打开本地知识库: %s" % kb_id)
    resolved = _resolve_kb_path(spec.get("path"), config_path=config_path, cfg=cfg)
    p = Path(str(resolved)) if resolved else Path(spec.get("path") or "")
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    exe = _find_obsidian_exe()
    subprocess.Popen([exe, str(p)], creationflags=_NO_WINDOW_FLAGS)
    return {"path": str(p), "executable": exe}


def _find_obsidian_exe():
    env = os.environ.get("OBSIDIAN_EXE")
    if env and Path(env).exists():
        return env
    candidates = [
        Path(r"D:\programs\Obsidian\Obsidian.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Obsidian" / "Obsidian.exe",
        Path(r"C:\Program Files\Obsidian\Obsidian.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return "Obsidian.exe"


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
