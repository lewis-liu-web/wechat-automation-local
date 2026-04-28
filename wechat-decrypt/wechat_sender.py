#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Foreground-first WeChat sender helpers.

This module keeps message receiving and reply generation out of the sender path.
It exposes a compatibility ``send_reply`` function plus a richer ``SendResult``
for callers that need observability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
import ctypes
import sys
import time

ROOT = Path(__file__).resolve().parent
MEMORY = (ROOT.parent.parent / 'memory').resolve()
WECHAT_HWND_HINT = 67810

LogFn = Callable[[str], None]
ConfirmFn = Callable[[dict, int, str, float], bool]


@dataclass
class SendResult:
    ok: bool
    mode: str
    attempted: List[str] = field(default_factory=list)
    reason: str = ''
    confirmed: Optional[bool] = None
    detail: Dict[str, object] = field(default_factory=dict)

    def __bool__(self) -> bool:  # compatibility with old bool return style
        return bool(self.ok)


def _noop_log(msg: str) -> None:
    pass


def _log(log: Optional[LogFn], msg: str) -> None:
    (log or _noop_log)(msg)


def find_wechat_hwnd(hwnd_hint: int = WECHAT_HWND_HINT):
    import win32gui
    try:
        if win32gui.IsWindow(hwnd_hint) and win32gui.GetWindowText(hwnd_hint) == '微信':
            return hwnd_hint
    except Exception:
        pass
    found = []
    def cb(hwnd, extra):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == '微信':
            found.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not found:
        raise RuntimeError('微信窗口未找到')
    return found[0]


def find_descendant_windows(hwnd):
    import win32gui
    out = []
    def cb(child, extra):
        try:
            out.append((child, win32gui.GetClassName(child), win32gui.GetWindowText(child)))
        except Exception:
            pass
    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return out


def _post_ctrl_combo(hwnd, vk):
    import win32gui, win32con
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_CONTROL, 0x001D0001)
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_CONTROL, 0xC01D0001)


def _post_left_click(hwnd, x, y):
    import win32gui, win32con
    lp = (int(y) << 16) | (int(x) & 0xffff)
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)


def _post_enter(hwnd):
    import win32gui, win32con
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0x001C0001)
    win32gui.PostMessage(hwnd, win32con.WM_CHAR, 0x0D, 0x001C0001)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0xC01C0001)


def normalize_chat_name(value: object) -> str:
    return ''.join(ch for ch in str(value or '') if not ch.isspace() and ch not in '.…·-_:：[]【】()（）')


def target_name_matches(target_name: object, observed_text: object) -> bool:
    """Fuzzy match target chat title against OCR/UIA text."""
    a = normalize_chat_name(target_name)
    b = normalize_chat_name(observed_text)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return len(a) >= 6 and a[:6] in b


def target_aliases(target: Optional[dict]) -> List[str]:
    if not target:
        return []
    aliases = []
    for key in ('name', 'username', 'remark', 'alias'):
        val = target.get(key)
        if val and str(val) not in aliases:
            aliases.append(str(val))
    for val in target.get('send_aliases') or target.get('aliases') or []:
        if val and str(val) not in aliases:
            aliases.append(str(val))
    return aliases


def probe_uia_state(target: Optional[dict] = None, log: Optional[LogFn] = None) -> Dict[str, object]:
    """Read-only UIA probe. It never clicks or types.

    Returns best-effort diagnostics; failures are reported as ``ok=False`` so the
    caller can fall back to OCR/search/physical sending without breaking.
    """
    state: Dict[str, object] = {'ok': False, 'reason': 'not_started'}
    try:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        import win32gui
        hwnd = find_wechat_hwnd()
        state.update({'hwnd': hwnd, 'window_title': win32gui.GetWindowText(hwnd), 'children': len(find_descendant_windows(hwnd))})
        try:
            from pywinauto import Desktop  # optional dependency
            app_window = Desktop(backend='uia').window(handle=hwnd)
            texts = []
            for ctrl in app_window.descendants()[:160]:
                try:
                    text = ctrl.window_text()
                    if text:
                        texts.append(text)
                except Exception:
                    pass
            aliases = target_aliases(target)
            matched = any(target_name_matches(alias, txt) for alias in aliases for txt in texts)
            state.update({'ok': True, 'reason': 'uia_available', 'text_sample': texts[:30], 'target_matched': matched})
        except Exception as e:
            state.update({'ok': True, 'reason': 'win32_only', 'uia_error': repr(e), 'target_matched': None})
    except Exception as e:
        state.update({'ok': False, 'reason': 'probe_failed', 'error': repr(e)})
    _log(log, 'UIA probe state=%r' % state)
    return state


