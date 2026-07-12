"""Tests for agent_worker.process_once strict-contract migration.

These tests pin the consumer-level injection introduced in Stage 2:
`process_once` must force `payload.reliable_result_contract = True` before
calling the provider, and must treat strict `silent`/`escalate` actions as
successful terminal states instead of provider failures.
"""

import sys
import time
import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_jobs
import agent_worker
from agent_provider import AgentResult, EchoAgentProvider


def _make_db(tmp_path: Path) -> Path:
    return tmp_path / "agent_jobs.sqlite"


def _enqueue(db_path: Path, *, payload: dict | None = None, task_type: str = "deep_agent") -> dict:
    return agent_jobs.enqueue_job(
        job_key=f"test-{time.time_ns()}",
        group_key="test-group",
        task_type=task_type,
        payload=payload or {},
        db_path=db_path,
    )


def _contract_dict(action="reply", reply_text="", reason_code="answered", risk_level="low"):
    return {
        "schema_version": 1,
        "action": action,
        "reply_text": reply_text,
        "reason_code": reason_code,
        "risk_level": risk_level,
    }


class _FakeProvider:
    name = "fake"

    def __init__(self, result: AgentResult):
        self.result = result
        self.captured_job = None

    def run(self, job: dict, timeout: float | None = None) -> AgentResult:
        self.captured_job = job
        return self.result

    def health(self):
        return {"ok": True, "ready": True}


def test_release_dispatching_returns_job_to_queued_and_clears_lock_meta(tmp_path):
    """Claim a job into dispatching, release it, and verify lock metadata is cleared."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "x"})
    claimed = agent_jobs.claim_dispatchable(worker_id="w1", db_path=db, max_global_dispatching=1)
    assert claimed is not None
    assert claimed["status"] == agent_jobs.STATUS_DISPATCHING
    assert claimed["dispatch_owner"] == "w1"
    assert claimed["dispatch_locked_until"] is not None

    released = agent_jobs.release_dispatching(int(job["id"]), reason="test release", db_path=db)
    assert released is True

    released_job = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert released_job["status"] == agent_jobs.STATUS_QUEUED
    assert released_job["dispatch_owner"] is None
    assert released_job["dispatch_locked_until"] is None
    assert released_job["finished_at"] is None


def test_process_once_injects_strict_flag_into_empty_payload(tmp_path):
    """Empty payload must be written back into job before setting the strict flag."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="hi", raw={"agent_result": _contract_dict(action="reply", reply_text="hi")}))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "completed"
    assert provider.captured_job is not None
    assert provider.captured_job["payload"]["reliable_result_contract"] is True


def test_process_once_injects_strict_flag_onto_existing_payload(tmp_path):
    """Existing payload must have the strict flag added while preserving other keys."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "question", "agent_timeout": 60})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="hi", raw={"agent_result": _contract_dict(action="reply", reply_text="hi")}))

    agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert provider.captured_job["payload"]["prompt"] == "question"
    assert provider.captured_job["payload"]["agent_timeout"] == 60
    assert provider.captured_job["payload"]["reliable_result_contract"] is True


def test_process_once_persists_strict_flag_in_stored_payload(tmp_path):
    """The strict contract flag must be persisted to the job record, not only in memory."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "question"})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="hi", raw={"agent_result": _contract_dict(action="reply", reply_text="hi")}))

    agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored is not None
    assert stored["payload"]["reliable_result_contract"] is True
    assert stored["payload"]["prompt"] == "question"


def test_process_once_persistence_failure_fails_job_and_does_not_call_provider(tmp_path, monkeypatch):
    """If merge_payload fails, the job must be terminal failed and provider.run never called."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "question"})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="hi", raw={"agent_result": _contract_dict(action="reply", reply_text="hi")}))
    monkeypatch.setattr(agent_jobs, "merge_payload", lambda *a, **k: False)

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert provider.captured_job is None
    assert out["action"] == "persistence_failed"
    assert out["error"] == "failed to persist strict contract flag"
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED


def test_process_once_persistence_failure_when_fail_job_fails_returns_safe_failure(tmp_path, monkeypatch):
    """If merge_payload and fail_job both fail, return a safe failure marker without claiming success."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "question"})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="hi", raw={"agent_result": _contract_dict(action="reply", reply_text="hi")}))
    monkeypatch.setattr(agent_jobs, "merge_payload", lambda *a, **k: False)
    monkeypatch.setattr(agent_jobs, "fail_job", lambda *a, **k: False)

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert provider.captured_job is None
    assert out["action"] == "persistence_failed"
    assert out["ok"] is False
    assert out["error"] == "failed to persist strict contract flag"


