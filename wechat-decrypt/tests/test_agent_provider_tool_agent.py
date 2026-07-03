"""Tests for agent_provider tool_agent / leann_search POC."""

import json
import sys
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


def test_extract_tool_call_parses_leann_search_json():
    text = (
        "我需要查一下资料。\n"
        '{"tool": "leann_search", "index_name": "product_kb", "query": "工作号真实号", "top_k": 3}\n'
        "根据结果回复。"
    )
    call = ap._extract_tool_call(text)
    assert call is not None
    assert call["tool"] == "leann_search"
    assert call["index_name"] == "product_kb"
    assert call["query"] == "工作号真实号"
    assert call["top_k"] == 3


def test_extract_tool_call_ignores_non_tool_json():
    text = '{"reply": "hello"}'
    assert ap._extract_tool_call(text) is None


def test_extract_tool_call_ignores_wechat_query_history_json():
    """wechat_query_history must not be executed by the Python tool loop."""
    text = '{"tool": "wechat_query_history", "chat_name": "群名", "keyword": "会议", "limit": 10}'
    assert ap._extract_tool_call(text) is None


def test_run_leann_search_calls_subprocess_with_correct_args():
    fake_stdout = json.dumps([{"title": "工作号真实号", "content": "号码认证产品"}])
    with mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout=fake_stdout,
            stderr="",
        )
        result = ap._run_leann_search("leann", "product_kb", "工作号真实号", 5, 30)

    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    cmd = kwargs.get("args") or args[0]
    assert cmd[:4] == ["leann", "search", "product_kb", "工作号真实号"]
    assert "--top-k" in cmd
    assert "5" in cmd
    assert "--json" in cmd
    assert "--non-interactive" in cmd
    assert kwargs["env"].get("HF_ENDPOINT") == "https://hf-mirror.com"
    assert result == fake_stdout


def test_run_leann_search_sets_create_no_window():
    with mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="[]", stderr="")
        ap._run_leann_search("leann", "x", "q", 3, 10)
    args, kwargs = mock_run.call_args
    assert kwargs.get("creationflags") == ap._NO_WINDOW_FLAGS


def test_run_leann_search_returns_failure_marker_on_empty_output():
    with mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="   ", stderr="")
        result = ap._run_leann_search("leann", "product_kb", "q", 3, 10)
    assert result == "(LEANN search failed)"


def test_run_leann_search_returns_failure_marker_on_nonzero_rc():
    with mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="index not found")
        result = ap._run_leann_search("leann", "product_kb", "q", 3, 10)
    assert result == "(LEANN search failed)"


def test_run_leann_search_returns_failure_marker_on_exception():
    with mock.patch("agent_provider.subprocess.run", side_effect=OSError("boom")):
        result = ap._run_leann_search("leann", "product_kb", "q", 3, 10)
    assert result == "(LEANN search failed)"


def test_tool_result_to_text_summarizes_json_results():
    raw = json.dumps({
        "results": [
            {"title": "T1", "content": "C1" * 500},
            {"title": "T2", "content": "C2"},
        ]
    })
    text = ap._tool_result_to_text(raw)
    assert "[1] T1" in text
    assert "[2] T2" in text


def test_tool_result_to_text_returns_no_results_marker_for_empty_list():
    assert ap._tool_result_to_text(json.dumps({"results": []})) == "(LEANN search returned no results)"


def test_hermes_provider_runs_tool_loop_and_re_runs_hermes():
    """HermesProvider.run should invoke leann_search when the agent emits a tool call."""
    provider = ap.HermesProvider(cli_path="hermes", max_tool_rounds=2, leann_cli_path="leann")

    first_stdout = (
        '{"tool": "leann_search", "index_name": "kb", "query": "q", "top_k": 3}\n'
        "Query: q\n"
    )
    second_stdout = "最终回复内容"

    def fake_invoke(prompt, wait):
        if "[工具调用结果" in prompt:
            return {"ok": True, "stdout": second_stdout, "stderr": "", "rc": 0}
        return {"ok": True, "stdout": first_stdout, "stderr": "", "rc": 0}

    with mock.patch.object(provider, "_invoke_hermes", side_effect=fake_invoke), \
         mock.patch("agent_provider._run_leann_search", return_value='{"results": [{"title": "R", "content": "C"}]}') as mock_leann:
        result = provider.run({"payload": {"prompt": "q", "agent_mode": "tool_agent"}})

    assert result.ok is True
    assert result.reply_text == "最终回复内容"
    mock_leann.assert_called_once_with("leann", "kb", "q", 3, 120)


