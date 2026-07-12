#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite-backed deep-agent job queue for WeChat automation.

This module is intentionally self-contained and has no dependency on the
monitor loop.  M1 only provides the durable state machine and scheduling helpers;
later steps can plug it into reply routing, workers, and the control UI.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "temp" / "agent_jobs.sqlite"

STATUS_QUEUED = "queued"
STATUS_DISPATCHING = "dispatching"
STATUS_SUBMITTED = "submitted"
STATUS_AGENT_RUNNING = "agent_running"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

SEND_PENDING = "pending"
SEND_SENT = "sent"
SEND_FAILED = "failed"
SEND_SKIPPED = "skipped"

TERMINAL_STATUSES = {STATUS_SENT, STATUS_FAILED, STATUS_TIMEOUT, STATUS_EXPIRED, STATUS_CANCELLED}
VALID_STATUSES = {
    STATUS_QUEUED,
    STATUS_DISPATCHING,
    STATUS_SUBMITTED,
    STATUS_AGENT_RUNNING,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
}

_WRITE_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _json_loads(value: str | None) -> Dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        return {}


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def sanitize_agent_result_text(text: str) -> str:
    """Lightweight format cleanup before a reply is stored or sent.

    Responsibilities: strip ANSI escapes, terminal box characters, and the
    "Resume this session" prompt; merge remaining non-empty lines into a single
    line.  Detection of tool logs or Hermes init noise belongs to the provider
    layer (``agent_provider._clean_agent_output``).
    """
    value = str(text or "").replace("\r", "").strip()
    if not value:
        return ""
    value = _ANSI_ESCAPE_RE.sub("", value)
    for marker in ("Resume this session with:", "\nSession:"):
        if marker in value:
            value = value.split(marker, 1)[0].strip()
    if "╰" in value:
        value = value.split("╰", 1)[0].strip()

    cleaned_lines: List[str] = []
    for line in value.splitlines():
        line = line.strip().strip("│").strip()
        if not line:
            continue
        if set(line) <= {"─", "╭", "╮", "╰", "╯", "│", " ", }:
            continue
        cleaned_lines.append(line)
    return " ".join(cleaned_lines).strip()


