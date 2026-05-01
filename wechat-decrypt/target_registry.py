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
import shutil
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
    data = load_candidates(candidates_path)

    existing = find_target(cfg, key)
    if existing:
        existing["enabled"] = True
        if knowledge_bases:
            cur = list(existing.get("knowledge_bases") or [])
            for kb in knowledge_bases:
                if kb not in cur:
                    cur.append(kb)
            existing["knowledge_bases"] = cur
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
        if knowledge_bases:
            cur = list(existing.get("knowledge_bases") or [])
            for kb in knowledge_bases:
                if kb not in cur:
                    cur.append(kb)
            existing["knowledge_bases"] = cur
        cand["status"] = "enabled"
        cand["updated_at"] = now_text()
        data["updated_at"] = now_text()
        save_json_atomic(config_path, cfg)
        save_json_atomic(candidates_path, data)
        return existing
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


def list_knowledge_bases(config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    rows = []
    for kb_id, spec in sorted((cfg.get("knowledge_bases") or {}).items()):
        spec = spec or {}
        rows.append({
            "id": kb_id,
            "type": spec.get("type") or "local",
            "enabled": spec.get("enabled", True),
            "knowledge_base_id": spec.get("knowledge_base_id") or "",
            "path": spec.get("path") or spec.get("dir") or "",
            "description": spec.get("description") or "",
        })
    return rows


def add_knowledge_base(kb_id, kb_type="getnote", knowledge_base_id=None, path=None, description="",
                       executable=None, scope="scene", limit=None, timeout=None,
                       enabled=True, replace=False, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    cfg.setdefault("knowledge_bases", {})
    if kb_id in cfg["knowledge_bases"] and not replace:
        raise ValueError("knowledge base already exists: %s (use --replace to update)" % kb_id)
    spec = {"type": kb_type, "enabled": bool(enabled)}
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
    else:
        raise ValueError("unsupported knowledge base type: %s" % kb_type)
    if description:
        spec["description"] = description
    cfg["knowledge_bases"][kb_id] = spec
    save_json_atomic(config_path, cfg)
    out = dict(spec)
    out["id"] = kb_id
    return out


def _unknown_kb_message(kb, cfg):
    for known_id, spec in (cfg.get("knowledge_bases") or {}).items():
        if str((spec or {}).get("knowledge_base_id") or "") == str(kb):
            return "unknown knowledge base id: %s. Did you mean configured alias '%s'? Use: python manage_targets.py kb <群名> %s" % (kb, known_id, known_id)
    return "unknown knowledge base id: %s. Run 'python manage_targets.py kb-list' or create one with 'python manage_targets.py kb-add <别名> --kid <外部知识库ID>'" % kb


def validate_knowledge_bases(knowledge_bases, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    known = cfg.get("knowledge_bases") or {}
    for kb in knowledge_bases or []:
        if str(kb).startswith("legacy:"):
            continue
        if kb not in known:
            raise ValueError(_unknown_kb_message(kb, cfg))
    return True


def bind_wiki(key, knowledge_bases, replace=False, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    t = find_target(cfg, key)
    if not t:
        raise ValueError("target not found: %s" % key)
    validate_knowledge_bases(knowledge_bases, config_path=config_path)
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


def _wiki_root():
    return ROOT / "wiki"


def create_local_kb_dir(kb_id, description="", replace=False, config_path=CONFIG_PATH):
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
        "path": str(base_dir.resolve()),
        "description": description or "本地知识库: %s" % kb_id,
        "enabled": True,
    }
    kbs[kb_id] = spec
    save_json_atomic(config_path, cfg)
    return spec


def get_kb_info(kb_id, config_path=CONFIG_PATH):
    """Return knowledge base info including file stats for local dirs."""
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
    return info


def import_kb_file(kb_id, source_path, config_path=CONFIG_PATH):
    """Copy a file or directory into a local knowledge base."""
    cfg = load_config(config_path)
    spec = (cfg.get("knowledge_bases") or {}).get(kb_id)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持导入本地知识库: %s" % kb_id)
    dst = Path(spec.get("path") or "")
    if not dst.exists():
        dst.mkdir(parents=True, exist_ok=True)
    src = Path(source_path)
    if not src.exists():
        raise ValueError("源路径不存在: %s" % source_path)
    copied = []
    if src.is_file():
        if src.suffix.lower() != ".md":
            raise ValueError("本地知识库暂时只支持 markdown 格式内容（.md）: %s" % source_path)
        tgt = dst / src.name
        shutil.copy2(str(src), str(tgt))
        copied.append(str(tgt))
    elif src.is_dir():
        skipped = 0
        for item in src.rglob("*"):
            if item.is_file():
                if item.suffix.lower() != ".md":
                    skipped += 1
                    continue
                rel = item.relative_to(src)
                tgt = dst / rel
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(tgt))
                copied.append(str(tgt))
        if not copied and skipped:
            raise ValueError("本地知识库暂时只支持 markdown 格式内容（.md）；目录中没有可导入的 .md 文件")
    else:
        raise ValueError("不支持的源路径类型: %s" % source_path)
    return copied


def open_kb_dir(kb_id, config_path=CONFIG_PATH):
    """Open local knowledge base directory in file manager."""
    cfg = load_config(config_path)
    spec = (cfg.get("knowledge_bases") or {}).get(kb_id)
    if not spec:
        raise ValueError("知识库不存在: %s" % kb_id)
    if spec.get("type") != "local":
        raise ValueError("仅支持打开本地知识库目录: %s" % kb_id)
    p = Path(spec.get("path") or "")
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    import platform
    import subprocess
    system = platform.system()
    if system == "Windows":
        subprocess.Popen(["explorer", str(p)])
    elif system == "Darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return str(p)


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

