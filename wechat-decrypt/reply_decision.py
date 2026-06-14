#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Product reply decision layer for the WeChat bot.

This module decides whether a triggered message should be passed to the
content-understanding agent, and how. It sits *after* trigger / session
detection and *before* the LLM call.

The product goal: in group chats the bot must stay quiet unless the user
clearly wants it. We use a small, rule-based v1:

- private / personal_assistant: default to reply, skip tiny social acks
- group_assistant (conservative): only reply on explicit @bot, trigger, or
  active-session follow-up that looks like a question
- customer_service (knowledge_grounded): reply only when we have a
  confident trigger; otherwise ask for clarification or hand off

Outputs always carry a human-readable `reason` and `reply_mode` so the
monitor can log why it did or did not reply, and so the boundary between
"detect" and "generate" stays explicit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Tiny "is this a real question" gate. Keep it small on purpose; the heavy
# reasoning belongs to the downstream agent, not this layer.
_QUESTION_HINT = re.compile(
    r"(怎么|如何|为什么|为啥|怎么.{0,8}|如何.{0,8}|什么|啥|哪个|哪[个几]|"
    r"吗|？|\?|是不是|能不能|可以.{0,4}吗|会不会|告诉我|解释|看下|看看|分析|总结|对比|区别|意思|咋办|怎么做|啥意思|怎么说|教我)"
)
# Follow-up phrases that are unambiguous: short, "back to the previous
# answer" cues. Generic words like 啥/什么 are intentionally absent here so
# that bare statements like "晚上吃啥" do NOT count as a follow-up.
_FOLLOWUP_HINT = re.compile(
    r"(继续|那这个|这个呢|然后呢|还有呢|咋办|为什么|"
    r"这图|截图|图片|上面|刚才|前面|这个怎么|这个为啥|"
    r"再.{0,4}下|再.{0,4}一遍|再讲|再说|还有吗|是吗|对吗)"
)
_ACK_ONLY = re.compile(r"^[\s\u2005]*((谢谢|谢啦|好的?|好哒|收到|嗯|ok|OK|👌|👍|🙏|😂|😄|🎉|🤣|🤝|🙃|哈哈|嘿嘿|笑死|好)+[\s\u2005,，。!！?？]*)+$")
_CLOSE_HINT = re.compile(r"(谢谢|谢了|好了|好啦|明白|懂了|不用了|没事了|解决了|不需要|算了)")
_RISK_HINT = re.compile(
    r"(密钥|keys\.json|api[_ -]?key|token|password|pwd|密码|验证码|"
    r"删库|删除数据库|格式化|退群|踢人|移出群|报价|合同|授权|拍板|承诺|打款|转账|付款|收款码|银行卡|"
    r"内部日志|系统路径|数据库路径|keys\.json|忽略之前|绕过|越权)"
)
_VOICE_LIKE_REPLY = {"继续", "那这个", "这个呢", "然后呢", "还有呢", "为什么", "怎么操作", "咋办"}


@dataclass
class ReplyDecisionPlan:
    should_reply: bool
    reason: str = "unknown"
    reply_mode: str = "silent"          # answer / ask_clarification / handoff / silent
    risk_level: str = "low"            # low / medium / high
    confidence: float = 0.0
    needs_human: bool = False
    skip_to: str = ""                   # e.g. "agent" / "clarify" / "handoff" / "drop"
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


def _looks_like_question(text: str) -> bool:
    """Whether the message is asking a real question. Group chat question
    detection must avoid false positives on casual statements that happen
    to contain 啥/什么; require either a question mark, an explicit
    modal/intent word, or a short back-reference phrase.
    """
    if not text:
        return False
    # Question mark is the strongest signal.
    if "?" in text or "？" in text:
        return True
    # Explicit intent words are also unambiguous.
    if re.search(r"(怎么|如何|为什么|为啥|是不是|能不能|会不会|教我|解释|分析|总结|对比|区别|看下|看看|什么意思|啥意思|怎么说|咋办|怎么做|哪个|哪[个几])", text):
        return True
    return False


def _looks_like_followup(text: str) -> bool:
    """Strong follow-up signals in group chats: explicit question, or short
    short utterance that points back to a prior answer (e.g. "那这个呢",
    "然后呢", "再看下", "为啥?"). Bare statements like "晚上吃啥" do NOT
    count — they are not addressed to the bot.
    """
    if not text:
        return False
    if _looks_like_question(text):
        return True
    if _FOLLOWUP_HINT.search(text):
        return True
    return False


def _detect_risk(text: str) -> Optional[str]:
    if not text:
        return None
    m = _RISK_HINT.search(text)
    return m.group(0) if m else None


