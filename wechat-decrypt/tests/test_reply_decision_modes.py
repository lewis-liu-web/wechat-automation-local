#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Product response mode decision tests for the two-mode simplification."""

from reply_decision import decide, ReplyDecisionPlan


def _make_msg(text: str, **overrides) -> dict:
    msg = {"message_content": text, "image_path": None, "session_image_paths": []}
    msg.update(overrides)
    return msg


def test_legacy_personal_mode_behaves_like_balanced_without_trigger():
    target = {"name": "bot群聊测试", "username": "47965620946@chatroom"}
    msg = _make_msg("聊聊天")
    ctx = {
        "target_policy": {"mode": "personal_assistant"},
        "trigger_matched": False,
        "session_active": False,
        "session_state": "idle",
    }
    plan = decide(target, msg, ctx)
    assert isinstance(plan, ReplyDecisionPlan)
    assert plan.should_reply is False
    assert plan.reply_mode == "silent"
    assert plan.reason == "group_default_silent"


def test_balanced_trigger_replies():
    target = {"name": "bot群聊测试", "username": "47965620946@chatroom"}
    msg = _make_msg("介绍一下移动办公")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": True,
        "session_active": False,
        "session_state": "idle",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is True
    assert plan.reply_mode == "answer"
    assert plan.skip_to == "agent"


def test_customer_service_untriggered_clarifies():
    target = {"name": "bot群聊测试", "username": "47965620946@chatroom"}
    msg = _make_msg("有优惠吗")
    ctx = {
        "target_policy": {"mode": "customer_service"},
        "trigger_matched": False,
        "session_active": False,
        "session_state": "idle",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is True
    assert plan.reply_mode == "ask_clarification"
    assert plan.reason == "cs_need_clarification"

def test_group_assistant_active_session_bare_statement_replies():
    target = {"name": "bot群聊测试", "username": "47965620946@chatroom"}
    msg = _make_msg("公交卡充值失败")
    ctx = {
        "target_policy": {"mode": "group_assistant"},
        "trigger_matched": False,
        "session_active": True,
        "session_state": "active",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is True
    assert plan.reply_mode == "answer"
    assert plan.reason == "active_session_followup"


def test_customer_service_active_session_bare_statement_replies():
    target = {"name": "bot群聊测试", "username": "47965620946@chatroom"}
    msg = _make_msg("公交卡充值失败")
    ctx = {
        "target_policy": {"mode": "customer_service"},
        "trigger_matched": False,
        "session_active": True,
        "session_state": "active",
    }
    plan = decide(target, msg, ctx)
    assert plan.should_reply is True
    assert plan.reply_mode == "answer"
    assert plan.reason == "cs_active_followup"


def test_active_session_ack_only_stays_silent():
    target = {"name": "bot群聊测试", "username": "47965620946@chatroom"}
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
        assert plan.reason == "ack_only_in_session"
