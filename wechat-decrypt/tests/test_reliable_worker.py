"""Tests for the durable Stage 1 worker (``reliable_worker``).

These tests are entirely mock-driven: no real provider, no real WeChat
sender, no real target registry side effects.  They drive the worker
through ``reliable_pipeline`` against a temp SQLite database and assert
on the durable effects (job status, outbox rows, retry/dead-letter,
test-mode gate, hard target precondition, strict contract parsing).
"""
import json
import os
import sys
import tempfile
import time as _stdlib_time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reliable_pipeline as pipeline
import reliable_worker as worker
from wechat_bot_monitor import build_event_payload

# Bounded in-process retry in process_once calls time.sleep between
# attempts.  Tests must not pay real seconds for that backoff, so we
# replace the time module reference in the worker namespace only.
# (Assigning worker.time.sleep would mutate the shared time module and
# poison every other test collected in the same run.)
worker.time = SimpleNamespace(
    time=_stdlib_time.time,
    monotonic=_stdlib_time.monotonic,
    sleep=lambda *_a, **_kw: None,
)


# --- Helpers ----------------------------------------------------------------

def _event(local_id: int, text: str = "hello", sender: str = "wxid_sender") -> dict:
    return {
        "local_id": local_id,
        "message_content": text,
        "sender_username": sender,
        "sender_display_name": "Sender",
        "local_type": 1,
    }


def _seed_turn(db: Path, *, target_id: str = "wxid_target", sender: str = "wxid_sender",
               group_key: str = "wxid_target", last_local_id: int = 1,
               mention_name: str = None) -> dict:
    pipeline.persist_inbound_event(
        event_id="wx:message_0.db:Msg_%s:%d" % (target_id, last_local_id),
        target_id=target_id,
        group_key=group_key,
        sender_id=sender,
        local_id=last_local_id,
        payload=_event(last_local_id),
        received_at=float(last_local_id),
        db_path=db,
    )
    pipeline.add_event_to_window(
        "wx:message_0.db:Msg_%s:%d" % (target_id, last_local_id),
        debounce_seconds=1, max_window_seconds=10,
        now=float(last_local_id), db_path=db,
    )
    turns = pipeline.close_due_windows(now=float(last_local_id) + 1, db_path=db)
    assert len(turns) == 1
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    return jobs[0]


def _seed_turn_with_binding(db: Path, *, instance_id: str,
                            target_id: str = "wxid_target", sender: str = "wxid_sender",
                            group_key: str = "wxid_target", last_local_id: int = 1) -> dict:
    """Seed a turn whose target snapshot carries a dedicated_agent_instance_id."""
    target = {
        "name": target_id,
        "username": target_id,
        "db": "message_0.db",
        "table": "Msg_%s" % target_id,
        "dedicated_agent_instance_id": instance_id,
    }
    payload = {
        "message": _event(last_local_id),
        "target": target,
        "event_context": {},
        "target_policy": {},
        "schema_version": 1,
    }
    pipeline.persist_inbound_event(
        event_id="wx:message_0.db:Msg_%s:%d" % (target_id, last_local_id),
        target_id=target_id,
        group_key=group_key,
        sender_id=sender,
        local_id=last_local_id,
        payload=payload,
        received_at=float(last_local_id),
        db_path=db,
    )
    pipeline.add_event_to_window(
        "wx:message_0.db:Msg_%s:%d" % (target_id, last_local_id),
        debounce_seconds=1, max_window_seconds=10,
        now=float(last_local_id), db_path=db,
    )
    turns = pipeline.close_due_windows(now=float(last_local_id) + 1, db_path=db)
    assert len(turns) == 1
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    return jobs[0]


def _agent_result(*, ok: bool = True, status: str = "done", reply_text: str = "",
                  raw: dict = None, error: str = "",
                  provider: str = "mock", worker_id: str = "mock-1") -> SimpleNamespace:
    """Construct an AgentResult-shaped object via SimpleNamespace.

    The worker duck-types ``AgentResult`` by checking ``ok`` and
    ``status``; ``reply_text``, ``raw``, and ``error`` are read by
    attribute access.  Tests must avoid importing the real
    ``agent_provider`` module: that file currently has unrelated
    in-progress modifications that can break collection.
    """
    return SimpleNamespace(
        ok=ok, status=status, reply_text=reply_text, raw=raw,
        error=error, latency=0.0, provider=provider, worker_id=worker_id,
    )


def _contract_dict(action: str, text: str = "", reason: str = "ok", risk: str = "low") -> dict:
    return {
        "schema_version": pipeline.AGENT_RESULT_VERSION,
        "action": action,
        "reply_text": text,
        "reason_code": reason,
        "risk_level": risk,
    }


def _pass_filter(text: str) -> str:
    return text


# --- Fixtures ----------------------------------------------------------------

class FakeProvider:
    """Provider stand-in that returns a preset AgentResult or raises.

    If ``trace`` is provided, ``run`` writes it as the MCP trace artifact
    at the worker-provided path before returning/raising, simulating the
    MCP server writing the trace file.
    """

    def __init__(self, *, result=None, raises: Exception = None,
                 capture: list = None, name: str = "mock",
                 trace: dict = None, trace_id_override: str = None) -> None:
        self._result = result
        self._raises = raises
        self._capture = capture if capture is not None else []
        self.name = name
        self._trace = trace
        self._trace_id_override = trace_id_override
        self._timeouts = []

    def run(self, job, timeout=None):
        self._timeouts.append(timeout)
        self._capture.append(job)
        if self._trace is not None:
            payload = job.get("payload") or {}
            trace_id = payload.get("_knowledge_trace_id")
            trace_dir = payload.get("_knowledge_trace_dir")
            if trace_id and trace_dir:
                actual_trace_id = self._trace_id_override or trace_id
                trace = dict(self._trace)
                trace["trace_id"] = actual_trace_id
                # Always write at the worker-expected path; a wrong trace_id
                # inside the file is an out-of-scope artifact.
                path = Path(trace_dir) / f"{trace_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(trace), encoding="utf-8")
        if self._raises is not None:
            raise self._raises
        return self._result


def _fake_send_result(*, ok: bool, reason: str = "", mode: str = "foreground",
                      attempted=None, confirmed=None) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, reason=reason, mode=mode,
                           attempted=list(attempted or []), confirmed=confirmed,
                           detail={})


# --- Tests -------------------------------------------------------------------

def test_process_once_returns_empty_when_no_job_available():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        provider = FakeProvider(result=_agent_result(reply_text="ignored"))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db)
        assert out == {"status": "empty", "job": None, "outbox": None}
        assert provider._capture == []


def test_process_once_requires_final_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider()
        try:
            worker.process_once(provider, "worker", {}, db_path=db)
        except worker.FinalFilterRequired:
            pass
        else:
            raise AssertionError("expected FinalFilterRequired when final_filter missing")
        # No provider call should have happened and the job must remain queued.
        assert provider._capture == []
        counts = pipeline.counts(db_path=db)
        assert counts["turn_jobs"] == {"queued": 1}