def send_reply_backend(text: str, log: Optional[LogFn] = None) -> bool:
    """Best-effort background paste/send; not reliable for Qt WeChat."""
    import win32gui, win32con
    import pyperclip
    hwnd = find_wechat_hwnd()
    pyperclip.copy(text)
    client = win32gui.GetClientRect(hwnd)
    input_x = int(client[2] * 0.72)
    input_y = int(client[3] * 0.91)
    send_x = int(client[2] * 0.94)
    send_y = int(client[3] * 0.91)
    fg = win32gui.GetForegroundWindow()
    children = find_descendant_windows(hwnd)
    _log(log, 'backend send try hwnd=%s fg=%s client=%s input=(%s,%s) send=(%s,%s) children=%d' % (
        hwnd, fg, client, input_x, input_y, send_x, send_y, len(children)))
    targets = [hwnd] + [c[0] for c in children]
    for target in targets[:16]:
        try:
            _post_left_click(target, input_x, input_y)
        except Exception:
            pass
    time.sleep(0.08)
    for target in targets[:16]:
        try:
            _post_ctrl_combo(target, ord('A'))
            win32gui.PostMessage(target, win32con.WM_PASTE, 0, 0)
        except Exception:
            pass
    time.sleep(0.12)
    for target in targets[:16]:
        try:
            _post_enter(target)
        except Exception:
            pass
    time.sleep(0.08)
    for target in targets[:16]:
        try:
            _post_left_click(target, send_x, send_y)
        except Exception:
            pass
    time.sleep(0.05)
    preserved = (win32gui.GetForegroundWindow() == fg)
    _log(log, 'backend send attempted; foreground_preserved=%s (not DB-confirmed)' % preserved)
    return preserved


def open_chat_from_visible_list(hwnd, target_name: str, ljqCtrl, log: Optional[LogFn] = None) -> bool:
    if not target_name:
        return False
    t0 = time.time()
    try:
        from PIL import ImageGrab
        if str(MEMORY) not in sys.path:
            sys.path.insert(0, str(MEMORY))
        import ocr_utils
        import win32gui
        client = win32gui.GetClientRect(hwnd)
        c0 = win32gui.ClientToScreen(hwnd, (0, 0))
        x1 = c0[0] + 40
        y1 = c0[1] + 70
        x2 = c0[0] + min(360, max(260, int(client[2] * 0.34)))
        y2 = c0[1] + min(client[3] - 20, 620)
        img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
        res = ocr_utils.ocr_image(img, enhance=False, engine='rapid')
        for d in res.get('details', []) or []:
            txt = d.get('text') or ''
            if target_name_matches(target_name, txt):
                box = d.get('bbox') or []
                cy = sum(float(p[1]) for p in box[:4]) / 4.0 if len(box) >= 4 else 0
                click_x = c0[0] + 170
                click_y = int(y1 + cy)
                _log(log, 'UI list-ocr hit target=%r text=%r click_xy=(%s,%s) dt=%.3fs' % (
                    target_name, txt, click_x, click_y, time.time() - t0))
                ljqCtrl.Click(click_x, click_y)
                time.sleep(0.20)
                return True
        _log(log, 'UI list-ocr miss target=%r text=%r dt=%.3fs' % (target_name, res.get('text', '')[:120], time.time() - t0))
    except Exception as e:
        _log(log, 'UI list-ocr failed target=%r err=%r dt=%.3fs' % (target_name, e, time.time() - t0))
    return False


def _open_chat_by_search(hwnd, target_name: str, ljqCtrl, pyperclip, log: Optional[LogFn] = None) -> bool:
    import win32gui
    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    search_x = c0[0] + 150
    search_y = c0[1] + 38
    first_result_x = c0[0] + 170
    first_result_y = c0[1] + 115
    _log(log, 'UI search fallback target=%r hwnd=%s search_xy=(%s,%s) result_xy=(%s,%s) client=%s dpi=%s' % (
        target_name, hwnd, search_x, search_y, first_result_x, first_result_y, client, getattr(ljqCtrl, 'dpi_scale', None)))
    ljqCtrl.Click(search_x, search_y)
    time.sleep(0.10)
    ljqCtrl.Press('ctrl+a')
    time.sleep(0.03)
    pyperclip.copy(target_name)
    ljqCtrl.Press('ctrl+v')
    time.sleep(0.45)
    ljqCtrl.Click(first_result_x, first_result_y)
    time.sleep(0.35)
    return True


