import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_provider
import control_api


HERMES_PROFILE_LIST = """
 Profile          Model                        Gateway      Alias        Distribution
 ───────────────    ───────────────────────────    ───────────    ───────────    ────────────────────
 ◆default         mimo-v2.5-pro                running      —            —
  wechat-bot-worker1 mimo-v2.5-pro                stopped      wechat-bot-worker1 —
  wechat-bot-worker2 mimo-v2.5-pro                stopped      wechat-bot-worker2 —
"""


def test_parse_hermes_profile_list_output():
    profiles = agent_provider.parse_hermes_profile_list(HERMES_PROFILE_LIST)

    assert [p["profile"] for p in profiles] == ["default", "wechat-bot-worker1", "wechat-bot-worker2"]
    assert profiles[0]["id"] == "hermes-default"
    assert profiles[0]["provider"] == "hermes"
    assert profiles[0]["model"] == "mimo-v2.5-pro"
    assert profiles[0]["gateway_status"] == "running"
    assert profiles[0]["alias"] == ""
    assert profiles[1]["id"] == "hermes-wechat-bot-worker1"
    assert profiles[1]["label"] == "wechat-bot-worker1"
    assert profiles[1]["worker_id"] == "wechat-bot-worker1"


def test_discover_hermes_profiles_uses_cli(monkeypatch):
    class Result:
        returncode = 0
        stdout = HERMES_PROFILE_LIST
        stderr = ""

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(agent_provider.subprocess, "run", fake_run)

    profiles = agent_provider.discover_hermes_profiles(cli_path="hermes-test", timeout=2)

    assert calls == [["hermes-test", "profile", "list"]]
    assert len(profiles) == 3
    assert profiles[2]["profile"] == "wechat-bot-worker2"


def test_control_api_hermes_profile_discovery_endpoint(monkeypatch):
    expected = [{"id": "hermes-default", "provider": "hermes", "profile": "default"}]
    monkeypatch.setattr(control_api, "discover_hermes_profiles", lambda: expected)

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body, status, _ = handler._route("GET", "/agent-providers/hermes/profiles", {}, {})

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is True
    assert payload["profiles"] == expected


def test_control_api_register_agent_instance_is_idempotent(tmp_path, monkeypatch):
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({"agent_provider": {"instances": []}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)

    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body = {
        "instance": {
            "id": "hermes-wechat-bot-worker1",
            "provider": "hermes",
            "profile": "wechat-bot-worker1",
            "worker_id": "wechat-bot-worker1",
            "label": "wechat-bot-worker1",
        }
    }

    first_body, first_status, _ = handler._route("POST", "/agent/instances", {}, body)
    second_body, second_status, _ = handler._route("POST", "/agent/instances", {}, body)

    assert first_status == 200
    assert second_status == 200
    assert json.loads(first_body.decode("utf-8"))["created"] is True
    assert json.loads(second_body.decode("utf-8"))["created"] is False
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["agent_provider"]["instances"] == [body["instance"]]


def test_async_loop_start_defaults_to_registered_instances(tmp_path, monkeypatch):
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({
        "agent_provider": {
            "instances": [
                {"id": "hermes-wechat-bot-worker1", "provider": "hermes", "profile": "wechat-bot-worker1"},
                {"id": "hermes-wechat-bot-worker2", "provider": "hermes", "profile": "wechat-bot-worker2"},
            ]
        }
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(control_api, "_ASYNC_LOOP_THREAD", None)

    class DummyThread:
        daemon = True
        def start(self):
            pass
        def is_alive(self):
            return False

    monkeypatch.setattr(control_api.threading, "Thread", lambda *a, **k: DummyThread())
    control_api._ASYNC_LOOP_STATE.clear()

    result = control_api._async_loop_start({})

    assert control_api._ASYNC_LOOP_STATE["config"]["instance_ids"] == ["hermes-wechat-bot-worker1", "hermes-wechat-bot-worker2"]


def test_async_loop_start_falls_back_to_discovered_profiles(tmp_path, monkeypatch):
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(json.dumps({"agent_provider": {"instances": []}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(control_api.reg, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(control_api, "_ASYNC_LOOP_THREAD", None)
    monkeypatch.setattr(control_api, "discover_hermes_profiles", lambda: [
        {"id": "hermes-wechat-bot-worker1", "provider": "hermes", "profile": "wechat-bot-worker1"},
        {"id": "hermes-wechat-bot-worker2", "provider": "hermes", "profile": "wechat-bot-worker2"},
    ])

    class DummyThread:
        daemon = True
        def start(self):
            pass
        def is_alive(self):
            return False

    monkeypatch.setattr(control_api.threading, "Thread", lambda *a, **k: DummyThread())
    control_api._ASYNC_LOOP_STATE.clear()

    control_api._async_loop_start({})

    assert control_api._ASYNC_LOOP_STATE["config"]["instance_ids"] == ["hermes-wechat-bot-worker1", "hermes-wechat-bot-worker2"]
    assert "hermes-worker1" not in control_api._ASYNC_LOOP_STATE["config"]["instance_ids"]