def test_process_once_reply_creates_outbox_when_filter_passes():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            reply_text="",  # reply_text is ignored when raw is present
            raw={"agent_result": _contract_dict("reply", "safe reply", "ok")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"] is not None
        assert out["outbox"]["status"] == pipeline.OUTBOX_PENDING
        assert out["outbox"]["reply_text"] == "safe reply"
        # The provider job carried the reliable_result_contract flag and a
        # non-empty prompt built from the durable event (legacy flat shape).
        assert provider._capture[0]["payload"]["reliable_result_contract"] is True
        assert provider._capture[0]["payload"]["prompt"] == "Sender: hello"
        assert provider._capture[0]["target_id"] == "wxid_target"


def test_process_once_silent_creates_no_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("silent", "", "smalltalk")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_DONE


def test_process_once_escalate_creates_no_outbox_and_marks_escalation():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("escalate", "", "human", "high")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_ESCALATED
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM escalations").fetchone()
        assert row is not None and row["reason_code"] == "human"


def test_process_once_provider_ok_false_fails_job_with_no_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(ok=False, status="error", error="/secret/path: SECRET_TOKEN"))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert "SECRET_TOKEN" not in (out["job"]["error"] or "")
        assert "/secret/path" not in (out["job"]["error"] or "")
        # Provider diagnostics must be a fixed safe schema with no secrets.
        diag = out["job"].get("provider_diagnostics") or {}
        assert diag == {"result_type": "SimpleNamespace", "ok": False, "status": "error", "stage": "unknown", "retry_count": 0}
        assert "SECRET_TOKEN" not in str(diag)
        assert "/secret/path" not in str(diag)
        # Persisted JSON must also exclude secrets.
        with pipeline._connect(db) as con:
            raw = con.execute("SELECT provider_diagnostics_json FROM turn_jobs WHERE id=?", (out["job"]["id"],)).fetchone()[0]
        assert "SECRET_TOKEN" not in raw
        assert "/secret/path" not in raw
        assert pipeline.counts(db_path=db).get("send_outbox", {}) == {}


def test_process_once_provider_failed_stage_persists_to_diagnostics_json():
    """Strict provider failures that carry a whitelisted ``stage`` enum must
    have that stage persisted into ``turn_jobs.provider_diagnostics_json``
    without leaking any raw stdout or error text.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(
            result=_agent_result(
                ok=False,
                status="failed",
                raw={"stage": "contract_violation", "stdout": "rogue text"},
            )
        )
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        diag = out["job"].get("provider_diagnostics") or {}
        assert diag.get("stage") == "contract_violation"
        assert diag.get("result_type") == "SimpleNamespace"
        # No raw stdout or provider body may leak into the persisted column.
        assert "rogue text" not in str(diag)
        with pipeline._connect(db) as con:
            raw = con.execute(
                "SELECT provider_diagnostics_json FROM turn_jobs WHERE id=?",
                (out["job"]["id"],),
            ).fetchone()[0]
        parsed = json.loads(raw)
        assert parsed.get("stage") == "contract_violation"
        assert "rogue text" not in raw


def test_process_once_provider_timeout_marks_failed_with_no_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(ok=False, status="timeout", error="slow"))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        # Spec: timeout is a non-retryable failure (provider did not produce
        # a usable document; we must not requeue indefinitely).
        assert out["job"]["status"] == pipeline.JOB_FAILED


def test_process_once_pure_text_reply_text_is_rejected_with_no_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            ok=True, status="done", reply_text="This is plain stdout, not JSON.", raw=None,
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert pipeline.counts(db_path=db).get("send_outbox", {}) == {}


def test_process_once_hermes_box_and_ansi_are_rejected_with_no_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            ok=True, status="done",
            reply_text="╭─ ⚕ Hermes ─╮\n这是回复\n╰─╯",
            raw={"agent_result": "╭─ ⚕ Hermes ─╮\n这是回复\n╰─╯"},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED


def test_process_once_provider_exception_fails_job_and_releases_lease():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(raises=RuntimeError("/secret/path: SECRET_TOKEN leaked"))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert "SECRET_TOKEN" not in (out["job"]["error"] or "")
        assert "/secret/path" not in (out["job"]["error"] or "")
        assert out["job"]["error"] == "provider run failed"
        # The lease must be released so a new worker can re-claim.
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT lease_owner FROM turn_jobs").fetchone()
        assert row["lease_owner"] in (None, "")


def test_process_once_non_agent_result_fails_job():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        # Plain object missing ``ok`` / ``status`` attributes.
        bad = SimpleNamespace(reply_text="hi")
        provider = FakeProvider(result=bad)
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED


def test_process_once_non_agent_result_wrong_types_fails_job():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        bad = SimpleNamespace(ok="true", status=42, reply_text="hi")
        provider = FakeProvider(result=bad)
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED


def test_process_once_invalid_contract_does_not_leak_provider_reply_text():
    """A malformed provider body with a secret must not appear in the failure."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        secret = "C:/private/path SECRET_TOKEN"
        bad = SimpleNamespace(ok=True, status="done", reply_text=secret, raw=None)
        provider = FakeProvider(result=bad)
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert secret not in str(out)
        # Persisted error must be the fixed safe marker, never the secret.
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM turn_jobs WHERE id=?", (int(out["job"]["id"]),)).fetchone()
        item = pipeline._row(row)
        assert item is not None
        assert secret not in (item.get("error") or "")
        assert secret not in (item.get("result_json") or "")
        assert "invalid agent result contract" in (item.get("error") or "")


