"""Tests for agent output sanitization in agent_provider.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_provider as ap


def test_clean_only_initializing_hints():
    stdout = "Query: 介绍一下产品\nInitializing agent…\n────────────────────────────────────────\n\n"
    assert ap._clean_agent_output(stdout) == ""


def test_extract_hermes_reply_boxed():
    stdout = (
        "Query: 介绍一下产品\n"
        "Initializing agent…\n"
        "╭─ ⚕ Hermes ──────────────────────────╮\n"
        "│ 你好，这是回复内容。                  │\n"
        "╰─────────────────────────────────────╯\n"
    )
    reply = ap._extract_hermes_reply(stdout)
    assert "你好，这是回复内容" in reply


def test_extract_hermes_reply_no_box_chinese():
    stdout = "preparing read_file…\nread some/path\n\n这是最终回复。\n"
    assert ap._extract_hermes_reply(stdout) == "这是最终回复。"


def test_extract_hermes_reply_pure_tool_logs():
    stdout = (
        "Query: 介绍一下产品\n"
        "Initializing agent…\n"
        "preparing read_file…\n"
        "read some/path\n"
        "find another/path\n"
        "────────────────────────────────────────\n"
        "Resume session? [Y/n]\n"
    )

def test_clean_filters_resume_session_and_box_lines():
    stdout = (
        "\x1b[32mQuery: hello\x1b[0m\n"
        "Initializing agent…\n"
        "preparing find…\n"
        "│   │\n"
        "────────────────────────────────────────\n"
        "Resume session? [Y/n]\n"
    )
    assert ap._clean_agent_output(stdout) == ""

def test_sendable_reply_from_assistant_rejects_tool_noise():
    content = "Initializing agent…\npreparing read_file…\n"
    assert ap._sendable_reply_from_assistant(content) == ""


def test_sendable_reply_from_assistant_keeps_chinese():
    content = "preparing read_file…\n\n这是回复。\n"
    assert ap._sendable_reply_from_assistant(content) == "这是回复。"
