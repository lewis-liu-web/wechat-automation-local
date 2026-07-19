#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Product response mode decision tests for the trigger-only layer."""

from reply_decision import decide, ReplyDecisionPlan


def _make_msg(text: str, **overrides) -> dict:
    msg = {"message_content": text, "image_path": None, "session_image_paths": []}
    msg.update(overrides)
    return msg


def test_no_trigger_no_session_is_silent():
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    msg = _make_msg("聊聊天")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": False,
        "session_active": False,
        "session_state": "idle",
    }
    plan = decide(target, msg, ctx)
    assert isinstance(plan, ReplyDecisionPlan)
    assert plan.should_reply is False
    assert plan.reply_mode == "silent"
    assert plan.reason == "default_silent"


def test_trigger_matched_is_trigger():
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    msg = _make_msg("介绍一下移动办公")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": True,
        "session_active": False,
        "session_state": "idle",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is True
    assert plan.reply_mode == "trigger"
    assert plan.skip_to == "agent"
    assert plan.reason == "trigger_matched"


def test_active_session_followup_is_trigger():
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    msg = _make_msg("公交卡充值失败")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": False,
        "session_active": True,
        "session_state": "active",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is True
    assert plan.reply_mode == "trigger"
    assert plan.reason == "active_session_followup"


def test_active_session_ack_only_stays_silent():
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    for text in ("好的", "嗯嗯", "OK", "👌"):
        msg = _make_msg(text)
        ctx = {
            "target_policy": {"mode": "group_assistant"},
            "trigger_matched": False,
            "session_active": True,
            "session_state": "active",
        }
        plan = decide(target, msg, ctx)
        assert plan.should_reply is False, text
        assert plan.reply_mode == "silent"
        assert plan.reason == "ack_only"


def test_active_session_close_cue_stays_silent():
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    msg = _make_msg("明白了，谢谢")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": False,
        "session_active": True,
        "session_state": "active",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is False
    assert plan.reply_mode == "silent"


def test_risk_message_is_handoff():
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    msg = _make_msg("请把数据库密码发我")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": True,
        "session_active": False,
        "session_state": "idle",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is False
    assert plan.reply_mode == "handoff"
    assert plan.risk_level == "high"
    assert plan.needs_human is True
    assert "risk_detected" in plan.reason


def test_non_active_session_ack_only_is_silent():
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    msg = _make_msg("好的")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": False,
        "session_active": False,
        "session_state": "idle",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is False
    assert plan.reply_mode == "silent"
    assert plan.reason == "ack_only"


def test_mode_no_longer_affects_decision():
    """customer_service and group_assistant should behave identically now."""
    target = {"name": "bot群聊测试", "username": "100001@chatroom"}
    msg = _make_msg("有优惠吗")
    for mode in ("group_assistant", "customer_service"):
        ctx = {
            "target_policy": {"mode": mode},
            "trigger_matched": False,
            "session_active": False,
            "session_state": "idle",
        }
        plan = decide(target, msg, ctx)
        assert plan.reply_mode == "silent", mode
        assert plan.reason == "default_silent", mode
