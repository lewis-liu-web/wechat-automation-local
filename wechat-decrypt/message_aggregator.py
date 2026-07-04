#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal message aggregator for WeChat bot.

Turns raw message stream into effective agent jobs using short
conversation windows per (chat, sender).

Key primitives:
- EventBuffer: in-memory ring buffer of recent messages per window
- ConversationWindow: 3-second debounce, 12-second hard ceiling
- JobBuilder: aggregates text + image refs into a single job payload
- CapabilityRegistry: runtime probe for vision, text-agent, etc.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# CapabilityRegistry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityRegistry:
    """Runtime-probed capabilities of this deployment."""

    text_agent: bool = True
    local_vision_mmx: bool = False
    image_download_available: bool = False
    can_send_image: bool = False

    @classmethod
    def probe(cls) -> "CapabilityRegistry":
        has_mmx = bool(shutil.which("mmx"))
        if not has_mmx:
            # Also check common npm global paths on Windows
            for p in (
                r"C:\npm\node_global\mmx.cmd",
                os.path.expandvars(r"%APPDATA%\npm\mmx.cmd"),
                os.path.expandvars(r"%LOCALAPPDATA%\npm\mmx.cmd"),
            ):
                if os.path.isfile(p):
                    has_mmx = True
                    break
        return cls(
            text_agent=True,
            local_vision_mmx=has_mmx,
            image_download_available=True,
            can_send_image=False,
        )

    def describe_missing(self, required: List[str]) -> str:
        missing = [c for c in required if not getattr(self, c, False)]
        if not missing:
            return ""
        return "当前环境缺少能力: " + ", ".join(missing)


# Singleton, probed once at import time.
_CAPABILITIES: Optional[CapabilityRegistry] = None
_CAP_LOCK = threading.Lock()


def get_capabilities() -> CapabilityRegistry:
    global _CAPABILITIES
    if _CAPABILITIES is None:
        with _CAP_LOCK:
            if _CAPABILITIES is None:
                _CAPABILITIES = CapabilityRegistry.probe()
    return _CAPABILITIES


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# Default policy values for image-task detection and flush boundaries.
# These may be extended or overridden via target.policy.* or config.*.
_DEFAULT_IMAGE_TASK_MARKERS: List[str] = [
    "分析", "看看", "识别", "截图", "提取", "总结", "对比", "整理",
    "生成", "写", "描述", "解释", "什么意思", "怎么办", "怎么处理",
    "怎么看", "查一下", "帮我看", "图里", "图片", "照片",
    "怎么样", "觉得", "评价", "好看", "有意思",
]
_DEFAULT_TERMINATION_WORDS: List[str] = ["好了", "就这样", "结束"]

