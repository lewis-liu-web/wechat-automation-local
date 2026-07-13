"""Tests for strict Hermes provider prompts and AgentResult handling."""

import json
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
    contract = _contract()
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        provider = ap.HermesProvider(cli_path="hermes-test", hermes_home=str(home))
        with mock.patch("agent_provider.subprocess.run", return_value=_completed(contract)):
            run_result = provider.run({"payload": {"prompt": "q"}}, timeout=5)

        with mock.patch("agent_provider.subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock.Mock(pid=123)
            sub = provider.submit({"id": 2, "payload": {"prompt": "q"}}, timeout=5)
        session_dir = home / ".wechat_agent_jobs" / sub.raw["bridge_session_id"]
        (session_dir / "result.json").write_text(
            json.dumps({"rc": 0, "stdout": contract, "stderr": "", "latency": 0.1}),
            encoding="utf-8",
        )
        poll_result = provider.poll(sub.raw["bridge_session_id"], 1)

    assert run_result.ok == poll_result.ok is True
    assert run_result.status == poll_result.status == "done"
    assert run_result.reply_text == poll_result.reply_text == "answer"
    assert run_result.raw["agent_result"] == poll_result.raw["agent_result"]


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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