def _row_to_dict(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    out["payload"] = _json_loads(out.pop("payload_json", "{}"))
    out["aggregator_summary"] = _json_loads(out.get("aggregator_summary") or "{}")
    if out.get("result_text"):
        out["result_text"] = sanitize_agent_result_text(str(out["result_text"]))
    return out


def open_db(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path or DEFAULT_DB_PATH)
    _ensure_parent(db_path)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_key TEXT NOT NULL UNIQUE,
            group_key TEXT NOT NULL,
            target_name TEXT,
            sender TEXT,
            message_local_id INTEGER,
            task_type TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            provider TEXT,
            worker_id TEXT,
            result_text TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            ack_sent_at REAL,
            sent_at REAL,
            send_status TEXT NOT NULL DEFAULT 'pending',
            external_provider TEXT,
            external_session_id TEXT,
            external_user_msg_id INTEGER,
            external_status TEXT,
            submitted_at REAL,
            last_polled_at REAL,
            next_poll_at REAL,
            agent_deadline_at REAL,
            reconcile_attempts INTEGER DEFAULT 0,
            send_attempts INTEGER DEFAULT 0,
            result_message_id TEXT,
            dispatch_owner TEXT,
            dispatch_locked_until REAL
        )
        """
    )
    # Migration: add external_* columns if they don't exist
    cols = {row["name"] for row in con.execute("PRAGMA table_info(agent_jobs)").fetchall()}
    migrations = [
        ("external_provider", "TEXT"),
        ("external_session_id", "TEXT"),
        ("external_user_msg_id", "INTEGER"),
        ("external_status", "TEXT"),
        ("submitted_at", "REAL"),
        ("last_polled_at", "REAL"),
        ("next_poll_at", "REAL"),
        ("agent_deadline_at", "REAL"),
        ("reconcile_attempts", "INTEGER DEFAULT 0"),
        ("send_attempts", "INTEGER DEFAULT 0"),
        ("result_message_id", "TEXT"),
        ("dispatch_owner", "TEXT"),
        ("dispatch_locked_until", "REAL"),
        ("aggregator_summary", "TEXT"),
    ]
    for col_name, col_type in migrations:
        if col_name not in cols:
            con.execute(f"ALTER TABLE agent_jobs ADD COLUMN {col_name} {col_type}")
    con.execute("CREATE INDEX IF NOT EXISTS agent_jobs_status ON agent_jobs(status)")
    con.execute("CREATE INDEX IF NOT EXISTS agent_jobs_group_status ON agent_jobs(group_key, status)")
    con.execute("CREATE INDEX IF NOT EXISTS agent_jobs_created ON agent_jobs(created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS agent_jobs_message ON agent_jobs(target_name, message_local_id)")
    con.execute("CREATE INDEX IF NOT EXISTS agent_jobs_external_session ON agent_jobs(external_session_id)")
    con.execute("CREATE INDEX IF NOT EXISTS agent_jobs_next_poll ON agent_jobs(next_poll_at)")
    con.commit()
    return con


@contextmanager
def _connect(path: Optional[Path] = None):
    con = open_db(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def enqueue_job(*,
                job_key: str,
                group_key: str,
                task_type: str,
                payload: Dict[str, Any],
                target_name: str | None = None,
                sender: str | None = None,
                message_local_id: int | None = None,
                priority: int = 0,
                provider: str | None = None,
                is_aggregated: bool | None = None,
                aggregated_local_ids: List[Any] | None = None,
                session_image_paths: List[str] | None = None,
                text_parts_count: int | None = None,
                aggregator_summary: Dict[str, Any] | None = None,
                agent_timeout: float | None = None,
                knowledge_hits: List[Dict[str, Any]] | None = None,
                knowledge_bases: List[str] | None = None,
                reply_mode: str | None = None,
                retrieval_debug: Dict[str, Any] | None = None,
                skill_name: str | None = None,
                dedicated_agent_instance_id: str | None = None,
                db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Create a queued job, or return the existing job with the same key.
    `job_key` is the duplicate-reply guard.  For WeChat messages it should be
    derived from target/table/local_id rather than random UUIDs.

    Optional aggregation/skill metadata is merged into ``payload`` only for
    keys not already present, preserving backward compatibility with payloads
    that already contain fields such as ``skill_prompt``.
    """
    if not job_key:
        raise ValueError("job_key is required")
    if not group_key:
        raise ValueError("group_key is required")
    if not task_type:
        raise ValueError("task_type is required")

    merged_payload = dict(payload)
    _optional_payload_fields = {
        "is_aggregated": is_aggregated,
        "aggregated_local_ids": aggregated_local_ids,
        "session_image_paths": session_image_paths,
        "text_parts_count": text_parts_count,
        "agent_timeout": agent_timeout,
        "knowledge_hits": knowledge_hits,
        "knowledge_bases": knowledge_bases,
        "reply_mode": reply_mode,
        "retrieval_debug": retrieval_debug,
        "skill_name": skill_name,
        "dedicated_agent_instance_id": dedicated_agent_instance_id,
        "aggregator_summary": aggregator_summary,
    }
    for key, value in _optional_payload_fields.items():
        if value is not None and key not in merged_payload:
            merged_payload[key] = value
    ts = _now()
    with _WRITE_LOCK, _connect(db_path) as con:
        con.execute(
            """
            INSERT OR IGNORE INTO agent_jobs (
                job_key, group_key, target_name, sender, message_local_id,
                task_type, priority, status, payload_json, provider, aggregator_summary, created_at, send_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(job_key), str(group_key), target_name, sender, message_local_id,
                str(task_type), int(priority), STATUS_QUEUED, _json_dumps(merged_payload),
                provider,
                _json_dumps(aggregator_summary) if aggregator_summary is not None else None,
                ts, SEND_PENDING,
            ),
        )
        row = con.execute("SELECT * FROM agent_jobs WHERE job_key=?", (str(job_key),)).fetchone()
    job = _row_to_dict(row)
    if job is None:
        raise RuntimeError("failed to enqueue job")
    return job


def get_job(job_id: int | None = None, *, job_key: str | None = None,
            db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    if job_id is None and not job_key:
        raise ValueError("job_id or job_key is required")
    with _connect(db_path) as con:
        if job_key:
            row = con.execute("SELECT * FROM agent_jobs WHERE job_key=?", (str(job_key),)).fetchone()
        else:
            row = con.execute("SELECT * FROM agent_jobs WHERE id=?", (int(job_id or 0),)).fetchone()
    return _row_to_dict(row)


def list_jobs(*, status: str | None = None, group_key: str | None = None,
              limit: int = 50, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    where: List[str] = []
    params: List[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if group_key:
        where.append("group_key = ?")
        params.append(group_key)
    sql = "SELECT * FROM agent_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with _connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    return [j for r in rows if (j := _row_to_dict(r)) is not None]


def count_jobs(*, statuses: Iterable[str] | None = None,
               db_path: Optional[Path] = None) -> Dict[str, int]:
    sql = "SELECT status, COUNT(*) AS c FROM agent_jobs"
    params: List[Any] = []
    if statuses:
        values = list(statuses)
        sql += " WHERE status IN (" + ",".join("?" for _ in values) + ")"
        params.extend(values)
    sql += " GROUP BY status"
    with _connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    return {str(r["status"]): int(r["c"] or 0) for r in rows}


def claim_next_job(*, worker_id: str, provider: str | None = None,
                   instance_id: str | None = None,
                   max_global_running: int = 1,
                   per_group_concurrency: int = 1,
                   active_workers: int = 1,
                   db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Atomically claim the next queued job if capacity allows.

    Same-group serial scheduling is enforced by default.  Returns None when no
    job can be claimed or global capacity is full.
    """
    if not worker_id:
        raise ValueError("worker_id is required")
    now = _now()
    with _WRITE_LOCK, _connect(db_path) as con:
        running = con.execute(
            "SELECT COUNT(DISTINCT worker_id) AS c FROM agent_jobs WHERE status=? AND worker_id IS NOT NULL",
            (STATUS_RUNNING,),
        ).fetchone()["c"]
        if int(running or 0) >= max(1, int(active_workers)):
            return None
        global_running_jobs = con.execute(
            "SELECT COUNT(*) AS c FROM agent_jobs WHERE status=?",
            (STATUS_RUNNING,),
        ).fetchone()["c"]
        if int(global_running_jobs or 0) >= max(1, int(max_global_running)):
            return None

        rows = con.execute(
            "SELECT * FROM agent_jobs WHERE status=? ORDER BY priority DESC, created_at ASC, id ASC LIMIT 50",
            (STATUS_QUEUED,),
        ).fetchall()
        selected = None
        for row in rows:
            payload = _json_loads(row["payload_json"])
            bound_instance = payload.get("dedicated_agent_instance_id")
            if bound_instance and str(bound_instance) != str(instance_id or ""):
                continue
            if per_group_concurrency > 0:
                same_group_running = con.execute(
                    "SELECT COUNT(*) AS c FROM agent_jobs WHERE group_key=? AND status=?",
                    (row["group_key"], STATUS_RUNNING),
                ).fetchone()["c"]
                if int(same_group_running or 0) >= int(per_group_concurrency):
                    continue
            selected = row
            break
        if selected is None:
            return None

        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, worker_id=?, provider=COALESCE(?, provider), started_at=?, error=NULL
            WHERE id=? AND status=?
            """,
            (STATUS_RUNNING, worker_id, provider, now, selected["id"], STATUS_QUEUED),
        )
        if cur.rowcount != 1:
            return None
        row = con.execute("SELECT * FROM agent_jobs WHERE id=?", (selected["id"],)).fetchone()
    return _row_to_dict(row)


def count_active_workers(*, db_path: Optional[Path] = None) -> int:
    """Number of distinct worker_id currently holding a running job."""
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT COUNT(DISTINCT worker_id) AS c FROM agent_jobs WHERE status=? AND worker_id IS NOT NULL",
            (STATUS_RUNNING,),
        ).fetchone()
    return int(row["c"] or 0)


def mark_ack_sent(job_id: int, *, db_path: Optional[Path] = None) -> bool:
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            "UPDATE agent_jobs SET ack_sent_at=COALESCE(ack_sent_at, ?) WHERE id=?",
            (_now(), int(job_id)),
        )
        return cur.rowcount == 1


def complete_job(job_id: int, result_text: str, *, db_path: Optional[Path] = None) -> bool:
    result_text = sanitize_agent_result_text(result_text)
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, result_text=?, error=NULL, finished_at=?
            WHERE id=? AND status IN (?, ?, ?)
            """,
            (STATUS_DONE, result_text, _now(), int(job_id), STATUS_RUNNING, STATUS_SUBMITTED, STATUS_AGENT_RUNNING),
        )
        return cur.rowcount == 1