@dataclass
class MessageEvent:
    """One raw message from DB polling."""

    chat_id: str
    sender_id: str
    local_id: int
    timestamp: float
    msg_type: str  # "text", "image", "voice", "file"
    content: str = ""
    image_path: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AggregatedTurn:
    """Result of collapsing a conversation window."""

    chat_id: str
    sender_id: str
    start_local_id: int
    end_local_id: int
    start_time: float
    end_time: float
    text_parts: List[str] = field(default_factory=list)
    image_paths: List[str] = field(default_factory=list)
    context_messages: List[Dict[str, Any]] = field(default_factory=list)
    trigger_matched: bool = False
    in_session: bool = False
    event_context: Dict[str, Any] = field(default_factory=dict)
    target: Dict[str, Any] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def combined_text(self) -> str:
        """Merge text parts, strip sender prefix, dedupe."""
        seen: set[str] = set()
        out: List[str] = []
        for part in self.text_parts:
            # Strip "username:\n" prefix added by group decryption
            cleaned = re.sub(r"^[^:\n]{1,80}:\n", "", part, count=1).strip()
            if cleaned.startswith("<bytes "):
                continue
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
        return "\n".join(out)

    def has_image_task_description(self) -> bool:
        """Return True if the user provided text describing what to do with the image(s).

        If the user explicitly @-ed the bot or is in an active session, any
        non-empty follow-up text with an image is treated as a task.

        The default keyword list may be extended via
        target.policy.image_task_markers or config.image_task_markers.
        """
        text = self.combined_text()
        if not text:
            return False
        if self.trigger_matched or self.in_session:
            return True
        if self.event_context.get("trigger_matched") or self.event_context.get("session_active"):
            return True
        low = text.lower()
        markers = list(_DEFAULT_IMAGE_TASK_MARKERS)
        extra_markers: List[str] = []
        if isinstance(self.target, dict):
            policy = self.target.get("policy")
            if isinstance(policy, dict):
                extra_markers = list(policy.get("image_task_markers") or [])
        if not extra_markers and isinstance(self.config, dict):
            extra_markers = list(self.config.get("image_task_markers") or [])
        for m in extra_markers:
            if m and m not in markers:
                markers.append(m)
        return any(m in low for m in markers)

    def to_generate_reply_message(self) -> Dict[str, Any]:
        """Build the dict that reply_engine.generate_reply expects."""
        text = self.combined_text()
        # Use the last message's raw as base, then overlay aggregates
        base = dict(self.raw.get("last_message", {}))
        base["content"] = text
        base["message_content"] = text
        base["local_id"] = self.end_local_id
        base["image_path"] = self.image_paths[0] if self.image_paths else None
        base["session_image_paths"] = self.image_paths
        base["context_messages"] = self.context_messages
        base["event_context"] = self.event_context
        for src_key in ("mention_name", "sender_display_name", "sender_name", "from_display_name"):
            if not base.get(src_key):
                base[src_key] = self.event_context.get(src_key) or self.event_context.get("mention_name") or self.event_context.get("sender_display_name")
        base["target_policy"] = self.event_context.get("target_policy", {})
        # Aggregation metadata for downstream agent routing / context shaping
        base["is_aggregated"] = len(self.text_parts) > 1
        base["aggregated_local_ids"] = [ctx.get("local_id") for ctx in self.context_messages]
        base["text_parts_count"] = len(self.text_parts)
        # Preserve trigger/session flags so reply_engine can route correctly
        base["_aggregator_trigger_matched"] = self.trigger_matched
        base["_aggregator_in_session"] = self.in_session
        return base


# ---------------------------------------------------------------------------
# EventBuffer + ConversationWindow
# ---------------------------------------------------------------------------

_DEBOUNCE_SECONDS = 5.0
_MAX_WINDOW_SECONDS = 12.0
_BUFFER_LOCK = threading.Lock()

# Key -> List[MessageEvent]
_buffers: Dict[str, List[MessageEvent]] = {}
# Key -> window_opened_at (float)
_window_opened_at: Dict[str, float] = {}
# Key -> last_event_at (float)
_window_last_at: Dict[str, float] = {}
# Key -> metadata needed when the window is flushed by timer rather than by next message
_window_meta: Dict[str, Dict[str, Any]] = {}


def _window_key(chat_id: str, sender_id: str) -> str:
    return f"{chat_id}::{sender_id}"


def has_open_window(chat_id: str, sender_id: str) -> bool:
    """Return True if this chat+sender already has a pending turn."""
    key = _window_key(chat_id, sender_id)
    with _BUFFER_LOCK:
        return key in _buffers


def _clean_text_for_policy(text: str) -> str:
    """Strip sender prefix and whitespace for keyword checks."""
    cleaned = re.sub(r"^[^:\n]{1,80}:\n", "", text, count=1).strip()
    return cleaned


def _termination_words(
    target: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]],
) -> List[str]:
    """Return configured termination words, falling back to defaults."""
    if isinstance(target, dict):
        policy = target.get("policy")
        if isinstance(policy, dict) and policy.get("termination_words"):
            return list(policy["termination_words"])
    if isinstance(config, dict) and config.get("termination_words"):
        return list(config["termination_words"])
    return list(_DEFAULT_TERMINATION_WORDS)


def _max_aggregated_messages(
    target: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]],
) -> Optional[int]:
    """Return configured maximum messages per turn, or None for unlimited."""
    raw: Any = None
    if isinstance(target, dict):
        policy = target.get("policy")
        if isinstance(policy, dict):
            raw = policy.get("max_aggregated_messages")
    if raw is None and isinstance(config, dict):
        raw = config.get("max_aggregated_messages")
    if raw is None:
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def _contains_termination_word(text: str, target: Optional[Dict[str, Any]], config: Optional[Dict[str, Any]]) -> bool:
    return any(word in text for word in _termination_words(target, config) if word)


