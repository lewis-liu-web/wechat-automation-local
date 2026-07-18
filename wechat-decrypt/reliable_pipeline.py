#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable transport primitives for the staged WeChat reply pipeline.

This module deliberately owns no knowledge retrieval, reply-decision policy, or
WeChat UI automation.  It stores immutable inbound events, debounced turns,
leased worker jobs, and leased outbound sends in one SQLite database so every
state transition survives a monitor or worker restart.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
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


def _migrate_provenance_column(con: sqlite3.Connection) -> None:
    """Add ``provenance_json`` to ``turn_jobs`` if the column is missing."""
    rows = con.execute("PRAGMA table_info(turn_jobs)").fetchall()
    if not any(str(r["name"]) == "provenance_json" for r in rows):
        con.execute("ALTER TABLE turn_jobs ADD COLUMN provenance_json TEXT")


def _migrate_shadow_column(con: sqlite3.Connection) -> None:
    """Add ``shadow_json`` to ``turn_jobs`` if the column is missing."""
    rows = con.execute("PRAGMA table_info(turn_jobs)").fetchall()
    if not any(str(r["name"]) == "shadow_json" for r in rows):
        con.execute("ALTER TABLE turn_jobs ADD COLUMN shadow_json TEXT")


def _migrate_provider_diagnostics_column(con: sqlite3.Connection) -> None:
    """Add ``provider_diagnostics_json`` to ``turn_jobs`` if the column is missing."""
    rows = con.execute("PRAGMA table_info(turn_jobs)").fetchall()
    if not any(str(r["name"]) == "provider_diagnostics_json" for r in rows):
        con.execute("ALTER TABLE turn_jobs ADD COLUMN provider_diagnostics_json TEXT")


def _migrate_send_started_column(con: sqlite3.Connection) -> None:
    """Add ``send_started_at`` to ``send_outbox`` if the column is missing.

    ``send_started_at`` is persisted immediately before the sender is invoked.
    It is a durable record that a send was *authorized* for the row — not proof
    the call actually ran, since the process can die between the write and the
    invocation.  The safety invariant is one-directional: a dead-letter with
    ``send_started_at IS NULL`` was never authorized, so the message provably
    never went out and a requeue cannot duplicate a send.  A non-NULL value
    means a send was authorized (and may or may not have completed), so the
    safe requeue path conservatively refuses it.

    Pre-existing rows predate this tracking, so NULL would be ambiguous (never
    sent vs. never tracked).  They are backfilled to a non-NULL sentinel
    (``created_at``) so the default requeue path rejects them; recovering such a
    legacy row requires an explicit, audited ``legacy_override`` after a human
    verifies (via error/result_json) that the sender was never invoked.  Rows
    created after this migration keep a truthful NULL until the sender runs.
    """
    rows = con.execute("PRAGMA table_info(send_outbox)").fetchall()
    if not any(str(r["name"]) == "send_started_at" for r in rows):
        con.execute("ALTER TABLE send_outbox ADD COLUMN send_started_at REAL")
        con.execute(
            "UPDATE send_outbox SET send_started_at=COALESCE(created_at, 0) WHERE send_started_at IS NULL"
        )


def _migrate_requeue_count_column(con: sqlite3.Connection) -> None:
    """Add ``requeue_count`` to ``send_outbox`` if the column is missing."""
    rows = con.execute("PRAGMA table_info(send_outbox)").fetchall()
    if not any(str(r["name"]) == "requeue_count" for r in rows):
        con.execute("ALTER TABLE send_outbox ADD COLUMN requeue_count INTEGER NOT NULL DEFAULT 0")


def _migrate_lease_id_column(con: sqlite3.Connection) -> None:
    """Add ``lease_id`` to ``send_outbox`` if the column is missing.

    ``lease_id`` is a unique per-claim token.  The owner string alone is NOT a
    lease guard (every ``send_once`` defaults to the same ``DEFAULT_SEND_OWNER``,
    so a stale worker and a reclaiming worker share it).  Each claim mints a
    fresh ``lease_id``; the send-started marker and the send result are guarded
    on it, so a stale worker whose claim was reclaimed cannot finalize or mark
    the new owner's row.
    """
    rows = con.execute("PRAGMA table_info(send_outbox)").fetchall()
    if not any(str(r["name"]) == "lease_id" for r in rows):
        con.execute("ALTER TABLE send_outbox ADD COLUMN lease_id TEXT")


