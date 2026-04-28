#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lightweight target DB refresher for wechat_bot_monitor.

- Reads wechat_bot_targets.json.
- Refreshes only target message_N.db files.
- Detects raw .db/.db-wal/.db-shm size/mtime changes.
- Does NOT run key scanning.
- Decrypts to a temporary file, verifies SQLite, then atomically replaces output.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS_CONFIG = ROOT / "wechat_bot_targets.json"
STATE_FILE = ROOT / "fast_refresh_state.json"
TMP_DIR = ROOT / ".fast_refresh_tmp"
LOG = ROOT / "fast_refresh_targets.log"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import load_config as load_path_config
from decrypt_db import decrypt_database
from key_utils import get_key_info, strip_key_metadata


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path, default):
    if not Path(path).exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize_target_db(db_name):
    rel = str(db_name or "message_0.db").replace("\\", "/").lstrip("/")
    if "/" not in rel:
        rel = "message/" + rel
    norm = os.path.normpath(rel)
    if os.path.isabs(norm) or norm.startswith(".."):
        raise ValueError("unsafe db path: %r" % db_name)
    if not norm.endswith(".db") or norm.endswith("-wal") or norm.endswith("-shm"):
        raise ValueError("not a normal .db target: %r" % db_name)
    return norm


def target_dbs_from_config(path=TARGETS_CONFIG):
    cfg = load_json(path, {})
    out = []
    seen = set()
    for t in cfg.get("targets", []):
        if not t.get("enabled", True):
            continue
        rel = normalize_target_db(t.get("db", "message_0.db"))
        key = rel.replace("\\", "/").lower()
        if key not in seen:
            seen.add(key)
            out.append(rel)
    return out


def file_sig(path):
    path = Path(path)
    if not path.exists():
        return None
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def raw_fingerprint(db_dir, rel):
    raw = Path(db_dir) / rel
    return {
        "db": file_sig(raw),
        "wal": file_sig(str(raw) + "-wal"),
        "shm": file_sig(str(raw) + "-shm"),
    }


def verify_sqlite(path):
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA schema_version").fetchone()
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        con.close()


def refresh_one(rel, path_cfg, keys, state, force=False):
    db_dir = Path(path_cfg["db_dir"])
    out_dir = Path(path_cfg["decrypted_dir"])
    raw_path = db_dir / rel
    if not raw_path.exists():
        log("skip missing raw db rel=%s path=%s" % (rel, raw_path))
        return {"rel": rel, "status": "missing"}

    fp = raw_fingerprint(db_dir, rel)
    state_key = rel.replace("\\", "/")
    old_fp = state.get(state_key, {}).get("fingerprint")
    if not force and old_fp == fp:
        return {"rel": rel, "status": "unchanged"}

    key_info = get_key_info(keys, rel)
    if not key_info:
        log("fail no key rel=%s" % rel)
        return {"rel": rel, "status": "no_key"}

    enc_key = bytes.fromhex(key_info["enc_key"])
    final_path = out_dir / rel
    tmp_path = TMP_DIR / rel
    if tmp_path.exists():
        tmp_path.unlink()
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    ok = decrypt_database(str(raw_path), str(tmp_path), enc_key)
    dt = time.time() - t0
    if not ok:
        log("fail decrypt rel=%s dt=%.3fs" % (rel, dt))
        return {"rel": rel, "status": "decrypt_failed", "dt": dt}

    try:
        verify_sqlite(tmp_path)
    except Exception as e:
        log("fail sqlite verify rel=%s err=%r" % (rel, e))
        return {"rel": rel, "status": "verify_failed", "error": repr(e), "dt": dt}

    final_path.parent.mkdir(parents=True, exist_ok=True)
    replace_tmp = final_path.with_suffix(final_path.suffix + ".replace_tmp")
    if replace_tmp.exists():
        replace_tmp.unlink()
    shutil.move(str(tmp_path), str(replace_tmp))
    replace_tmp.replace(final_path)

    state[state_key] = {
        "fingerprint": fp,
        "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dt": dt,
        "size": final_path.stat().st_size,
    }
    log("refreshed rel=%s dt=%.3fs out=%s" % (rel, dt, final_path))
    return {"rel": rel, "status": "refreshed", "dt": dt}


def refresh_targets(targets_config=TARGETS_CONFIG, force=False):
    path_cfg = load_path_config()
    keys_file = Path(path_cfg["keys_file"])
    if not keys_file.exists():
        raise FileNotFoundError("keys file missing: %s" % keys_file)
    with keys_file.open(encoding="utf-8") as f:
        keys = strip_key_metadata(json.load(f))

    rels = target_dbs_from_config(targets_config)
    state = load_json(STATE_FILE, {})
    results = []
    for rel in rels:
        results.append(refresh_one(rel, path_cfg, keys, state, force=force))
    save_json_atomic(STATE_FILE, state)
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refresh only WeChat bot target DBs.")
    ap.add_argument("--config", default=str(TARGETS_CONFIG), help="wechat_bot_targets.json path")
    ap.add_argument("--force", action="store_true", help="refresh even fingerprint is unchanged")
    ap.add_argument("--json", action="store_true", help="print compact JSON result")
    args = ap.parse_args(argv)

    t0 = time.time()
    results = refresh_targets(Path(args.config), force=args.force)
    dt = time.time() - t0
    changed = sum(1 for r in results if r.get("status") == "refreshed")
    failed = [r for r in results if r.get("status") not in ("unchanged", "refreshed")]
    summary = {"total": len(results), "changed": changed, "failed": len(failed), "dt": dt, "results": results}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    else:
        log("summary total=%d changed=%d failed=%d dt=%.3fs" % (len(results), changed, len(failed), dt))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())