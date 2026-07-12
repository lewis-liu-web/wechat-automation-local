"""Tests for the Stage 1 reliable-pipeline control_api endpoints.

These tests only exercise the new ``/reliable-pipeline/*`` routes added
in the Stage 1 integration. They MUST NOT touch the older
``/agent/worker/run-once``, ``/agent/jobs``, or M5 async-loop routes.

The reliable pipeline is gated behind ``config['reliable_pipeline']
['enabled']`` (``is True``); until it is enabled the worker and sender
run-once endpoints return ``{"action": "disabled"}`` without invoking
the worker / sender / provider.

``test_target_only`` is read from the caller's config — the request
body has no field that can override it, and the gate is enforced inside
``reliable_worker.send_once`` itself.

All status / run-once responses are whitelisted to scalar identifiers
plus numeric counters and timestamps; ``reply_text``, ``payload_json``,
``result_json``, ``error``, ``reason``, ``attempted``, and ``exception``
never leave the control plane.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import control_api  # noqa: E402
import reliable_pipeline  # noqa: E402
import reliable_worker  # noqa: E402
import reply_engine  # noqa: E402


# --- Helpers ----------------------------------------------------------------


def _route(method: str, path: str, body: dict | None = None):
    """Dispatch a control_api route directly without starting a socket."""
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _patch_config(monkeypatch, *, enabled: bool = False, db_path: str | None = None,
                  test_target_only: bool = False) -> dict:
    """Replace ``control_api.reg.load_config`` with a stub returning a
    fixed in-memory config.

    We must patch the function itself (not just ``reg.CONFIG_PATH``)
    because ``target_registry.load_config`` binds ``CONFIG_PATH`` as a
    default argument at function-definition time, so changing the
    module constant after import does not change the captured path.
    Stubbing the function is the only reliable way to make the control
    plane read the test config.
    """
    cfg = {
        "targets": [],
        "default_triggers": [],
        "knowledge_bases": {},
        "reliable_pipeline": {
            "enabled": enabled,
            "test_target_only": test_target_only,
        },
    }
    if db_path is not None:
        cfg["reliable_pipeline"]["db_path"] = db_path
    monkeypatch.setattr(control_api.reg, "load_config", lambda *a, **k: cfg)
    return cfg


# --- Status route -----------------------------------------------------------


class TestStatusRoute:
    def test_status_returns_counts_and_dead_letters_with_configured_db_path(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "rp.sqlite")
        _patch_config(monkeypatch, db_path=db_path)

        # Stub the DB calls and capture db_path to prove the configured
        # path is forwarded (not the default DB).
        counts_calls = []
        dl_calls = []

        def fake_counts(*, db_path=None):
            counts_calls.append(db_path)
            return {"send_outbox": {"dead_letter": 2}}

        def fake_dl(*, limit=50, db_path=None):
            dl_calls.append((limit, db_path))
            return [
                {"id": 11, "job_id": 7, "target_id": "wxid_t", "group_key": "wxid_t",
                 "status": "dead_letter", "attempts": 5, "max_attempts": 5,
                 "created_at": 1.0, "dead_at": 2.0, "next_attempt_at": 3.0,
                 "reply_text": "LEAK", "error": "LEAK", "result_json": "LEAK",
                 "outbox_key": "LEAK", "mention_name": "LEAK", "lease_owner": "LEAK"},
            ]

        monkeypatch.setattr(control_api.reliable_pipeline, "counts", fake_counts)
        monkeypatch.setattr(control_api.reliable_pipeline, "list_dead_letters", fake_dl)

        status, resp = _route("GET", "/reliable-pipeline/status")
        assert status == 200
        assert resp["ok"] is True
        assert resp["enabled"] is False
        assert resp["db_status"] == "ok"
        assert resp["counts"]["send_outbox"]["dead_letter"] == 2
        assert isinstance(resp["dead_letters"], list)
        assert len(resp["dead_letters"]) == 1
        dl = resp["dead_letters"][0]
        # Strict whitelist: only ids/status/numerics/timestamps leak.
        assert dl == {"id": 11, "job_id": 7, "target_id": "wxid_t",
                      "group_key": "wxid_t", "status": "dead_letter",
                      "attempts": 5, "max_attempts": 5,
                      "created_at": 1.0, "dead_at": 2.0, "next_attempt_at": 3.0}
        # No body leak in any field.
        assert "LEAK" not in json.dumps(resp)
        # Configured db_path was forwarded to BOTH calls.
        assert [str(value) for value in counts_calls] == [str(Path(db_path))]
        assert [(limit, str(value)) for limit, value in dl_calls] == [(50, str(Path(db_path)))]

    def test_status_returns_unavailable_marker_on_db_error(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "rp.sqlite")
        _patch_config(monkeypatch, db_path=db_path)

        def boom(**kwargs):
            raise RuntimeError("disk is on fire at /secret/path")

        monkeypatch.setattr(control_api.reliable_pipeline, "counts", boom)
        monkeypatch.setattr(control_api.reliable_pipeline, "list_dead_letters", boom)

        status, resp = _route("GET", "/reliable-pipeline/status")
        assert status == 200
        assert resp["ok"] is True
        assert resp["db_status"] == "unavailable"
        assert resp["counts"] == {}
        assert resp["dead_letters"] == []
        # No exception text or filesystem path leaks.
        body = json.dumps(resp)
        assert "disk is on fire" not in body
        assert "/secret/path" not in body
        assert "RuntimeError" not in body


# --- Worker run-once route --------------------------------------------------


class TestWorkerRunOnceRoute:
    def test_worker_disabled_returns_action_disabled(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=False)

        called = MagicMock()
        monkeypatch.setattr(control_api.reliable_worker, "process_once", called)

        status, resp = _route("POST", "/reliable-pipeline/worker/run-once", {})
        assert status == 200
        assert resp["ok"] is True
        assert resp["action"] == "disabled"
        assert resp["enabled"] is False
        called.assert_not_called()

    def test_worker_enabled_calls_process_once_with_postcheck_final_filter(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=True)

        captured = {}

        def fake_process_once(*, provider, owner, config, provider_factory, final_filter, timeout, **kwargs):
            captured["provider"] = provider
            captured["owner"] = owner
            captured["config"] = config
            captured["provider_factory"] = provider_factory
            captured["final_filter"] = final_filter
            captured["timeout"] = timeout
            return {"status": "empty", "job": None, "outbox": None}

        monkeypatch.setattr(control_api.reliable_worker, "process_once", fake_process_once)
        monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: MagicMock(name="prov"))

        status, resp = _route(
            "POST",
            "/reliable-pipeline/worker/run-once",
            {"owner": "test-worker", "timeout": 17.5},
        )
        assert status == 200
        assert resp["ok"] is True
        assert resp["action"] == "processed"
        assert resp["status"] == "empty"
        # final_filter is EXACTLY reply_engine.postcheck (not a wrapper).
        assert captured["final_filter"] is reply_engine.postcheck
        assert captured["timeout"] == 17.5
        assert captured["owner"] == "test-worker"
        assert captured["config"] is control_api.reg.load_config()
        # provider_factory must be a callable bound to this control_api's cfg.
        assert callable(captured["provider_factory"])

    def test_worker_default_owner_and_timeout(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=True)

        captured = {}

        def fake(**kw):
            captured.update(kw)
            return {"status": "empty", "job": None, "outbox": None}

        monkeypatch.setattr(control_api.reliable_worker, "process_once", fake)
        monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: MagicMock())

        _route("POST", "/reliable-pipeline/worker/run-once", {})
        assert captured["owner"] == control_api.reliable_worker.DEFAULT_REPLY_OWNER
        assert captured["timeout"] is None
        assert captured["final_filter"] is reply_engine.postcheck
        assert callable(captured["provider_factory"])

    def test_worker_sanitizes_process_once_response(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=True)

        body_job = {
            "id": 7, "turn_id": 3, "target_id": "wxid_t", "group_key": "wxid_t",
            "status": "done", "attempts": 1, "deadline_at": 456.0,
            "created_at": 1.0, "started_at": 2.0, "finished_at": 3.0,
            "error": "invalid agent result contract",
            "payload_json": '{"prompt": "user text"}',
            "result_json": '{"reply_text": "agent reply"}',
            "lease_owner": "worker-1", "job_key": "k",
        }
        body_outbox = {
            "id": 11, "job_id": 7, "target_id": "wxid_t", "group_key": "wxid_t",
            "before_local_id": 99, "status": "pending",
            "attempts": 0, "max_attempts": 5, "next_attempt_at": 4.0,
            "created_at": 1.0, "sent_at": None, "dead_at": None,
            "reply_text": "the actual reply body",
            "mention_name": "@user",
            "error": "send failed: secret",
            "result_json": '{"attempted":["x"]}',
            "lease_owner": "sender-1", "outbox_key": "ok",
        }

        monkeypatch.setattr(
            control_api.reliable_worker, "process_once",
            lambda **kw: {"status": "applied", "job": body_job, "outbox": body_outbox},
        )
        monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: MagicMock())

        status, resp = _route("POST", "/reliable-pipeline/worker/run-once", {})
        assert status == 200
        assert resp["action"] == "processed"
        body = json.dumps(resp)
        # No body leak.
        assert "secret" not in body
        assert "user text" not in body
        assert "agent reply" not in body
        assert "the actual reply body" not in body
        assert "@user" not in body
        assert "send failed" not in body
        # Whitelisted keys are present.
        assert resp["job"]["id"] == 7
        assert resp["job"]["status"] == "done"
        assert resp["outbox"]["id"] == 11
        assert resp["outbox"]["status"] == "pending"
        # Non-whitelisted keys are gone.
        for forbidden in ("error", "payload_json", "result_json", "lease_owner",
                          "job_key", "reply_text", "mention_name", "outbox_key"):
            assert forbidden not in resp["job"], forbidden
            assert forbidden not in resp["outbox"], forbidden

    def test_worker_rejects_non_numeric_timeout(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=True)
        monkeypatch.setattr(
            control_api.reliable_worker, "process_once",
            MagicMock(return_value={"status": "empty", "job": None, "outbox": None}),
        )
        status, resp = _route(
            "POST", "/reliable-pipeline/worker/run-once", {"timeout": "not-a-number"},
        )
        assert status == 200
        assert resp["ok"] is False
        assert resp["action"] == "error"
        assert "timeout" in resp["error"]

    def test_worker_provider_exception_is_reported_not_leaked(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=True)

        def fake_process_once(**kw):
            raise RuntimeError("db crashed: /private/secret")

        monkeypatch.setattr(control_api.reliable_worker, "process_once", fake_process_once)
        monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: MagicMock())

        status, resp = _route("POST", "/reliable-pipeline/worker/run-once", {})
        assert status == 200
        assert resp["ok"] is False
        assert resp["action"] == "error"
        assert "/private/secret" not in resp["error"]

# --- Sender run-once route --------------------------------------------------


class TestSenderRunOnceRoute:
    def test_sender_disabled_returns_action_disabled(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=False)

        called = MagicMock()
        monkeypatch.setattr(control_api.reliable_worker, "send_once", called)

        status, resp = _route("POST", "/reliable-pipeline/sender/run-once", {})
        assert status == 200
        assert resp["ok"] is True
        assert resp["action"] == "disabled"
        called.assert_not_called()

    def test_sender_enabled_calls_send_once_with_config_test_target_only(self, monkeypatch, tmp_path):
        # test_target_only set in config; body MUST NOT override it.
        _patch_config(monkeypatch, enabled=True, test_target_only=True)

        captured = {}

        def fake(**kw):
            captured.update(kw)
            return {"status": "empty", "processed": [], "skipped": []}

        monkeypatch.setattr(control_api.reliable_worker, "send_once", fake)

        status, resp = _route(
            "POST",
            "/reliable-pipeline/sender/run-once",
            {"owner": "test-sender", "limit": 7, "test_target_only": False},
        )
        assert status == 200
        assert resp["ok"] is True
        assert resp["action"] == "processed"
        assert captured["owner"] == "test-sender"
        assert captured["limit"] == 7
        # test_target_only from config is preserved; body cannot override.
        assert captured["config"]["reliable_pipeline"]["test_target_only"] is True
        # Body did not surface a test_target_only field either.
        assert "test_target_only" not in captured

    def test_sender_sanitizes_processed_and_skipped(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=True)

        fake_out = {
            "status": "processed",
            "processed": [
                {"outbox_id": 5, "target_id": "wxid_t", "ok": True,
                 "reason": "confirmed: the reply body",
                 "attempted": ["mode_a", "mode_b"],
                 "exception": "repr-of-exception",
                 "row": {"id": 5, "reply_text": "LEAK",
                         "result_json": "LEAK", "error": "LEAK"}},
            ],
            "skipped": [
                {"outbox_id": 6, "target_id": "wxid_u",
                 "error": "test_mode_target_rejected: target name \"chat\"",
                 "row": {"id": 6, "reply_text": "LEAK"}},
            ],
        }
        monkeypatch.setattr(
            control_api.reliable_worker, "send_once",
            lambda **kw: fake_out,
        )

        status, resp = _route("POST", "/reliable-pipeline/sender/run-once", {})
        assert status == 200
        body = json.dumps(resp)
        assert "LEAK" not in body
        assert "confirmed: the reply body" not in body
        assert "mode_a" not in body
        assert "repr-of-exception" not in body
        assert "test_mode_target_rejected" not in body
        # Whitelist preserved.
        assert resp["processed"][0]["outbox_id"] == 5
        assert resp["processed"][0]["target_id"] == "wxid_t"
        assert resp["processed"][0]["ok"] is True
        assert resp["processed"][0]["row"] == {"id": 5}
        assert resp["skipped"][0]["row"] == {"id": 6}

    def test_sender_rejects_non_numeric_limit(self, monkeypatch, tmp_path):
        _patch_config(monkeypatch, enabled=True)
        status, resp = _route(
            "POST", "/reliable-pipeline/sender/run-once", {"limit": "abc"},
        )
        assert status == 200
        assert resp["ok"] is False
        assert resp["action"] == "error"
        assert "limit" in resp["error"]


# --- Legacy endpoints unchanged --------------------------------------------


def test_legacy_agent_worker_run_once_unaffected(monkeypatch, tmp_path):
    """The new ``/reliable-pipeline/*`` routes MUST NOT touch the
    legacy ``/agent/worker/run-once`` route.  If our edits had
    accidentally wired it to ``reliable_worker.process_once``, the
    agent_worker mock below would never be hit.
    """
    _patch_config(monkeypatch, enabled=True)
    monkeypatch.setattr(control_api, "_build_agent_provider", lambda *a, **k: MagicMock())
    legacy_called = MagicMock(return_value={"status": "ok"})
    monkeypatch.setattr(control_api.agent_worker, "process_once", legacy_called)
    # The reliable_worker.process_once MUST NOT be invoked by this route.
    reliable_called = MagicMock()
    monkeypatch.setattr(control_api.reliable_worker, "process_once", reliable_called)

    status, resp = _route("POST", "/agent/worker/run-once", {})
    assert status == 200
    legacy_called.assert_called_once()
    reliable_called.assert_not_called()


def test_legacy_agent_jobs_route_still_resolves(monkeypatch, tmp_path):
    """Sanity: the legacy ``/agent/jobs`` route still resolves without
    raising, and the new reliable-pipeline routes do not interfere."""
    _patch_config(monkeypatch, enabled=True)
    status, resp = _route("GET", "/agent/jobs")
    assert status == 200