def test_hermes_provider_stops_at_max_tool_rounds():
    provider = ap.HermesProvider(cli_path="hermes", max_tool_rounds=1, leann_cli_path="leann")
    stdout = '{"tool": "leann_search", "index_name": "kb", "query": "q", "top_k": 3}\n仍想再查'

    with mock.patch.object(provider, "_invoke_hermes", return_value={"ok": True, "stdout": stdout, "stderr": "", "rc": 0}), \
         mock.patch("agent_provider._run_leann_search", return_value='{"results": []}') as mock_leann:
        result = provider.run({"payload": {"prompt": "q", "agent_mode": "tool_agent"}})

    assert result.ok is True
    # Should run tool once, then stop because max_tool_rounds=1 was reached.
    assert mock_leann.call_count == 1


def test_hermes_provider_strips_tool_call_json_at_max_rounds():
    """If the model still emits a tool call after the budget is exhausted,
    the JSON line must not appear in the final reply."""
    provider = ap.HermesProvider(cli_path="hermes", max_tool_rounds=1, leann_cli_path="leann")
    stdout = '{"tool": "leann_search", "index_name": "kb", "query": "q", "top_k": 3}\n这是最终回复'

    with mock.patch.object(provider, "_invoke_hermes", return_value={"ok": True, "stdout": stdout, "stderr": "", "rc": 0}), \
         mock.patch("agent_provider._run_leann_search", return_value='{"results": []}'):
        result = provider.run({"payload": {"prompt": "q", "agent_mode": "tool_agent"}})

    assert result.ok is True
    assert "leann_search" not in result.reply_text
    assert "{" not in result.reply_text
    assert "这是最终回复" in result.reply_text


def test_extract_tool_call_ignores_malformed_and_empty_json():
    """Invalid/empty JSON lines must be ignored; _extract_tool_call should not loop forever."""
    assert ap._extract_tool_call("{") is None
    assert ap._extract_tool_call("") is None
    assert ap._extract_tool_call("not json at all") is None
    assert ap._extract_tool_call("{ }") is None


def test_hermes_provider_handles_missing_tool_arguments_gracefully():
    """A tool call missing index_name or query should not crash or loop forever."""
    provider = ap.HermesProvider(cli_path="hermes", max_tool_rounds=2, leann_cli_path="leann")

    first_stdout = '{"tool": "leann_search", "index_name": "", "query": "", "top_k": 3}\n'
    second_stdout = "最终回复内容"

    def fake_invoke(prompt, wait):
        if "[工具调用结果" in prompt:
            return {"ok": True, "stdout": second_stdout, "stderr": "", "rc": 0}
        return {"ok": True, "stdout": first_stdout, "stderr": "", "rc": 0}

    with mock.patch.object(provider, "_invoke_hermes", side_effect=fake_invoke), \
         mock.patch("agent_provider._run_leann_search") as mock_leann:
        result = provider.run({"payload": {"prompt": "q", "agent_mode": "tool_agent"}})

    assert result.ok is True
    assert result.reply_text == "最终回复内容"
    # LEANN should not be called when required arguments are missing.
    assert mock_leann.call_count == 0


def test_run_leann_search_keeps_query_as_single_argv_element():
    """Queries with spaces or special characters must remain one argv element."""
    query = 'hello world & more "things"'
    with mock.patch("agent_provider.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="[]", stderr="")
        ap._run_leann_search("leann", "kb", query, 3, 10)

    args, kwargs = mock_run.call_args
    cmd = kwargs.get("args") or args[0]
    # The query should appear exactly once and un-split in the command list.
    assert cmd == [
        "leann",
        "search",
        "kb",
        query,
        "--top-k",
        "3",
        "--json",
        "--non-interactive",
    ]


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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
