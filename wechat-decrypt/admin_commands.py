#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Admin slash-command handling for the WeChat bot.

Pure functions that turn a message text into a structured ``AdminCommandResult``,
using ``target_registry`` to verify the sender is an admin and to load mode bundles.
No I/O, no global state — the caller (``wechat_bot_monitor``) is responsible for
applying ``target_updates``, sending ``reply_text``, and persisting config.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import target_registry


# Commands that flip the per-target admin mute switch.
_MUTE_COMMANDS = frozenset({"mute", "stop"})
_UNMUTE_COMMANDS = frozenset({"continue", "unmute", "resume"})

# Modes accepted by ``/mode <name>``; ``target_registry.mode_bundle`` knows the rest.
_VALID_MODES = ("customer_service", "group_assistant")


@dataclass
class AdminCommandResult:
    handled: bool
    action: Optional[str]
    reply_text: str
    target_updates: Dict[str, Any] = field(default_factory=dict)
    save_config: bool = False
    log_reason: Optional[str] = None


def is_admin_command(text) -> Optional[Tuple[str, List[str]]]:
    """Return (command, args) when *text* looks like a slash command.

    Returns ``None`` for empty input, text that does not start with ``/``, or text
    that consists only of ``/`` with no command body. The command is lowercased and
    stripped of the leading ``/``; remaining whitespace-separated tokens are args.
    """
    if text is None:
        return None
    stripped = str(text).strip()
    if not stripped or not stripped.startswith("/"):
        return None
    body = stripped[1:].strip()
    if not body:
        return None
    tokens = body.split()
    if not tokens:
        return None
    command = tokens[0].lower()
    args = tokens[1:]
    return command, args


def handle_admin_command(text, target, cfg, sender_username, sender_display_name=None) -> AdminCommandResult:
    """Dispatch one slash command to a structured result for the monitor loop.

    Returns ``AdminCommandResult(handled=False, ...)`` when *text* is not a slash
    command or *sender_username*/*sender_display_name* is not an admin of *target*.
    For unknown slash commands the result is still ``handled=True`` with a usage hint,
    so the caller can reply without re-running the detection logic.
    """
    stripped = str(text or "").strip()
    parsed = is_admin_command(stripped)
    if parsed is None:
        return AdminCommandResult(False, None, "", {}, False, None)
    if not target_registry.is_admin_sender(target, sender_username, sender_display_name):
        return AdminCommandResult(False, None, "", {}, False, None)

    command, args = parsed
    if command == "help":
        return AdminCommandResult(
            handled=True,
            action="help",
            reply_text=_format_help(),
            target_updates={},
            save_config=False,
            log_reason="admin_help",
        )
    if command in _MUTE_COMMANDS:
        return AdminCommandResult(
            handled=True,
            action="mute",
            reply_text="已静音：新的自动回复已暂停发送。/continue 可恢复。",
            target_updates={"admin_muted": True},
            save_config=True,
            log_reason="admin_mute",
        )
    if command in _UNMUTE_COMMANDS:
        return AdminCommandResult(
            handled=True,
            action="unmute",
            reply_text="已恢复自动回复。",
            target_updates={"admin_muted": False},
            save_config=True,
            log_reason="admin_unmute",
        )
    if command == "mode" and len(args) == 1 and args[0] in _VALID_MODES:
        mode = args[0]
        return AdminCommandResult(
            handled=True,
            action="mode",
            reply_text="已切换模式: %s" % mode,
            target_updates=target_registry.mode_bundle(mode),
            save_config=True,
            log_reason="admin_mode",
        )
    return AdminCommandResult(
        handled=True,
        action="unknown",
        reply_text="未知指令。可用：/help, /mute, /continue, /mode ...",
        target_updates={},
        save_config=False,
        log_reason="admin_unknown",
    )


def _format_help() -> str:
    return "\n".join(
        [
            "管理员指令:",
            "/help - 显示本帮助",
            "/mute 或 /stop - 静音目标（暂停自动回复）",
            "/continue 或 /unmute 或 /resume - 恢复自动回复",
            "/mode <customer_service|group_assistant> - 切换响应模式",
        ]
    )
