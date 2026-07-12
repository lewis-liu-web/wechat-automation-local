#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable transport primitives for the staged WeChat reply pipeline.

This module deliberately owns no knowledge retrieval, reply-decision policy, or
WeChat UI automation.  It stores immutable inbound events, debounced turns,
leased worker jobs, and leased outbound sends in one SQLite database so every
state transition survives a monitor or worker restart.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "temp" / "reliable_pipeline.sqlite"
SCHEMA_VERSION = 1
AGENT_RESULT_VERSION = 1

INBOUND_PENDING = "pending"
INBOUND_TURNED = "turned"
TURN_OPEN = "open"
TURN_READY = "ready"
TURN_JOB_CREATED = "job_created"
TURN_DONE = "done"
TURN_ESCALATED = "escalated"
TURN_FAILED = "failed"
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_TIMEOUT = "timeout"
JOB_ESCALATED = "escalated"
OUTBOX_PENDING = "pending"
OUTBOX_SENDING = "sending"
OUTBOX_SENT = "sent"
OUTBOX_RETRY = "retry"
OUTBOX_DEAD = "dead_letter"

_WRITE_LOCK = threading.RLock()


class AgentResultContractError(ValueError):
    """Raised when a worker result does not satisfy the wire contract."""


@dataclass(frozen=True)
class AgentResultContract:
    action: str
    reply_text: str = ""
    reason_code: str = ""
    risk_level: str = "low"
    schema_version: int = AGENT_RESULT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action": self.action,
            "reply_text": self.reply_text,
            "reason_code": self.reason_code,
            "risk_level": self.risk_level,
        }


