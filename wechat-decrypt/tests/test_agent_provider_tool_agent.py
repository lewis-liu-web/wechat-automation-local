"""Tests for strict Hermes provider prompts and AgentResult handling."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_provider as ap


def test_build_prompt_includes_tool_description_in_tool_agent_mode():
    job = {
        "payload": {
            "prompt": "介绍一下工作号真实号",
            "clean_text": "介绍一下工作号真实号",
            "agent_mode": "tool_agent",
            "available_tools": [
                {"name": "leann_search", "description": "本地 LEANN 语义搜索工具"},
            ],
            "knowledge_hits": [],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "[可用工具]" in prompt
    assert "leann_search(index_name, query, top_k)" in prompt
    assert "[知识库片段]" not in prompt
    assert "[SILENT]" in prompt


def test_build_prompt_keeps_standard_mode_unchanged():
    job = {
        "payload": {
            "prompt": "介绍一下工作号真实号",
            "clean_text": "介绍一下工作号真实号",
            # available_tools should be ignored unless agent_mode == "tool_agent".
            "available_tools": [
                {"name": "leann_search", "description": "本地 LEANN 语义搜索工具"},
            ],
            "knowledge_hits": [
                {
                    "source": "local",
                    "kb_id": "scene.a",
                    "rel_path": "faq.md",
                    "content": "工作号真实号是一款号码认证产品。",
                }
            ],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "[知识库片段]" in prompt
    assert "[可用工具]" not in prompt
    assert "leann_search" not in prompt




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


def test_hermes_provider_run_is_strict_without_payload_flag():
    """HermesProvider.run is always strict; a missing reliable_result_contract flag does not select the legacy path."""
    provider = ap.HermesProvider(cli_path="hermes-test")
    with mock.patch("agent_provider.subprocess.run", return_value=_completed(_contract())):
        result = provider.run({"payload": {"prompt": "q"}}, timeout=5)
    assert result.ok is True
    assert result.status == "done"
    assert result.reply_text == "answer"
    assert result.raw.get("strict") is True
    assert result.raw["agent_result"]["action"] == "reply"


def test_hermes_provider_run_rejects_legacy_display_text_and_tool_call_json():
    """Legacy display-text, box frames, ANSI, and bare tool-call JSON are rejected regardless of payload/meta."""
    provider = ap.HermesProvider(cli_path="hermes-test")
    invalid_outputs = [
        "╭─ ⚕ Hermes ─╮\nanswer\n╰─╯",
        "\x1b[31merror\x1b[0m",
        "Warning: tool failed\nanswer",
        "plain Chinese reply",
        '{"tool":"leann_search","query":"x"}',
        _contract() + "\nextra trailing prose",
        json.dumps({"schema_version": 1, "action": "reply", "reply_text": "answer"}),
    ]
    for stdout in invalid_outputs:
        with mock.patch("agent_provider.subprocess.run", return_value=_completed(stdout)):
            result = provider.run({"payload": {"prompt": "q"}}, timeout=5)
        assert result.ok is False, stdout
        assert result.status == "failed"


def test_hermes_provider_run_and_poll_produce_equivalent_agent_result():
    """The same strict contract fixture produces equivalent AgentResult via run and submit/poll."""
    cases = [
        ("reply", "answer", "answered", "low", "done"),
        ("silent", "", "smalltalk", "low", "silent"),
        ("escalate", "", "risk", "high", "escalated"),
    ]
    for action, reply_text, reason_code, risk_level, expected_status in cases:
        contract = _contract(action, reply_text, reason_code, risk_level)
        job = {"id": 2, "payload": {"prompt": "q"}}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            provider = ap.HermesProvider(cli_path="hermes-test", hermes_home=str(home))
            with mock.patch("agent_provider.subprocess.run", return_value=_completed(contract)):
                run_result = provider.run(job, timeout=5)

            with mock.patch("agent_provider.subprocess.Popen") as mock_popen:
                mock_popen.return_value = mock.Mock(pid=123)
                sub = provider.submit(job, timeout=5)
            session_dir = home / ".wechat_agent_jobs" / sub.raw["bridge_session_id"]
            (session_dir / "result.json").write_text(
                json.dumps({"rc": 0, "stdout": contract, "stderr": "", "latency": 0.1}),
                encoding="utf-8",
            )
            poll_result = provider.poll(sub.raw["bridge_session_id"], 1)

        assert run_result.ok == poll_result.ok is True, action
        assert run_result.status == poll_result.status == expected_status, action
        assert run_result.reply_text == poll_result.reply_text == reply_text, action
        assert run_result.raw["agent_result"] == poll_result.raw["agent_result"], action


def test_hermes_provider_submit_persists_strict_meta_always():
    """HermesProvider.submit always writes strict=True in session meta.json regardless of incoming payload."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        provider = ap.HermesProvider(cli_path="hermes-test", hermes_home=str(home))
        with mock.patch("agent_provider.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock.Mock(pid=123)
            result = provider.submit({"id": 1, "payload": {"prompt": "q"}}, timeout=5)
        assert result.ok is True
        assert result.status == "submitted"
        meta_path = home / ".wechat_agent_jobs" / result.raw["bridge_session_id"] / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["strict"] is True
        prompt_path = home / ".wechat_agent_jobs" / result.raw["bridge_session_id"] / "prompt.txt"
        prompt = prompt_path.read_text(encoding="utf-8")
        assert "严格输出协议" in prompt


def test_hermes_provider_poll_is_strict_only():
    """HermesProvider.poll parses the whole stdout as AgentResult and rejects legacy display text."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        provider = ap.HermesProvider(cli_path="hermes-test", hermes_home=str(home))
        session = "hermes-test-session"
        session_dir = home / ".wechat_agent_jobs" / session
        session_dir.mkdir(parents=True)
        (session_dir / "meta.json").write_text(
            json.dumps({"strict": True, "deadline_at": time.time() + 60}),
            encoding="utf-8",
        )
        result_path = session_dir / "result.json"
        result_path.write_text(
            json.dumps({"rc": 0, "stdout": _contract(), "stderr": "", "latency": 0.1}),
            encoding="utf-8",
        )
        reply = provider.poll(session, 1)
        assert reply.ok is True
        assert reply.status == "done"
        assert reply.raw["agent_result"]["reply_text"] == "answer"

        result_path.write_text(
            json.dumps({"rc": 0, "stdout": "plain text reply", "stderr": "", "latency": 0.1}),
            encoding="utf-8",
        )
        invalid = provider.poll(session, 1)
        assert invalid.ok is False



def test_build_prompt_includes_wechat_mcp_hint_when_enabled():
    """When enable_wechat_mcp_tool is true, the prompt hints about the Hermes MCP server."""
    job = {
        "payload": {
            "prompt": "查一下上个月的记录",
            "clean_text": "查一下上个月的记录",
            "agent_mode": "tool_agent",
            "available_tools": [
                {"name": "leann_search", "description": "本地 LEANN 语义搜索工具"},
            ],
            "enable_wechat_mcp_tool": True,
            "knowledge_hits": [],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "[可用工具]" in prompt
    assert "leann_search(index_name, query, top_k)" in prompt
    assert "如果 Hermes 已配置 WeChat MCP server" in prompt
    assert "wechat_query_history(chat_name, keyword, limit)" not in prompt


def test_build_prompt_excludes_wechat_history_tool_by_default():
    """By default only leann_search is described in the tool_agent prompt."""
    job = {
        "payload": {
            "prompt": "介绍一下工作号真实号",
            "clean_text": "介绍一下工作号真实号",
            "agent_mode": "tool_agent",
            "available_tools": [
                {"name": "leann_search", "description": "本地 LEANN 语义搜索工具"},
            ],
            "knowledge_hits": [],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "leann_search(index_name, query, top_k)" in prompt
    assert "wechat_query_history" not in prompt


def test_build_prompt_prefers_prompt_for_non_tool_agent():
    """raw_agent/standard mode must keep prompt first so aggregator context is preserved."""
    job = {
        "payload": {
            "prompt": "full aggregator context",
            "clean_text": "short clean",
            "agent_mode": "raw_agent",
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "用户请求：full aggregator context" in prompt
    assert "short clean" not in prompt


def test_build_prompt_prefers_clean_text_in_tool_agent_mode():
    """tool_agent mode prefers clean_text because the monitor does not pre-retrieve KB hits."""
    job = {
        "payload": {
            "prompt": "full aggregator context",
            "clean_text": "short clean",
            "agent_mode": "tool_agent",
            "available_tools": [{"name": "leann_search", "description": "本地 LEANN 语义搜索工具"}],
            "knowledge_hits": [],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "用户请求：short clean" in prompt
    assert "full aggregator context" not in prompt


def test_build_prompt_lists_allowed_index_names():
    job = {
        "payload": {
            "prompt": "q",
            "clean_text": "q",
            "agent_mode": "tool_agent",
            "available_tools": [
                {"name": "leann_search", "allowed_index_names": ["idx_a", "idx_b"], "default_index_name": "idx_a"}
            ],
            "knowledge_hits": [],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "idx_a" in prompt
    assert "idx_b" in prompt
    assert "禁止搜索未列出的索引" in prompt


# Legacy HermesProvider.run tool-loop tests (removed in Stage 2 Phase B strict cutover).
# The strict-only provider no longer runs the Python tool loop; tool execution is
# delegated to Hermes's native MCP layer.


def test_hermes_args_include_skill_and_toolsets_in_order():

    provider = ap.HermesProvider(
        cli_path="hermes",
        profile="p1",
        model="m1",
        skill="wechat_auto",
        toolsets="wechat,leann",
    )
    args = provider._args("/tmp/prompt.txt")
    # Expected order: cli, --profile, chat, -q @file, -Q, --source, --accept-hooks, -m, -s, -t
    assert args[0] == "hermes"
    assert ["--profile", "p1"] == args[1:3]
    assert args[3] == "chat"
    profile_idx = args.index("--profile")
    chat_idx = args.index("chat")
    model_idx = args.index("-m")
    skill_idx = args.index("-s")
    toolsets_idx = args.index("-t")
    assert profile_idx < chat_idx < model_idx < skill_idx < toolsets_idx
    assert args[skill_idx + 1] == "wechat_auto"
    assert args[toolsets_idx + 1] == "wechat,leann"


def test_hermes_args_omit_skill_when_not_configured():
    """No -s should appear when skill is absent, preserving legacy command lines."""
    provider = ap.HermesProvider(cli_path="hermes", toolsets="wechat")
    args = provider._args()
    assert "-s" not in args
    assert "-t" in args


def test_provider_from_config_passes_skill_and_toolsets():
    """Instance config skill/toolsets must reach HermesProvider."""
    config = {
        "agent_provider": {
            "instances": [
                {
                    "id": "hermes-1",
                    "provider": "hermes",
                    "skill": "wechat_auto",
                    "toolsets": "wechat,leann",
                }
            ]
        }
    }
    provider = ap.provider_from_config(config, instance_id="hermes-1")
    assert isinstance(provider, ap.HermesProvider)
    assert provider.skill == "wechat_auto"
    assert provider.toolsets == "wechat,leann"
    args = provider._args()
    assert "-s" in args
    assert "wechat_auto" in args
    assert "wechat,leann" in args


def test_provider_from_config_legacy_providers_passes_skill_and_toolsets():
    """Legacy providers.hermes path must also read skill/toolsets."""
    config = {
        "agent_provider": {
            "default": "hermes",
            "providers": {
                "hermes": {
                    "cli_path": "hermes",
                    "skill": "wechat_auto",
                    "toolsets": "wechat,leann",
                }
            },
        }
    }
    provider = ap.provider_from_config(config)
    assert isinstance(provider, ap.HermesProvider)
    assert provider.skill == "wechat_auto"
    assert provider.toolsets == "wechat,leann"
    args = provider._args()
    assert "-s" in args
    assert "wechat_auto" in args
    assert "wechat,leann" in args


def test_build_prompt_path_hint_mentions_multimodal_and_decode_image_limits():
    """When image_paths exist without descriptions, the prompt must guide the agent."""
    job = {
        "payload": {
            "prompt": "看看这张图",
            "clean_text": "看看这张图",
            "agent_mode": "tool_agent",
            "image_paths": ["/tmp/img.jpg"],
            "image_descriptions": [],
            "available_tools": [{"name": "leann_search", "description": "本地 LEANN 语义搜索工具"}],
            "knowledge_hits": [],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "本地解密路径如下" in prompt
    assert "/tmp/img.jpg" in prompt
    assert "多模态 provider" in prompt
    assert "decode_image" in prompt
    assert "decode_image(chat_name, local_id)" in prompt
    assert "decode_image(image_path)" not in prompt
    assert "不能替代视觉理解" in prompt
    assert "不要编造图片内容" in prompt


# Stage 3 MCP trace + knowledge facade prompt wiring.

def test_prepare_model_job_passes_trace_env_to_subprocess():
    """_prepare_model_job must forward the worker-provided trace env to the Hermes subprocess."""
    provider = ap.HermesProvider(cli_path="hermes-test")
    job = {
        "payload": {
            "_knowledge_trace_id": "trace-123",
            "_knowledge_trace_dir": "/tmp/traces",
            "_config_path": "/tmp/cfg.json",
            "_allowed_kb_ids": ["scene.a"],
        }
    }
    _, env = provider._prepare_model_job(job)
    assert env["WECHAT_MCP_TRACE_ID"] == "trace-123"
    assert env["WECHAT_MCP_TRACE_DIR"] == "/tmp/traces"
    assert env["WECHAT_MCP_ALLOWED_KB_IDS"] == json.dumps(["scene.a"])


def test_prepare_model_job_requires_tool_call_for_grounded_authorized_kb():
    provider = ap.HermesProvider(cli_path="hermes-test")
    _, env = provider._prepare_model_job({
        "payload": {
            "_allowed_kb_ids": ["scene.a"],
            "target_policy": {"reply_policy": "knowledge_grounded"},
        }
    })
    assert env["HERMES_TOOL_CHOICE_REQUIRED"] == "1"


def test_prepare_model_job_does_not_require_tool_for_non_grounded_kb():
    provider = ap.HermesProvider(cli_path="hermes-test")
    _, env = provider._prepare_model_job({
        "payload": {
            "_allowed_kb_ids": ["scene.a"],
            "target_policy": {"reply_policy": "balanced"},
        }
    })
    assert "HERMES_TOOL_CHOICE_REQUIRED" not in env


def test_prepare_model_job_reads_trace_env_from_os_environ():
    """_prepare_model_job falls back to os.environ for trace env when the payload does not set them."""
    provider = ap.HermesProvider(cli_path="hermes-test")
    with mock.patch.dict(os.environ, {"WECHAT_MCP_TRACE_ID": "trace-999", "WECHAT_MCP_TRACE_DIR": "/var/traces"}):
        _, env = provider._prepare_model_job({"payload": {}})
    assert env["WECHAT_MCP_TRACE_ID"] == "trace-999"
    assert env["WECHAT_MCP_TRACE_DIR"] == "/var/traces"


def test_build_strict_agent_result_includes_trace_id():
    """A strict AgentResult must carry the trace identifiers so the worker can correlate the MCP trace."""
    if ap.AgentResultContract is None:
        pytest.skip("reliable_pipeline not available")
    with mock.patch.dict(os.environ, {"WECHAT_MCP_TRACE_ID": "trace-123", "WECHAT_MCP_TRACE_DIR": "/tmp/traces"}):
        provider = ap.HermesProvider(cli_path="hermes-test")
        contract = ap.AgentResultContract(action="reply", reply_text="hello", reason_code="answered", risk_level="low")
        result = provider._build_strict_agent_result(contract, latency=0.1, extra_raw={})
    assert result.raw["trace_id"] == "trace-123"
    assert result.raw["trace_dir"] == "/tmp/traces"


def test_strict_failed_result_includes_trace_id():
    """Failure results also carry trace identifiers for correlation."""
    with mock.patch.dict(os.environ, {"WECHAT_MCP_TRACE_ID": "trace-456", "WECHAT_MCP_TRACE_DIR": "/tmp/traces"}):
        result = ap._strict_failed_result("err", latency=0.1, raw={"x": 1}, provider="hermes", worker_id="w")
    assert result.raw["trace_id"] == "trace-456"
    assert result.raw["trace_dir"] == "/tmp/traces"
    assert result.raw["x"] == 1


def test_strict_prompt_requires_facade_and_cites_provenance():
    """The strict prompt must instruct the model to use the MCP knowledge facade and cite provenance."""
    job = {
        "payload": {
            "prompt": "产品价格",
            "reliable_result_contract": True,
            "allowed_kb_ids": ["scene.a"],
        }
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "mcp__wechat_kb_search__search_knowledge" in prompt
    assert "provenance" in prompt
    assert "ok/no_hit/provider_failure/invalid" in prompt
    assert "不预取知识库片段" in prompt


def test_strict_knowledge_grounded_prompt_requires_one_search_before_result():
    job = {
        "payload": {
            "prompt": "产品价格",
            "reliable_result_contract": True,
            "allowed_kb_ids": ["scene.a"],
            "target_policy": {"reply_policy": "knowledge_grounded"},
        }
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "必须调用一次工具 mcp__wechat_kb_search__search_knowledge" in prompt
    assert "reply、silent 或 escalate AgentResult 前" in prompt


def test_strict_non_grounded_prompt_keeps_search_optional():
    job = {
        "payload": {
            "prompt": "产品价格",
            "reliable_result_contract": True,
            "allowed_kb_ids": ["scene.a"],
            "target_policy": {"reply_policy": "balanced"},
        }
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "当需要基于知识库回答时" in prompt
    assert "必须调用一次工具" not in prompt


def test_strict_prompt_drops_prefetched_knowledge_hits():
    """Strict mode must not leak pre-fetched knowledge_hits into the prompt."""
    job = {
        "payload": {
            "prompt": "产品价格",
            "reliable_result_contract": True,
            "allowed_kb_ids": ["scene.a"],
            "knowledge_hits": [{"content": "secret prefetched hit"}],
        }
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "[知识库片段]" not in prompt
    assert "secret prefetched hit" not in prompt
    assert "不预取知识库片段" in prompt


def test_hermes_provider_run_generic_exception_carries_stage_runner_error():
    """A generic runtime exception in HermesProvider.run is tagged as runner_error."""
    provider = ap.HermesProvider(cli_path="hermes-test")
    exc = TimeoutError()  # not TimeoutExpired, so falls to generic runner error
    with mock.patch("agent_provider.subprocess.run", side_effect=exc):
        result = provider.run({"payload": {"prompt": "q"}}, timeout=5)
    assert result.ok is False
    assert result.raw.get("stage") == "runner_error"


def test_hermes_provider_run_subprocess_timeout_expired_carries_stage_timeout():
    """A subprocess.TimeoutExpired in HermesProvider.run is tagged as timeout."""
    import subprocess
    provider = ap.HermesProvider(cli_path="hermes-test")
    exc = subprocess.TimeoutExpired(cmd=["hermes-test"], timeout=5)
    with mock.patch("agent_provider.subprocess.run", side_effect=exc):
        result = provider.run({"payload": {"prompt": "q"}}, timeout=5)
    assert result.ok is False
    assert result.raw.get("stage") == "timeout"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