def _migrate_recovery_audit_table(con: sqlite3.Connection) -> None:
    """Create the immutable ``send_outbox_recovery_audit`` table.

    Every dead-letter requeue appends the row's prior state here.  UPDATE and
    DELETE are rejected by triggers so the audit trail cannot be rewritten or
    erased by any future code in this database.
    """
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS send_outbox_recovery_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outbox_id INTEGER NOT NULL,
            prior_status TEXT NOT NULL,
            prior_attempts INTEGER NOT NULL,
            prior_error TEXT,
            prior_result_json TEXT,
            prior_dead_at REAL,
            prior_send_started_at REAL,
            legacy_override INTEGER NOT NULL DEFAULT 0,
            verification_evidence TEXT,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            requeued_at REAL NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS send_outbox_recovery_audit_no_update
        BEFORE UPDATE ON send_outbox_recovery_audit
        BEGIN
            SELECT RAISE(ABORT, 'send_outbox_recovery_audit is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS send_outbox_recovery_audit_no_delete
        BEFORE DELETE ON send_outbox_recovery_audit
        BEGIN
            SELECT RAISE(ABORT, 'send_outbox_recovery_audit is append-only');
        END;
        """
    )
    # Additive safety for tables created before these columns existed (e.g. an
    # earlier iteration created the table via CREATE TABLE IF NOT EXISTS, which
    # will not retrofit new columns).  ALTER only the ones actually missing.
    cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(send_outbox_recovery_audit)").fetchall()}
    if "legacy_override" not in cols:
        con.execute("ALTER TABLE send_outbox_recovery_audit ADD COLUMN legacy_override INTEGER NOT NULL DEFAULT 0")
    if "verification_evidence" not in cols:
        con.execute("ALTER TABLE send_outbox_recovery_audit ADD COLUMN verification_evidence TEXT")


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
    _migrate_provenance_column(con)
    _migrate_shadow_column(con)
    _migrate_provider_diagnostics_column(con)
    _migrate_send_started_column(con)
    _migrate_requeue_count_column(con)
    _migrate_lease_id_column(con)
    _migrate_recovery_audit_table(con)
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
    for key in ("payload_json", "result_json", "provenance_json", "provider_diagnostics_json"):
        if key in item:
            item[key[:-5]] = _load(item.pop(key), {})
    return item


def source_event_id(target: Dict[str, Any], message: Dict[str, Any]) -> str:
    return "wx:%s:%s:%s" % (
        target.get("db") or "",
        target.get("table") or "",
        int(message.get("local_id") or 0),
    )


def _snapshot_shadow_flag(payload: Dict[str, Any], target_id: str) -> Dict[str, Any]:
    """Snapshot ``reliable_pipeline_shadow`` into the event payload's target dict.

    Monitor ingress (``build_event_payload``) whitelists ``payload['target']``
    fields and drops the config flag, so resolve it here from the targets
    config referenced by ``payload['_config_path']``.  Snapshotting at persist
    time freezes the value into the immutable event row: close-time then only
    ORs payload fields, and later config edits cannot retroactively change an
    already-ingressed event.  An explicitly present payload value always wins.
    Fail-open: any read/parse problem leaves the payload untouched.
    """
    if not isinstance(payload, dict):
        return payload
    target = payload.get("target")
    if not isinstance(target, dict) or "reliable_pipeline_shadow" in target:
        return payload
    config_path = str(payload.get("_config_path") or "").strip()
    if not config_path:
        return payload
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return payload
    targets = cfg.get("targets") if isinstance(cfg, dict) else None
    if not isinstance(targets, list):
        return payload
    for entry in targets:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("username") or "") != str(target_id or ""):
            continue
        if bool(entry.get("reliable_pipeline_shadow")):
            payload = dict(payload)
            payload["target"] = dict(target)
            payload["target"]["reliable_pipeline_shadow"] = True
        return payload
    return payload


def persist_inbound_event(*, event_id: str, target_id: str, group_key: str, sender_id: str,
                          local_id: int, payload: Dict[str, Any], received_at: Optional[float] = None,
                          db_path: Optional[Path] = None) -> Tuple[Dict[str, Any], bool]:
    """Insert one immutable event. Returns `(event, inserted)` for idempotent reads."""
    if not event_id or not target_id or not group_key or not sender_id:
        raise ValueError("event_id, target_id, group_key, and sender_id are required")
    payload = _snapshot_shadow_flag(payload, target_id)
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
    # Promote target binding so downstream job dispatch can enforce dedicated worker routing.
    # A single window should only contain events for one target; reject conflicting bindings.
    dedicated_ids = set()
    for p in payloads:
        did = (p["payload"].get("target") or {}).get("dedicated_agent_instance_id") or ""
        if did:
            dedicated_ids.add(did)
    if len(dedicated_ids) > 1:
        raise ValueError("conflicting dedicated_agent_instance_id in turn window: %s" % sorted(dedicated_ids))
    dedicated_agent_instance_id = next(iter(dedicated_ids), "")
    # Shadow mode propagates like the dedicated binding: if any event's target
    # snapshot opts into shadow, the whole turn is shadowed (OR semantics).
    shadow = any(bool((p["payload"].get("target") or {}).get("reliable_pipeline_shadow")) for p in payloads)
    turn_payload = {
        "schema_version": SCHEMA_VERSION,
        "dedicated_agent_instance_id": dedicated_agent_instance_id,
        "shadow": shadow,
        "target": {"dedicated_agent_instance_id": dedicated_agent_instance_id},
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
            """UPDATE send_outbox SET status=?, lease_owner=NULL, lease_until=NULL, lease_id=NULL,
               next_attempt_at=MIN(next_attempt_at, ?) WHERE status=? AND lease_until<=?""",
            (OUTBOX_RETRY, ts, OUTBOX_SENDING, ts),
        ).rowcount
    return {"jobs": int(jobs or 0), "outbox": int(outbox or 0)}


def claim_next_job(*, owner: str, lease_seconds: float = 120.0,
                   deadline_seconds: float = 300.0, db_path: Optional[Path] = None,
                   now: Optional[float] = None, instance_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not owner:
        raise ValueError("owner is required")
    ts = float(now if now is not None else _now())
    with _WRITE_LOCK, _connect(db_path) as con:
        con.execute("UPDATE turn_jobs SET status=?, lease_owner=NULL, lease_until=NULL WHERE status=? AND lease_until<?",
                    (JOB_QUEUED, JOB_RUNNING, ts))
        rows = con.execute("SELECT * FROM turn_jobs WHERE status=? ORDER BY created_at,id LIMIT 100", (JOB_QUEUED,)).fetchall()
        selected = None
        for candidate in rows:
            payload = _load(candidate["payload_json"], {})
            bound = str(payload.get("dedicated_agent_instance_id") or "").strip()
            if instance_id:
                # Instance runner claims only jobs bound to it.
                if bound != str(instance_id).strip():
                    continue
            else:
                # General runner claims only unbound jobs.
                if bound:
                    continue
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
                       provenance: Optional[Dict[str, Any]] = None,
                       db_path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Persist a validated worker decision and materialize reply/escalation effects.

    `final_filter` is the one caller-owned deterministic safety filter. It must
    return a string; an empty filtered reply fails the job and creates no outbox.
    """
    contract = result if isinstance(result, AgentResultContract) else parse_agent_result(result)
    if not callable(final_filter):
        raise ValueError("final_filter must be callable")
    ts = float(now if now is not None else _now())
    provenance_json = _dump(provenance) if provenance else None
    with _WRITE_LOCK, _connect(db_path) as con:
        job = con.execute("SELECT * FROM turn_jobs WHERE id=?", (int(job_id),)).fetchone()
        if not job:
            raise ValueError("unknown turn job")
        if job["status"] not in {JOB_RUNNING, JOB_QUEUED}:
            return {"job": _row(job), "applied": False,
                    "shadow": bool(_load(job["payload_json"], {}).get("shadow"))}
        shadow = bool(_load(job["payload_json"], {}).get("shadow"))
        shadow_json = None
        result_json = _dump(contract.to_dict())
        if contract.action == "silent":
            con.execute("UPDATE turn_jobs SET status=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                        (JOB_DONE, result_json, ts, job_id))
            con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_DONE, job["turn_id"]))
            if shadow:
                shadow_json = _dump({
                    "shadow": True, "would_send": False,
                    "action": contract.action, "reason_code": contract.reason_code,
                    "recorded_at": ts,
                })
        elif contract.action == "escalate":
            con.execute("UPDATE turn_jobs SET status=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                        (JOB_ESCALATED, result_json, ts, job_id))
            con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_ESCALATED, job["turn_id"]))
            con.execute("INSERT OR IGNORE INTO escalations(job_id,target_id,group_key,reason_code,risk_level,created_at) VALUES (?,?,?,?,?,?)",
                        (job_id, job["target_id"], job["group_key"], contract.reason_code, contract.risk_level, ts))
            if shadow:
                shadow_json = _dump({
                    "shadow": True, "would_send": False,
                    "action": contract.action, "reason_code": contract.reason_code,
                    "recorded_at": ts,
                })
        else:
            filtered = str(final_filter(contract.reply_text) or "").strip()
            if not filtered:
                con.execute("UPDATE turn_jobs SET status=?, error=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                            (JOB_FAILED, "reply rejected by final safety filter", result_json, ts, job_id))
                con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_FAILED, job["turn_id"]))
                if shadow:
                    shadow_json = _dump({
                        "shadow": True, "would_send": False,
                        "action": contract.action, "reason_code": contract.reason_code,
                        "error": "reply rejected by final safety filter",
                        "recorded_at": ts,
                    })
            else:
                con.execute("UPDATE turn_jobs SET status=?, result_json=?, finished_at=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                            (JOB_DONE, result_json, ts, job_id))
                con.execute("UPDATE turns SET status=? WHERE id=?", (TURN_DONE, job["turn_id"]))
                if shadow:
                    # Shadow mode records the would-be reply decision but never
                    # creates a sendable outbox row.
                    shadow_json = _dump({
                        "shadow": True, "would_send": True,
                        "reply_text": filtered, "reply_chars": len(filtered),
                        "mention_name": mention_name,
                        "recorded_at": ts,
                    })
                else:
                    before_local_id = _job_turn_end_local_id(con, job_id)
                    outbox_key = "job:%d" % job_id
                    con.execute(
                        """INSERT OR IGNORE INTO send_outbox
                           (outbox_key,job_id,target_id,group_key,before_local_id,mention_name,reply_text,status,max_attempts,next_attempt_at,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (outbox_key, job_id, job["target_id"], job["group_key"], before_local_id, mention_name,
                         filtered, OUTBOX_PENDING, max(1, int(max_send_attempts)), ts, ts),
                    )
        if provenance_json is not None:
            con.execute("UPDATE turn_jobs SET provenance_json=? WHERE id=?", (provenance_json, job_id))
        if shadow_json is not None:
            con.execute("UPDATE turn_jobs SET shadow_json=? WHERE id=?", (shadow_json, job_id))
        updated = con.execute("SELECT * FROM turn_jobs WHERE id=?", (job_id,)).fetchone()
        outbox = con.execute("SELECT * FROM send_outbox WHERE job_id=?", (job_id,)).fetchone()
    return {"job": _row(updated), "outbox": _row(outbox), "applied": True, "shadow": shadow}


def fail_job(*, job_id: int, error: str, retryable: bool = False,
             provenance: Optional[Dict[str, Any]] = None,
             provider_diagnostics: Optional[Dict[str, Any]] = None,
             db_path: Optional[Path] = None, now: Optional[float] = None) -> bool:
    ts = float(now if now is not None else _now())
    status = JOB_QUEUED if retryable else JOB_FAILED
    provenance_json = _dump(provenance) if provenance else None
    diagnostics_json = _dump(provider_diagnostics) if provider_diagnostics else None
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """UPDATE turn_jobs SET status=?, error=?, lease_owner=NULL, lease_until=NULL,
               finished_at=CASE WHEN ? THEN NULL ELSE ? END WHERE id=? AND status IN (?,?)""",
            (status, str(error or "")[:500], 1 if retryable else 0, ts, job_id, JOB_RUNNING, JOB_QUEUED),
        )
        if cur.rowcount:
            if provenance_json is not None:
                con.execute("UPDATE turn_jobs SET provenance_json=? WHERE id=?", (provenance_json, job_id))
            if diagnostics_json is not None:
                con.execute("UPDATE turn_jobs SET provider_diagnostics_json=? WHERE id=?", (diagnostics_json, job_id))
            if not retryable:
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
        # Reclaim expired in-flight leases; clear their lease token so a stale
        # holder can no longer use it against the row.
        con.execute("UPDATE send_outbox SET status=?, lease_owner=NULL, lease_until=NULL, lease_id=NULL WHERE status=? AND lease_until<=?",
                    (OUTBOX_RETRY, OUTBOX_SENDING, ts))
        rows = con.execute(
            """SELECT * FROM send_outbox WHERE status IN (?,?) AND next_attempt_at<=?
               ORDER BY next_attempt_at,id LIMIT ?""", (OUTBOX_PENDING, OUTBOX_RETRY, ts, max(1, int(limit)))
        ).fetchall()
        for row in rows:
            # Mint a fresh per-claim lease token.  The owner string is shared
            # across senders, so this unique token is the real guard used by the
            # send-started marker and the send result transition.
            lease_id = "%s:%s" % (owner, uuid.uuid4().hex)
            cur = con.execute(
                """UPDATE send_outbox SET status=?, lease_owner=?, lease_until=?, lease_id=?, attempts=attempts+1
                   WHERE id=? AND status IN (?,?) AND next_attempt_at<=?""",
                (OUTBOX_SENDING, owner, ts + max(1.0, lease_seconds), lease_id, row["id"], OUTBOX_PENDING, OUTBOX_RETRY, ts),
            )
            if cur.rowcount:
                claimed_row = con.execute("SELECT * FROM send_outbox WHERE id=?", (row["id"],)).fetchone()
                item = _row(claimed_row)
                if item:
                    claimed.append(item)
    return claimed


def record_send_result(*, outbox_id: int, owner: str, lease_id: str, confirmed: bool,
                       detail: Dict[str, Any] | None = None, error: str = "",
                       db_path: Optional[Path] = None, now: Optional[float] = None,
                       retry_base_seconds: float = 5.0) -> Dict[str, Any]:
    """Record the outcome of a send attempt for a claimed outbox row.

    Every transition is guarded by ``lease_id=? AND status='sending'`` and
    clears the token on write.  ``lease_id`` is a unique per-claim token (the
    owner string is shared across senders and is NOT a guard), so a stale
    worker whose claim was reclaimed — even under the same owner — cannot move
    the new owner's row to sent/retry/dead and cause a duplicate send or an
    incorrect terminal state.  ``owner`` is kept only for the error message.
    Raises ``ValueError`` when the caller no longer holds the current lease.
    An already-``sent`` row returns idempotently.
    """
    ts = float(now if now is not None else _now())
    owner = str(owner or "")
    lease_id = str(lease_id or "")
    if not owner:
        raise ValueError("owner is required")
    if not lease_id:
        raise ValueError("lease_id is required")
    with _WRITE_LOCK, _connect(db_path) as con:
        row = con.execute("SELECT * FROM send_outbox WHERE id=?", (int(outbox_id),)).fetchone()
        if not row:
            raise ValueError("unknown outbox row")
        if row["status"] == OUTBOX_SENT:
            return _row(row) or {}
        detail_json = _dump(detail or {})
        if confirmed:
            cur = con.execute("UPDATE send_outbox SET status=?, result_json=?, sent_at=?, lease_owner=NULL, lease_until=NULL, lease_id=NULL, error=NULL WHERE id=? AND lease_id=? AND status=?",
                              (OUTBOX_SENT, detail_json, ts, outbox_id, lease_id, OUTBOX_SENDING))
        elif int(row["attempts"] or 0) >= int(row["max_attempts"] or 1):
            cur = con.execute("UPDATE send_outbox SET status=?, result_json=?, error=?, dead_at=?, lease_owner=NULL, lease_until=NULL, lease_id=NULL WHERE id=? AND lease_id=? AND status=?",
                              (OUTBOX_DEAD, detail_json, str(error or "send confirmation failed")[:500], ts, outbox_id, lease_id, OUTBOX_SENDING))
        else:
            delay = max(1.0, float(retry_base_seconds)) * (2 ** max(0, int(row["attempts"] or 1) - 1))
            cur = con.execute("UPDATE send_outbox SET status=?, result_json=?, error=?, next_attempt_at=?, lease_owner=NULL, lease_until=NULL, lease_id=NULL WHERE id=? AND lease_id=? AND status=?",
                              (OUTBOX_RETRY, detail_json, str(error or "send confirmation failed")[:500], ts + delay, outbox_id, lease_id, OUTBOX_SENDING))
        if not cur.rowcount:
            raise ValueError(
                "send result rejected: outbox row lease is no longer held by %r (reclaimed by another sender)" % owner)
        updated = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
    return _row(updated) or {}


def list_dead_letters(*, limit: int = 50, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with _connect(db_path) as con:
        rows = con.execute("SELECT * FROM send_outbox WHERE status=? ORDER BY dead_at DESC LIMIT ?",
                           (OUTBOX_DEAD, max(1, int(limit)))).fetchall()
    return [item for row in rows if (item := _row(row))]


def record_send_started(*, outbox_id: int, lease_id: str, lease_extension_seconds: float = 45.0,
                        db_path: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """Persist the moment the sender is about to be invoked for a claimed row.

    ``send_started_at`` is a durable record that a send was *authorized* for a
    claimed row (written immediately before invocation).  A dead-letter with
    ``send_started_at IS NULL`` was never authorized, so the message provably
    never went out, which is what makes a safe requeue possible.

    The update is guarded by ``status='sending' AND lease_id=? AND
    lease_until>now``.  ``lease_id`` is a unique per-claim token (the owner
    string is shared across senders and is NOT a guard), so a stale worker
    whose claim was reclaimed — even under the same owner — cannot mark the new
    owner's row.  The update also atomically extends ``lease_until`` by
    ``lease_extension_seconds``, closing the race where the lease expires
    between marking and the actual sender call: a successful mark grants this
    caller a fresh send window during which no other worker can reclaim.
    ``COALESCE`` makes the first invocation win so a stale worker cannot move
    the marker.  Returns ``True`` only when this caller holds the current lease
    and may proceed to invoke the sender.
    """
    ts = float(now if now is not None else _now())
    ext = max(1.0, float(lease_extension_seconds))
    lease_id = str(lease_id or "")
    if not lease_id:
        return False
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """UPDATE send_outbox
               SET send_started_at=COALESCE(send_started_at, ?),
                   lease_until=?
               WHERE id=? AND status=? AND lease_id=? AND lease_until>?""",
            (ts, ts + ext, int(outbox_id), OUTBOX_SENDING, lease_id, ts),
        )
        return bool(cur.rowcount)


def requeue_dead_letter(*, outbox_id: int, reason: str, actor: str,
                        db_path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Recover a dead-letter outbox row that provably never reached the sender.

    Only a dead-letter whose ``send_started_at IS NULL`` may be requeued: that
    column is written the moment a send is authorized (immediately before
    ``send_reply_detailed`` runs), so a NULL value means no send was ever
    authorized and the message provably never went out — replay cannot
    duplicate a send.  Rows with a non-NULL marker (a send was authorized, and
    may or may not have completed) are rejected to protect the Stage 3 "no
    unconfirmed duplicate send" guarantee.

    The prior state is appended to the immutable ``send_outbox_recovery_audit``
    table (UPDATE/DELETE are rejected by triggers) before the row is reset to
    ``retry`` with ``attempts=0`` and an incremented ``requeue_count``.
    """
    ts = float(now if now is not None else _now())
    reason = str(reason or "").strip()
    actor = str(actor or "").strip()
    if not reason:
        raise ValueError("requeue reason is required")
    if not actor:
        raise ValueError("requeue actor is required")
    with _WRITE_LOCK, _connect(db_path) as con:
        row = con.execute("SELECT * FROM send_outbox WHERE id=?", (int(outbox_id),)).fetchone()
        if not row:
            raise ValueError("unknown outbox row")
        if row["status"] != OUTBOX_DEAD:
            raise ValueError("outbox row is not dead_letter (status=%r)" % (row["status"],))
        if row["send_started_at"] is not None:
            raise ValueError(
                "outbox row reached the sender (send_started_at set); refusing to requeue to avoid duplicate send")
        con.execute(
            """INSERT INTO send_outbox_recovery_audit
               (outbox_id, prior_status, prior_attempts, prior_error, prior_result_json,
                prior_dead_at, prior_send_started_at, legacy_override, verification_evidence,
                reason, actor, requeued_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(outbox_id), str(row["status"]), int(row["attempts"] or 0), row["error"],
             row["result_json"], row["dead_at"], row["send_started_at"], 0, None,
             reason, actor, ts),
        )
        con.execute(
            """UPDATE send_outbox SET status=?, attempts=0, requeue_count=COALESCE(requeue_count,0)+1,
               next_attempt_at=?, error=NULL, result_json=NULL, lease_owner=NULL, lease_until=NULL, lease_id=NULL,
               dead_at=NULL, send_started_at=NULL WHERE id=?""",
            (OUTBOX_RETRY, ts, int(outbox_id)),
        )
        updated = con.execute("SELECT * FROM send_outbox WHERE id=?", (int(outbox_id),)).fetchone()
    return _row(updated) or {}


# Known pre-send gate-rejection error prefixes produced by the worker's gate
# checks before the sender is invoked.  Used only as a consistency check in the
# legacy recovery path — NOT as a proof on its own, since the public
# ``record_send_result(detail=...)`` accepts arbitrary detail (any caller could
# persist ``skipped: True`` or a matching error after an attempted send).
_PRE_SEND_GATE_ERROR_PREFIXES = (
    "test_mode_target_rejected",
    "target not configured",
)


def recover_legacy_gate_rejection(*, outbox_id: int, expected_error: str,
                                  expected_reply_text_sha256: str, verification_evidence: str,
                                  actor: str, db_path: Optional[Path] = None,
                                  now: Optional[float] = None) -> Dict[str, Any]:
    """Manual override to requeue a PRE-TRACKING dead-letter after inspection.

    Rows created before send-start tracking have ``send_started_at`` backfilled
    to a non-NULL sentinel, so the safe ``requeue_dead_letter`` path (which
    requires ``send_started_at IS NULL``) refuses them.  This override exists
    for that legacy case ONLY, and it is a MANUAL operator action, not a
    machine-verified proof of safety.

    Guarantees it DOES provide:
      * ``status`` must be ``dead_letter`` and ``send_started_at`` non-NULL
        (legacy sentinel), and
      * the caller must supply the row's exact current ``error`` string AND the
        SHA-256 of its reply text.  These pins force the operator to actually
        read the row before invoking, so a blind or accidental call fails — but
        they are an inspection-confirmation, NOT a cryptographic guarantee,
        since a determined caller can compute matching values for any row.
      * as a consistency check, ``result_json.skipped is True`` and ``error``
        starts with a known pre-send gate-rejection prefix.  Again a guard, not
        proof: the public ``record_send_result`` accepts arbitrary detail.

    The real safety control is procedural: this function is NOT exposed over
    HTTP, and the operator must record what they inspected in
    ``verification_evidence`` (e.g. the dead-letter's error and skipped flag
    showing a pre-send gate rejection).  All prior state plus the evidence is
    appended to the immutable audit table before the row is reset to ``retry``.
    Invoke directly only after manually confirming the specific row never
    reached the sender.
    """
    ts = float(now if now is not None else _now())
    expected_error = str(expected_error or "")
    expected_reply_text_sha256 = str(expected_reply_text_sha256 or "").strip().lower()
    verification_evidence = str(verification_evidence or "").strip()
    actor = str(actor or "").strip()
    if not expected_error:
        raise ValueError("expected_error is required for legacy recovery")
    if not expected_reply_text_sha256:
        raise ValueError("expected_reply_text_sha256 is required for legacy recovery")
    if not verification_evidence:
        raise ValueError("verification_evidence is required for legacy recovery")
    if not actor:
        raise ValueError("requeue actor is required")
    with _WRITE_LOCK, _connect(db_path) as con:
        row = con.execute("SELECT * FROM send_outbox WHERE id=?", (int(outbox_id),)).fetchone()
        if not row:
            raise ValueError("unknown outbox row")
        if row["status"] != OUTBOX_DEAD:
            raise ValueError("outbox row is not dead_letter (status=%r)" % (row["status"],))
        if row["send_started_at"] is None:
            raise ValueError("send_started_at is NULL; use requeue_dead_letter (safe path) instead")
        result = _load(row["result_json"], {})
        if not (isinstance(result, dict) and result.get("skipped") is True):
            raise ValueError("prior result does not prove a pre-send gate rejection (skipped is not true)")
        error = str(row["error"] or "")
        if not error.startswith(_PRE_SEND_GATE_ERROR_PREFIXES):
            raise ValueError("prior error is not a known pre-send gate rejection")
        if error != expected_error:
            raise ValueError("error does not match the pinned expected_error")
        reply_text = str(row["reply_text"] or "")
        actual_sha = hashlib.sha256(reply_text.encode("utf-8")).hexdigest()
        if actual_sha != expected_reply_text_sha256:
            raise ValueError("reply_text hash does not match the pinned expected_reply_text_sha256")
        con.execute(
            """INSERT INTO send_outbox_recovery_audit
               (outbox_id, prior_status, prior_attempts, prior_error, prior_result_json,
                prior_dead_at, prior_send_started_at, legacy_override, verification_evidence,
                reason, actor, requeued_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(outbox_id), str(row["status"]), int(row["attempts"] or 0), row["error"],
             row["result_json"], row["dead_at"], row["send_started_at"], 1, verification_evidence,
             "legacy gate-rejection recovery", actor, ts),
        )
        con.execute(
            """UPDATE send_outbox SET status=?, attempts=0, requeue_count=COALESCE(requeue_count,0)+1,
               next_attempt_at=?, error=NULL, result_json=NULL, lease_owner=NULL, lease_until=NULL, lease_id=NULL,
               dead_at=NULL, send_started_at=NULL WHERE id=?""",
            (OUTBOX_RETRY, ts, int(outbox_id)),
        )
        updated = con.execute("SELECT * FROM send_outbox WHERE id=?", (int(outbox_id),)).fetchone()
    return _row(updated) or {}


def counts(*, db_path: Optional[Path] = None) -> Dict[str, Dict[str, int]]:
    tables = {"inbound_events": "status", "turns": "status", "turn_jobs": "status", "send_outbox": "status"}
    result: Dict[str, Dict[str, int]] = {}
    with _connect(db_path) as con:
        for table, field in tables.items():
            rows = con.execute("SELECT %s AS state, COUNT(*) AS count FROM %s GROUP BY %s" % (field, table, field)).fetchall()
            result[table] = {str(row["state"]): int(row["count"]) for row in rows}
    return result