def test_process_once_factory_receives_bound_instance_id():
    """The worker must pass the durable snapshot binding to provider_factory."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn_with_binding(db, instance_id="hermes-worker-3")
        captured = []
        def factory(instance_id):
            captured.append(instance_id)
            return FakeProvider(result=_agent_result(
                raw={"agent_result": _contract_dict("silent", "", "smalltalk")},
            ))
        fallback = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("silent", "", "smalltalk")},
        ))
        out = worker.process_once(fallback, "worker", {},
                                  provider_factory=factory,
                                  final_filter=_pass_filter, db_path=db, now=10.0,
                                  instance_id="hermes-worker-3")
        assert out["status"] == "applied"
        assert captured == ["hermes-worker-3"]


def test_process_once_factory_failure_falls_back_to_legacy_provider():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn_with_binding(db, instance_id="hermes-worker-3")
        def factory(instance_id):
            raise RuntimeError("provider unavailable")
        fallback = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("silent", "", "smalltalk")},
        ))
        out = worker.process_once(fallback, "worker", {},
                                  provider_factory=factory,
                                  final_filter=_pass_filter, db_path=db, now=10.0,
                                  instance_id="hermes-worker-3")
        assert out["status"] == "applied"


def test_process_once_factory_none_binding_still_calls_factory():
    """When no binding is present, factory is called with None."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        captured = []
        def factory(instance_id):
            captured.append(instance_id)
            return FakeProvider(result=_agent_result(
                raw={"agent_result": _contract_dict("silent", "", "smalltalk")},
            ))
        fallback = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("silent", "", "smalltalk")},
        ))
        out = worker.process_once(fallback, "worker", {},
                                  provider_factory=factory,
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert captured == [None]


def test_process_once_empty_filter_response_fails_job_with_no_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("reply", "would reply", "ok")},
        ))

        def _block(_text: str) -> str:
            return ""

        out = worker.process_once(provider, "worker", {},
                                  final_filter=_block, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert pipeline.counts(db_path=db).get("send_outbox", {}) == {}


# --- Status guard: only successful terminal runs apply the contract ---------

def test_process_once_status_submitted_with_valid_contract_still_fails():
    """ok=True alone is not enough. status='submitted' is an in-progress
    marker, not a successful terminal run. The contract must not be applied
    and no outbox is created, even when the document is a valid JSON
    AgentResult contract.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            ok=True, status="submitted",
            raw={"agent_result": _contract_dict("reply", "would reply", "ok")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert provider._capture[0]["payload"]["reliable_result_contract"] is True
        assert pipeline.counts(db_path=db).get("send_outbox", {}) == {}


def test_process_once_status_running_with_valid_contract_still_fails():
    """Same as above for status='running' (poll-style async providers)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            ok=True, status="running",
            raw={"agent_result": _contract_dict("reply", "would reply", "ok")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert pipeline.counts(db_path=db).get("send_outbox", {}) == {}


def test_process_once_ok_true_with_done_status_and_valid_contract_succeeds():
    """The positive path: ok=True, status='done', valid contract → outbox."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            ok=True, status="done",
            raw={"agent_result": _contract_dict("reply", "safe reply", "ok")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"] is not None
        assert out["outbox"]["status"] == pipeline.OUTBOX_PENDING


def test_process_once_ok_true_with_completed_status_and_valid_contract_succeeds():
    """status='completed' is also an accepted terminal success status."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = FakeProvider(result=_agent_result(
            ok=True, status="completed",
            raw={"agent_result": _contract_dict("reply", "safe reply", "ok")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"] is not None
        assert out["outbox"]["status"] == pipeline.OUTBOX_PENDING


# --- reply_text JSON contract fallback is always rejected ---------------

def test_process_once_test_provider_name_ignores_reply_text_and_accepts_raw_agent_result():
    """A provider whose name used to trigger the reply_text fallback (e.g.
    ``name='echo'``) must now be treated like any production provider:
    a JSON contract in ``reply_text`` is rejected, but a proper contract
    in ``raw['agent_result']`` is still accepted.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        contract_text = '{"schema_version": 1, "action": "reply", "reply_text": "compat reply", "reason_code": "x", "risk_level": "low"}'

        # 1. JSON in reply_text with no raw contract is rejected.
        provider_no_raw = FakeProvider(
            result=_agent_result(ok=True, status="done", reply_text=contract_text, raw=None),
            name="echo",
        )
        out = worker.process_once(provider_no_raw, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED

        # 2. Proper raw['agent_result'] contract is accepted regardless of provider name.
        _seed_turn(db, last_local_id=2)  # create a fresh job
        contract = _contract_dict("reply", "raw contract reply", "ok", "low")
        provider_raw = FakeProvider(
            result=_agent_result(ok=True, status="done", reply_text="unused",
                                 raw={"agent_result": contract}),
            name="echo",
        )
        out = worker.process_once(provider_raw, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"]["reply_text"] == "raw contract reply"


def test_process_once_test_provider_config_flag_ignores_reply_text_and_accepts_raw_agent_result():
    """A legacy ``config['reliable_pipeline']['test_provider_compat']=True``
    flag must be ignored: reply_text JSON is always rejected, but a proper
    ``raw['agent_result']`` contract still succeeds.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        contract_text = '{"schema_version": 1, "action": "reply", "reply_text": "via cfg", "reason_code": "x", "risk_level": "low"}'
        cfg = {"reliable_pipeline": {"test_provider_compat": True}}

        # 1. JSON in reply_text with no raw contract is rejected.
        provider_no_raw = FakeProvider(
            result=_agent_result(ok=True, status="done", reply_text=contract_text, raw=None),
            name="hermes",
        )
        out = worker.process_once(provider_no_raw, "worker", cfg,
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED

        # 2. Proper raw['agent_result'] contract is accepted regardless of the flag.
        _seed_turn(db, last_local_id=2)  # create a fresh job
        contract = _contract_dict("reply", "raw contract reply", "ok", "low")
        provider_raw = FakeProvider(
            result=_agent_result(ok=True, status="done", reply_text="unused",
                                 raw={"agent_result": contract}),
            name="hermes",
        )
        out = worker.process_once(provider_raw, "worker", cfg,
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"]["reply_text"] == "raw contract reply"


def test_process_once_production_provider_rejects_contract_in_reply_text():
    """A production-named provider (e.g. ``hermes``) MUST NOT accept a
    contract delivered through ``reply_text``.  Display text is never
    a contract channel for production providers — the contract must
    travel through ``raw['agent_result']``.  A misbehaving model that
    puts a JSON document into display text must be rejected, with no
    outbox created.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        contract_text = '{"schema_version": 1, "action": "reply", "reply_text": "bypass attempt", "reason_code": "x", "risk_level": "low"}'
        provider = FakeProvider(
            result=_agent_result(ok=True, status="done",
                                reply_text=contract_text, raw=None),
            name="hermes",  # production provider
        )
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED
        assert pipeline.counts(db_path=db).get("send_outbox", {}) == {}


def test_process_once_unknown_provider_name_rejects_contract_in_reply_text():
    """Arbitrary provider names must also reject display-text contracts;
    the contract must travel through ``raw['agent_result']``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        contract_text = '{"schema_version": 1, "action": "reply", "reply_text": "rogue", "reason_code": "x", "risk_level": "low"}'
        provider = FakeProvider(
            result=_agent_result(ok=True, status="done",
                                reply_text=contract_text, raw=None),
            name="rogue-provider",
        )
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED


def test_process_once_provider_without_name_attribute_rejects_reply_text():
    """A provider that does not expose ``name`` and without the config
    flag set must still reject display-text contracts.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        contract_text = '{"schema_version": 1, "action": "reply", "reply_text": "no name", "reason_code": "x", "risk_level": "low"}'
        provider = FakeProvider(
            result=_agent_result(ok=True, status="done",
                                reply_text=contract_text, raw=None),
        )
        # Defang the default FakeProvider name so the test is neutral.
        provider.name = "production-ish"
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert out["outbox"] is None
        assert out["job"]["status"] == pipeline.JOB_FAILED


# --- send_once ---------------------------------------------------------------

def test_send_once_returns_empty_when_no_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", {}, db_path=db)
        assert out == {"status": "empty", "processed": [], "skipped": []}
        sender.assert_not_called()


def test_send_once_confirmed_send_marks_outbox_sent():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [target]}
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=True, reason="confirmed",
                                                              attempted=["uia_send"],
                                                              confirmed=True)) as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        assert out["status"] == "processed"
        assert out["skipped"] == []
        assert len(out["processed"]) == 1
        rec = out["processed"][0]
        assert rec["ok"] is True
        assert rec["row"]["status"] == pipeline.OUTBOX_SENT
        # The recorded status is durable; a second claim yields nothing.
        with mock.patch.object(worker, "send_reply_detailed") as sender2:
            again = worker.send_once("sender", cfg, db_path=db, now=13.0)
        assert again == {"status": "empty", "processed": [], "skipped": []}
        sender2.assert_not_called()
        # The sender was called once with the configured target and reply text.
        kwargs = sender.call_args.kwargs
        assert kwargs["target"] is target
        assert kwargs["mention_name"] in (None, "")
        assert kwargs["before_local_id"] is not None


def test_send_once_failed_confirmation_uses_exponential_backoff():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            max_send_attempts=5,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [target]}
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=False, reason="no_confirm",
                                                              confirmed=False)):
            # T=11: first send attempt fails -> row is rescheduled.
            out = worker.send_once("sender", cfg, db_path=db, now=11.0)
        assert out["processed"][0]["ok"] is False
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY
        assert row["attempts"] == 1
        # next_attempt_at must follow retry_base * 2 ** (attempts-1) = 5s.
        expected_next = 11.0 + 5.0 * (2 ** 0)
        assert abs(row["next_attempt_at"] - expected_next) < 1e-6
        assert pipeline.list_dead_letters(db_path=db) == []

        # T=17 (>16 = 11+5): the next attempt is claimable.
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=True, reason="ok",
                                                              confirmed=True)):
            out2 = worker.send_once("sender", cfg, db_path=db, now=17.0)
        assert out2["processed"][0]["ok"] is True
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row2 = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row2["status"] == pipeline.OUTBOX_SENT


def test_send_once_dead_letters_when_max_attempts_reached():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            max_send_attempts=2,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [target]}
        # First attempt: SendResult.ok=False -> retry (attempts=1, max=2).
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=False, reason="t1",
                                                              confirmed=False)):
            out = worker.send_once("sender", cfg, db_path=db, now=11.0)
        assert out["processed"][0]["ok"] is False
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY
        assert row["attempts"] == 1

        # Advance time past next_attempt_at and run again: attempts=2=max -> dead_letter.
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=False, reason="t2",
                                                              confirmed=False)):
            out2 = worker.send_once("sender", cfg, db_path=db, now=row["next_attempt_at"] + 1)
        assert out2["processed"][0]["ok"] is False
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row2 = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row2["status"] == pipeline.OUTBOX_DEAD
        assert row2["attempts"] == 2
        assert pipeline.list_dead_letters(db_path=db)[0]["id"] == outbox_id

        # Dead-lettered rows are never re-claimed.
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out3 = worker.send_once("sender", cfg, db_path=db, now=999999)
        assert out3 == {"status": "empty", "processed": [], "skipped": []}
        sender.assert_not_called()


# --- Hard target precondition (always, not only test_target_only) ----------

def test_send_once_rejects_when_target_missing_from_config_without_test_mode():
    """Hard precondition: the sender must NOT be invoked unless the target
    is present in ``config['targets']``.  ``send_reply_detailed(target=None)``
    would otherwise fall back to whatever chat WeChat is currently showing,
    which could route the message into the wrong active conversation.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        # Empty targets list with test_target_only off: the sender must
        # still be refused because the target is not configured.
        cfg = {"targets": []}
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_not_called()
        assert len(out["skipped"]) == 1
        skip = out["skipped"][0]
        assert skip["outbox_id"] == outbox_id
        assert "target not configured" in skip["error"]
        assert out["processed"] == []
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY
        assert "target not configured" in (row["error"] or "")
        # Lease is cleared so the row can be re-claimed.
        assert row["lease_owner"] in (None, "")


def test_send_once_rejects_when_config_omits_targets_key():
    """An entirely missing ``targets`` key is treated the same as empty."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", {}, db_path=db, now=12.0)
        sender.assert_not_called()
        assert len(out["skipped"]) == 1
        assert "target not configured" in out["skipped"][0]["error"]
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY


def test_send_once_rejects_when_target_id_not_in_config_targets():
    """A target list that does not contain the outbox target_id is also rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        # Config has targets but none match the outbox's target_id.
        unrelated = {"name": "随便聊聊群", "username": "wxid_other",
                     "db": "message_0.db", "table": "Msg_wxid_other"}
        cfg = {"targets": [unrelated]}
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_not_called()
        assert len(out["skipped"]) == 1
        assert "target not configured" in out["skipped"][0]["error"]
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY


# --- test_target_only --------------------------------------------------------

def test_send_once_test_target_only_rejects_non_allowed_target():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        wrong = {"name": "随便聊聊群", "username": "wxid_target",
                 "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [wrong], "reliable_pipeline": {"test_target_only": True}}
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_not_called()
        assert len(out["skipped"]) == 1
        skip = out["skipped"][0]
        assert skip["outbox_id"] == outbox_id
        assert "test_mode_target_rejected" in skip["error"]
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY
        assert "test_mode_target_rejected" in (row["error"] or "")


def test_send_once_test_target_only_rejects_missing_target():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        cfg = {"targets": [], "reliable_pipeline": {"test_target_only": True}}
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_not_called()
        assert len(out["skipped"]) == 1
        assert "not configured" in out["skipped"][0]["error"]
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY


def test_send_once_test_target_only_allows_exact_name():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [target], "reliable_pipeline": {"test_target_only": True}}
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=True, reason="ok",
                                                              confirmed=True)) as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_called_once()
        assert out["processed"][0]["ok"] is True
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_SENT


def test_send_once_test_target_only_rejects_fuzzy_name_match():
    """A near-miss name (suffix/prefix whitespace, case, suffix) must NOT slip through."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        near_misses = ["bot群聊测试 ", " bot群聊测试", "bot群聊测试\n", "Bot群聊测试", "bot群聊测试1"]
        for near_name in near_misses:
            wrong = {"name": near_name, "username": "wxid_target",
                     "db": "message_0.db", "table": "Msg_wxid_target"}
            cfg = {"targets": [wrong], "reliable_pipeline": {"test_target_only": True}}
            # Reset the durable row to pending between iterations.
            with pipeline._connect(db) as con:  # type: ignore[attr-defined]
                con.execute(
                    "UPDATE send_outbox SET status=?, attempts=0, next_attempt_at=0, lease_owner=NULL, lease_until=NULL, sent_at=NULL, dead_at=NULL, error=NULL WHERE id=?",
                    (pipeline.OUTBOX_PENDING, outbox_id),
                )
            with mock.patch.object(worker, "send_reply_detailed") as sender:
                out = worker.send_once("sender", cfg, db_path=db, now=12.0)
            sender.assert_not_called()
            assert out["skipped"] and "test_mode_target_rejected" in out["skipped"][0]["error"], near_name


def test_send_once_test_target_only_allows_configured_allowlist():
    """A configured allowlist permits exactly the named targets."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db, target_id="family", group_key="family")
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]
        target = {"name": "family", "username": "family",
                  "db": "message_0.db", "table": "Msg_family"}
        cfg = {"targets": [target], "reliable_pipeline": {
            "test_target_only": True,
            "test_target_names": ["bot群聊测试", "family"],
        }}
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=True, reason="ok",
                                                              confirmed=True)) as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_called_once()
        assert out["processed"] and out["processed"][0]["ok"] is True


