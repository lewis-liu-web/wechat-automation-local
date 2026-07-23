"""Focused tests for reliable_pipeline escalation and terminal-failure listing.

These drive the durable pipeline against a temp SQLite database and assert
that ``list_escalations`` and ``list_recent_terminal_failures`` return the
expected rows after ``apply_agent_result`` (escalate) and ``fail_job``.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import json
import pytest
import reliable_pipeline as pipeline


def _event(local_id, text="hello"):
    return {
        "local_id": local_id,
        "message_content": text,
        "sender_username": "wxid_sender",
        "sender_display_name": "Sender",
        "local_type": 1,
    }


def _event_id(target_id, local_id):
    return "wx:message_0.db:Msg_%s:%d" % (target_id, local_id)


def _persist(db, local_id=1, text="hello", target_id="wxid_target"):
    event_id = _event_id(target_id, local_id)
    return pipeline.persist_inbound_event(
        event_id=event_id,
        target_id=target_id,
        group_key=target_id,
        sender_id="wxid_sender",
        local_id=local_id,
        payload=_event(local_id, text),
        received_at=float(local_id),
        db_path=db,
    )


def _ready_job(db, local_id=1, target_id="wxid_target"):
    _persist(db, local_id, target_id=target_id)
    pipeline.add_event_to_window(
        _event_id(target_id, local_id),
        debounce_seconds=1,
        max_window_seconds=10,
        now=local_id,
        db_path=db,
    )
    turns = pipeline.close_due_windows(now=local_id + 1, db_path=db)
    assert len(turns) == 1
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    return jobs[0]


class TestListEscalations:
    def test_list_escalations_returns_escalation_after_apply_agent_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite"
            job = _ready_job(db)

            contract = pipeline.AgentResultContract(
                action="escalate",
                reply_text="",
                reason_code="needs_human_review",
                risk_level="high",
            )
            pipeline.apply_agent_result(
                job_id=job["id"],
                result=contract,
                final_filter=lambda x: x,
                db_path=db,
                now=10.0,
            )

            rows = pipeline.list_escalations(limit=10, db_path=db)
            assert len(rows) == 1
            row = rows[0]
            assert row["job_id"] == job["id"]
            assert row["target_id"] == "wxid_target"
            assert row["group_key"] == "wxid_target"
            assert row["reason_code"] == "needs_human_review"
            assert row["risk_level"] == "high"
            assert row["created_at"] == 10.0
            assert row["job_status"] == pipeline.JOB_ESCALATED

    def test_list_escalations_empty_when_no_escalations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite"
            _ready_job(db)
            rows = pipeline.list_escalations(limit=10, db_path=db)
            assert rows == []

    def test_list_escalations_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite"
            for i in range(1, 4):
                job = _ready_job(db, local_id=i, target_id="t%d" % i)
                contract = pipeline.AgentResultContract(
                    action="escalate",
                    reply_text="",
                    reason_code="r%d" % i,
                    risk_level="medium",
                )
                pipeline.apply_agent_result(
                    job_id=job["id"],
                    result=contract,
                    final_filter=lambda x: x,
                    db_path=db,
                    now=10.0 + i,
                )
            rows = pipeline.list_escalations(limit=2, db_path=db)
            assert len(rows) == 2
            # Ordered by created_at DESC.
            assert rows[0]["reason_code"] == "r3"
            assert rows[1]["reason_code"] == "r2"


class TestListRecentTerminalFailures:
    def test_list_recent_terminal_failures_includes_failed_and_escalated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite"

            # Failed job via final safety filter.
            job_failed = _ready_job(db, local_id=1, target_id="t_failed")
            pipeline.apply_agent_result(
                job_id=job_failed["id"],
                result=pipeline.AgentResultContract(
                    action="reply",
                    reply_text="blocked",
                    reason_code="filter_hit",
                    risk_level="low",
                ),
                final_filter=lambda x: "",  # empty -> fails the job
                db_path=db,
                now=20.0,
            )

            # Escalated job.
            job_escalated = _ready_job(db, local_id=2, target_id="t_escalated")
            pipeline.apply_agent_result(
                job_id=job_escalated["id"],
                result=pipeline.AgentResultContract(
                    action="escalate",
                    reply_text="",
                    reason_code="human_needed",
                    risk_level="high",
                ),
                final_filter=lambda x: x,
                db_path=db,
                now=30.0,
            )

            rows = pipeline.list_recent_terminal_failures(limit=10, db_path=db)
            assert len(rows) == 2
            # Most recent first.
            assert rows[0]["id"] == job_escalated["id"]
            assert rows[0]["status"] == pipeline.JOB_ESCALATED
            assert rows[0]["reason_code"] == "human_needed"
            assert rows[1]["id"] == job_failed["id"]
            assert rows[1]["status"] == pipeline.JOB_FAILED
            assert rows[1]["reason_code"] == "filter_hit"

    def test_list_recent_terminal_failures_includes_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite"
            job = _ready_job(db, local_id=1)
            pipeline.fail_job(job_id=job["id"], error="took too long", db_path=db, now=5.0)
            # Promote to timeout directly.
            with pipeline._connect(db) as con:
                con.execute(
                    "UPDATE turn_jobs SET status=?, error=? WHERE id=?",
                    (pipeline.JOB_TIMEOUT, "timeout", job["id"]),
                )
            rows = pipeline.list_recent_terminal_failures(limit=10, db_path=db)
            assert len(rows) == 1
            assert rows[0]["status"] == pipeline.JOB_TIMEOUT
            assert rows[0]["error"] == "timeout"

    def test_list_recent_terminal_failures_empty_when_no_terminal_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite"
            _ready_job(db)
            rows = pipeline.list_recent_terminal_failures(limit=10, db_path=db)
            assert rows == []

    def test_list_recent_terminal_failures_limit_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pipeline.sqlite"
            for i in range(1, 4):
                job = _ready_job(db, local_id=i, target_id="t%d" % i)
                pipeline.fail_job(job_id=job["id"], error="e%d" % i, db_path=db, now=10.0 + i)
            rows = pipeline.list_recent_terminal_failures(limit=2, db_path=db)
            assert len(rows) == 2
            # finished_at DESC.
            assert rows[0]["error"] == "e3"
            assert rows[1]["error"] == "e2"


class TestEscalationControlAPIRoutes:
    def _patch_cfg(self, monkeypatch, tmp_path, db_path):
        import control_api  # noqa: E402
        cfg = {
            "targets": [],
            "default_triggers": [],
            "knowledge_bases": {},
            "reliable_pipeline": {"enabled": False, "db_path": db_path},
        }
        monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: cfg)
        return control_api

    def test_escalations_route_returns_items_and_count(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "rp.sqlite")
        control_api = self._patch_cfg(monkeypatch, tmp_path, db_path)
        db = Path(db_path)

        job = _ready_job(db, local_id=1, target_id="t1")
        pipeline.apply_agent_result(
            job_id=job["id"],
            result=pipeline.AgentResultContract(
                action="escalate",
                reply_text="",
                reason_code="api_test",
                risk_level="high",
            ),
            final_filter=lambda x: x,
            db_path=db,
            now=2.0,
        )

        handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
        body_b, status, _ = handler._route("GET", "/reliable-pipeline/escalations", {}, {})
        resp = json.loads(body_b.decode("utf-8"))
        assert status == 200
        assert resp["ok"] is True
        assert resp["count"] == 1
        assert resp["items"][0]["reason_code"] == "api_test"
        assert resp["items"][0]["job_status"] == pipeline.JOB_ESCALATED

    def test_terminal_failures_route_returns_items_and_count(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "rp.sqlite")
        control_api = self._patch_cfg(monkeypatch, tmp_path, db_path)
        db = Path(db_path)

        job = _ready_job(db, local_id=1, target_id="t1")
        pipeline.fail_job(job_id=job["id"], error="boom", db_path=db, now=2.0)

        handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
        body_b, status, _ = handler._route("GET", "/reliable-pipeline/terminal-failures", {}, {})
        resp = json.loads(body_b.decode("utf-8"))
        assert status == 200
        assert resp["ok"] is True
        assert resp["count"] == 1
        assert resp["items"][0]["error"] == "boom"
        assert resp["items"][0]["status"] == pipeline.JOB_FAILED

    def test_escalations_route_honors_limit(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "rp.sqlite")
        control_api = self._patch_cfg(monkeypatch, tmp_path, db_path)
        db = Path(db_path)

        for i in range(1, 4):
            job = _ready_job(db, local_id=i, target_id="t%d" % i)
            pipeline.apply_agent_result(
                job_id=job["id"],
                result=pipeline.AgentResultContract(
                    action="escalate",
                    reply_text="",
                    reason_code="r%d" % i,
                    risk_level="medium",
                ),
                final_filter=lambda x: x,
                db_path=db,
                now=2.0 + i,
            )

        handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
        body_b, status, _ = handler._route("GET", "/reliable-pipeline/escalations", {"limit": ["2"]}, {})
        resp = json.loads(body_b.decode("utf-8"))
        assert status == 200
        assert resp["count"] == 2
        assert resp["items"][0]["reason_code"] == "r3"
        assert resp["items"][1]["reason_code"] == "r2"