def ingest_event(
    event: MessageEvent,
    *,
    trigger_matched: bool = False,
    in_session: bool = False,
    event_context: Optional[Dict[str, Any]] = None,
    target: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[AggregatedTurn]:
    """Add a message to the buffer.  Returns an AggregatedTurn when the
    conversation window closes, otherwise None.
    """
    key = _window_key(event.chat_id, event.sender_id)
    now = time.time()

    with _BUFFER_LOCK:
        return_value: Optional[AggregatedTurn] = None

        # If a window exists and has been idle > DEBOUNCE_SECONDS or has hit
        # the hard ceiling, close it and continue with the current event as the
        # start of a fresh window.
        if key in _buffers and key in _window_last_at:
            since_last = now - _window_last_at[key]
            elapsed = now - _window_opened_at.get(key, now)
            if elapsed >= _MAX_WINDOW_SECONDS or since_last >= _DEBOUNCE_SECONDS:
                meta = _window_meta.get(key) or {}
                return_value = _build_turn(
                    key,
                    bool(meta.get("trigger_matched")),
                    bool(meta.get("in_session")),
                    meta.get("event_context") if isinstance(meta.get("event_context"), dict) else event_context,
                    meta.get("target") if isinstance(meta.get("target"), dict) else target,
                    meta.get("config") if isinstance(meta.get("config"), dict) else config,
                )

        # Termination-word flush: the current event ends the previous turn and
        # starts a new one.  This only applies to an existing, non-empty window
        # (not a window that was just flushed above).
        cleaned = _clean_text_for_policy(event.content or "")
        if key in _buffers and _contains_termination_word(cleaned, target, config):
            meta = _window_meta.get(key) or {}
            return_value = _build_turn(
                key,
                bool(meta.get("trigger_matched")),
                bool(meta.get("in_session")),
                meta.get("event_context") if isinstance(meta.get("event_context"), dict) else event_context,
                meta.get("target") if isinstance(meta.get("target"), dict) else target,
                meta.get("config") if isinstance(meta.get("config"), dict) else config,
            )

        # Initialise or extend window
        if key not in _buffers:
            _buffers[key] = []
            _window_opened_at[key] = now
        _buffers[key].append(event)
        _window_last_at[key] = now

        # Max-messages flush: once the window reaches the configured limit,
        # return it immediately with the current event included.
        max_messages = _max_aggregated_messages(target, config)
        if max_messages is not None and len(_buffers[key]) >= max_messages:
            meta = _window_meta.get(key) or {}
            return _build_turn(
                key,
                bool(meta.get("trigger_matched")) or trigger_matched,
                bool(meta.get("in_session")) or in_session,
                meta.get("event_context") if isinstance(meta.get("event_context"), dict) else event_context,
                meta.get("target") if isinstance(meta.get("target"), dict) else target,
                meta.get("config") if isinstance(meta.get("config"), dict) else config,
            )

        # Update stored metadata, propagating trigger/session flags so that a
        # later trigger-bearing event marks the whole turn as triggered.
        meta = _window_meta.get(key) or {}
        _window_meta[key] = {
            "trigger_matched": bool(meta.get("trigger_matched")) or trigger_matched,
            "in_session": bool(meta.get("in_session")) or in_session,
            "event_context": event_context or {},
            "target": target or {},
            "config": config or {},
        }
        return return_value


def _build_turn(
    key: str,
    trigger_matched: bool,
    in_session: bool,
    event_context: Optional[Dict[str, Any]],
    target: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]],
) -> AggregatedTurn:
    """Build AggregatedTurn and clear the buffer.  Must hold _BUFFER_LOCK."""
    events = _buffers.pop(key, [])
    _window_opened_at.pop(key, None)
    _window_last_at.pop(key, None)
    _window_meta.pop(key, None)

    if not events:
        # Should not happen, but guard anyway
        return AggregatedTurn(
            chat_id="", sender_id="", start_local_id=0, end_local_id=0,
            start_time=0.0, end_time=0.0,
        )

    events.sort(key=lambda e: e.local_id)
    texts: List[str] = []
    images: List[str] = []
    ctx: List[Dict[str, Any]] = []
    for e in events:
        if e.image_path:
            images.append(e.image_path)
        texts.append(e.content)
        ctx.append({
            "local_id": e.local_id,
            "sender_id": e.sender_id,
            "real_sender_id": e.raw.get("real_sender_id") or e.sender_id,
            "create_time": e.timestamp,
            "message_content": e.content,
            "image_path": e.image_path,
            "status": e.raw.get("status", 1),
        })

    return AggregatedTurn(
        chat_id=events[0].chat_id,
        sender_id=events[0].sender_id,
        start_local_id=events[0].local_id,
        end_local_id=events[-1].local_id,
        start_time=events[0].timestamp,
        end_time=events[-1].timestamp,
        text_parts=texts,
        image_paths=images,
        context_messages=ctx,
        trigger_matched=trigger_matched,
        in_session=in_session,
        event_context=event_context or {},
        target=target or {},
        config=config or {},
        raw={"events": [e.raw for e in events], "last_message": events[-1].raw},
    )