def test_process_once_treats_strict_silent_as_successful_terminal(tmp_path):
    """Strict silent action must complete the job, not fail it."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": _contract_dict(action="silent", reason_code="smalltalk")
    }
    provider = _FakeProvider(AgentResult(ok=True, status="silent", reply_text="", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "silent"
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_SENT
    assert completed["send_status"] == agent_jobs.SEND_SKIPPED
    assert completed["result_text"] == ""


def test_process_once_treats_strict_escalate_as_successful_terminal(tmp_path):
    """Strict escalate action must complete the job, not fail it."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": _contract_dict(action="escalate", reason_code="human", risk_level="high")
    }
    provider = _FakeProvider(AgentResult(ok=True, status="escalated", reply_text="", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "escalated"
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_SENT
    assert completed["send_status"] == agent_jobs.SEND_SKIPPED
    assert completed["result_text"] == ""


def test_process_once_does_not_mark_skipped_when_complete_job_silent_fails(tmp_path, monkeypatch):
    """If complete_job_silent fails, a strict silent action must not report success or leave a sendable state."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": _contract_dict(action="silent", reason_code="smalltalk")
    }
    provider = _FakeProvider(AgentResult(ok=True, status="silent", reply_text="", raw=raw))

    monkeypatch.setattr(agent_jobs, "complete_job_silent", lambda *a, **k: False)

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["ok"] is False
    assert out["action"] == "silent"
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    # A sendable state is status=done + send_status=pending. The helper failing
    # must not leave the job in that window.
    assert not (completed["status"] == agent_jobs.STATUS_DONE and completed["send_status"] == agent_jobs.SEND_PENDING)
    assert completed["status"] != agent_jobs.STATUS_DONE


def test_process_once_strict_silent_does_not_invoke_sender(tmp_path, monkeypatch):
    """Strict silent action must not invoke the WeChat sender even when send is enabled."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": _contract_dict(action="silent", reason_code="smalltalk")
    }
    provider = _FakeProvider(AgentResult(ok=True, status="silent", reply_text="", raw=raw))

    send_mock = MagicMock(return_value=True)
    monkeypatch.setattr(agent_worker, "_send_result_back", send_mock)
    monkeypatch.setattr(agent_worker, "_HAS_SENDER", True)

    out = agent_worker.process_once(
        provider=provider, worker_id="w1", db_path=db, timeout_seconds=10, send_enabled=True
    )

    assert out["action"] == "silent"
    assert out["ok"] is True
    send_mock.assert_not_called()
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_SENT
    assert completed["send_status"] == agent_jobs.SEND_SKIPPED



def test_process_once_missing_agent_result_fails(tmp_path):
    """Strict job with terminal ok=True but no raw['agent_result'] must fail."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="display text only", raw={}))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True  # fail_job returns True
    assert provider.captured_job["payload"]["reliable_result_contract"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED
    assert completed["send_status"] == agent_jobs.SEND_PENDING
    assert not completed["result_text"]


def test_process_once_malformed_agent_result_fails(tmp_path):
    """Strict job with raw['agent_result'] missing required fields must fail."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": {
            "action": "reply",
            "reply_text": "incomplete contract",
            # missing schema_version, reason_code, risk_level
        }
    }
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="incomplete contract", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED


def test_process_once_rejects_display_text_reply_text_channel(tmp_path):
    """A JSON contract smuggled in plain reply_text must be rejected when no raw contract exists."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    smuggled = json.dumps(_contract_dict(action="reply", reply_text="smuggled"))
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text=smuggled, raw={}))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED


def test_process_once_valid_contract_reply_text_disagreement_is_ignored(tmp_path):
    """The parsed contract reply_text wins; provider reply_text is ignored after validation."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {
        "agent_result": _contract_dict(action="reply", reply_text="from contract")
    }
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="display text noise", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "completed"
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["result_text"] == "from contract"


def test_process_once_rejects_display_text_noise_shapes(tmp_path):
    """Same display-text noise shapes that HermesProvider rejects must fail foreground consumer."""
    db = _make_db(tmp_path)
    noises = [
        "Query: 介绍一下产品\nInitializing agent…\n╭─ ⚕ Hermes ──╮\n│ 你好 │\n╰───────────╯",
        "\x1b[32mQuery: hello\x1b[0m\nInitializing agent…",
        "preparing find…\nfinalizing find…\n some prose",
    ]
    for noise in noises:
        job = _enqueue(db, payload={})
        provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text=noise, raw={"agent_result": noise}))
        out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)
        assert out["action"] == agent_jobs.STATUS_FAILED, repr(noise)
        completed = agent_jobs.get_job(int(job["id"]), db_path=db)
        assert completed["status"] == agent_jobs.STATUS_FAILED


