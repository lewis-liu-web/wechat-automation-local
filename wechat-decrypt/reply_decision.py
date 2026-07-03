#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trigger-only reply decision layer for the WeChat bot.

In the thin-monitor architecture the monitor only decides whether to hand a
message off to the agent.  The agent itself is responsible for content
understanding, clarification, and reply generation.

Decision outputs are therefore limited to:

- trigger   -> pass the message to the downstream agent / reply engine
- silent    -> drop the message (acks, noise, session-close cues)
- handoff   -> high-risk or boundary-crossing; do not trigger the agent

All legacy fields are kept on ``ReplyDecisionPlan`` so existing serialized
decisions and event payloads remain readable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Pure acknowledgement / social noise.
_ACK_ONLY = re.compile(r"^[\s\u2005]*((谢谢|谢啦|好的?|好哒|收到|嗯|ok|OK|👌|👍|🙏|😂|😄|🎉|🤣|🤝|🙃|哈哈|嘿嘿|笑死|好)+[\s\u2005,，。!！?？]*)+$", re.IGNORECASE)
# Session-close cues. The monitor's session layer is the source of truth for
# expiration, but a close hint makes us return silent immediately.
_CLOSE_HINT = re.compile(r"(谢谢|谢了|好了|好啦|明白|懂了|不用了|没事了|解决了|不需要|算了)")
# Hard-risk gate: never auto-trigger on these.
_RISK_HINT = re.compile(
    r"(密钥|keys\.json|api[_ -]?key|token|password|pwd|密码|验证码|"
    r"删库|删除数据库|格式化|退群|踢人|移出群|报价|合同|授权|拍板|承诺|打款|转账|付款|收款码|银行卡|"
    r"内部日志|系统路径|数据库路径|keys\.json|忽略之前|绕过|越权)"
)


@dataclass
class ReplyDecisionPlan:
    should_reply: bool
    reason: str = "unknown"
    reply_mode: str = "silent"          # trigger / silent / handoff
    risk_level: str = "low"            # low / medium / high
    confidence: float = 0.0            # kept for backward compatibility; no longer required output
    needs_human: bool = False
    skip_to: str = ""                   # legacy; kept for compatibility
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "should_reply": self.should_reply,
            "reason": self.reason,
            "reply_mode": self.reply_mode,
            "risk_level": self.risk_level,
            "confidence": round(float(self.confidence), 3),
            "needs_human": self.needs_human,
            "skip_to": self.skip_to,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


def _msg_text(msg: Dict[str, Any]) -> str:
    return str((msg or {}).get("message_content") or "").strip()


def _is_image_only(msg: Dict[str, Any], text: str) -> bool:
    if not text:
        return True
    if text.startswith("<bytes ") and len(text) < 32:
        return True
    return False


def _is_ack_only(text: str) -> bool:
    if not text:
        return False
    return bool(_ACK_ONLY.match(text))


def _looks_like_close(text: str) -> bool:
    if not text:
        return False
    return bool(_CLOSE_HINT.search(text))


def _detect_risk(text: str) -> Optional[str]:
    if not text:
        return None
    m = _RISK_HINT.search(text)
    return m.group(0) if m else None


def _is_active_session_followup(msg: Dict[str, Any], text: str, has_images: bool) -> bool:
    """Open-session follow-up detection.

    Once a sender is in an active session, their next non-empty message
    counts as a follow-up unless it is a pure acknowledgement or a close
    hint. The downstream agent decides whether the message actually needs
    a reply.
    """
    if has_images:
        return True
    if not text:
        return False
    if _is_ack_only(text):
        return False
    if _looks_like_close(text):
        return False
    return True


def decide(target: Dict[str, Any], msg: Dict[str, Any],
           event_context: Optional[Dict[str, Any]] = None) -> ReplyDecisionPlan:
    """Return a thin trigger/silent/handoff decision for a single message.

    `event_context` is the event-centered context built upstream. The
    decision layer treats it as the only source of truth for "is the user
    still talking to the bot" so we don't depend on hidden state.
    """
    text = _msg_text(msg)
    has_images = bool(msg.get("image_path") or msg.get("session_image_paths"))
    is_image_only = has_images and _is_image_only(msg, text)

    session_active = bool(isinstance(event_context, dict) and (event_context.get("session_active") or event_context.get("session_state") == "active"))
    trigger_matched = bool((event_context or {}).get("trigger_matched"))

    plan = ReplyDecisionPlan(should_reply=False, reason="not_evaluated")

    # 1. Hard risk gate: never auto-trigger on high-risk requests.
    risk = _detect_risk(text)
    if risk:
        plan.should_reply = False
        plan.reply_mode = "handoff"
        plan.risk_level = "high"
        plan.confidence = 0.95
        plan.needs_human = True
        plan.skip_to = "handoff"
        plan.reason = "risk_detected:%s" % risk
        plan.extra = {"risk_keyword": risk}
        return plan

    # 2. Pure acknowledgement / social noise: stay silent.
    if _is_ack_only(text) and not has_images:
        plan.reason = "ack_only"
        plan.reply_mode = "silent"
        plan.confidence = 0.9
        return plan

    # 3. Active session: follow-ups trigger the agent; close cues stay silent.
    if session_active:
        if is_image_only or _is_active_session_followup(msg, text, has_images):
            plan.should_reply = True
            plan.reply_mode = "trigger"
            plan.confidence = 0.75
            plan.reason = "active_session_followup"
            plan.skip_to = "agent"
            return plan
        # Close cue or otherwise non-follow-up inside a session -> silent.
        plan.should_reply = False
        plan.reply_mode = "silent"
        plan.confidence = 0.9
        plan.reason = "session_close_or_non_followup"
        return plan

    # 4. Trigger matched: hand to agent.
    if trigger_matched:
        plan.should_reply = True
        plan.reply_mode = "trigger"
        plan.confidence = 0.9
        plan.reason = "trigger_matched"
        plan.skip_to = "agent"
        return plan

    # 5. Default: stay silent in group chats.
    plan.should_reply = False
    plan.reply_mode = "silent"
    plan.confidence = 0.85
    plan.reason = "default_silent"
    return plan


__all__ = ["ReplyDecisionPlan", "decide"]
