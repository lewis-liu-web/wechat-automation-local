"""Tests for the admin command protocol and admin sender gating.

These tests cover ``target_registry.is_admin_sender`` together with the pure
helpers in ``admin_commands`` (message-level admin command parsing and
handling).  They run against in-memory target dicts and config dicts - no
filesystem mutation is required.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin_commands
import target_registry as reg


# ---------------------------------------------------------------------------
# is_admin_sender cases
# ---------------------------------------------------------------------------


def test_is_admin_sender_true_for_admin_category_with_empty_whitelist():
    target = {"name": "ops", "username": "wxid_ops", "category": "admin"}
    assert reg.is_admin_sender(target, "wxid_alice") is True


def test_is_admin_sender_true_when_whitelist_contains_sender_case_insensitive():
    target = {
        "name": "ops",
        "username": "wxid_ops",
        "category": "admin",
        "admin_senders": ["wxid_alice", "wxid_bob", "测试助手"],
    }
    assert reg.is_admin_sender(target, "WXID_Alice") is True
    assert reg.is_admin_sender(target, "wxid_BOB") is True
    assert reg.is_admin_sender(target, "wxid_other", sender_display_name="测试助手") is True
    assert reg.is_admin_sender(target, "wxid_other", sender_display_name="  测试助手  ") is True

def test_is_admin_sender_false_when_whitelist_excludes_sender():
    target = {
        "name": "ops",
        "username": "wxid_ops",
        "category": "admin",
        "admin_senders": ["wxid_alice", "测试助手"],
    }
    assert reg.is_admin_sender(target, "wxid_eve", sender_display_name="陌生人") is False


def test_is_admin_sender_false_for_user_category_even_with_whitelist():
    target = {
        "name": "general",
        "username": "wxid_general",
        "category": "user",
        "admin_senders": ["wxid_alice"],
    }
    assert reg.is_admin_sender(target, "wxid_alice") is False
    # And with empty whitelist (would normally mean "everyone") - user category still wins.
    target_user_no_whitelist = {"name": "g", "username": "wxid_g", "category": "user"}
    assert reg.is_admin_sender(target_user_no_whitelist, "wxid_alice") is False


# ---------------------------------------------------------------------------
# handle_admin_command: /mute
# ---------------------------------------------------------------------------


def test_handle_admin_command_mute_sets_admin_muted():
    target = {
        "name": "ops",
        "username": "wxid_ops",
        "category": "admin",
    }
    cfg = {"targets": [target]}
    result = admin_commands.handle_admin_command(
        "/mute",
        target,
        cfg,
        sender_username="wxid_alice",
    )
    assert result.handled is True
    assert result.target_updates == {"admin_muted": True}


def test_handle_admin_command_mute_ignored_for_non_admin_sender():
    target = {
        "name": "ops",
        "username": "wxid_ops",
        "category": "admin",
        "admin_senders": ["wxid_alice"],
    }
    cfg = {"targets": [target]}
    result = admin_commands.handle_admin_command(
        "/mute",
        target,
        cfg,
        sender_username="wxid_eve",
    )
    assert result.handled is False


# ---------------------------------------------------------------------------
# handle_admin_command: /mode
# ---------------------------------------------------------------------------


def test_handle_admin_command_mode_customer_service_returns_full_bundle():
    target = {
        "name": "ops",
        "username": "wxid_ops",
        "category": "admin",
    }
    cfg = {"targets": [target]}
    result = admin_commands.handle_admin_command(
        "/mode customer_service",
        target,
        cfg,
        sender_username="wxid_alice",
    )
    assert result.handled is True
    updates = result.target_updates
    assert updates["mode"] == "customer_service"
    assert updates["reply_policy"] == "knowledge_grounded"
    assert "session_policy" in updates
    assert "context_policy" in updates
    # Sanity-check that the bundle came from reg.mode_bundle so the keys
    # match the canonical policy shapes.
    assert updates == reg.mode_bundle("customer_service")


# ---------------------------------------------------------------------------
# handle_admin_command: unknown command / non-command
# ---------------------------------------------------------------------------


def test_handle_admin_command_unknown_command_returns_usage_reply():
    target = {
        "name": "ops",
        "username": "wxid_ops",
        "category": "admin",
    }
    cfg = {"targets": [target]}
    result = admin_commands.handle_admin_command(
        "/whatever",
        target,
        cfg,
        sender_username="wxid_alice",
    )
    assert result.handled is True
    # The reply should be a non-empty usage hint.
    assert result.reply_text
    assert "未知" in result.reply_text or "用法" in result.reply_text or "/help" in result.reply_text


def test_handle_admin_command_non_slash_message_returns_not_handled():
    target = {
        "name": "ops",
        "username": "wxid_ops",
        "category": "admin",
    }
    cfg = {"targets": [target]}
    # Admin sender but a regular message, not a command.
    result = admin_commands.handle_admin_command(
        "hello there",
        target,
        cfg,
        sender_username="wxid_alice",
    )
    assert result.handled is False