def merge_payload(job_id: int, patch: Dict[str, Any], *, db_path: Optional[Path] = None) -> bool:
    """Merge metadata into a job payload without disturbing user/task fields."""
    if not isinstance(patch, dict) or not patch:
        return False
    with _WRITE_LOCK, _connect(db_path) as con:
        row = con.execute("SELECT payload_json FROM agent_jobs WHERE id=?", (int(job_id),)).fetchone()
        if not row:
            return False
        payload = _json_loads(row["payload_json"])
        payload.update(patch)
        cur = con.execute(
            "UPDATE agent_jobs SET payload_json=? WHERE id=?",
            (_json_dumps(payload), int(job_id)),
        )
        return cur.rowcount == 1


def recover_job_result(job_id: int, result_text: str, *, db_path: Optional[Path] = None) -> bool:
    """Store a recovered agent result for a failed/timed-out/done job."""
    result_text = sanitize_agent_result_text(result_text)
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, result_text=?, error=NULL, finished_at=?, send_status=?
            WHERE id=? AND status IN (?, ?, ?, ?, ?, ?)
            """,
            (
                STATUS_DONE, result_text, _now(), SEND_PENDING, int(job_id),
                STATUS_FAILED, STATUS_TIMEOUT, STATUS_EXPIRED, STATUS_DONE, STATUS_RUNNING, STATUS_CANCELLED,
            ),
        )
        return cur.rowcount == 1


def fail_job(job_id: int, error: str, *, status: str = STATUS_FAILED,
             db_path: Optional[Path] = None) -> bool:
    if status not in {STATUS_FAILED, STATUS_TIMEOUT, STATUS_CANCELLED}:
        raise ValueError("failure status must be failed, timeout, or cancelled")
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, error=?, finished_at=?, next_poll_at=NULL,
                dispatch_owner=NULL, dispatch_locked_until=NULL
            WHERE id=? AND status IN (?, ?, ?, ?, ?)
            """,
            (status, str(error or "")[:500], _now(), int(job_id),
             STATUS_QUEUED, STATUS_RUNNING, STATUS_SUBMITTED, STATUS_AGENT_RUNNING, STATUS_DISPATCHING),
        )
        return cur.rowcount == 1