def _is_active_session_followup(msg: Dict[str, Any], text: str, has_images: bool) -> bool:
    if has_images:
        return True
    if _looks_like_followup(text):
        return True
    return False


def decide(target: Dict[str, Any], msg: Dict[str, Any],
           event_context: Optional[Dict[str, Any]] = None) -> ReplyDecisionPlan:
    """Return a structured product decision for a single message.

    `event_context` is the event-centered context built upstream. The
    decision layer treats it as the only source of truth for "is the user
    still talking to the bot" so we don't depend on hidden state.
    """
    text = _msg_text(msg)
    has_images = bool(msg.get("image_path") or msg.get("session_image_paths"))
    is_image_only = has_images and _is_image_only(msg, text)

    policy = (event_context or {}).get("target_policy") if isinstance(event_context, dict) else None
    if not isinstance(policy, dict):
        policy = {}
    mode = str(policy.get("mode") or "group_assistant").lower()
    reply_policy = str(policy.get("reply_policy") or "conservative").lower()
    session_active = bool(isinstance(event_context, dict) and (event_context.get("session_active") or event_context.get("session_state") == "active"))
    session_state = str((event_context or {}).get("session_state") or "").lower()
    trigger_matched = bool((event_context or {}).get("trigger_matched"))
    sender = str((event_context or {}).get("sender") or "")

    plan = ReplyDecisionPlan(should_reply=False, reason="not_evaluated")

    # 1. Hard risk gate: never auto-reply to high-risk requests.
    risk = _detect_risk(text)
    if risk:
        plan.should_reply = True
        plan.reply_mode = "handoff"
        plan.risk_level = "high"
        plan.confidence = 0.95
        plan.needs_human = True
        plan.skip_to = "clarify_or_handoff"
        plan.reason = "risk_detected:%s" % risk
        plan.extra = {"risk_keyword": risk}
        return plan

    # 2. Pure acknowledgement in a non-active session: stay silent.
    if not session_active and _is_ack_only(text) and not has_images:
        plan.reason = "ack_only_no_session"
        plan.reply_mode = "silent"
        plan.confidence = 0.9
        return plan

    # 3. Mode-specific decision.
    if mode == "personal_assistant":
        # Default reply, but skip tiny acks that are clearly social noise.
        if _is_ack_only(text) and not has_images:
            plan.reason = "ack_only_private"
            plan.reply_mode = "silent"
            plan.confidence = 0.85
            return plan
        plan.should_reply = True
        plan.reply_mode = "answer"
        plan.confidence = 0.9
        plan.reason = "personal_default_reply"
        plan.skip_to = "agent"
        return plan

    if mode == "customer_service":
        # Reply only on explicit trigger or clear question in active session.
        if trigger_matched:
            plan.should_reply = True
            plan.reply_mode = "answer"
            plan.confidence = 0.85
            plan.reason = "cs_trigger_matched"
            plan.skip_to = "agent"
            return plan
        if session_active and (is_image_only or _is_active_session_followup(msg, text, has_images)):
            plan.should_reply = True
            plan.reply_mode = "answer"
            plan.confidence = 0.75
            plan.reason = "cs_active_followup"
            plan.skip_to = "agent"
            return plan
        if session_active and _looks_like_close(text):
            plan.should_reply = False
            plan.reply_mode = "silent"
            plan.confidence = 0.9
            plan.reason = "cs_session_close"
            return plan
        # No clear ask: ask for clarification instead of guessing.
        plan.should_reply = True
        plan.reply_mode = "ask_clarification"
        plan.confidence = 0.6
        plan.reason = "cs_need_clarification"
        plan.skip_to = "clarify"
        plan.extra = {"hint": "no_clear_question"}
        return plan

    # Default: group_assistant (conservative)
    if trigger_matched:
        plan.should_reply = True
        plan.reply_mode = "answer"
        plan.confidence = 0.9
        plan.reason = "trigger_matched"
        plan.skip_to = "agent"
        return plan
    if session_active and (is_image_only or _is_active_session_followup(msg, text, has_images)):
        plan.should_reply = True
        plan.reply_mode = "answer"
        plan.confidence = 0.75
        plan.reason = "active_session_followup"
        plan.skip_to = "agent"
        return plan
    if session_active and _looks_like_close(text):
        plan.should_reply = False
        plan.reply_mode = "silent"
        plan.confidence = 0.9
        plan.reason = "session_close"
        return plan
    plan.should_reply = False
    plan.reply_mode = "silent"
    plan.confidence = 0.85
    plan.reason = "group_default_silent" if not session_active else "group_session_no_followup"
    return plan


__all__ = ["ReplyDecisionPlan", "decide"]