def test_send_once_test_target_only_rejects_unlisted_target_with_allowlist():
    """A target not in the configured allowlist is rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db, target_id="other_group", group_key="other_group")
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]
        wrong = {"name": "other_group", "username": "other_group",
                 "db": "message_0.db", "table": "Msg_other_group"}
        cfg = {"targets": [wrong], "reliable_pipeline": {
            "test_target_only": True,
            "test_target_names": ["bot群聊测试", "family"],
        }}
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_not_called()
        assert out["skipped"] and "test_mode_target_rejected" in out["skipped"][0]["error"]
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY
        assert "test_mode_target_rejected" in (row["error"] or "")


def test_send_once_sender_exception_is_recorded_and_releases_lease():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"],
            result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter,
            max_send_attempts=3,
            db_path=db,
            now=11.0,
        )
        outbox_id = applied["outbox"]["id"]

        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [target]}

        with mock.patch.object(worker, "send_reply_detailed",
                               side_effect=RuntimeError("sender crash")):
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        assert out["processed"][0]["ok"] is False
        assert "exception" in out["processed"][0]
        with pipeline._connect(db) as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["status"] == pipeline.OUTBOX_RETRY
        assert row["attempts"] == 1
        assert "sender crash" in (row["error"] or "")
        # Lease is cleared.
        assert row["lease_owner"] in (None, "")


def test_send_once_marks_send_started_and_extends_lease_beyond_claim():
    """The marker is persisted before the sender runs and extends the lease to
    the configured send window, beyond a short claim lease."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"], result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter, db_path=db, now=11.0)
        outbox_id = applied["outbox"]["id"]
        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [target], "reliable_pipeline": {"send_lease_seconds": 180.0}}
        observed = {}

        def _slow_send(*args, **kwargs):
            with pipeline._connect(db) as con:
                row = con.execute(
                    "SELECT send_started_at, lease_until FROM send_outbox WHERE id=?",
                    (outbox_id,)).fetchone()
            observed["send_started_at"] = row["send_started_at"]
            observed["lease_until"] = row["lease_until"]
            return _fake_send_result(ok=True, reason="confirmed", attempted=["uia_send"], confirmed=True)

        with mock.patch.object(worker, "send_reply_detailed", side_effect=_slow_send):
            out = worker.send_once("sender", cfg, db_path=db, lease_seconds=5.0, now=100.0)
        assert out["processed"][0]["ok"] is True
        # Marker set at the fresh mark time (100) and lease extended to 100+180,
        # far beyond the short 5s claim lease (which would have expired at 105).
        assert observed["send_started_at"] == 100.0
        assert observed["lease_until"] == 280.0