def mark_sending(job_id: int, *, db_path: Optional[Path] = None) -> bool:
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            "UPDATE agent_jobs SET status=? WHERE id=? AND status=?",
            (STATUS_SENDING, int(job_id), STATUS_DONE),
        )
        return cur.rowcount == 1




def mark_sent(job_id: int, *, db_path: Optional[Path] = None) -> bool:
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            "UPDATE agent_jobs SET status=?, send_status=?, sent_at=? WHERE id=? AND status IN (?, ?, ?)",
            (STATUS_SENT, SEND_SENT, _now(), int(job_id), STATUS_DONE, STATUS_SENDING, STATUS_FAILED),
        )
        return cur.rowcount == 1


def mark_send_failed(job_id: int, error: str, *, db_path: Optional[Path] = None) -> bool:
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            "UPDATE agent_jobs SET status=?, send_status=?, error=?, finished_at=COALESCE(finished_at, ?) WHERE id=?",
            (STATUS_FAILED, SEND_FAILED, str(error or "")[:500], _now(), int(job_id)),
        )
        return cur.rowcount == 1


def mark_send_skipped(job_id: int, *, reason: str = "no_reply_action", db_path: Optional[Path] = None) -> bool:
    """Mark a successfully completed job whose agent decided not to reply as terminal.

    Applies to strict silent/escalate actions: no actual message is sent, but the
    job should not be retried or displayed as pending send. Caller must ensure the
    job was already completed (status=done) before invoking this helper.
    """
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, send_status=?, sent_at=?, error=COALESCE(error, ?), finished_at=COALESCE(finished_at, ?)
            WHERE id=? AND status=?
            """,
            (
                STATUS_SENT, SEND_SKIPPED, _now(), str(reason or "")[:500], _now(), int(job_id), STATUS_DONE,
            ),
        )
        return cur.rowcount == 1


def timeout_stale_running(*, timeout_seconds: float, db_path: Optional[Path] = None) -> int:
    cutoff = _now() - max(1.0, float(timeout_seconds))
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, error=?, finished_at=?
            WHERE status=? AND started_at IS NOT NULL AND started_at < ?
            """,
            (STATUS_TIMEOUT, "job timed out", _now(), STATUS_RUNNING, cutoff),
        )
        return int(cur.rowcount or 0)


