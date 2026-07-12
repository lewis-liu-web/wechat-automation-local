"""Hermes strict AgentResult contract tests.

Stage 2 owns the strict ``AgentResult`` wire behavior of
``HermesProvider.run`` / ``HermesProvider.poll`` (contract JSON required,
display-text rejected).  Legacy display-text extraction symbols in
``agent_provider`` are no longer exercised by these tests; see
``tests/test_agent_output_sanitization.py`` for the rejection shapes.
"""



import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_provider as provider_module


def _strict_job():
    return {"payload": {"prompt": "question", "reliable_result_contract": True}}


def _contract(action="reply", reply_text="answer", reason_code="answered", risk_level="low"):
    return json.dumps({
        "schema_version": 1,
        "action": action,
        "reply_text": reply_text,
        "reason_code": reason_code,
        "risk_level": risk_level,
    })


def _completed(stdout, stderr="", rc=0):
    return mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)


def test_strict_run_accepts_reply_and_preserves_contract():
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(_contract())):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is True
    assert result.status == "done"
    assert result.reply_text == "answer"
    assert result.raw["agent_result"]["action"] == "reply"


def test_strict_run_maps_silent_and_escalate_without_reply_text():
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(_contract("silent", "", "smalltalk"))):
        silent = provider.run(_strict_job(), timeout=5)
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(_contract("escalate", "", "risk", "high"))):
        escalated = provider.run(_strict_job(), timeout=5)
    assert (silent.ok, silent.status, silent.reply_text) == (True, "silent", "")
    assert (escalated.ok, escalated.status, escalated.reply_text) == (True, "escalated", "")
    assert escalated.raw["agent_result"]["risk_level"] == "high"


def test_strict_run_rejects_nonzero_timeout_and_terminal_display_text():
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    invalid_stdout = [
        "╭─ ⚕ Hermes ─╮\nanswer\n╰─╯",
        "\x1b[31merror\x1b[0m",
        "Warning: tool failed\nanswer",
        "answer",
        '{"tool":"leann_search","query":"x"}',
        _contract() + "\nextra",
        json.dumps({"schema_version": 1, "action": "reply", "reply_text": "answer"}),
        json.dumps({"schema_version": 1, "action": "reply", "reply_text": "answer", "reason_code": "x", "risk_level": "low", "extra": True}),
    ]
    for text in invalid_stdout:
        with mock.patch("agent_provider.subprocess.run", return_value=_completed(text)):
            result = provider.run(_strict_job(), timeout=5)
        assert result.ok is False, text
        assert result.status == "failed"
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(_contract(), rc=1)):
        failed = provider.run(_strict_job(), timeout=5)
    assert failed.ok is False
    assert failed.raw["rc"] == 1
    timed_out = subprocess.TimeoutExpired("hermes-test", 5, output=b"answer", stderr=b"err")
    with mock.patch("agent_provider.subprocess.run", side_effect=timed_out):
        timeout = provider.run(_strict_job(), timeout=5)
    assert timeout.ok is False
    assert timeout.status == "failed"
    assert timeout.raw["timeout"] is True


def test_strict_poll_uses_whole_stdout_contract_only():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        provider = provider_module.HermesProvider(cli_path="hermes-test", hermes_home=str(home))
        session = "hermes-test-session"
        session_dir = home / ".wechat_agent_jobs" / session
        session_dir.mkdir(parents=True)
        (session_dir / "meta.json").write_text(json.dumps({"strict": True, "deadline_at": time.time() + 60}), encoding="utf-8")
        result_path = session_dir / "result.json"
        result_path.write_text(json.dumps({"rc": 0, "stdout": _contract(), "stderr": "", "latency": 0.1}), encoding="utf-8")
        reply = provider.poll(session, 1)
        assert reply.ok is True
        assert reply.status == "done"
        assert reply.raw["agent_result"]["reply_text"] == "answer"
        result_path.write_text(json.dumps({"rc": 0, "stdout": "╭─ ⚕ Hermes ─╮\nanswer\n╰─╯", "stderr": "", "latency": 0.1}), encoding="utf-8")
        invalid = provider.poll(session, 1)
        assert invalid.ok is False
        result_path.write_text(json.dumps({"rc": 1, "stdout": _contract(), "stderr": "failure", "latency": 0.1}), encoding="utf-8")
        nonzero = provider.poll(session, 1)
        assert nonzero.ok is False


def test_strict_prompt_replaces_legacy_no_json_instruction():
    prompt = provider_module._build_wechat_deep_prompt(_strict_job())
    assert "仅输出一个符合下方严格协议的 JSON 对象" in prompt
    assert "不要输出思考过程、工具日志、Markdown 计划或 JSON 外壳" not in prompt