def test_send_once_send_lease_undersized_config_falls_back_to_claim_lease():
    """A send_lease_seconds smaller than the claim lease must not shrink the
    in-flight window below the claim lease."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"], result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter, db_path=db, now=11.0)
        outbox_id = applied["outbox"]["id"]
        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        # send_lease_seconds=2.0 < default claim lease 45.0 -> max() picks 45.
        cfg = {"targets": [target], "reliable_pipeline": {"send_lease_seconds": 2.0}}
        observed = {}

        def _slow_send(*args, **kwargs):
            with pipeline._connect(db) as con:
                row = con.execute("SELECT lease_until FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
            observed["lease_until"] = row["lease_until"]
            return _fake_send_result(ok=True, reason="confirmed", attempted=["uia_send"], confirmed=True)

        with mock.patch.object(worker, "send_reply_detailed", side_effect=_slow_send):
            worker.send_once("sender", cfg, db_path=db, now=100.0)
        assert observed["lease_until"] == 145.0  # 100 + 45 (claim lease), not 102


def test_send_once_gate_rejection_leaves_send_started_null():
    """A gate-rejected row never reaches the marker, so send_started_at stays NULL."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"], result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter, db_path=db, now=11.0)
        outbox_id = applied["outbox"]["id"]
        cfg = {"targets": []}  # target not configured -> pre-send gate rejection
        with mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_not_called()
        assert len(out["skipped"]) == 1
        with pipeline._connect(db) as con:
            row = con.execute("SELECT send_started_at FROM send_outbox WHERE id=?", (outbox_id,)).fetchone()
        assert row["send_started_at"] is None