def dismiss_job(job_id: int, *, reason: str = "dismissed", db_path: Optional[Path] = None) -> bool:
    """Mark a terminal job as dismissed (cancelled) so it no longer shows as abnormal."""
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, error=COALESCE(error, ?), finished_at=COALESCE(finished_at, ?)
            WHERE id=? AND status IN (?, ?, ?, ?)
            """,
            (
                STATUS_CANCELLED, str(reason or "dismissed")[:500], _now(), int(job_id),
                STATUS_FAILED, STATUS_TIMEOUT, STATUS_EXPIRED, STATUS_DONE,
            ),
        )
        return cur.rowcount == 1


def release_stale_dispatching(*, lock_timeout_seconds: float = 60.0,
                              db_path: Optional[Path] = None) -> int:
    """Return dispatching jobs to queued after their short dispatch lock expires."""
    cutoff = _now() - max(0.0, float(lock_timeout_seconds))
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, dispatch_owner=NULL, dispatch_locked_until=NULL,
                error=COALESCE(error, ?)
            WHERE status=? AND dispatch_locked_until IS NOT NULL AND dispatch_locked_until < ?
            """,
            (STATUS_QUEUED, "dispatch lock expired", STATUS_DISPATCHING, cutoff),
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# M5 async lifecycle helpers
# ---------------------------------------------------------------------------