def test_process_once_non_terminal_status_with_valid_contract_fails(tmp_path):
    """ok=True with a valid contract but non-terminal status (e.g. running) must fail."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {"agent_result": _contract_dict(action="reply", reply_text="not yet")}
    provider = _FakeProvider(AgentResult(ok=True, status="running", reply_text="not yet", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED


def test_process_once_provider_exception_fails_job_without_crashing(tmp_path):
    """A provider that raises must be caught and the job marked failed."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})

    class _RaisingProvider:
        name = "raise"

        def run(self, job, timeout=None):
            raise RuntimeError("boom")

    out = agent_worker.process_once(provider=_RaisingProvider(), worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True
    assert out["error"] == "provider run failed"
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED
    assert completed["error"] == "provider run failed"


def test_process_once_provider_exception_excludes_paths_and_secrets(tmp_path):
    """Exception text must not leak filesystem paths or secret-like fragments."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    leaked_path = "C:/\u7528\u6237/user/secrets/keys.json"
    leaked_secret = "sk-live-abc123def456"

    class _LeakingProvider:
        name = "leak"

        def run(self, job, timeout=None):
            raise RuntimeError(f"failed to read {leaked_path}: token {leaked_secret}")

    out = agent_worker.process_once(provider=_LeakingProvider(), worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True
    assert out["error"] == "provider run failed"
    assert leaked_path not in (out["error"] or "")
    assert leaked_secret not in (out["error"] or "")
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED
    assert completed["error"] == "provider run failed"
    assert leaked_path not in (completed["error"] or "")
    assert leaked_secret not in (completed["error"] or "")


def test_process_once_empty_sanitized_reply_contract_fails(tmp_path):
    """A reply contract that sanitizes to empty must fail with no send."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    raw = {"agent_result": _contract_dict(action="reply", reply_text="\x1b[32m\x1b[0m")}
    provider = _FakeProvider(AgentResult(ok=True, status="done", reply_text="\x1b[32m\x1b[0m", raw=raw))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED
    assert not completed["result_text"]


def test_process_once_with_echo_provider_completes(tmp_path):
    """EchoAgentProvider must produce a strict contract that agent_worker completes."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"prompt": "hello"})
    provider = EchoAgentProvider(worker_id="echo-1")

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == "completed"
    assert out["ok"] is True
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_DONE
    assert "已收到测试任务" in completed["result_text"]


def test_process_once_echo_non_strict_returns_legacy_result(tmp_path):
    """EchoAgentProvider without strict flag must still return legacy result without agent_result."""
    db = _make_db(tmp_path)
    job = agent_jobs.enqueue_job(
        job_key=f"test-{time.time_ns()}",
        group_key="test-group",
        task_type="deep_agent",
        payload={"prompt": "hello"},  # no reliable_result_contract
        db_path=db,
    )
    # Claim the job and remove the strict flag that process_once normally injects,
    # then run the provider directly to inspect its legacy result shape.
    claimed = agent_jobs.claim_next_job(worker_id="w1", db_path=db)
    assert claimed is not None
    agent_jobs.merge_payload(int(claimed["id"]), {"reliable_result_contract": False})
    provider = EchoAgentProvider(worker_id="echo-1")
    result = provider.run(claimed, timeout=10)
    assert result.ok is True
    assert result.raw == {}
    assert "已收到测试任务" in result.reply_text


def test_process_once_provider_error_excludes_paths_and_secrets(tmp_path):
    """Provider-returned error strings must not be echoed in the response or persisted job."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    leaked_path = "/private/path"
    leaked_secret = "SECRET_TOKEN"
    provider = _FakeProvider(
        AgentResult(ok=False, status="failed", error=f"{leaked_path} {leaked_secret}")
    )

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["ok"] is True
    assert out["error"] == "provider result failed"
    assert leaked_path not in str(out)
    assert leaked_secret not in str(out)
    completed = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert completed["status"] == agent_jobs.STATUS_FAILED
    assert completed["error"] == "provider result failed"
    assert leaked_path not in (completed["error"] or "")
    assert leaked_secret not in (completed["error"] or "")


class _MalformedResult:
    """Arbitrary object with raw/reply_text fields but no valid AgentResult shape."""
    raw = {"agent_result": "C:/private/path SECRET_TOKEN"}
    reply_text = "C:/private/path SECRET_TOKEN"


def test_process_once_provider_returns_none_fails_safely(tmp_path):
    """A provider returning None must not crash and must leave the job failed/not sendable."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    provider = _FakeProvider(None)

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["error"] == agent_worker._SAFE_AGENT_RESULT_SHAPE
    assert "C:/private/path" not in str(out)
    assert "SECRET_TOKEN" not in str(out)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert not stored["result_text"]
    assert stored["error"] == agent_worker._SAFE_AGENT_RESULT_SHAPE


def test_process_once_provider_returns_malformed_object_fails_safely(tmp_path):
    """A provider returning an arbitrary object with raw/reply text fields must be rejected."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    provider = _FakeProvider(_MalformedResult())

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["error"] == agent_worker._SAFE_AGENT_RESULT_SHAPE
    assert "C:/private/path" not in str(out)
    assert "SECRET_TOKEN" not in str(out)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert not stored["result_text"]
    assert stored["error"] == agent_worker._SAFE_AGENT_RESULT_SHAPE


def test_process_once_provider_returns_secret_string_fails_safely(tmp_path):
    """A provider returning a secret-bearing string must not leak anything."""
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={})
    provider = _FakeProvider("C:/private/path SECRET_TOKEN")

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["action"] == agent_jobs.STATUS_FAILED
    assert out["error"] == agent_worker._SAFE_AGENT_RESULT_SHAPE
    assert "C:/private/path" not in str(out)
    assert "SECRET_TOKEN" not in str(out)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_FAILED
    assert not stored["result_text"]
    assert stored["error"] == agent_worker._SAFE_AGENT_RESULT_SHAPE


class _HostileBridgeValue:
    """Object whose string/repr/bool/int conversion raises to catch unsafe coercion."""

    def __init__(self, secret: str):
        self._secret = secret

    def __str__(self):
        raise RuntimeError(self._secret)

    def __repr__(self):
        raise RuntimeError(self._secret)

    def __bool__(self):
        raise RuntimeError(self._secret)

    def __int__(self):
        raise RuntimeError(self._secret)


class _HostileDictSubclass(dict):
    """Plain dict subclass rejected by exact-type bridge policy but usable for reading."""
    pass


class _HostileGetDict(dict):
    """Dict subclass whose ``get`` accessor raises to catch unsafe inspection."""

    def get(self, key, default=None):
        raise RuntimeError("C:/private/path SECRET_TOKEN")


def test_process_once_ignores_hostile_bridge_metadata_in_dict_subclass(tmp_path):
    """A valid strict contract in a hostile dict subclass raw completes without persisting bridge metadata."""
    secret = "C:/private/path SECRET_TOKEN"
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"reliable_result_contract": True})

    provider = _FakeProvider(AgentResult(
        ok=True,
        status="done",
        reply_text="",
        raw=_HostileGetDict({
            "agent_result": _contract_dict(action="reply", reply_text="hello from contract"),
            "bridge_session_id": _HostileBridgeValue(secret),
            "bridge_user_msg_id": _HostileBridgeValue(secret),
        }),
    ))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["ok"] is True
    assert out["action"] == "completed"
    assert secret not in str(out)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_DONE
    assert stored["result_text"] == "hello from contract"
    assert secret not in (stored["error"] or "")
    payload = stored.get("payload") or {}
    assert "bridge_session_id" not in payload
    assert "bridge_user_msg_id" not in payload


def test_process_once_ignores_hostile_bridge_metadata_in_plain_dict(tmp_path):
    """A valid strict contract with hostile bridge values completes without persisting secrets."""
    secret = "C:/private/path SECRET_TOKEN"
    db = _make_db(tmp_path)
    job = _enqueue(db, payload={"reliable_result_contract": True})

    provider = _FakeProvider(AgentResult(
        ok=True,
        status="done",
        reply_text="",
        raw={
            "agent_result": _contract_dict(action="reply", reply_text="hello from contract"),
            "bridge_session_id": _HostileBridgeValue(secret),
            "bridge_user_msg_id": _HostileBridgeValue(secret),
        },
    ))

    out = agent_worker.process_once(provider=provider, worker_id="w1", db_path=db, timeout_seconds=10)

    assert out["ok"] is True
    assert out["action"] == "completed"
    assert secret not in str(out)
    stored = agent_jobs.get_job(int(job["id"]), db_path=db)
    assert stored["status"] == agent_jobs.STATUS_DONE
    assert stored["result_text"] == "hello from contract"
    assert secret not in (stored["error"] or "")
    payload = stored.get("payload") or {}
    assert "bridge_session_id" not in payload
    assert "bridge_user_msg_id" not in payload