def test_send_once_aborts_when_lease_lost_before_send():
    """If the lease is reclaimed before the marker, the sender is not invoked."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        job = pipeline.claim_next_job(owner="worker", db_path=db, now=10.0)
        applied = pipeline.apply_agent_result(
            job_id=job["id"], result=_contract_dict("reply", "ok", "ok"),
            final_filter=_pass_filter, db_path=db, now=11.0)
        target = {"name": "bot群聊测试", "username": "wxid_target",
                  "db": "message_0.db", "table": "Msg_wxid_target"}
        cfg = {"targets": [target]}
        with mock.patch.object(pipeline, "record_send_started", return_value=False), \
                mock.patch.object(worker, "send_reply_detailed") as sender:
            out = worker.send_once("sender", cfg, db_path=db, now=12.0)
        sender.assert_not_called()
        assert len(out["skipped"]) == 1
        assert "lease lost" in out["skipped"][0]["error"]


# --- Normalizer regression tests --------------------------------------------

def _seed_event_with_payload(db: Path, payload: dict, *, local_id: int = 1,
                             target_id: str = "wxid_target",
                             sender: str = "wxid_sender",
                             group_key: str = "wxid_target",
                             now: float = 1.0) -> None:
    """Persist an event with an arbitrary payload snapshot and attach it to
    the current window without closing the window. The caller must close the
    window and create jobs when ready.
    """
    pipeline.persist_inbound_event(
        event_id="wx:message_0.db:Msg_%s:%d" % (target_id, local_id),
        target_id=target_id,
        group_key=group_key,
        sender_id=sender,
        local_id=local_id,
        payload=payload,
        received_at=now,
        db_path=db,
    )
    pipeline.add_event_to_window(
        "wx:message_0.db:Msg_%s:%d" % (target_id, local_id),
        debounce_seconds=1, max_window_seconds=10,
        now=now, db_path=db,
    )


def _seed_turn_with_payload(db: Path, payload: dict, *, local_id: int = 1,
                            target_id: str = "wxid_target",
                            sender: str = "wxid_sender",
                            group_key: str = "wxid_target",
                            now: float = 1.0) -> dict:
    """Persist, attach, close, and create a single job for one event."""
    _seed_event_with_payload(
        db, payload, local_id=local_id, target_id=target_id,
        sender=sender, group_key=group_key, now=now,
    )
    turns = pipeline.close_due_windows(now=now + 1, db_path=db)
    assert len(turns) == 1
    jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
    assert len(jobs) == 1
    return jobs[0]


def test_process_once_nested_snapshot_prompt_and_auth():
    """Production ingress stores the full build_event_payload() snapshot; the
    worker must extract text, mention, target policy, and authorization from
    the nested shape, not the legacy flat shape.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        cfg = {
            "targets": [{
                "name": "bot群聊测试",
                "username": "wxid_target",
                "table": "Msg_wxid_target",
                "knowledge_bases": ["scene_a", "core"],
            }]
        }
        target = cfg["targets"][0]
        m = {
            "local_id": 42,
            "real_sender_id": "wxid_sender",
            "sender_username": "wxid_sender",
            "sender_display_name": "Alice",
            "mention_name": "Alice",
            "message_content": "产品价格是多少？",
            "local_type": 1,
            "create_time": 0,
            "image_path": "",
            "session_image_paths": [],
        }
        payload = build_event_payload(
            target, m, cfg=cfg,
            config_path="/tmp/wechat_bot_targets.json",
            target_policy={"mode": "customer_service"},
        )
        _seed_turn_with_payload(db, payload, local_id=42)

        provider = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("reply", "reply text", "ok")},
        ))
        out = worker.process_once(provider, "worker", cfg,
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        captured_payload = provider._capture[0]["payload"]
        assert captured_payload["prompt"] == "Alice: 产品价格是多少？"
        assert captured_payload["mention_name"] == "Alice"
        assert captured_payload["_allowed_kb_ids"] == ["scene_a", "core"]
        assert captured_payload["_config_path"] == "/tmp/wechat_bot_targets.json"
        assert captured_payload["target"]["name"] == "bot群聊测试"
        assert captured_payload["target_policy"]["mode"] == "customer_service"
        assert "当前响应模式：客服" in captured_payload["mode_instruction"]

        # The mention name must survive all the way to the send call.
        with mock.patch.object(worker, "send_reply_detailed",
                               return_value=_fake_send_result(ok=True, reason="confirmed",
                                                              attempted=["uia_send"],
                                                              confirmed=True)) as sender:
            send_out = worker.send_once("sender", cfg, db_path=db, now=11.0)
        assert send_out["status"] == "processed"
        assert sender.call_args.kwargs["mention_name"] == "Alice"


def test_process_once_multi_event_aggregates_text_and_uses_last_auth():
    """Multi-event turns must aggregate all messages in order and promote
    authorization/policy from the LAST event's snapshot, not the first.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        cfg = {"targets": [{"name": "g", "username": "wxid_target", "table": "Msg_wxid_target"}]}
        target = cfg["targets"][0]

        m1 = {
            "local_id": 10,
            "sender_username": "wxid_sender",
            "sender_display_name": "Alice",
            "message_content": "在吗",
            "local_type": 1,
            "create_time": 0,
            "image_path": "",
            "session_image_paths": [],
        }
        p1 = build_event_payload(
            target, m1, cfg=cfg,
            config_path="/tmp/cfg.json",
            target_policy={"mode": "group_assistant"},
        )
        # First event's snapshot is deliberately stale/wrong.
        p1["_allowed_kb_ids"] = ["stale_kb"]

        m2 = {
            "local_id": 11,
            "sender_username": "wxid_sender",
            "sender_display_name": "Alice",
            "message_content": "咨询一下移动云盘",
            "local_type": 1,
            "create_time": 0,
            "image_path": "",
            "session_image_paths": [],
        }
        p2 = build_event_payload(
            target, m2, cfg=cfg,
            config_path="/tmp/cfg.json",
            target_policy={"mode": "customer_service"},
        )
        p2["_allowed_kb_ids"] = ["scene_a", "core"]

        # Seed both events into the same window.
        _seed_event_with_payload(db, p1, local_id=10, now=10.0)
        _seed_event_with_payload(db, p2, local_id=11, now=10.5)
        turns = pipeline.close_due_windows(now=12.0, db_path=db)
        assert len(turns) == 1
        jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
        assert len(jobs) == 1

        provider = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("reply", "ok", "ok")},
        ))
        out = worker.process_once(provider, "worker", cfg,
                                  final_filter=_pass_filter, db_path=db, now=12.0)
        assert out["status"] == "applied"
        captured_payload = provider._capture[0]["payload"]
        assert captured_payload["prompt"] == "Alice: 在吗\nAlice: 咨询一下移动云盘"
        # Authorization must come from the last event, not the first.
        assert captured_payload["_allowed_kb_ids"] == ["scene_a", "core"]
        assert captured_payload["target_policy"]["mode"] == "customer_service"


def test_process_once_pure_image_message_is_valid():
    """A message with no text but with images is legitimate ingress and must
    not be rejected as malformed normalization.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        cfg = {"targets": [{"name": "g", "username": "wxid_target", "table": "Msg_wxid_target"}]}
        target = cfg["targets"][0]
        m = {
            "local_id": 1,
            "sender_username": "wxid_sender",
            "sender_display_name": "Bob",
            "message_content": "",
            "local_type": 3,
            "create_time": 0,
            "image_path": "/tmp/img.jpg",
            "session_image_paths": ["/tmp/img.jpg", "/tmp/img2.jpg"],
        }
        payload = build_event_payload(target, m, cfg=cfg)
        _seed_turn_with_payload(db, payload, local_id=1)

        provider = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("reply", "ok", "ok")},
        ))
        out = worker.process_once(provider, "worker", cfg,
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        captured_payload = provider._capture[0]["payload"]
        assert captured_payload["prompt"] == ""
        assert captured_payload["image_paths"] == ["/tmp/img.jpg", "/tmp/img2.jpg"]


def test_process_once_malformed_no_valid_snapshot_fails_job():
    """A turn whose events contain no valid dict snapshot must fail
    deterministically without invoking the provider.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        pipeline.persist_inbound_event(
            event_id="e1",
            target_id="wxid_target",
            group_key="wxid_target",
            sender_id="wxid_sender",
            local_id=1,
            payload="not a snapshot",
            received_at=1.0,
            db_path=db,
        )
        pipeline.add_event_to_window("e1", debounce_seconds=1, max_window_seconds=10,
                                     now=1.0, db_path=db)
        turns = pipeline.close_due_windows(now=2.0, db_path=db)
        assert len(turns) == 1
        jobs = pipeline.create_jobs_for_ready_turns(db_path=db)
        assert len(jobs) == 1

        provider = FakeProvider(result=_agent_result(
            raw={"agent_result": _contract_dict("reply", "ok", "ok")},
        ))
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert provider._capture == []
        job = worker._fetch_job(db, jobs[0]["id"])
        assert job["status"] == pipeline.JOB_FAILED
        assert "normalization failed" in job["error"]


def test_normalize_turn_payload_skips_empty_trailing_snapshot():
    """If the last event snapshot is an empty dict or arbitrary metadata, the
    normalizer must fall back to the previous valid snapshot rather than treat
    the empty dict as a valid event with no content.
    """
    payload = {
        "schema_version": 1,
        "events": [
            {
                "source_event_id": "e1",
                "local_id": 1,
                "received_at": 1.0,
                "message": {
                    "local_id": 1,
                    "message_content": "valid",
                    "sender_username": "wxid_sender",
                    "sender_display_name": "Sender",
                    "local_type": 1,
                },
            },
            {
                "source_event_id": "e2",
                "local_id": 2,
                "received_at": 2.0,
                "message": {},  # malformed trailing snapshot
            },
        ],
    }
    normalized = worker._normalize_turn_payload(payload)
    assert "_normalization_error" not in normalized
    assert normalized["prompt"] == "Sender: valid"
    assert normalized["local_id"] == 1


def test_normalize_turn_payload_all_snapshots_invalid_fails():
    """A turn with no valid snapshot at all should fail normalization."""
    payload = {
        "schema_version": 1,
        "events": [
            {"source_event_id": "e1", "local_id": 1, "received_at": 1.0, "message": {}},
            {"source_event_id": "e2", "local_id": 2, "received_at": 2.0, "message": {"metadata": "only"}},
        ],
    }
    normalized = worker._normalize_turn_payload(payload)
    assert normalized.get("_normalization_error") == "no valid event snapshot"


def test_normalize_turn_payload_no_content_fails():
    """A snapshot with no text and no images is not a replyable event; it must
    be treated as no valid snapshot for normalization.
    """
    payload = {
        "schema_version": 1,
        "events": [
            {
                "source_event_id": "e1",
                "local_id": 1,
                "received_at": 1.0,
                "message": {
                    "sender_username": "wxid_sender",
                    "sender_display_name": "Sender",
                    "local_type": 1,
                },
            },
        ],
    }
    normalized = worker._normalize_turn_payload(payload)
    assert normalized.get("_normalization_error") == "no valid event snapshot"


def test_normalize_turn_payload_flat_row_backward_compatible():
    """The legacy flat-row shape used by existing helpers must still produce a
    prompt instead of silently degrading to an empty user request.
    """
    payload = {
        "schema_version": 1,
        "events": [
            {
                "source_event_id": "e1",
                "local_id": 1,
                "received_at": 1.0,
                "message": {
                    "message_content": "hi",
                    "sender_username": "wxid_sender",
                    "sender_display_name": "Sender",
                    "local_type": 1,
                },
            }
        ],
    }
    normalized = worker._normalize_turn_payload(payload)
    assert normalized["prompt"] == "Sender: hi"
    assert "_normalization_error" not in normalized


# --- Knowledge-search trace provenance (Stage 3) ----------------------------

def _provenance(db: Path, job_id: int) -> dict:
    """Read the parsed provenance_json for a job row."""
    with pipeline._connect(db) as con:
        row = con.execute("SELECT provenance_json FROM turn_jobs WHERE id=?", (job_id,)).fetchone()
    return json.loads(row["provenance_json"] or "{}")


def test_process_once_attaches_valid_provenance_to_job():
    """A valid MCP trace artifact is loaded, validated, and persisted as a
    structured provenance summary alongside the durable job result.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn(db)
            trace = {
                "status": "ok",
                "provenance": [{"kb_id": "kb1", "source": "scene", "content": "answer"}],
                "no_source_reason": "",
            }
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
                trace=trace,
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            job = out["job"]
            prov = _provenance(db, job["id"])
            assert prov["status"] == "ok"
            assert prov["provenance"] == trace["provenance"]
            assert prov["no_source_reason"] == ""
            assert "trace_id" in prov
            assert prov["trace_id"] == provider._capture[0]["payload"][worker.TRACE_PAYLOAD_ID]
            # The trace file must be cleaned up after reading.
            assert not Path(provider._capture[0]["payload"][worker.TRACE_PAYLOAD_DIR]).exists()
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_missing_trace_is_no_tool_call():
    """When the MCP server writes no trace artifact, the worker records an
    explicit ``no_tool_call`` provenance status.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn(db)
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            prov = _provenance(db, out["job"]["id"])
            assert prov["status"] == "no_tool_call"
            assert prov["provenance"] == []
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def _grounded_event_payload(local_id: int = 1, reply_policy: str = "knowledge_grounded") -> dict:
    return {
        "message": _event(local_id),
        "target": {"name": "bot群聊测试", "username": "wxid_target"},
        "target_policy": {"reply_policy": reply_policy},
        "_allowed_kb_ids": ["scene.a"],
        "schema_version": 1,
    }


def test_process_once_grounded_target_without_trace_fails_without_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn_with_payload(db, _grounded_event_payload())
            provider = FakeProvider(result=_agent_result(
                reply_text="reply",
                raw={"agent_result": _contract_dict("reply", "reply")},
            ))
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "failed"
            assert out["outbox"] is None
            assert out["job"]["error"] == "required knowledge search was not invoked"
            assert _provenance(db, out["job"]["id"])["status"] == "no_tool_call"
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_grounded_target_uses_extended_provider_timeout():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn_with_payload(db, _grounded_event_payload())
        provider = FakeProvider(result=_agent_result(
            reply_text="reply",
            raw={"agent_result": _contract_dict("reply", "reply")},
        ))
        worker.process_once(provider, "worker", {}, final_filter=_pass_filter, db_path=db)
        assert provider._timeouts == [worker.DEFAULT_KNOWLEDGE_PROVIDER_TIMEOUT]



def test_process_once_grounded_target_rejects_unauthorized_trace_kb():
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn_with_payload(db, _grounded_event_payload())
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
                trace={
                    "status": "ok",
                    "provenance": [{"kb_id": "scene.forbidden", "content": "secret"}],
                    "no_source_reason": "",
                },
            )
            out = worker.process_once(provider, "worker", {}, final_filter=_pass_filter, db_path=db)
            assert out["status"] == "failed"
            assert out["outbox"] is None
            assert out["job"]["error"] == "invalid knowledge search trace"
            assert _provenance(db, out["job"]["id"])["status"] == "invalid"
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)

def test_process_once_non_grounded_target_without_trace_still_applies():
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn_with_payload(db, _grounded_event_payload(reply_policy="balanced"))
            provider = FakeProvider(result=_agent_result(
                reply_text="reply",
                raw={"agent_result": _contract_dict("reply", "reply")},
            ))
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            assert out["outbox"] is not None
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_grounded_target_with_no_hit_trace_still_applies():
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn_with_payload(db, _grounded_event_payload())
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
                trace={"status": "no_hit", "provenance": [], "no_source_reason": "no kb match"},
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            assert out["outbox"] is not None
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_no_hit_trace_is_no_hit():
    """An attempted search with no hits is recorded as ``no_hit`` and keeps
    the no-source reason from the MCP artifact.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn(db)
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
                trace={"status": "no_hit", "provenance": [], "no_source_reason": "no kb match"},
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            prov = _provenance(db, out["job"]["id"])
            assert prov["status"] == "no_hit"
            assert prov["no_source_reason"] == "no kb match"
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_provider_failure_trace_is_provider_failure():
    """When the MCP search tool reports a provider-level failure, the worker
    records ``provider_failure`` status and preserves the no-source reason so
    it is not confused with ``invalid`` or ``no_hit``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn(db)
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
                trace={"status": "provider_failure", "provenance": [],
                       "no_source_reason": "leann server unreachable"},
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            prov = _provenance(db, out["job"]["id"])
            assert prov["status"] == "provider_failure"
            assert prov["no_source_reason"] == "leann server unreachable"
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_malformed_trace_is_marked_invalid():
    """A trace artifact that is not valid JSON or does not match the expected
    schema is safely ignored and marked ``invalid``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn(db)
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
                trace={"invalid": "schema"},
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            prov = _provenance(db, out["job"]["id"])
            assert prov["status"] == "invalid"
            assert "error" in prov
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_out_of_scope_trace_id_is_invalid():
    """A trace artifact whose trace_id does not match the worker-generated id
    for this job is rejected as ``invalid``; the worker never trusts an
    agent-provided or arbitrary path.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn(db)
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
                trace={"status": "ok", "provenance": [{"kb_id": "kb1"}],
                       "no_source_reason": ""},
                trace_id_override="wrong-agent-id",
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            prov = _provenance(db, out["job"]["id"])
            assert prov["status"] == "invalid"
            assert "trace_id mismatch" in prov.get("error", "")
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


def test_process_once_trace_path_is_worker_controlled():
    """The trace directory is generated by the worker under the configured
    base dir and is never derived from provider payload or model content.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            _seed_turn(db)
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
            )
            worker.process_once(provider, "worker", {},
                                final_filter=_pass_filter, db_path=db)
            payload = provider._capture[0]["payload"]
            trace_dir = Path(payload[worker.TRACE_PAYLOAD_DIR])
            trace_id = payload[worker.TRACE_PAYLOAD_ID]
            assert trace_dir.is_absolute()
            assert trace_dir.parent == trace_base
            assert trace_dir.name.startswith("job_")
            assert trace_id in trace_dir.name
            # The internal payload keys are never sent to the model; the
            # worker-provided id is the only trace identifier exposed.
            assert worker.TRACE_PAYLOAD_DIR.startswith("_")
            assert worker.TRACE_PAYLOAD_ID.startswith("_")
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)