def claim_dispatchable(*, worker_id: str, provider: str | None = None,
                       instance_id: str | None = None,
                       max_global_dispatching: int = 1,
                       per_group_concurrency: int = 1,
                       db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Atomically claim a queued job for dispatching (M5 async flow).
    
    Similar to claim_next_job but transitions to dispatching instead of running.
    Respects global and per-group concurrency limits.
    """
    if not worker_id:
        raise ValueError("worker_id is required")
    now = _now()
    with _WRITE_LOCK, _connect(db_path) as con:
        # Check global external capacity.  Submitted/agent_running jobs still
        # occupy external agent slots even after the short local dispatch lock
        # is released.
        active = con.execute(
            "SELECT COUNT(*) AS c FROM agent_jobs WHERE status IN (?, ?, ?, ?)",
            (STATUS_DISPATCHING, STATUS_SUBMITTED, STATUS_AGENT_RUNNING, STATUS_RUNNING),
        ).fetchone()["c"]
        if int(active or 0) >= max(1, int(max_global_dispatching)):
            return None
        
        # Find queued jobs compatible with this provider. Jobs without an
        # explicit provider may be claimed by any on-duty instance; jobs with a
        # provider must only be claimed by that provider type.
        if provider:
            rows = con.execute(
                """
                SELECT * FROM agent_jobs
                WHERE status=? AND (provider IS NULL OR provider='' OR lower(provider)=lower(?))
                ORDER BY priority DESC, created_at ASC, id ASC LIMIT 50
                """,
                (STATUS_QUEUED, str(provider)),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM agent_jobs WHERE status=? ORDER BY priority DESC, created_at ASC, id ASC LIMIT 50",
                (STATUS_QUEUED,),
            ).fetchall()
        selected = None
        for row in rows:
            payload = _json_loads(row["payload_json"])
            bound_instance = payload.get("dedicated_agent_instance_id")
            if bound_instance and str(bound_instance) != str(instance_id or ""):
                continue
            if per_group_concurrency > 0:
                same_group_active = con.execute(
                    "SELECT COUNT(*) AS c FROM agent_jobs WHERE group_key=? AND status IN (?, ?, ?, ?)",
                    (row["group_key"], STATUS_DISPATCHING, STATUS_SUBMITTED, STATUS_AGENT_RUNNING, STATUS_RUNNING),
                ).fetchone()["c"]
                if int(same_group_active or 0) >= int(per_group_concurrency):
                    continue
            selected = row
            break
        if selected is None:
            return None
        
        # Lock for dispatch
        lock_until = now + 30.0  # 30 second dispatch lock
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, dispatch_owner=?, dispatch_locked_until=?, provider=COALESCE(?, provider), error=NULL
            WHERE id=? AND status=?
            """,
            (STATUS_DISPATCHING, worker_id, lock_until, provider, selected["id"], STATUS_QUEUED),
        )
        if cur.rowcount != 1:
            return None
        row = con.execute("SELECT * FROM agent_jobs WHERE id=?", (selected["id"],)).fetchone()
    return _row_to_dict(row)


def release_dispatching(job_id: int, *, reason: str = "dispatch lock released", db_path: Optional[Path] = None) -> bool:
    """Return a dispatching job to the queued state without marking it failed.

    Use this when a transient pre-submit failure (e.g. persistence write) should
    not permanently fail the job; it will be re-dispatched on the next cycle.
    """
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, error=?, dispatch_owner=NULL, dispatch_locked_until=NULL, finished_at=NULL
            WHERE id=? AND status=?
            """,
            (STATUS_QUEUED, str(reason or "")[:500], int(job_id), STATUS_DISPATCHING),
        )
        return cur.rowcount == 1


def mark_submission_failed(job_id: int, *, external_provider: str, external_session_id: str,
                           external_user_msg_id: int | None = None, error: str = "mark_submitted failed",
                           db_path: Optional[Path] = None) -> bool:
    """Record a submitted external job whose DB state could not be updated.

    Moves the job to failed while preserving the external session info so the
    result can be audited or manually recovered. Does NOT allow re-dispatch.
    """
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, error=?, finished_at=?,
                external_provider=?, external_session_id=?, external_user_msg_id=?,
                dispatch_owner=NULL, dispatch_locked_until=NULL
            WHERE id=? AND status=?
            """,
            (
                STATUS_FAILED, str(error or "")[:500], _now(),
                str(external_provider or "")[:200], str(external_session_id or "")[:500],
                int(external_user_msg_id or 0), int(job_id), STATUS_DISPATCHING,
            ),
        )
        return cur.rowcount == 1