def send_reply_foreground(text: str, target: Optional[dict] = None, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> SendResult:
    result = SendResult(ok=False, mode='foreground')
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    if str(MEMORY) not in sys.path:
        sys.path.insert(0, str(MEMORY))
    import win32gui, win32con
    import pyperclip
    import ljqCtrl
    hwnd = find_wechat_hwnd()
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    time.sleep(0.15)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        _log(log, 'SetForegroundWindow warn: %r' % (e,))
    time.sleep(0.25)

    aliases = target_aliases(target)
    target_name = aliases[0] if aliases else ''
    if cfg is None:
        cfg = {}
    strategy = str(cfg.get('send_strategy') or 'current_or_uia_then_ocr_search_physical')
    use_uia_probe = cfg.get('uia_probe_enabled', True) and 'uia' in strategy
    use_ocr_list = 'ocr' in strategy
    use_search = 'search' in strategy
    current_verified = False

    if use_uia_probe:
        result.attempted.append('uia_probe')
        state = probe_uia_state(target, log=log)
        result.detail['uia_probe'] = state
        if state.get('target_matched'):
            current_verified = True
            result.attempted.append('current_chat_verified')
            _log(log, 'UIA current chat appears to match target=%r' % target_name)
        elif target_name:
            # No UIA action yet: Qt WeChat UIA tree is inconsistent. Keep this
            # as a safety/readiness probe and use OCR/search to actually switch.
            result.reason = 'uia_not_matched_or_unavailable'

    if target_name and not current_verified:
        opened = False
        if use_ocr_list:
            result.attempted.append('ocr_visible_list')
            opened = open_chat_from_visible_list(hwnd, target_name, ljqCtrl, log=log)
        if not opened and use_search:
            result.attempted.append('search_fallback')
            _open_chat_by_search(hwnd, target_name, ljqCtrl, pyperclip, log=log)
        elif not opened and not use_search:
            result.reason = (result.reason + '|target_not_switched_search_disabled').strip('|')
    elif not target_name:
        result.attempted.append('current_chat_no_target')
        _log(log, 'UI target missing; fallback to currently selected chat')

    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    x = c0[0] + int(client[2] * 0.72)
    y = c0[1] + int(client[3] * 0.88)
    send_x = c0[0] + int(client[2] * 0.92)
    send_y = c0[1] + int(client[3] * 0.94)
    _log(log, 'UI click input hwnd=%s target=%r c0=%s client=%s input_xy=(%s,%s) send_xy=(%s,%s) dpi=%s' % (
        hwnd, target_name, c0, client, x, y, send_x, send_y, getattr(ljqCtrl, 'dpi_scale', None)))
    result.attempted.append('physical_input')
    ljqCtrl.Click(x, y)
    time.sleep(0.15)
    ljqCtrl.Press('ctrl+a')
    time.sleep(0.05)
    pyperclip.copy(text)
    ljqCtrl.Press('ctrl+v')
    time.sleep(0.25)
    ljqCtrl.Press('enter')
    time.sleep(0.25)
    ljqCtrl.Click(send_x, send_y)
    result.ok = True
    result.reason = 'sent_physical_foreground'
    return result


def _confirm_result(result: SendResult, target: Optional[dict], before_local_id: Optional[int], text: str,
                    confirm: Optional[ConfirmFn], confirm_timeout: float) -> SendResult:
    if target is None or before_local_id is None or confirm is None:
        result.confirmed = None
        result.ok = bool(result.ok)
        return result
    try:
        result.confirmed = bool(confirm(target, before_local_id, text, confirm_timeout))
        result.ok = bool(result.confirmed)
        if not result.confirmed:
            result.reason = (result.reason + '|confirm_timeout').strip('|')
    except Exception as e:
        result.confirmed = False
        result.ok = False
        result.reason = 'confirm_error'
        result.detail['confirm_error'] = repr(e)
    return result


def send_reply_detailed(text: str, mode: Optional[str] = None, target: Optional[dict] = None,
                        before_local_id: Optional[int] = None, cfg: Optional[dict] = None,
                        confirm: Optional[ConfirmFn] = None, log: Optional[LogFn] = None) -> SendResult:
    cfg = cfg or {}
    mode = (mode or cfg.get('send_mode') or 'backend_only').lower()
    confirm_timeout = float(cfg.get('send_confirm_timeout') or 5.0)

    if mode in ('backend', 'backend_only', 'background'):
        attempted = send_reply_backend(text, log=log)
        result = SendResult(ok=bool(attempted), mode=mode, attempted=['backend'], reason='backend_attempted')
        return _confirm_result(result, target, before_local_id, text, confirm, confirm_timeout)

    if mode in ('foreground', 'front'):
        result = send_reply_foreground(text, target=target, cfg=cfg, log=log)
        return _confirm_result(result, target, before_local_id, text, confirm, confirm_timeout)

    if mode in ('backend_then_foreground', 'background_then_foreground'):
        try:
            backend_ok = send_reply_backend(text, log=log)
            backend_result = SendResult(ok=bool(backend_ok), mode=mode, attempted=['backend'], reason='backend_attempted')
            backend_result = _confirm_result(backend_result, target, before_local_id, text, confirm, confirm_timeout)
            if backend_result.ok:
                return backend_result
            _log(log, 'backend send not confirmed, fallback foreground reason=%s' % backend_result.reason)
        except Exception as e:
            _log(log, 'backend send failed, fallback foreground: %r' % (e,))
        result = send_reply_foreground(text, target=target, cfg=cfg, log=log)
        result.mode = mode
        if 'backend' not in result.attempted:
            result.attempted.insert(0, 'backend')
        return _confirm_result(result, target, before_local_id, text, confirm, confirm_timeout)

    raise ValueError('unknown send_mode: %s' % mode)


def send_reply(text: str, mode: Optional[str] = None, target: Optional[dict] = None,
               before_local_id: Optional[int] = None, cfg: Optional[dict] = None,
               confirm: Optional[ConfirmFn] = None, log: Optional[LogFn] = None) -> bool:
    return bool(send_reply_detailed(text, mode=mode, target=target, before_local_id=before_local_id,
                                    cfg=cfg, confirm=confirm, log=log))