# --- Shadow mode: full worker path, never a sendable outbox (Stage 4) -------

def test_process_once_shadow_job_completes_without_outbox():
    """A shadow turn job runs the full worker decision path and is marked
    done, but never creates a sendable outbox row; the would-be reply is
    recorded in ``turn_jobs.shadow_json``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        trace_base = Path(tmp) / "traces"
        trace_base.mkdir()
        os.environ[worker.TRACE_ENV_DIR] = str(trace_base)
        try:
            db = Path(tmp) / "pipeline.sqlite"
            payload = _event(1)
            payload["target"] = {"username": "wxid_target", "reliable_pipeline_shadow": True}
            _seed_turn_with_payload(db, payload)
            provider = FakeProvider(
                result=_agent_result(
                    reply_text="reply",
                    raw={"agent_result": _contract_dict("reply", "reply")},
                ),
            )
            out = worker.process_once(provider, "worker", {},
                                      final_filter=_pass_filter, db_path=db)
            assert out["status"] == "applied"
            assert out["outbox"] is None
            assert out["job"]["status"] == pipeline.JOB_DONE
            with pipeline._connect(db) as con:
                rows = con.execute("SELECT COUNT(*) AS n FROM send_outbox WHERE job_id=?",
                                   (out["job"]["id"],)).fetchone()
                record = con.execute("SELECT shadow_json FROM turn_jobs WHERE id=?",
                                     (out["job"]["id"],)).fetchone()
            assert rows["n"] == 0
            shadow = json.loads(record["shadow_json"])
            assert shadow["shadow"] is True
            assert shadow["would_send"] is True
            assert shadow["reply_text"] == "reply"
            assert shadow["reply_chars"] == len("reply")
        finally:
            os.environ.pop(worker.TRACE_ENV_DIR, None)



# --- Provider retry (in-process bounded retry) -----------------------------

class _SequencedProvider:
    """Provider stub that returns a preset sequence of results/raises.

    Each ``run`` pops the next entry from ``results`` and returns it or
    raises it.  ``calls`` records the arguments of every invocation.
    """

    def __init__(self, results):
        self._results = list(results)
        self.calls = []  # list of (job, timeout)

    def run(self, job, timeout=None):
        self.calls.append((job, timeout))
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item




def test_provider_retry_then_success_skips_fail_job():
    """A retryable stage on attempt 1 must be retried; success on attempt 2.

    fail_job must NOT be called and no outbox may be created on the first
    attempt.  The second attempt's success must produce an outbox.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = _SequencedProvider([
            _agent_result(ok=False, status="failed",
                          raw={"stage": "contract_violation"}),
            _agent_result(ok=True, status="done",
                          raw={"agent_result": _contract_dict("reply", "hello", "ok")}),
        ])
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "applied"
        assert out["outbox"] is not None
        assert len(provider.calls) == 2