def flush_all_pending(
    target: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[AggregatedTurn]:
    """Force-close every open window.  Used at shutdown or on demand."""
    now = time.time()
    results: List[AggregatedTurn] = []
    with _BUFFER_LOCK:
        for key in list(_buffers.keys()):
            opened = _window_opened_at.get(key, 0)
            # Only flush windows that have been open for at least 1 second
            # to avoid cutting off a brand-new burst
            if now - opened >= 1.0:
                meta = _window_meta.get(key) or {}
                results.append(_build_turn(
                    key,
                    bool(meta.get("trigger_matched")),
                    bool(meta.get("in_session")),
                    meta.get("event_context") if isinstance(meta.get("event_context"), dict) else None,
                    meta.get("target") if isinstance(meta.get("target"), dict) else target,
                    meta.get("config") if isinstance(meta.get("config"), dict) else config,
                ))
    return results


def flush_due() -> List[AggregatedTurn]:
    """Close windows whose debounce or hard ceiling has elapsed."""
    now = time.time()
    results: List[AggregatedTurn] = []
    with _BUFFER_LOCK:
        for key in list(_buffers.keys()):
            opened = _window_opened_at.get(key, now)
            last = _window_last_at.get(key, opened)
            if now - opened >= _MAX_WINDOW_SECONDS or now - last >= _DEBOUNCE_SECONDS:
                meta = _window_meta.get(key) or {}
                results.append(_build_turn(
                    key,
                    bool(meta.get("trigger_matched")),
                    bool(meta.get("in_session")),
                    meta.get("event_context") if isinstance(meta.get("event_context"), dict) else None,
                    meta.get("target") if isinstance(meta.get("target"), dict) else None,
                    meta.get("config") if isinstance(meta.get("config"), dict) else None,
                ))
    return results


# ---------------------------------------------------------------------------
# Convenience: build MessageEvent from monitor message dict
# ---------------------------------------------------------------------------

def event_from_monitor_message(
    msg: Dict[str, Any],
    target: Dict[str, Any],
) -> MessageEvent:
    """Convert a message dict from wechat_bot_monitor into MessageEvent."""
    chat_id = str(target.get("username") or target.get("name") or "unknown")
    sender_id = str(
        msg.get("real_sender_id")
        or msg.get("sender_username")
        or msg.get("sender")
        or "unknown"
    )
    local_id = int(msg.get("local_id") or 0)
    timestamp = float(msg.get("create_time") or time.time())
    local_type = int(msg.get("local_type") or 0)
    if local_type == 3:
        msg_type = "image"
    elif local_type == 34:
        msg_type = "voice"
    elif local_type == 49:
        msg_type = "file"
    else:
        msg_type = "text"
    content = str(msg.get("message_content") or msg.get("content") or "").strip()
    image_path = msg.get("image_path") or None
    if not image_path:
        session_paths = msg.get("session_image_paths") or []
        if session_paths:
            image_path = session_paths[0]
    return MessageEvent(
        chat_id=chat_id,
        sender_id=sender_id,
        local_id=local_id,
        timestamp=timestamp,
        msg_type=msg_type,
        content=content,
        image_path=image_path,
        raw=dict(msg),
    )


# ---------------------------------------------------------------------------
# CLI / module smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cap = get_capabilities()
    print("Capabilities:", cap)

    # Simulate a text-then-image burst
    t0 = time.time()
    e1 = MessageEvent("grp1", "u1", 1, t0, "text", content="分析一下这张图")
    e2 = MessageEvent("grp1", "u1", 2, t0 + 1.0, "image", image_path="/tmp/a.jpg")

    turn1 = ingest_event(e1, trigger_matched=True, target={"name": "test"})
    print("After e1:", turn1)  # None (window still open)

    time.sleep(3.5)  # Wait past debounce
    turn2 = ingest_event(e2, in_session=True, target={"name": "test"})
    print("After e2 + sleep:", turn2)
    if turn2:
        print("Combined text:", turn2.combined_text())
        print("Image paths:", turn2.image_paths)
        print("Job msg:", json.dumps(turn2.to_generate_reply_message(), ensure_ascii=False, indent=2)[:600])
