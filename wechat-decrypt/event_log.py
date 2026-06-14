#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local structured event log for the WeChat bot.

Stores events in a small SQLite database under `temp/event_log.sqlite`.
This is the foundation for both the basic dashboard stats and any future
async topic analysis.  It is intentionally agent-agnostic: the bot writes
events, the UI / async jobs read them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "temp" / "event_log.sqlite"

_WRITE_LOCK = threading.Lock()


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def open_db(path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(path or DEFAULT_DB_PATH)
    _ensure_parent(p)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            target TEXT,
            sender TEXT,
            payload TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS events_kind ON events(kind)")
    con.execute("CREATE INDEX IF NOT EXISTS events_target ON events(target)")
    return con


@contextmanager
def _connect(path: Optional[Path] = None):
    con = open_db(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def log_event(kind: str, target: Optional[str] = None, sender: Optional[str] = None,
              payload: Optional[Dict[str, Any]] = None,
              db_path: Optional[Path] = None) -> int:
    """Append a single event. Returns the new row id.

    Safe to call from the bot's hot path: each call opens a short-lived
    connection under a process-wide lock so concurrent writers do not
    corrupt the SQLite WAL.
    """
    if not kind:
        raise ValueError("event kind is required")
    data = {
        "ts": time.time(),
        "kind": str(kind),
        "target": str(target) if target else None,
        "sender": str(sender) if sender else None,
        "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
    }
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            "INSERT INTO events (ts, kind, target, sender, payload) VALUES (?, ?, ?, ?, ?)",
            (data["ts"], data["kind"], data["target"], data["sender"], data["payload"]),
        )
        return int(cur.lastrowid or 0)


def get_recent(limit: int = 50, kind: Optional[str] = None, target: Optional[str] = None,
               db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    sql = "SELECT id, ts, kind, target, sender, payload FROM events"
    where = []
    params: List[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if target:
        where.append("target = ?")
        params.append(target)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with _connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        out.append({
            "id": int(r["id"]),
            "ts": float(r["ts"]),
            "kind": r["kind"],
            "target": r["target"],
            "sender": r["sender"],
            "payload": payload,
        })
    return out


def get_stats(since: Optional[float] = None, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Return aggregate counts grouped by kind / reason / target.

    `since` is a unix timestamp; if None, the whole log is used.
    """
    where = ""
    params: List[Any] = []
    if since is not None:
        where = " WHERE ts >= ?"
        params.append(float(since))

    with _connect(db_path) as con:
        by_kind_rows = con.execute(
            "SELECT kind, COUNT(*) AS c FROM events" + where + " GROUP BY kind",
            params,
        ).fetchall()
        by_target_rows = con.execute(
            "SELECT target, COUNT(*) AS c FROM events" + where + " GROUP BY target",
            params,
        ).fetchall()
        reason_rows = con.execute(
            "SELECT json_extract(payload, '$.reason') AS r, COUNT(*) AS c FROM events"
            + where + " GROUP BY r",
            params,
        ).fetchall()
        total_row = con.execute("SELECT COUNT(*) AS c FROM events" + where, params).fetchone()
        first_row = con.execute("SELECT MIN(ts) AS t FROM events" + where, params).fetchone()
        last_row = con.execute("SELECT MAX(ts) AS t FROM events" + where, params).fetchone()

    by_kind = {r["kind"]: int(r["c"]) for r in by_kind_rows if r["kind"]}
    by_target = {r["target"] or "(none)": int(r["c"]) for r in by_target_rows}
    by_reason = {r["r"] or "(none)": int(r["c"]) for r in reason_rows}
    return {
        "total": int(total_row["c"] or 0),
        "by_kind": by_kind,
        "by_target": by_target,
        "by_reason": by_reason,
        "first_ts": float(first_row["t"]) if first_row and first_row["t"] else None,
        "last_ts": float(last_row["t"]) if last_row and last_row["t"] else None,
    }


def iter_events(since: Optional[float] = None, kinds: Optional[Iterable[str]] = None,
                target: Optional[str] = None,
                db_path: Optional[Path] = None) -> Iterable[Dict[str, Any]]:
    """Generator variant for batch consumers (e.g. async topic jobs)."""
    where = []
    params: List[Any] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(float(since))
    if kinds:
        placeholder = ",".join("?" for _ in kinds)
        where.append("kind IN (" + placeholder + ")")
        params.extend(list(kinds))
    if target:
        where.append("target = ?")
        params.append(target)
    sql = "SELECT id, ts, kind, target, sender, payload FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC"
    with _connect(db_path) as con:
        cur = con.execute(sql, params)
        while True:
            r = cur.fetchone()
            if not r:
                return
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {}
            yield {
                "id": int(r["id"]),
                "ts": float(r["ts"]),
                "kind": r["kind"],
                "target": r["target"],
                "sender": r["sender"],
                "payload": payload,
            }


def stats_since_ts(ts: float, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return get_stats(since=ts, db_path=db_path)


__all__ = [
    "DEFAULT_DB_PATH",
    "log_event",
    "get_recent",
    "get_stats",
    "iter_events",
    "stats_since_ts",
]
