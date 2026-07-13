"""Stage 2 strict Hermes AgentResult contract tests.

Anything that is not a valid AgentResult contract JSON document must be
rejected by ``HermesProvider.run``. Display-text noise (Hermes box frames,
ANSI escapes, tool logs, partial-JSON chatter) is never extracted into a
WeChat reply.

The strict provider accepts exactly one AgentResult JSON object and projects it
to ``AgentResult(ok=True, status="done"|"silent"|"escalated")``. Any other
output produces ``AgentResult(ok=False, status="failed")`` with a fixed stage.
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
from reliable_pipeline import AgentResultContract


def _strict_job(extra: dict | None = None):
    payload: dict = {"prompt": "question", "reliable_result_contract": True}
    if extra:
        payload.update(extra)
    return {"payload": payload}


def _contract(action="reply", reply_text="answer", reason_code="answered", risk_level="low"):
    return AgentResultContract(
        action=action,
        reply_text=reply_text,
        reason_code=reason_code,
        risk_level=risk_level,
    ).to_dict()


def _completed(stdout, stderr="", rc=0):
    return mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Display-text noise that the legacy extractor used to scrub must now simply
# fail strict parsing.  No reply_text, no status="done".
# ---------------------------------------------------------------------------


def test_strict_rejects_query_initializing_box():
    """Query lines + Hermes box frame are display-text noise, not a contract."""
    stdout = (
        "Query: 介绍一下产品\n"
        "Initializing agent…\n"
        "╭─ ⚕ Hermes ──────────────────────────╮\n"
        "│ 你好，这是回复内容。                  │\n"
        "╰─────────────────────────────────────╯\n"
    )
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.status == "failed"
    assert result.reply_text == ""
    assert result.raw["stage"] == "contract_violation"


def test_strict_rejects_ansi_escape_lines():
    """ANSI-coloured prose is display-text; strict mode must not salvage it."""
    stdout = "\x1b[32mQuery: hello\x1b[0m\nInitializing agent…\npreparing find…\n"
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.reply_text == ""
    assert result.raw["stage"] == "contract_violation"


def test_strict_rejects_resume_prompt_window():
    """Hermes resume-session prompt must NOT be promoted to a reply."""
    stdout = (
        "Query: 介绍一下产品\n"
        "preparing read_file…\n"
        "read some/path\n"
        "────────────────────────────────────────\n"
        "Resume session? [Y/n]\n"
    )
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.reply_text == ""
    assert result.raw["stage"] == "contract_violation"


def test_strict_rejects_tool_logs_only():
    """Tool-name chatter with no surrounding contract JSON is rejected."""
    stdout = "preparing read_file…\nread some/path\nfind another/path\n"
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.reply_text == ""
    assert result.raw["stage"] == "contract_violation"


def test_strict_rejects_prose_only_with_no_contract():
    """Plain Chinese reply without a contract wrapper is rejected, not extracted."""
    stdout = "这是最终回复。\n"
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.reply_text == ""
    assert result.raw["stage"] == "contract_violation"


def test_strict_rejects_partial_contract_with_extra_prose():
    """Even when JSON parses, trailing prose invalidates the whole document."""
    stdout = json.dumps(_contract("reply", "answer", "answered", "low")) + "\nextra chatter"
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.reply_text == ""
    assert result.raw["stage"] == "contract_violation"


def test_strict_rejects_tool_call_json_object_only():
    """A bare tool-call JSON (missing required fields) is not a contract."""
    stdout = json.dumps({"tool": "leann_search", "query": "x"})
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.reply_text == ""
    assert result.raw["stage"] == "contract_violation"


# ---------------------------------------------------------------------------
# Properties that USED to be guarded by the legacy extractors must now hold on
# the strict happy path: only an exact contract JSON yields a reply.
# ---------------------------------------------------------------------------


def test_strict_accepts_reply_contract_and_carries_wire_payload():
    """A clean reply contract exposes reply_text plus raw['agent_result']."""
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    contract = _contract("reply", "你好，这是回复内容。", "answered", "low")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(json.dumps(contract, ensure_ascii=False))):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is True
    assert result.status == "done"
    assert result.reply_text == "你好，这是回复内容。"
    assert result.raw["agent_result"]["action"] == "reply"
    assert result.raw["agent_result"]["reply_text"] == "你好，这是回复内容。"
    assert result.raw["strict"] is True
    assert result.raw["schema_version"] == 1


def test_strict_accepts_silent_contract_without_reply_text():
    """silent action carries no reply_text; consumer maps it to status='silent'."""
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    contract = _contract("silent", "", "smalltalk", "low")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(json.dumps(contract, ensure_ascii=False))):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is True
    assert result.status == "silent"
    assert result.reply_text == ""
    assert result.raw["agent_result"]["action"] == "silent"
    assert result.raw["agent_result"]["reason_code"] == "smalltalk"


def test_strict_accepts_escalate_contract_without_reply_text():
    """escalate action carries no reply_text; consumer maps to status='escalated'."""
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    contract = _contract("escalate", "", "needs_human", "high")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(json.dumps(contract, ensure_ascii=False))):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is True
    assert result.status == "escalated"
    assert result.reply_text == ""
    assert result.raw["agent_result"]["action"] == "escalate"
    assert result.raw["agent_result"]["risk_level"] == "high"


# ---------------------------------------------------------------------------
# Failure-shape invariants the strict mode must satisfy regardless of input.
# ---------------------------------------------------------------------------


def test_strict_rejects_nonzero_rc_with_clean_contract():
    """rc != 0 with a contract body still fails — Stage 2 must not salvage it."""
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    with mock.patch(
        "agent_provider.subprocess.run",
        return_value=_completed(json.dumps(_contract("reply", "answer", "answered", "low")), rc=1),
    ):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.raw["rc"] == 1
    assert result.raw["stage"] in {"rc_nonzero_with_stdout", "rc_nonzero_no_stdout"}


def test_strict_rejects_timeout_with_partial_stdout():
    """TimeoutExpired with answer-shaped stdout must not return reply_text."""
    provider = provider_module.HermesProvider(cli_path="hermes-test")
    timeout_exc = subprocess.TimeoutExpired("hermes-test", 5, output=b"answer", stderr=b"err")
    with mock.patch("agent_provider.subprocess.run", side_effect=timeout_exc):
        result = provider.run(_strict_job(), timeout=5)
    assert result.ok is False
    assert result.reply_text == ""
    assert result.raw["timeout"] is True
    assert result.raw.get("stage") in {None, "timeout"}


def test_strict_poll_accepts_contract_only_payload():
    """poll() under strict meta reads the whole-stdout contract, never box parts."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        provider = provider_module.HermesProvider(cli_path="hermes-test", hermes_home=str(home))
        session = "hermes-test-session"
        session_dir = home / ".wechat_agent_jobs" / session
        session_dir.mkdir(parents=True)
        (session_dir / "meta.json").write_text(
            json.dumps({"strict": True, "deadline_at": time.time() + 60}),
            encoding="utf-8",
        )
        result_path = session_dir / "result.json"
        contract = _contract("reply", "answer", "answered", "low")
        result_path.write_text(
            json.dumps({"rc": 0, "stdout": json.dumps(contract), "stderr": "", "latency": 0.1}),
            encoding="utf-8",
        )
        reply = provider.poll(session, 1)
        assert reply.ok is True
        assert reply.status == "done"
        assert reply.raw["agent_result"]["reply_text"] == "answer"
        # Display-text in the same shape is rejected, not parsed.
        result_path.write_text(
            json.dumps({"rc": 0, "stdout": "╭─ ⚕ Hermes ─╮\nanswer\n╰─╯", "stderr": "", "latency": 0.1}),
            encoding="utf-8",
        )
        invalid = provider.poll(session, 1)
        assert invalid.ok is False
        assert invalid.raw["stage"] == "contract_violation"