def test_provider_no_retry_on_non_retryable_stage():
    """An AgentResult with ok=False and a non-retryable stage must fail
    terminally on the first attempt (no retry, no sleep).
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = _SequencedProvider([
            _agent_result(ok=False, status="failed",
                          raw={"stage": "runner_error"}),
        ])
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert len(provider.calls) == 1
        diag = out["job"].get("provider_diagnostics") or {}
        assert diag.get("stage") == "runner_error"
        assert diag.get("retry_count") == 0


def test_provider_no_retry_on_timeout():
    """A TimeoutExpired (stage=timeout) must fail terminally without retry."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = _SequencedProvider([subprocess.TimeoutExpired(cmd="hermes", timeout=120)])
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert len(provider.calls) == 1
        diag = out["job"].get("provider_diagnostics") or {}
        assert diag.get("stage") == "timeout"
        assert diag.get("retry_count") == 0


def test_provider_retry_exhausted_records_retry_count_one():
    """Two retryable-stage failures must exhaust the retry budget and
    record retry_count=1 in the final diagnostics.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = _SequencedProvider([
            _agent_result(ok=False, status="failed",
                          raw={"stage": "contract_violation"}),
            _agent_result(ok=False, status="failed",
                          raw={"stage": "contract_violation"}),
        ])
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        assert out["status"] == "failed"
        assert len(provider.calls) == 2  # exactly 2 attempts
        diag = out["job"].get("provider_diagnostics") or {}
        assert diag.get("stage") == "contract_violation"
        assert diag.get("retry_count") == 1


def test_provider_retry_diagnostics_do_not_leak_raw_stdout():
    """The persisted diagnostics must not contain the provider's raw stdout,
    even when the failure payload includes a long sentinel string."""
    SENTINEL = "X" * 300  # > 200 chars to make leakage obvious
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pipeline.sqlite"
        _seed_turn(db)
        provider = _SequencedProvider([
            _agent_result(ok=False, status="failed",
                          raw={"stage": "contract_violation", "stdout": SENTINEL}),
            _agent_result(ok=False, status="failed",
                          raw={"stage": "contract_violation", "stdout": SENTINEL}),
        ])
        out = worker.process_once(provider, "worker", {},
                                  final_filter=_pass_filter, db_path=db, now=10.0)
        diag = out["job"].get("provider_diagnostics") or {}
        # The sentinel must not appear in any stringified diagnostic value.
        assert SENTINEL not in json.dumps(diag)
        # And the persisted SQLite column must not contain it either.
        with pipeline._connect(db) as con:
            raw_col = con.execute(
                "SELECT provider_diagnostics_json FROM turn_jobs WHERE id=?",
                (out["job"]["id"],),
            ).fetchone()[0]
        assert SENTINEL not in raw_col