def parse_agent_result(value: Any) -> AgentResultContract:
    """Validate a complete AgentResult document; no display-text fallback exists."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentResultContractError("AgentResult must be one JSON object") from exc
    if not isinstance(value, dict):
        raise AgentResultContractError("AgentResult must be an object")
    required = {"schema_version", "action", "reply_text", "reason_code", "risk_level"}
    if set(value) != required:
        raise AgentResultContractError("AgentResult must contain exactly the versioned contract fields")
    if value.get("schema_version") != AGENT_RESULT_VERSION:
        raise AgentResultContractError("unsupported AgentResult schema_version")
    action = value.get("action")
    if action not in {"reply", "silent", "escalate"}:
        raise AgentResultContractError("AgentResult action must be reply, silent, or escalate")
    reply_text = value.get("reply_text")
    reason_code = value.get("reason_code")
    risk_level = value.get("risk_level")
    if not isinstance(reply_text, str):
        raise AgentResultContractError("AgentResult reply_text must be a string")
    if not isinstance(reason_code, str):
        raise AgentResultContractError("AgentResult reason_code must be a string")
    if risk_level not in {"low", "medium", "high"}:
        raise AgentResultContractError("AgentResult risk_level must be low, medium, or high")
    if action == "reply" and not reply_text.strip():
        raise AgentResultContractError("reply action requires reply_text")
    if action != "reply" and reply_text:
        raise AgentResultContractError("silent and escalate must not include reply_text")
    if action in {"silent", "escalate"} and not reason_code.strip():
        raise AgentResultContractError("silent and escalate require reason_code")
    return AgentResultContract(action=action, reply_text=reply_text.strip(),
                               reason_code=reason_code.strip(), risk_level=risk_level)


def _now() -> float:
    return time.time()


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def open_db(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path or DEFAULT_DB_PATH)
    _ensure_parent(db_path)
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS pipeline_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbound_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_event_id TEXT NOT NULL UNIQUE,
            target_id TEXT NOT NULL,
            group_key TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            local_id INTEGER NOT NULL,
            received_at REAL NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            turn_id INTEGER,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS inbound_events_pending
            ON inbound_events(status, target_id, local_id);
        CREATE TABLE IF NOT EXISTS turn_windows (
            window_key TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            group_key TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            first_event_id INTEGER NOT NULL,
            last_event_id INTEGER NOT NULL,
            opened_at REAL NOT NULL,
            last_event_at REAL NOT NULL,
            due_at REAL NOT NULL,
            hard_deadline_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        );
        CREATE INDEX IF NOT EXISTS turn_windows_due ON turn_windows(status, due_at);
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_key TEXT NOT NULL UNIQUE,
            target_id TEXT NOT NULL,
            group_key TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            start_event_id INTEGER NOT NULL,
            end_event_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            created_at REAL NOT NULL,
            closed_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS turns_status ON turns(status, group_key, id);
        CREATE TABLE IF NOT EXISTS turn_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_key TEXT NOT NULL UNIQUE,
            turn_id INTEGER NOT NULL UNIQUE,
            target_id TEXT NOT NULL,
            group_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            lease_owner TEXT,
            lease_until REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            deadline_at REAL,
            result_json TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            FOREIGN KEY(turn_id) REFERENCES turns(id)
        );
        CREATE INDEX IF NOT EXISTS turn_jobs_claim ON turn_jobs(status, lease_until, created_at);
        CREATE INDEX IF NOT EXISTS turn_jobs_group ON turn_jobs(group_key, status);
        CREATE TABLE IF NOT EXISTS send_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outbox_key TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL UNIQUE,
            target_id TEXT NOT NULL,
            group_key TEXT NOT NULL,
            before_local_id INTEGER NOT NULL,
            mention_name TEXT NOT NULL DEFAULT '',
            reply_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            lease_owner TEXT,
            lease_until REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            next_attempt_at REAL NOT NULL,
            error TEXT,
            result_json TEXT,
            created_at REAL NOT NULL,
            sent_at REAL,
            dead_at REAL,
            FOREIGN KEY(job_id) REFERENCES turn_jobs(id)
        );
        CREATE INDEX IF NOT EXISTS send_outbox_claim ON send_outbox(status, next_attempt_at, lease_until);
        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE,
            target_id TEXT NOT NULL,
            group_key TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(job_id) REFERENCES turn_jobs(id)
        );
        """
    )
    con.execute("INSERT OR IGNORE INTO pipeline_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)))
    con.commit()
    return con


@contextmanager
def _connect(path: Optional[Path] = None):
    con = open_db(path)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    item = dict(row)
    for key in ("payload_json", "result_json"):
        if key in item:
            item[key[:-5]] = _load(item.pop(key), {})
    return item


def source_event_id(target: Dict[str, Any], message: Dict[str, Any]) -> str:
    return "wx:%s:%s:%s" % (
        target.get("db") or "",
        target.get("table") or "",
        int(message.get("local_id") or 0),
    )


def persist_inbound_event(*, event_id: str, target_id: str, group_key: str, sender_id: str,
                          local_id: int, payload: Dict[str, Any], received_at: Optional[float] = None,
                          db_path: Optional[Path] = None) -> Tuple[Dict[str, Any], bool]:
    """Insert one immutable event. Returns `(event, inserted)` for idempotent reads."""
    if not event_id or not target_id or not group_key or not sender_id:
        raise ValueError("event_id, target_id, group_key, and sender_id are required")
    now = float(received_at if received_at is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO inbound_events
               (source_event_id,target_id,group_key,sender_id,local_id,received_at,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (event_id, target_id, group_key, sender_id, int(local_id), now, _dump(payload), now),
        )
        row = con.execute("SELECT * FROM inbound_events WHERE source_event_id=?", (event_id,)).fetchone()
    item = _row(row)
    if item is None:
        raise RuntimeError("persisted inbound event could not be read")
    return item, bool(cur.rowcount)


def add_event_to_window(event_id: str, *, debounce_seconds: float = 5.0,
                        max_window_seconds: float = 12.0,
                        db_path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Durably append an event to its sender window; close an old window first."""
    ts = float(now if now is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        event = con.execute("SELECT * FROM inbound_events WHERE source_event_id=?", (event_id,)).fetchone()
        if not event:
            raise ValueError("unknown inbound event")
        key = "%s::%s" % (event["target_id"], event["sender_id"])
        current = con.execute("SELECT * FROM turn_windows WHERE window_key=?", (key,)).fetchone()
        closed_turn = None
        if current and (ts >= current["due_at"] or ts >= current["hard_deadline_at"]):
            closed_turn = _close_window_tx(con, current, ts)
            current = None
        if current:
            con.execute(
                """UPDATE turn_windows SET last_event_id=?, last_event_at=?,
                   due_at=? WHERE window_key=?""",
                (event["id"], ts, ts + max(0.0, debounce_seconds), key),
            )
        else:
            con.execute(
                """INSERT INTO turn_windows
                   (window_key,target_id,group_key,sender_id,first_event_id,last_event_id,opened_at,last_event_at,due_at,hard_deadline_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (key, event["target_id"], event["group_key"], event["sender_id"], event["id"], event["id"],
                 ts, ts, ts + max(0.0, debounce_seconds), ts + max(0.0, max_window_seconds)),
            )
        window = con.execute("SELECT * FROM turn_windows WHERE window_key=?", (key,)).fetchone()
    return {"window": _row(window), "closed_turn": closed_turn}


def _close_window_tx(con: sqlite3.Connection, window: sqlite3.Row, closed_at: float) -> Dict[str, Any]:
    rows = con.execute(
        """SELECT * FROM inbound_events WHERE target_id=? AND sender_id=?
           AND id BETWEEN ? AND ? AND status=? ORDER BY id""",
        (window["target_id"], window["sender_id"], window["first_event_id"], window["last_event_id"], INBOUND_PENDING),
    ).fetchall()
    if not rows:
        con.execute("DELETE FROM turn_windows WHERE window_key=?", (window["window_key"],))
        return {}
    payloads = [_row(r) for r in rows]
    turn_key = "%s:%s:%s" % (window["target_id"], rows[0]["id"], rows[-1]["id"])
    turn_payload = {
        "schema_version": SCHEMA_VERSION,
        "events": [{
            "source_event_id": p["source_event_id"], "local_id": p["local_id"],
            "received_at": p["received_at"], "message": p["payload"],
        } for p in payloads if p],
    }
    con.execute(
        """INSERT OR IGNORE INTO turns
           (turn_key,target_id,group_key,sender_id,start_event_id,end_event_id,payload_json,status,created_at,closed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (turn_key, window["target_id"], window["group_key"], window["sender_id"], rows[0]["id"], rows[-1]["id"],
         _dump(turn_payload), TURN_READY, closed_at, closed_at),
    )
    turn = con.execute("SELECT * FROM turns WHERE turn_key=?", (turn_key,)).fetchone()
    con.execute("UPDATE inbound_events SET status=?, turn_id=? WHERE id BETWEEN ? AND ?",
                (INBOUND_TURNED, turn["id"], rows[0]["id"], rows[-1]["id"]))
    con.execute("DELETE FROM turn_windows WHERE window_key=?", (window["window_key"],))
    return _row(turn) or {}


def close_due_windows(*, now: Optional[float] = None, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    ts = float(now if now is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        windows = con.execute(
            "SELECT * FROM turn_windows WHERE due_at<=? OR hard_deadline_at<=? ORDER BY due_at", (ts, ts)
        ).fetchall()
        turns = [_close_window_tx(con, window, ts) for window in windows]
    return [turn for turn in turns if turn]


def create_jobs_for_ready_turns(*, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Materialize each ready turn into exactly one worker job, idempotently."""
    with _WRITE_LOCK, _connect(db_path) as con:
        rows = con.execute("SELECT * FROM turns WHERE status=? ORDER BY id", (TURN_READY,)).fetchall()
        jobs: List[Dict[str, Any]] = []
        for turn in rows:
            job_key = "turn:%s" % turn["turn_key"]
            now = _now()
            con.execute(
                """INSERT OR IGNORE INTO turn_jobs
                   (job_key,turn_id,target_id,group_key,payload_json,status,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (job_key, turn["id"], turn["target_id"], turn["group_key"], turn["payload_json"], JOB_QUEUED, now),
            )
            job = con.execute("SELECT * FROM turn_jobs WHERE job_key=?", (job_key,)).fetchone()
            con.execute("UPDATE turns SET status=? WHERE id=? AND status=?", (TURN_JOB_CREATED, turn["id"], TURN_READY))
            item = _row(job)
            if item:
                jobs.append(item)
    return jobs


def reclaim_expired_leases(*, now: Optional[float] = None, db_path: Optional[Path] = None) -> Dict[str, int]:
    ts = float(now if now is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        jobs = con.execute(
            "UPDATE turn_jobs SET status=?, lease_owner=NULL, lease_until=NULL WHERE status=? AND lease_until<?",
            (JOB_QUEUED, JOB_RUNNING, ts),
        ).rowcount
        outbox = con.execute(
            """UPDATE send_outbox SET status=?, lease_owner=NULL, lease_until=NULL,
               next_attempt_at=MIN(next_attempt_at, ?) WHERE status=? AND lease_until<?""",
            (OUTBOX_RETRY, ts, OUTBOX_SENDING, ts),
        ).rowcount
    return {"jobs": int(jobs or 0), "outbox": int(outbox or 0)}


def claim_next_job(*, owner: str, lease_seconds: float = 120.0,
                   deadline_seconds: float = 300.0, db_path: Optional[Path] = None,
                   now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if not owner:
        raise ValueError("owner is required")
    ts = float(now if now is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        con.execute("UPDATE turn_jobs SET status=?, lease_owner=NULL, lease_until=NULL WHERE status=? AND lease_until<?",
                    (JOB_QUEUED, JOB_RUNNING, ts))
        rows = con.execute("SELECT * FROM turn_jobs WHERE status=? ORDER BY created_at,id LIMIT 100", (JOB_QUEUED,)).fetchall()
        selected = None
        for candidate in rows:
            active = con.execute("SELECT 1 FROM turn_jobs WHERE group_key=? AND status=? LIMIT 1",
                                 (candidate["group_key"], JOB_RUNNING)).fetchone()
            if not active:
                selected = candidate
                break
        if not selected:
            return None
        cur = con.execute(
            """UPDATE turn_jobs SET status=?, lease_owner=?, lease_until=?, attempts=attempts+1,
               started_at=COALESCE(started_at,?), deadline_at=COALESCE(deadline_at,?)
               WHERE id=? AND status=?""",
            (JOB_RUNNING, owner, ts + max(1.0, lease_seconds), ts, ts + max(1.0, deadline_seconds),
             selected["id"], JOB_QUEUED),
        )
        if cur.rowcount != 1:
            return None
        row = con.execute("SELECT * FROM turn_jobs WHERE id=?", (selected["id"],)).fetchone()
    return _row(row)


def _job_turn_end_local_id(con: sqlite3.Connection, job_id: int) -> int:
    row = con.execute(
        """SELECT e.local_id FROM turn_jobs j JOIN turns t ON t.id=j.turn_id
           JOIN inbound_events e ON e.id=t.end_event_id WHERE j.id=?""", (job_id,)
    ).fetchone()
    return int(row["local_id"] or 0) if row else 0


def apply_agent_result(*, job_id: int, result: AgentResultContract | Dict[str, Any] | str,
                       final_filter, mention_name: str = "", max_send_attempts: int = 5,
                       db_path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Persist a validated worker decision and materialize reply/escalation effects.

    `final_filter` is the one caller-owned deterministic safety filter. It must
    return a string; an empty filtered reply fails the job and creates no outbox.
    """
    contract = result if isinstance(result, AgentResultContract) else parse_agent_result(result)
    if not callable(final_filter):
        raise ValueError("final_filter must be callable")
    ts = float(now if now is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        job = con.execute("SELECT * FROM turn_jobs WHERE id=?", (int(job_id),)).fetchone()
        if not job:
            raise ValueError("unknown turn job")
        if job["status"] not in {JOB_RUNNING, JOB_QUEUED}:
            return {"job": _row(job), "applied": False}
        result_json = _dump(contract.to_dict())
        if contract.action == "silent":
            con.execute("UPDATE turn_jobs SET status=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                        (JOB_DONE, result_json, ts, job_id))
            con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_DONE, job["turn_id"]))
        elif contract.action == "escalate":
            con.execute("UPDATE turn_jobs SET status=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                        (JOB_ESCALATED, result_json, ts, job_id))
            con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_ESCALATED, job["turn_id"]))
            con.execute("INSERT OR IGNORE INTO escalations(job_id,target_id,group_key,reason_code,risk_level,created_at) VALUES (?,?,?,?,?,?)",
                        (job_id, job["target_id"], job["group_key"], contract.reason_code, contract.risk_level, ts))
        else:
            filtered = str(final_filter(contract.reply_text) or "").strip()
            if not filtered:
                con.execute("UPDATE turn_jobs SET status=?, error=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                            (JOB_FAILED, "reply rejected by final safety filter", result_json, ts, job_id))
                con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_FAILED, job["turn_id"]))
            else:
                con.execute("UPDATE turn_jobs SET status=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                            (JOB_DONE, result_json, ts, job_id))
                con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_DONE, job["turn_id"]))
                before_local_id = _job_turn_end_local_id(con, job_id)
                outbox_key = "job:%d" % job_id
                con.execute(
                    """INSERT OR IGNORE INTO send_outbox
                       (outbox_key,job_id,target_id,group_key,before_local_id,mention_name,reply_text,status,max_attempts,next_attempt_at,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (outbox_key, job_id, job["target_id"], job["group_key"], before_local_id, mention_name,
                     filtered, OUTBOX_PENDING, max(1, int(max_send_attempts)), ts, ts),
                )
        updated = con.execute("SELECT * FROM turn_jobs WHERE id=?", (job_id,)).fetchone()
        outbox = con.execute("SELECT * FROM send_outbox WHERE job_id=?", (job_id,)).fetchone()
    return {"job": _row(updated), "outbox": _row(outbox), "applied": True}


def fail_job(*, job_id: int, error: str, retryable: bool = False,
             db_path: Optional[Path] = None, now: Optional[float] = None) -> bool:
    ts = float(now if now is not None else _now())
    status = JOB_QUEUED if retryable else JOB_FAILED
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """UPDATE turn_jobs SET status=?, error=?, lease_owner=NULL, lease_until=NULL,
               finished_at=CASE WHEN ? THEN NULL ELSE ? END WHERE id=? AND status IN (?,?)""",
            (status, str(error or "")[:500], 1 if retryable else 0, ts, job_id, JOB_RUNNING, JOB_QUEUED),
        )
        if not retryable and cur.rowcount:
            job = con.execute("SELECT turn_id FROM turn_jobs WHERE id=?", (job_id,)).fetchone()
            if job:
                con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_FAILED, job["turn_id"]))
    return bool(cur.rowcount)


def claim_sendable(*, owner: str, limit: int = 10, lease_seconds: float = 45.0,
                   db_path: Optional[Path] = None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    if not owner:
        raise ValueError("owner is required")
    ts = float(now if now is not None else _now())
    claimed: List[Dict[str, Any]] = []
    with _WRITE_LOCK, _connect(db_path) as con:
        con.execute("UPDATE send_outbox SET status=?, lease_owner=NULL, lease_until=NULL WHERE status=? AND lease_until<?",
                    (OUTBOX_RETRY, OUTBOX_SENDING, ts))
        rows = con.execute(
            """SELECT * FROM send_outbox WHERE status IN (?,?) AND next_attempt_at<=?
               ORDER BY next_attempt_at,id LIMIT ?""", (OUTBOX_PENDING, OUTBOX_RETRY, ts, max(1, int(limit)))
        ).fetchall()
        for row in rows:
            cur = con.execute(
                """UPDATE send_outbox SET status=?, lease_owner=?, lease_until=?, attempts=attempts+1
                   WHERE id=? AND status IN (?,?) AND next_attempt_at<=?""",
                (OUTBOX_SENDING, owner, ts + max(1.0, lease_seconds), row["id"], OUTBOX_PENDING, OUTBOX_RETRY, ts),
            )
            if cur.rowcount:
                claimed_row = con.execute("SELECT * FROM send_outbox WHERE id=?", (row["id"],)).fetchone()
                item = _row(claimed_row)
                if item:
                    claimed.append(item)
    return claimed


def record_send_result(*, outbox_id: int, confirmed: bool, detail: Dict[str, Any] | None = None,
                       error: str = "", db_path: Optional[Path] = None,
                       now: Optional[float] = None, retry_base_seconds: float = 5.0) -> Dict[str, Any]:
    ts = float(now if now is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        row = con.execute("SELECT * FROM send_outbox WHERE id=?", (int(outbox_id),)).fetchone()
        if not row:
            raise ValueError("unknown outbox row")
        if row["status"] == OUTBOX_SENT:
            return _row(row) or {}
        detail_json = _dump(detail or {})
        if confirmed:
            con.execute("UPDATE send_outbox SET status=?, result_json=?, sent_at=?, lease_owner=NULL, lease_until=NULL,error=NULL WHERE id=?",
                        (OUTBOX_SENT, detail_json, ts, outbox_id))
        elif int(row["attempts"] or 0) >= int(row["max_attempts"] or 1):
            con.execute("UPDATE send_outbox SET status=?, result_json=?, error=?, dead_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                        (OUTBOX_DEAD, detail_json, str(error or "send confirmation failed")[:500], ts, outbox_id))
        else:
            delay = max(1.0, float(retry_base_seconds)) * (2 ** max(0, int(row["attempts"] or 1) - 1))
            con.execute("UPDATE send_outbox SET status=?, result_json=?, error=?, next_attempt_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                        (OUTBOX_RETRY, detail_json, str(error or "send confirmation failed")[:500], ts + delay, outbox_id))
        updated = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
    return _row(updated) or {}


def list_dead_letters(*, limit: int = 50, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with _connect(db_path) as con:
        rows = con.execute("SELECT * FROM send_outbox WHERE status=? ORDER BY dead_at DESC LIMIT ?",
                           (OUTBOX_DEAD, max(1, int(limit)))).fetchall()
    return [item for row in rows if (item := _row(row))]


def counts(*, db_path: Optional[Path] = None) -> Dict[str, Dict[str, int]]:
    tables = {"inbound_events": "status", "turns": "status", "turn_jobs": "status", "send_outbox": "status"}
    result: Dict[str, Dict[str, int]] = {}
    with _connect(db_path) as con:
        for table, field in tables.items():
            rows = con.execute("SELECT %s AS state, COUNT(*) AS count FROM %s GROUP BY %s" % (field, table, field)).fetchall()
            result[table] = {str(row["state"]): int(row["count"]) for row in rows}
    return result