def mark_submitted(job_id: int, *, external_provider: str, external_session_id: str,
                   external_user_msg_id: int | None = None,
                   agent_deadline_at: float | None = None,
                   next_poll_at: float | None = None,
                   db_path: Optional[Path] = None) -> bool:
    """Mark a dispatching job as submitted to external agent."""
    now = _now()
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, external_provider=?, external_session_id=?, external_user_msg_id=?,
                external_status=?, submitted_at=?, agent_deadline_at=?, next_poll_at=?,
                dispatch_owner=NULL, dispatch_locked_until=NULL
            WHERE id=? AND status=?
            """,
            (
                STATUS_SUBMITTED, external_provider, external_session_id, external_user_msg_id,
                "submitted", now, agent_deadline_at, next_poll_at or (now + 5.0),
                int(job_id), STATUS_DISPATCHING,
            ),
        )
        return cur.rowcount == 1


def mark_agent_running(job_id: int, *, next_poll_at: float | None = None,
                       db_path: Optional[Path] = None) -> bool:
    """Mark a submitted job as agent_running (first poll confirmed it's still running)."""
    now = _now()
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, external_status=?, last_polled_at=?, next_poll_at=?, reconcile_attempts=reconcile_attempts+1
            WHERE id=? AND status=?
            """,
            (STATUS_AGENT_RUNNING, "running", now, next_poll_at or (now + 10.0), int(job_id), STATUS_SUBMITTED),
        )
        return cur.rowcount == 1


def mark_expired(job_id: int, *, reason: str = "past deadline", db_path: Optional[Path] = None) -> bool:
    """Mark a submitted/agent_running job as expired (past agent_deadline_at)."""
    now = _now()
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            """
            UPDATE agent_jobs
            SET status=?, error=?, finished_at=?, external_status=?
            WHERE id=? AND status IN (?, ?)
            """,
            (STATUS_EXPIRED, str(reason or "past deadline")[:500], now, "expired", int(job_id), STATUS_SUBMITTED, STATUS_AGENT_RUNNING),
        )
        return cur.rowcount == 1


def update_poll_state(job_id: int, *, next_poll_at: float, external_status: str | None = None,
                      db_path: Optional[Path] = None) -> bool:
    """Update next_poll_at and optionally external_status after a poll."""
    now = _now()
    with _WRITE_LOCK, _connect(db_path) as con:
        if external_status:
            cur = con.execute(
                """
                UPDATE agent_jobs
                SET last_polled_at=?, next_poll_at=?, external_status=?, reconcile_attempts=reconcile_attempts+1
                WHERE id=?
                """,
                (now, next_poll_at, external_status, int(job_id)),
            )
        else:
            cur = con.execute(
                """
                UPDATE agent_jobs
                SET last_polled_at=?, next_poll_at=?, reconcile_attempts=reconcile_attempts+1
                WHERE id=?
                """,
                (now, next_poll_at, int(job_id)),
            )
        return cur.rowcount == 1


def list_pollable(*, limit: int = 20, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List jobs that need polling (submitted/agent_running with next_poll_at <= now)."""
    now = _now()
    with _connect(db_path) as con:
        rows = con.execute(
            """
            SELECT * FROM agent_jobs
            WHERE status IN (?, ?) AND next_poll_at IS NOT NULL AND next_poll_at <= ?
            ORDER BY next_poll_at ASC
            LIMIT ?
            """,
            (STATUS_SUBMITTED, STATUS_AGENT_RUNNING, now, limit),
        ).fetchall()
    return [d for r in rows if (d := _row_to_dict(r)) is not None]


def list_sendable(*, limit: int = 20, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List jobs that are done and need sending (status=done, send_status=pending)."""
    with _connect(db_path) as con:
        rows = con.execute(
            """
            SELECT * FROM agent_jobs
            WHERE status=? AND send_status=?
            ORDER BY finished_at ASC
            LIMIT ?
            """,
            (STATUS_DONE, SEND_PENDING, limit),
        ).fetchall()
    return [d for r in rows if (d := _row_to_dict(r)) is not None]


def increment_send_attempts(job_id: int, *, db_path: Optional[Path] = None) -> bool:
    """Increment send_attempts counter."""
    with _WRITE_LOCK, _connect(db_path) as con:
        cur = con.execute(
            "UPDATE agent_jobs SET send_attempts=send_attempts+1 WHERE id=?",
            (int(job_id),),
        )
        return cur.rowcount == 1


if __name__ == "__main__":
    print(json.dumps(count_jobs(), ensure_ascii=False, indent=2))
