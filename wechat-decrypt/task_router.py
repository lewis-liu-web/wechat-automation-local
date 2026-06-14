#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task complexity router for WeChat bot.

Pure-function module that classifies incoming messages into routing decisions:

- ``fast_reply``: simple messages handled locally (smalltalk, KB Q&A, safety blocks)
- ``deep_agent``: complex tasks that should enter the job queue (image analysis,
  chart analysis, file summary, multi-step diagnosis, free-form tasks)
- ``block``: high-risk content that must be refused locally before any agent call

This module has no side effects, no DB access, and no dependency on the monitor
loop. It is designed to be testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------

ROUTE_FAST = "fast_reply"
ROUTE_DEEP = "deep_agent"
ROUTE_BLOCK = "block"


@dataclass
class RouteDecision:
    route: str
    reason: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pattern lists
# ---------------------------------------------------------------------------

# High-risk patterns: reuse the same vocabulary as reply_engine.HIGH_RISK_PATTERNS
# and PROMISE_PATTERNS.  Kept as a local copy so this module stays dependency-free.
_BLOCK_PATTERNS: List[str] = [
    "转账", "付款", "打款", "收款码", "银行卡", "验证码", "密码", "密钥",
    "token", "api key", "登录", "删", "删除", "格式化", "改配置", "系统设置",
    "发文件", "聊天记录", "数据库", "内部日志", "路径", "keys.json",
    "忽略之前", "忽略以上", "绕过", "越权", "退群", "踢人", "移出群",
]

_PROMISE_PATTERNS: List[str] = [
    "你替群主", "代表群主", "承诺", "保证", "报价", "授权", "拍板", "决定",
]

# Deep-agent trigger keywords: messages that imply multi-step reasoning,
# vision, file processing, or open-ended analysis.
_DEEP_KEYWORDS: List[str] = [
    "分析", "识别", "看图", "图里", "截图", "图片里", "图片中",
    "表格", "图表", "总结", "对比", "提取", "整理",
    "判断", "诊断", "排查", "生成方案", "写方案",
    "深度分析", "详细分析", "深入分析",
    "自由处理", "你自己判断", "你自己决定", "你来决定",
]

# Explicit free-mode instructions that force deep_agent routing.
_FREE_MODE_PHRASES: List[str] = [
    "自由模式", "自由处理", "深度分析", "你自己判断怎么做",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _contains_any(text: str, patterns: List[str]) -> bool:
    low = (text or "").lower()
    return any(p.lower() in low for p in patterns)


def _is_image_message(message_type: str, has_image: bool) -> bool:
    return message_type == "image" or has_image


def _is_file_or_voice(message_type: str) -> bool:
    return message_type in ("voice", "file", "video")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_message(
    clean_text: str,
    *,
    message_type: str = "text",
    has_image: bool = False,
    has_file: bool = False,
    target_mode: str = "",
) -> RouteDecision:
    """Classify a cleaned message into a routing decision.

    Parameters
    ----------
    clean_text:
        The message text after trigger stripping and sender-prefix removal.
    message_type:
        One of ``"text"``, ``"image"``, ``"voice"``, ``"file"``, ``"video"``.
    has_image:
        Whether the message or its session context includes an image.
    has_file:
        Whether the message includes a file attachment.
    target_mode:
        The target's product mode (e.g. ``"group_assistant"``).  Reserved for
        future mode-specific routing overrides.

    Returns
    -------
    RouteDecision
        ``route`` is one of ``"fast_reply"``, ``"deep_agent"``, ``"block"``.
    """
    text = (clean_text or "").strip()

    # --- 1. Block: high-risk content must never reach any agent ---
    if _contains_any(text, _BLOCK_PATTERNS):
        return RouteDecision(
            route=ROUTE_BLOCK,
            reason="pre_boundary_high_risk",
            detail="消息包含高风险关键词，本地直接拦截。",
        )
    if _contains_any(text, _PROMISE_PATTERNS):
        return RouteDecision(
            route=ROUTE_BLOCK,
            reason="pre_boundary_promise",
            detail="消息涉及承诺/授权/决策，需要本人确认。",
        )

    # --- 2. Deep agent: complex tasks ---

    # 2a. Explicit free-mode instructions
    if _contains_any(text, _FREE_MODE_PHRASES):
        return RouteDecision(
            route=ROUTE_DEEP,
            reason="explicit_free_mode",
            detail="用户明确要求自由模式/深度处理。",
        )

    # 2b. Image + analysis intent
    if _is_image_message(message_type, has_image):
        if _contains_any(text, _DEEP_KEYWORDS):
            return RouteDecision(
                route=ROUTE_DEEP,
                reason="image_analysis",
                detail="图片 + 分析类关键词，需要视觉处理能力。",
            )
        # Image with short/empty text: likely "看看这张图" intent
        if len(text) <= 20:
            return RouteDecision(
                route=ROUTE_DEEP,
                reason="image_implicit",
                detail="图片消息 + 短文本，默认需要视觉处理。",
            )

    # 2c. File/voice/video: always deep (need external processing)
    if _is_file_or_voice(message_type) or has_file:
        return RouteDecision(
            route=ROUTE_DEEP,
            reason="file_or_voice",
            detail="文件/语音/视频消息需要外部处理能力。",
        )

    # 2d. Deep analysis keywords in text-only messages
    if _contains_any(text, _DEEP_KEYWORDS):
        return RouteDecision(
            route=ROUTE_DEEP,
            reason="deep_keyword",
            detail="文本包含深度分析类关键词。",
        )

    # --- 3. Fast reply: everything else ---
    return RouteDecision(
        route=ROUTE_FAST,
        reason="default_fast",
        detail="普通消息，本地快速处理。",
    )


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    cases = [
        ("你好", "text", False, False),
        ("在吗", "text", False, False),
        ("帮我查一下押金怎么退", "text", False, False),
        ("帮我转账100块", "text", False, False),
        ("你代表群主承诺一下", "text", False, False),
        ("分析这张图里的主要结论", "text", True, False),
        ("看看", "image", True, False),
        ("帮我总结一下这份文件", "text", False, True),
        ("自由处理这个任务", "text", False, False),
        ("深度分析一下这个问题", "text", False, False),
        ("帮我对比一下这两个方案", "text", False, False),
        ("今天天气怎么样", "text", False, False),
        ("帮我发一下数据库路径", "text", False, False),
        ("帮我删掉那个文件", "text", False, False),
    ]

    for text, mtype, img, f in cases:
        d = route_message(text, message_type=mtype, has_image=img, has_file=f)
        print(f"{d.route:12s} | {d.reason:28s} | {text}")
