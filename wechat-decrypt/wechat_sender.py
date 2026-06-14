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
import os
import sys
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PYWECHAT_SRC = (ROOT.parent / 'pywechat_src').resolve()
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


def _safe_clipboard_copy(pyperclip_module, text: str, log: Optional[LogFn] = None,
                         label: str = 'clipboard', attempts: int = 6,
                         base_sleep: float = 0.08) -> bool:
    """Copy text to clipboard with short retries for transient OpenClipboard contention.

    Windows clipboard access is exclusive.  WeChat itself, IME helpers, or security
    tools can briefly hold it, which makes pyperclip raise even though a retry a few
    milliseconds later succeeds.  Treat this as a transport retry, not a fatal send
    failure, so completed agent jobs do not lose their result reply.
    """
    last_error = None
    total = max(1, int(attempts))
    for attempt in range(1, total + 1):
        try:
            pyperclip_module.copy(text)
            if attempt > 1:
                _log(log, '%s copy recovered attempt=%s' % (label, attempt))
            return True
        except Exception as e:
            last_error = e
            _log(log, '%s copy failed attempt=%s/%s err=%r' % (label, attempt, total, e))
            time.sleep(base_sleep * attempt)
    _log(log, '%s copy failed exhausted attempts=%s last_err=%r' % (label, total, last_error))
    return False


def _force_foreground_window(hwnd, log: Optional[LogFn] = None) -> bool:
    """Bring hwnd to foreground using multiple Windows API techniques.

    SetForegroundWindow fails when the calling process lacks foreground rights.
    This helper tries progressively more aggressive workarounds and verifies
    the result with GetForegroundWindow().
    """
    import win32gui, win32con, win32api, ctypes
    from ctypes import wintypes

    if win32gui.GetForegroundWindow() == hwnd:
        return True

    # Technique 1: Restore + BringWindowToTop
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.BringWindowToTop(hwnd)
    time.sleep(0.05)
    if win32gui.GetForegroundWindow() == hwnd:
        _log(log, 'foreground ok via_restore_bringtop')
        return True

    # Technique 2: SetWindowPos TOPMOST then NOTOPMOST (forces Z-order flash)
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    time.sleep(0.05)
    if win32gui.GetForegroundWindow() == hwnd:
        _log(log, 'foreground ok via_topmost_flash')
        return True

    # Technique 3: AllowSetForegroundWindow + SetForegroundWindow
    ASFW_ANY = wintypes.DWORD(-1)
    ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        _log(log, 'SetForegroundWindow(allow_any) warn: %r' % (e,))
    time.sleep(0.05)
    if win32gui.GetForegroundWindow() == hwnd:
        _log(log, 'foreground ok via_allow_any')
        return True

    # Technique 4: AttachThreadInput trick
    try:
        cur_tid = win32api.GetCurrentThreadId()
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_tid = win32gui.GetWindowThreadProcessId(fg_hwnd)[0]
        if fg_tid != cur_tid:
            ctypes.windll.user32.AttachThreadInput(fg_tid, cur_tid, True)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                _log(log, 'SetForegroundWindow(attach_input) warn: %r' % (e,))
            ctypes.windll.user32.AttachThreadInput(fg_tid, cur_tid, False)
        time.sleep(0.05)
        if win32gui.GetForegroundWindow() == hwnd:
            _log(log, 'foreground ok via_attach_thread')
            return True
    except Exception as e:
        _log(log, 'AttachThreadInput trick failed: %r' % (e,))

    # Technique 5: Simulate Alt keypress to gain foreground rights briefly
    try:
        ctypes.windll.user32.keybd_event(win32con.VK_MENU, 0x38, 0, 0)
        ctypes.windll.user32.keybd_event(win32con.VK_MENU, 0x38, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            _log(log, 'SetForegroundWindow(alt_key) warn: %r' % (e,))
        time.sleep(0.05)
        if win32gui.GetForegroundWindow() == hwnd:
            _log(log, 'foreground ok via_alt_key')
            return True
    except Exception as e:
        _log(log, 'Alt key foreground trick failed: %r' % (e,))

    _log(log, 'WARN: could not bring hwnd=%s to foreground; current_fg=%s' % (hwnd, win32gui.GetForegroundWindow()))
    return False


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


def _safe_control_attr(ctrl, name: str, default=None):
    try:
        val = getattr(ctrl, name)
        return val() if callable(val) and name.startswith('Get') else val
    except Exception:
        return default


def _control_text(ctrl) -> str:
    parts = []
    for attr in ('Name', 'AutomationId', 'ClassName', 'ControlTypeName'):
        val = _safe_control_attr(ctrl, attr, '')
        if val:
            parts.append(str(val))
    try:
        val = ctrl.window_text()  # pywinauto compatibility
        if val:
            parts.append(str(val))
    except Exception:
        pass
    return ' '.join(dict.fromkeys(parts))


def _control_rect(ctrl):
    rect = _safe_control_attr(ctrl, 'BoundingRectangle')
    if rect is None:
        try:
            rect = ctrl.rectangle()  # pywinauto compatibility
        except Exception:
            return None
    try:
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        try:
            left, top, right, bottom = map(int, (rect.Left, rect.Top, rect.Right, rect.Bottom))
        except Exception:
            return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _rect_center(rect: Tuple[int, int, int, int]) -> Tuple[int, int]:
    return int((rect[0] + rect[2]) / 2), int((rect[1] + rect[3]) / 2)


def _walk_uia_controls(root, limit: int = 500):
    out = []
    stack = [root]
    while stack and len(out) < limit:
        ctrl = stack.pop(0)
        out.append(ctrl)
        children = []
        try:
            children = list(ctrl.GetChildren())
        except Exception:
            try:
                children = list(ctrl.children())
            except Exception:
                children = []
        stack.extend(children[:80])
    return out


def _get_uia_root(hwnd):
    """Return a UIA root control for WeChat. Prefer installed uiautomation; pywinauto is optional."""
    try:
        import uiautomation as auto  # installed in this environment
        return auto.ControlFromHandle(hwnd)
    except Exception:
        from pywinauto import Desktop  # optional dependency
        return Desktop(backend='uia').window(handle=hwnd)


def _click_control(ctrl, ljqCtrl, log: Optional[LogFn] = None, label: str = 'uia_control') -> bool:
    rect = _control_rect(ctrl)
    if not rect:
        _log(log, 'UIA click failed label=%s reason=no_rect text=%r' % (label, _control_text(ctrl)[:120]))
        return False
    x, y = _rect_center(rect)
    _log(log, 'UIA click label=%s xy=(%s,%s) rect=%s text=%r' % (label, x, y, rect, _control_text(ctrl)[:120]))
    try:
        ljqCtrl.Click(x, y)
        return True
    except Exception as e:
        _log(log, 'UIA click failed label=%s err=%r' % (label, e))
        return False


def _is_edit_control(ctrl) -> bool:
    text = _control_text(ctrl).lower()
    return 'edit' in text or '编辑' in text or 'richedit' in text


def _is_button_control(ctrl) -> bool:
    text = _control_text(ctrl).lower()
    return 'button' in text or '按钮' in text


def _uia_collect_state(hwnd, target: Optional[dict] = None, limit: int = 500) -> Dict[str, object]:
    import win32gui
    root = _get_uia_root(hwnd)
    controls = _walk_uia_controls(root, limit=limit)
    texts = []
    for ctrl in controls:
        text = _control_text(ctrl).strip()
        if text:
            texts.append(text)
    aliases = target_aliases(target)
    matched = any(target_name_matches(alias, txt) for alias in aliases for txt in texts)

    title_candidates = []
    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    title_min_x = c0[0] + int(client[2] * 0.28)
    title_max_x = c0[0] + int(client[2] * 0.88)
    title_max_y = c0[1] + max(150, int(client[3] * 0.22))
    for ctrl in controls:
        text = _control_text(ctrl).strip()
        if not text:
            continue
        rect = _control_rect(ctrl)
        if not rect:
            continue
        cx, cy = _rect_center(rect)
        if not (title_min_x <= cx <= title_max_x and c0[1] <= cy <= title_max_y):
            continue
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        score = 0
        if target and any(target_name_matches(alias, text) for alias in aliases):
            score += 20
        if area >= 800:
            score += 5
        score += max(0, 10 - abs(cy - (c0[1] + 90)) // 12)
        title_candidates.append((score, rect[1], rect[0], text, rect))
    title_candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
    current_chat_title = title_candidates[0][3] if title_candidates else ''
    title_rect = title_candidates[0][4] if title_candidates else None
    title_matched = any(target_name_matches(alias, current_chat_title) for alias in aliases) if aliases and current_chat_title else False
    return {
        'root': root,
        'controls': controls,
        'texts': texts,
        'current_chat_title': current_chat_title,
        'current_chat_title_rect': title_rect,
        'state': {
            'ok': True,
            'reason': 'uia_available',
            'hwnd': hwnd,
            'window_title': win32gui.GetWindowText(hwnd),
            'window_class': win32gui.GetClassName(hwnd),
            'children': len(find_descendant_windows(hwnd)),
            'controls': len(controls),
            'text_sample': texts[:30],
            'target_matched': matched,
            'current_chat_title': current_chat_title,
            'current_chat_title_rect': title_rect,
            'title_target_matched': title_matched,
        },
    }


def _uia_is_effectively_available(hwnd, log: Optional[LogFn] = None) -> bool:
    """Return False for Qt/Chromium WeChat builds that expose no useful UIA tree.

    pywechat's UIA/pywinauto path needs visible Search/Edit/ListItem controls.  On
    the local WeChat 4 Qt shell, UIA only exposes Window + a tiny Pane, while Win32
    children are just Chrome_WidgetWin_0 / Intermediate D3D.  Trying UIA in that
    state only adds failed probes before OCR/coordinate fallback.
    """
    try:
        import win32gui
        data = _uia_collect_state(hwnd, target=None, limit=80)
        controls = data.get('controls') or []
        texts = data.get('texts') or []
        cls = win32gui.GetClassName(hwnd)
        child_classes = [c[1] for c in find_descendant_windows(hwnd)]
        has_useful_text = any(t and t not in ('微信', 'Weixin') and not t.endswith(' WindowControl') and not t.endswith(' PaneControl') for t in texts)
        has_edit = any(_is_edit_control(c) for c in controls)
        available = bool(has_edit or has_useful_text or len(controls) >= 8)
        if not available:
            _log(log, 'UIA disabled: no effective tree class=%r controls=%d texts=%r child_classes=%r' % (
                cls, len(controls), texts[:8], child_classes[:8]))
        else:
            _log(log, 'UIA enabled: class=%r controls=%d texts=%r has_edit=%s' % (
                cls, len(controls), texts[:8], has_edit))
        return available
    except Exception as e:
        _log(log, 'UIA availability probe failed err=%r; disable UIA for this send' % (e,))
        return False


def _pyweixin_send_to_friend(target: Optional[dict], text: str, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> bool:
    """Send via the verified pyweixin UIA path when WeChat exposes its cached UIA tree.

    This path was validated on WeChat 4.x after Narrator/accessibility warm-up: the
    Narrator process can be closed and the same WeChat process still keeps the UIA
    tree usable.  It should be tried before coordinate/physical fallbacks, but it
    intentionally does not start Narrator or restart WeChat.
    """
    aliases = target_aliases(target)
    target_name = aliases[0] if aliases else ''
    if not target_name:
        _log(log, 'pyweixin send skipped: missing target_name')
        return False
    t0 = time.time()
    try:
        if str(PYWECHAT_SRC) not in sys.path:
            sys.path.insert(0, str(PYWECHAT_SRC))
        from pyweixin.WeChatAuto import Messages
        clear = bool((cfg or {}).get('pyweixin_clear', True))
        search_pages = int((cfg or {}).get('pyweixin_search_pages', 5))
        send_delay = float((cfg or {}).get('pyweixin_send_delay', 0.2))
        close_weixin = bool((cfg or {}).get('pyweixin_close_weixin', False))
        is_maximize = bool((cfg or {}).get('pyweixin_is_maximize', False))
        _log(log, 'pyweixin send start target=%r pages=%s clear=%s' % (target_name, search_pages, clear))
        Messages.send_messages_to_friend(
            target_name,
            [text],
            search_pages=search_pages,
            clear=clear,
            send_delay=send_delay,
            is_maximize=is_maximize,
            close_weixin=close_weixin,
        )
        _log(log, 'pyweixin send returned target=%r dt=%.3fs' % (target_name, time.time() - t0))
        return True
    except Exception as e:
        _log(log, 'pyweixin send failed target=%r err=%r dt=%.3fs' % (target_name, e, time.time() - t0))
        return False



def probe_uia_state(target: Optional[dict] = None, log: Optional[LogFn] = None) -> Dict[str, object]:
    """Read-only UIA probe. It never clicks or types."""
    state: Dict[str, object] = {'ok': False, 'reason': 'not_started'}
    try:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        import win32gui
        hwnd = find_wechat_hwnd()
        try:
            data = _uia_collect_state(hwnd, target=target, limit=220)
            state.update(data['state'])
        except Exception as e:
            state.update({'ok': True, 'reason': 'win32_only', 'hwnd': hwnd,
                          'window_title': win32gui.GetWindowText(hwnd),
                          'children': len(find_descendant_windows(hwnd)),
                          'uia_error': repr(e), 'target_matched': None})
    except Exception as e:
        state.update({'ok': False, 'reason': 'probe_failed', 'error': repr(e)})
    _log(log, 'UIA probe state=%r' % state)
    return state


def _uia_current_chat_matches(target: Optional[dict] = None, log: Optional[LogFn] = None) -> bool:
    try:
        hwnd = find_wechat_hwnd()
        data = _uia_collect_state(hwnd, target=target, limit=260)
        state = data['state']
        title = str(state.get('current_chat_title') or '').strip()
        title_matched = bool(state.get('title_target_matched'))
        broad_matched = bool(state.get('target_matched'))
        _log(log, 'UIA current-check state=%r' % state)
        if title:
            _log(log, 'UIA current-check title=%r title_matched=%s broad_matched=%s' % (title, title_matched, broad_matched))
            return title_matched
        return broad_matched
    except Exception as e:
        _log(log, 'UIA current-check failed err=%r' % (e,))
        return False


def _find_uia_search_box(hwnd, log: Optional[LogFn] = None):
    import win32gui
    root = _get_uia_root(hwnd)
    controls = _walk_uia_controls(root, limit=500)
    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    max_x = c0[0] + min(420, max(260, int(client[2] * 0.42)))
    max_y = c0[1] + 130
    candidates = []
    for ctrl in controls:
        if not _is_edit_control(ctrl):
            continue
        rect = _control_rect(ctrl)
        if not rect:
            continue
        cx, cy = _rect_center(rect)
        text = _control_text(ctrl)
        score = 0
        if c0[0] <= cx <= max_x and c0[1] <= cy <= max_y:
            score += 10
        if any(k in text for k in ('搜索', 'Search', 'search')):
            score += 8
        if rect[2] - rect[0] >= 80:
            score += 2
        if score:
            candidates.append((score, rect[1], ctrl, text, rect))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    if candidates:
        _log(log, 'UIA search-box candidate score=%s rect=%s text=%r' % (candidates[0][0], candidates[0][4], candidates[0][3][:120]))
        return candidates[0][2]
    _log(log, 'UIA search-box not found edits=%d' % sum(1 for c in controls if _is_edit_control(c)))
    return None


def _uia_open_chat_from_visible_list(target: Optional[dict] = None, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> bool:
    """Open a target chat by clicking a visible row in WeChat's left conversation list.

    This intentionally does not use the WeChat search box. Recent WeChat builds can
    route search text into the built-in web/search surface instead of jumping to a
    chat, which risks sending to the wrong place. This probe only clicks currently
    visible conversation-list items.
    """
    aliases = target_aliases(target)
    target_name = aliases[0] if aliases else ''
    if not target_name:
        return False
    t0 = time.time()
    try:
        ljqCtrl = _load_ljqctrl(cfg)
        if ljqCtrl is None:
            _log(log, 'UIA visible-list open failed reason=ljqCtrl_unavailable')
            return False
        import win32gui
        hwnd = find_wechat_hwnd()
        root = _get_uia_root(hwnd)
        controls = _walk_uia_controls(root, limit=900)
        client = win32gui.GetClientRect(hwnd)
        c0 = win32gui.ClientToScreen(hwnd, (0, 0))
        # Left chat list roughly begins after the vertical nav bar and ends before
        # the conversation pane. Keep this generous so it works on different sizes.
        min_x = c0[0] + 55
        max_x = c0[0] + min(520, max(260, int(client[2] * 0.40)))
        min_y = c0[1] + 95
        max_y = c0[1] + max(180, client[3] - 30)
        candidates = []
        for ctrl in controls:
            text = _control_text(ctrl)
            if not text or not any(target_name_matches(alias, text) for alias in aliases):
                continue
            rect = _control_rect(ctrl)
            if not rect:
                continue
            cx, cy = _rect_center(rect)
            if not (min_x <= cx <= max_x and min_y <= cy <= max_y):
                continue
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            area = width * height
            score = 0
            # Conversation rows are usually list items / buttons / groups with a
            # readable name plus latest-message preview. Prefer wider row controls.
            low = text.lower()
            if 'listitem' in low or '列表项目' in text:
                score += 20
            if 'button' in low or '按钮' in text:
                score += 6
            if width >= 160:
                score += 5
            if height >= 35:
                score += 3
            score += min(10, area // 3000)
            candidates.append((score, rect[1], rect[0], ctrl, text, rect))
        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        if not candidates:
            _log(log, 'UIA visible-list open miss target=%r dt=%.3fs' % (target_name, time.time() - t0))
            return False
        score, _, _, ctrl, text, rect = candidates[0]
        if not _click_control(ctrl, ljqCtrl, log=log, label='visible_chat_row'):
            return False
        time.sleep(0.35)
        matched = _uia_current_chat_matches(target, log=log)
        _log(log, 'UIA visible-list open target=%r clicked_text=%r score=%s confirmed=%s dt=%.3fs' % (
            target_name, text[:160], score, matched, time.time() - t0))
        return matched
    except Exception as e:
        _log(log, 'UIA visible-list open failed target=%r err=%r dt=%.3fs' % (target_name, e, time.time() - t0))
        return False



def _load_ljqctrl(cfg: Optional[dict] = None):
    """Load ljqCtrl from environment/config or import path; avoids hard GA dependency."""
    candidates = []
    if cfg and cfg.get('ljqctrl_path'):
        candidates.append(str(cfg.get('ljqctrl_path')))
    env_path = os.environ.get('WECHAT_AUTO_LJQCTRL_PATH')
    if env_path:
        candidates.append(env_path)
    for custom in candidates:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location('ljqCtrl', custom)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
        except Exception:
            continue
    # Auto-discover ljqCtrl from common GA paths before giving up
    import sys
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _ga_paths = [
        os.path.join(_script_dir, '../memory'),
        os.path.join(_script_dir, '../../memory'),
        os.path.join(_script_dir, '../../../memory'),
        'D:/Program Files/GenericAgent/GenericAgent-main/memory',
        'D:/Program Files/GenericAgent/GenericAgent-sub-wechat/memory',
    ]
    for _p in _ga_paths:
        _ap = os.path.abspath(_p)
        if _ap not in sys.path and os.path.isdir(_ap):
            sys.path.insert(0, _ap)
    try:
        import ljqCtrl  # type: ignore
        return ljqCtrl
    except Exception:
        return None

def _load_ocr_utils(cfg: Optional[dict] = None):
    """Load ocr_utils from environment/config or import path; avoids hard GA dependency."""
    candidates = []
    if cfg and cfg.get('ocr_utils_path'):
        candidates.append(str(cfg.get('ocr_utils_path')))
    env_path = os.environ.get('WECHAT_AUTO_OCR_UTILS_PATH')
    if env_path:
        candidates.append(env_path)
    for custom in candidates:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location('ocr_utils', custom)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
        except Exception:
            continue
    try:
        import ocr_utils  # type: ignore
        return ocr_utils
    except Exception:
        return None

def _uia_open_chat_by_search(target: Optional[dict] = None, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> bool:
    aliases = target_aliases(target)
    target_name = aliases[0] if aliases else ''
    if not target_name:
        return False
    t0 = time.time()
    try:
        ljqCtrl = _load_ljqctrl(cfg)
        if ljqCtrl is None:
            _log(log, 'UIA open-chat failed reason=ljqCtrl_unavailable')
            return False
        import pyperclip
        hwnd = find_wechat_hwnd()
        search = _find_uia_search_box(hwnd, log=log)
        if not search or not _click_control(search, ljqCtrl, log=log, label='search_box'):
            _log(log, 'UIA open-chat failed target=%r reason=no_search_box dt=%.3fs' % (target_name, time.time() - t0))
            return False
        time.sleep(0.10)
        ljqCtrl.Press('ctrl+a')
        time.sleep(0.03)
        if not _safe_clipboard_copy(pyperclip, target_name, log=log, label='UIA open-chat target'):
            return False
        ljqCtrl.Press('ctrl+v')
        time.sleep(0.55)

        root = _get_uia_root(hwnd)
        controls = _walk_uia_controls(root, limit=700)
        candidates = []
        for ctrl in controls:
            text = _control_text(ctrl)
            if not target_name_matches(target_name, text):
                continue
            rect = _control_rect(ctrl)
            if not rect:
                continue
            # Prefer search results in the left pane, not the existing title text.
            candidates.append((rect[1], rect[0], ctrl, text, rect))
        candidates.sort(key=lambda x: (x[0], x[1]))
        if not candidates:
            _log(log, 'UIA open-chat miss target=%r reason=no_result dt=%.3fs' % (target_name, time.time() - t0))
            return False
        # Usually first matching result is just below search box; click its row center.
        _, _, ctrl, text, rect = candidates[0]
        if not _click_control(ctrl, ljqCtrl, log=log, label='search_result'):
            return False
        time.sleep(0.35)
        matched = _uia_current_chat_matches({'name': target_name}, log=log)
        _log(log, 'UIA open-chat target=%r clicked_text=%r confirmed=%s dt=%.3fs' % (target_name, text[:120], matched, time.time() - t0))
        return matched
    except Exception as e:
        _log(log, 'UIA open-chat failed target=%r err=%r dt=%.3fs' % (target_name, e, time.time() - t0))
        return False


def _find_uia_message_input(hwnd, log: Optional[LogFn] = None):
    import win32gui
    root = _get_uia_root(hwnd)
    controls = _walk_uia_controls(root, limit=700)
    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    min_x = c0[0] + int(client[2] * 0.28)
    min_y = c0[1] + int(client[3] * 0.58)
    candidates = []
    for ctrl in controls:
        if not _is_edit_control(ctrl):
            continue
        rect = _control_rect(ctrl)
        if not rect:
            continue
        cx, cy = _rect_center(rect)
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        score = 0
        if cx >= min_x and cy >= min_y:
            score += 10
        if area > 5000:
            score += 4
        if any(k in _control_text(ctrl) for k in ('输入', '消息', 'Edit', 'edit')):
            score += 1
        if score:
            candidates.append((score, -area, rect[1], ctrl, _control_text(ctrl), rect))
    candidates.sort(key=lambda x: (-x[0], x[1], -x[2]))
    if candidates:
        _log(log, 'UIA input candidate score=%s rect=%s text=%r' % (candidates[0][0], candidates[0][5], candidates[0][4][:120]))
        return candidates[0][3]
    _log(log, 'UIA input not found edits=%d' % sum(1 for c in controls if _is_edit_control(c)))
    return None


def _find_uia_send_button(hwnd, log: Optional[LogFn] = None):
    root = _get_uia_root(hwnd)
    controls = _walk_uia_controls(root, limit=700)
    candidates = []
    for ctrl in controls:
        text = _control_text(ctrl)
        if not text:
            continue
        if ('发送' in text or 'Send' in text) and (_is_button_control(ctrl) or True):
            rect = _control_rect(ctrl)
            if rect:
                candidates.append((rect[1], rect[0], ctrl, text, rect))
    candidates.sort(key=lambda x: (-x[0], -x[1]))
    if candidates:
        _log(log, 'UIA send-button candidate rect=%s text=%r' % (candidates[0][4], candidates[0][3][:120]))
        return candidates[0][2]
    _log(log, 'UIA send-button not found')
    return None


def _uia_input_and_send(text: str, target: Optional[dict] = None, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> bool:
    t0 = time.time()
    try:
        ljqCtrl = _load_ljqctrl(cfg)
        if ljqCtrl is None:
            _log(log, 'UIA input-send failed reason=ljqCtrl_unavailable')
            return False
        import pyperclip
        hwnd = find_wechat_hwnd()
        if target and not _uia_current_chat_matches(target, log=log):
            _log(log, 'UIA input-send aborted reason=target_mismatch target=%r' % (target_aliases(target)[:1] or ['']))
            return False
        edit = _find_uia_message_input(hwnd, log=log)
        if not edit or not _click_control(edit, ljqCtrl, log=log, label='message_input'):
            _log(log, 'UIA input-send failed reason=no_input dt=%.3fs' % (time.time() - t0))
            return False
        time.sleep(0.12)
        if target and not _uia_current_chat_matches(target, log=log):
            _log(log, 'UIA input-send aborted reason=target_mismatch_after_focus target=%r' % (target_aliases(target)[:1] or ['']))
            return False
        ljqCtrl.Press('ctrl+a')
        time.sleep(0.03)
        if not _safe_clipboard_copy(pyperclip, text, log=log, label='UIA input text'):
            return False
        ljqCtrl.Press('ctrl+v')
        time.sleep(0.18)
        button = _find_uia_send_button(hwnd, log=log)
        if button and _click_control(button, ljqCtrl, log=log, label='send_button'):
            _log(log, 'UIA input-send done via_button dt=%.3fs' % (time.time() - t0))
            return True
        ljqCtrl.Press('enter')
        _log(log, 'UIA input-send done via_enter dt=%.3fs' % (time.time() - t0))
        return True
    except Exception as e:
        _log(log, 'UIA input-send failed err=%r dt=%.3fs' % (e, time.time() - t0))
        return False


def send_reply_backend(text: str, log: Optional[LogFn] = None) -> bool:
    """Best-effort background paste/send; not reliable for Qt WeChat."""
    import win32gui, win32con
    import pyperclip
    hwnd = find_wechat_hwnd()
    if not _safe_clipboard_copy(pyperclip, text, log=log, label='backend text'):
        return False
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


def _cua_run(cmd: list, timeout: float = 10, log: Optional[LogFn] = None) -> 'subprocess.CompletedProcess[str]':
    """Run cua-driver CLI without popping a console window on Windows."""
    import subprocess
    kwargs: dict = dict(capture_output=True, text=True, timeout=timeout)
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


# Off-screen / placeholder detection thresholds for WeChat top-level
# windows.  The minimised WeChat reports itself as a 181x25 placeholder
# parked at (-31991, -32000); cua-driver's `is_on_screen` flag is
# unreliable for Qt windows, so we double-check with the bounds.
_MIN_USABLE_WIDTH = 400
_MIN_USABLE_HEIGHT = 400


def _is_main_wechat_process(app_name: str) -> bool:
    """Return True for the actual WeChat main process, excluding mini-program hosts."""
    return (app_name or '').lower() in ('weixin.exe', 'wechat.exe')


def _cua_filter_usable_candidates(windows):
    """Filter cua-driver list_windows entries to usable main WeChat windows.

    A candidate is unusable when ANY of the following holds:
    * the process is not the main WeChat executable (e.g. WeChatAppEx.exe
      mini-program windows are ignored).
    * bounds.x or bounds.y is negative (Windows places minimised Qt
      windows at -21333 or similar; cua-driver still reports them
      on_screen=True).
    * width or height is below the floor (catches the 181x25
      placeholder that WeChat exposes when minimised).
    * title is empty (Qt child surfaces leak into the list sometimes).

    Returns a list of (area, pid, window_id, width, height, title, x, y).
    """
    out = []
    for w in windows or []:
        title = (w.get('title') or '').strip()
        if not title:
            continue
        if not _is_main_wechat_process(w.get('app_name')):
            continue
        bounds = w.get('bounds') or {}
        try:
            x = int(bounds.get('x', 0))
            y = int(bounds.get('y', 0))
            width = int(bounds.get('width', 0))
            height = int(bounds.get('height', 0))
        except (TypeError, ValueError):
            continue
        if x < 0 or y < 0:
            continue
        if width < _MIN_USABLE_WIDTH or height < _MIN_USABLE_HEIGHT:
            continue
        pid = w.get('pid')
        window_id = w.get('window_id')
        if not pid or not window_id:
            continue
        area = width * height
        out.append((area, pid, window_id, width, height, title, x, y))
    return out


def _cua_call_bring_to_front(pid, log=None) -> bool:
    """Try to restore a minimised WeChat window. Best-effort."""
    import json
    try:
        args = json.dumps({'pid': int(pid)})
        result = _cua_run(['cua-driver', 'bring_to_front', args], timeout=10, log=log)
        ok = result.returncode == 0
        _log(log, 'cua bring_to_front pid=%s ok=%s' % (pid, ok))
        return ok
    except Exception as e:
        _log(log, 'cua bring_to_front failed: %r' % e)
        return False


def _cua_weixin_pids_from_windows(windows):
    """Return the set of main WeChat PIDs seen in the list_windows payload."""
    pids = set()
    for w in windows or []:
        if _is_main_wechat_process(w.get('app_name')):
            try:
                pids.add(int(w.get('pid')))
            except (TypeError, ValueError):
                pass
    return pids


def _cua_find_wechat_window(log=None) -> Optional[Tuple[int, int]]:
    """Find the main WeChat top-level window.

    Filters out off-screen placeholders (the 181x25 minimised surface)
    and the empty-title Qt child surfaces that occasionally appear in
    list_windows output.  Prefers the window whose title is the main
    WeChat title ("微信" / "WeChat") so that already-popped-out independent
    chat windows are not mistaken for the main window.  When the main
    window is minimised, restores it by title and retries; only falls back
    to ``bring_to_front`` / the largest candidate when the main window is
    genuinely unavailable.
    """
    import json, time
    main_titles = {'微信', 'WeChat'}

    def _parse(run_result):
        if run_result.returncode != 0:
            return []
        try:
            return json.loads(run_result.stdout).get('windows') or []
        except Exception:
            return []

    def _main_candidates(windows):
        return [c for c in _cua_filter_usable_candidates(windows) if c[5] in main_titles]

    result = _cua_run(['cua-driver', 'list_windows'], timeout=10, log=log)
    windows = _parse(result)
    pids = _cua_weixin_pids_from_windows(windows)

    # If a usable main-window-title candidate exists, use it immediately.
    mains = _main_candidates(windows)
    if mains:
        mains.sort(reverse=True)
        _, pid, window_id, width, height, title, x, y = mains[0]
        _log(log, 'cua found main WeChat pid=%s hwnd=%s size=%sx%s pos=(%s,%s) title=%r' % (
            pid, window_id, width, height, x, y, title))
        return (int(pid), int(window_id))

    # No usable main title.  If the main title is present but minimised/off-screen,
    # restore it by hwnd before trying anything else.
    main_window = None
    for w in windows:
        if w.get('title') in main_titles and _is_main_wechat_process(w.get('app_name')):
            main_window = w
            break
    if main_window:
        try:
            hwnd = int(main_window.get('window_id', 0))
            if hwnd:
                import win32gui, win32con
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.6)
        except Exception as e:
            _log(log, 'cua restore main window failed: %r' % e)
        retry = _cua_run(['cua-driver', 'list_windows'], timeout=10, log=log)
        windows = _parse(retry)
        mains = _main_candidates(windows)
        if mains:
            mains.sort(reverse=True)
            _, pid, window_id, width, height, title, x, y = mains[0]
            _log(log, 'cua found main WeChat after restore pid=%s hwnd=%s size=%sx%s pos=(%s,%s) title=%r' % (
                pid, window_id, width, height, x, y, title))
            return (int(pid), int(window_id))

    # Main title still unavailable.  Fall back to bring_to_front per PID, then
    # accept the largest usable candidate (which may be an independent window
    # if the user closed the main window entirely).
    if pids:
        for pid in sorted(pids):
            _cua_call_bring_to_front(pid, log=log)
        time.sleep(0.5)
        retry = _cua_run(['cua-driver', 'list_windows'], timeout=10, log=log)
        windows = _parse(retry)
        mains = _main_candidates(windows)
        if mains:
            mains.sort(reverse=True)
            _, pid, window_id, width, height, title, x, y = mains[0]
            _log(log, 'cua found main WeChat after bring_to_front pid=%s hwnd=%s size=%sx%s pos=(%s,%s) title=%r' % (
                pid, window_id, width, height, x, y, title))
            return (int(pid), int(window_id))

    candidates = _cua_filter_usable_candidates(windows)
    if not candidates:
        _log(log, 'cua no usable WeChat window in %s entries' % len(windows))
        return None
    candidates.sort(reverse=True)
    _, pid, window_id, width, height, title, x, y = candidates[0]
    _log(log, 'cua found fallback WeChat pid=%s hwnd=%s size=%sx%s pos=(%s,%s) title=%r (candidates=%s)' % (
        pid, window_id, width, height, x, y, title, len(candidates)))
    return (int(pid), int(window_id))


def _cua_find_separate_chat_window(target: Optional[dict], cfg: Optional[dict] = None,
                                  log: Optional[LogFn] = None) -> Optional[Tuple[int, int]]:
    """Find a usable independent WeChat chat window for ``target``.

    CLI list_windows can report Qt independent chat windows as tiny
    off-screen placeholder bounds while get_window_state still works for
    their HWND.  Unlike the main-window path, this matcher trusts an exact
    WeChat title match and does not apply size/position filtering.
    """
    import json
    cfg = cfg or {}
    names: List[str] = []
    for source in (target or {}, cfg):
        for key in ('cua_window_title', 'separate_window_title', 'window_title'):
            val = source.get(key) if isinstance(source, dict) else None
            if val and str(val) not in names:
                names.append(str(val))
    for val in target_aliases(target):
        if val and val not in names:
            names.append(val)
    if not names:
        _log(log, 'cua separate window: no target aliases')
        return None

    result = _cua_run(['cua-driver', 'list_windows'], timeout=10, log=log)
    if result.returncode != 0:
        _log(log, 'cua separate window list_windows failed rc=%s stderr=%s' % (result.returncode, result.stderr[:200]))
        return None
    try:
        data = json.loads(result.stdout)
    except Exception as e:
        _log(log, 'cua separate window list_windows parse failed: %r' % e)
        return None
    matches = []
    for w in data.get('windows') or []:
        title = (w.get('title') or '').strip()
        if not title:
            continue
        if not _is_main_wechat_process(w.get('app_name')):
            continue
        pid = w.get('pid')
        window_id = w.get('window_id')
        if not pid or not window_id:
            continue
        bounds = w.get('bounds') or {}
        try:
            width = int(bounds.get('width', 0))
            height = int(bounds.get('height', 0))
            x = int(bounds.get('x', 0))
            y = int(bounds.get('y', 0))
        except (TypeError, ValueError):
            width = height = x = y = 0
        area = max(0, width) * max(0, height)
        for name in names:
            if target_name_matches(name, title):
                exact = normalize_chat_name(name) == normalize_chat_name(title)
                matches.append((1 if exact else 0, area, pid, window_id, width, height, title, x, y))
                break
    if not matches:
        _log(log, 'cua separate window not found aliases=%r' % names)
        return None
    matches.sort(reverse=True)
    _, _, pid, window_id, width, height, title, x, y = matches[0]
    _log(log, 'cua separate window found pid=%s hwnd=%s size=%sx%s pos=(%s,%s) title=%r matches=%s' % (
        pid, window_id, width, height, x, y, title, len(matches)))
    return (int(pid), int(window_id))



def _cua_get_window_state(pid: int, window_id: int, log: Optional[LogFn] = None) -> Optional[dict]:
    """Get UIA tree and screenshot from cua-driver."""
    import json
    try:
        args = json.dumps({'pid': pid, 'window_id': window_id})
        result = _cua_run(
            ['cua-driver', 'get_window_state', args],
            timeout=15, log=log
        )
        if result.returncode != 0:
            _log(log, 'cua get_window_state failed rc=%s' % result.returncode)
            return None
        # Parse JSON - handle large output
        data = json.loads(result.stdout)
        return data
    except Exception as e:
        _log(log, 'cua get window state failed: %r' % e)
        return None


def _cua_click_element(pid: int, window_id: int, element_index: int, log: Optional[LogFn] = None) -> bool:
    """Click an element by index using cua-driver.

    The cua-driver daemon keeps the UIA element_index cache across
    `call` invocations, but the cache is keyed on the most recent
    ``get_window_state`` for the same (pid, window_id).  When a long
    time has passed since the last snapshot (or the cache was never
    populated in this daemon session), cua-driver returns
    ``Element N not in cache``.  To avoid that footgun we re-fetch
    the tree just before clicking, in the same Python process so
    the daemon definitely sees the new snapshot.
    """
    import json
    try:
        # Refresh the cache.  If this fails we still try the click
        # below -- the daemon may already have a valid snapshot.
        try:
            args_state = json.dumps({'pid': int(pid), 'window_id': int(window_id)})
            _cua_run(['cua-driver', 'get_window_state', args_state], timeout=10, log=log)
        except Exception as e:
            _log(log, 'cua cache refresh pre-click failed: %r' % e)
        args = json.dumps({
            'pid': int(pid),
            'window_id': int(window_id),
            'element_index': int(element_index),
            'dispatch': 'background',
        })
        result = _cua_run(
            ['cua-driver', 'click', args],
            timeout=10, log=log
        )
        ok = result.returncode == 0
        _log(log, 'cua click element=%s ok=%s' % (element_index, ok))
        return ok
    except Exception as e:
        _log(log, 'cua click failed: %r' % e)
        return False


def _cua_type_text(pid: int, window_id: int, text: str, log: Optional[LogFn] = None) -> bool:
    """Type text using cua-driver (PostMessage WM_CHAR for Qt apps)."""
    import json
    try:
        args = json.dumps({
            'pid': pid,
            'window_id': window_id,
            'text': text,
            'delay_ms': 10,
            'dispatch': 'background'
        })
        result = _cua_run(
            ['cua-driver', 'type_text', args],
            timeout=30, log=log
        )
        ok = result.returncode == 0
        _log(log, 'cua type_text len=%s ok=%s' % (len(text), ok))
        return ok
    except Exception as e:
        _log(log, 'cua type_text failed: %r' % e)
        return False


def _cua_set_value(pid: int, window_id: int, element_index: int, value: str,
                   log: Optional[LogFn] = None) -> bool:
    """Set a UIA Edit's value via ValuePattern (no focus / keystrokes).

    Used for the WeChat search box.  type_text (PostMessage WM_CHAR) only
    works when the Edit actually has keyboard focus, and Qt's
    ``set_value`` action via ``click`` is not always honoured.  Calling
    ``cua-driver set_value`` directly uses ``IUIAutomationValuePattern``
    which is the canonical Win32 write path and works against Qt's
    accessibility bridge.
    """
    import json
    try:
        # Refresh the cache so element_index is valid for this daemon session.
        try:
            args_state = json.dumps({'pid': int(pid), 'window_id': int(window_id)})
            _cua_run(['cua-driver', 'get_window_state', args_state], timeout=10, log=log)
        except Exception as e:
            _log(log, 'cua cache refresh pre-set_value failed: %r' % e)
        args = json.dumps({
            'pid': int(pid),
            'window_id': int(window_id),
            'element_index': int(element_index),
            'value': value,
        })
        result = _cua_run(
            ['cua-driver', 'set_value', args],
            timeout=10, log=log
        )
        ok = result.returncode == 0
        _log(log, 'cua set_value element=%s value=%r ok=%s' % (element_index, value[:40], ok))
        return ok
    except Exception as e:
        _log(log, 'cua set_value failed: %r' % e)
        return False




def _cua_press_key(pid: int, window_id: int, key: str, log: Optional[LogFn] = None) -> bool:
    """Press a key using cua-driver."""
    import json
    try:
        args = json.dumps({
            'pid': pid,
            'window_id': window_id,
            'key': key,
            'dispatch': 'background'
        })
        result = _cua_run(
            ['cua-driver', 'press_key', args],
            timeout=10, log=log
        )
        ok = result.returncode == 0
        _log(log, 'cua press_key %s ok=%s' % (key, ok))
        return ok
    except Exception as e:
        _log(log, 'cua press_key failed: %r' % e)
        return False


def _cua_hotkey(pid: int, window_id: int, keys: List[str],
                log: Optional[LogFn] = None) -> bool:
    """Press a key combination using cua-driver."""
    import json
    try:
        args = json.dumps({
            'pid': pid,
            'window_id': window_id,
            'keys': keys,
            'dispatch': 'background'
        })
        result = _cua_run(
            ['cua-driver', 'hotkey', args],
            timeout=10, log=log
        )
        ok = result.returncode == 0
        _log(log, 'cua hotkey %s ok=%s' % (keys, ok))
        return ok
    except Exception as e:
        _log(log, 'cua hotkey failed: %r' % e)
        return False


def _cua_click_coordinate(pid: int, window_id: int, x: int, y: int,
                          log: Optional[LogFn] = None) -> bool:
    """Click at pixel coordinates within a window (background-safe)."""
    import json
    try:
        args = json.dumps({
            'pid': pid,
            'window_id': window_id,
            'x': x,
            'y': y,
            'dispatch': 'background'
        })
        result = _cua_run(
            ['cua-driver', 'click', args],
            timeout=10, log=log
        )
        ok = result.returncode == 0
        _log(log, 'cua click coordinate (%s,%s) ok=%s' % (x, y, ok))
        return ok
    except Exception as e:
        _log(log, 'cua click coordinate failed: %r' % e)
        return False
# Marker for ListItem elements that match a session we want to open.
# UIA tree looks like:
#   - [69] ListItem "bot群聊测试..." [id=session_item_bot群聊测试 ...]
_SESSION_ID_PREFIX = 'session_item_'
# When the user has typed into the search box, WeChat shows a dropdown
# of candidates; the top hit has an id like search_result_0 (or
# sometimes no prefix -- it's just a ListItem with the chat name).
_SEARCH_RESULT_HINT = 'search_result_'


def _parse_tree_elements(tree_markdown: str):
    """Parse cua-driver's tree_markdown into (element_index, line) pairs.

    Skips lines that do not carry an element_index tag.  This is a
    forgiving parser -- malformed lines are simply ignored.
    """
    import re
    out = []
    if not tree_markdown:
        return out
    # Only match a leading "- [N]" or "[N]" at the start of a tree
    # line.  WeChat's tree can contain nested "[invoke,set_value]"
    # tags inside action lists -- we must not pick those up.
    pat = re.compile(r'^\s*-\s*\[(\d+)\]')
    for line in tree_markdown.split('\n'):
        m = pat.search(line)
        if m:
            out.append((int(m.group(1)), line))
    return out


def _find_session_listitem(tree_markdown: str, target_name: str):
    """Find the element_index of a ListItem matching ``target_name``.

    Returns the element_index, or None if not visible.  Matches
    against the explicit ``id=session_item_<name>`` attribute first
    (most reliable), then falls back to a textual prefix match on
    the displayed name.
    """
    import re
    if not tree_markdown or not target_name:
        return None
    name = target_name.strip()
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'ListItem' not in line:
            continue
        m = re.search(r'\[id=([^\]\s]+)', line)
        if m and m.group(1) == _SESSION_ID_PREFIX + name:
            return idx
    # Fallback: textual match (covers private chats where the
    # ListItem name appears before the [id=...] tag).
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'ListItem' not in line:
            continue
        if name in line:
            return idx
    return None


def _find_search_edit(tree_markdown: str):
    """Return the element_index of the search Edit, or None."""
    import re
    if not tree_markdown:
        return None
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'Edit' in line and '搜索' in line:
            return idx
    # Fallback: any Edit with set_value action.
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'Edit' in line and 'set_value' in line:
            return idx
    return None


def _find_chat_input_edit(tree_markdown: str):
    """Return the element_index of WeChat's message input Edit, or None."""
    if not tree_markdown:
        return None
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'Edit' in line and 'chat_input_field' in line:
            return idx
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'Edit' in line and '输入' in line and 'set_value' in line:
            return idx
    return None


def _find_dropdown_candidate(tree_markdown: str, target_name: str):
    """Return the element_index of the top dropdown hit, or None.

    Looks for a ListItem whose displayed text starts with the target
    name.  The dropdown items do not always carry an id= attribute.
    """
    if not tree_markdown or not target_name:
        return None
    name = target_name.strip()
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'ListItem' not in line:
            continue
        # Prefer items with the search_result_ hint, then plain match.
        if _SEARCH_RESULT_HINT in line and name in line:
            return idx
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'ListItem' not in line:
            continue
        if name in line:
            return idx
    return None


def _cua_get_tree_markdown(pid, window_id, log=None) -> Optional[str]:
    """Call get_window_state and return the tree_markdown string, or None."""
    import json
    args = json.dumps({'pid': pid, 'window_id': window_id})
    result = _cua_run(['cua-driver', 'get_window_state', args], timeout=15, log=log)
    if result.returncode != 0:
        _log(log, 'cua get_window_state failed rc=%s' % result.returncode)
        return None
    try:
        data = json.loads(result.stdout)
    except Exception as e:
        _log(log, 'cua get_window_state parse failed: %r' % e)
        return None
    return data.get('tree_markdown') or ''


def _cua_find_session_control_uiautomation(hwnd: int, names):
    """Return the first uiautomation session-list control matching any name."""
    try:
        root = _get_uia_root(hwnd)
        if root is None:
            return None
        controls = _walk_uia_controls(root, limit=900)
        import win32gui
        client = win32gui.GetClientRect(hwnd)
        c0 = win32gui.ClientToScreen(hwnd, (0, 0))
        # Restrict to the left conversation pane.
        max_x = c0[0] + min(520, max(260, int(client[2] * 0.42)))
        min_y = c0[1] + 80
        max_y = c0[1] + max(120, client[3] - 40)
        for ctrl in controls:
            text = _control_text(ctrl)
            if not text:
                continue
            if not any(target_name_matches(name, text) for name in names):
                continue
            rect = _control_rect(ctrl)
            if not rect:
                continue
            cx, cy = _rect_center(rect)
            if c0[0] <= cx <= max_x and min_y <= cy <= max_y:
                return ctrl
    except Exception:
        pass
    return None


def _cua_click_control_real(ctrl, hwnd: int, log: Optional[LogFn] = None) -> bool:
    """Click a uiautomation control using the real mouse cursor.

    Requires the target window to be foreground so the cursor lands on it.
    This is the only reliable way we have found to switch Qt WeChat chats.
    """
    try:
        rect = _control_rect(ctrl)
        if not rect:
            return False
        if not _force_foreground_window(hwnd, log=log):
            _log(log, 'cua switch_to_chat real-click: failed to foreground window')
            return False
        # Give the OS a moment to complete the foreground swap.
        import time
        time.sleep(0.08)
        ctrl.Click()
        _log(log, 'cua switch_to_chat real-click ok rect=%s' % (rect,))
        return True
    except Exception as e:
        _log(log, 'cua switch_to_chat real-click failed: %r' % e)
        return False


def _cua_double_click_control_real(ctrl, hwnd: int, log: Optional[LogFn] = None) -> bool:
    """Double-click a uiautomation control using the real mouse cursor.

    WeChat's "pop out independent chat window" gesture only works with a real
    double click on the left session list row.  Background UIA/PostMessage
    double clicks do not trigger it, so we briefly move the cursor and use
    SendInput, then restore the cursor position.
    """
    import ctypes
    try:
        rect = _control_rect(ctrl)
        if not rect:
            return False
        if not _force_foreground_window(hwnd, log=log):
            _log(log, 'cua double-click real: failed to foreground window')
            return False
        time.sleep(0.08)

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ('dx', ctypes.c_long),
                ('dy', ctypes.c_long),
                ('mouseData', ctypes.c_ulong),
                ('dwFlags', ctypes.c_ulong),
                ('time', ctypes.c_ulong),
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [('type', ctypes.c_ulong), ('mi', MOUSEINPUT)]

        cx, cy = _rect_center(rect)
        # Save cursor position so we can restore it afterwards.
        point = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        try:
            ctypes.windll.user32.SetCursorPos(cx, cy)
            time.sleep(0.03)
            for _ in range(2):
                for flag_down, flag_up in ((0x0002, 0x0004),):  # MOUSEEVENTF_LEFTDOWN / UP
                    inp = INPUT()
                    inp.type = 0  # INPUT_MOUSE
                    inp.mi = MOUSEINPUT(0, 0, 0, flag_down, 0, None)
                    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
                    time.sleep(0.02)
                    inp.mi.dwFlags = flag_up
                    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
                    time.sleep(0.02)
                time.sleep(0.06)
        finally:
            ctypes.windll.user32.SetCursorPos(point.x, point.y)

        _log(log, 'cua double-click real ok rect=%s' % (rect,))
        return True
    except Exception as e:
        _log(log, 'cua double-click real failed: %r' % e)
        return False

def _cua_switch_to_chat(pid, window_id, target, log=None) -> bool:
    """Open ``target`` as the active chat in WeChat.

    ``target`` may be a dict (uses ``target_aliases`` to enumerate
    candidate names -- name first, then username, remark, etc.) or a
    plain string.  Strategy:
    1. Read the UIA tree; if a session ListItem matching any alias
       is already visible, click it directly.
    2. Otherwise click the search Edit, type the alias (no Enter --
       Enter would open the WeChat web search), wait briefly, re-read
       the tree, and click the dropdown candidate.

    Returns True on success, False on any failure.  Each call is
    idempotent for the click steps; a failure path leaves the
    search box focused, not the active chat.
    """
    if isinstance(target, dict):
        names = [a for a in target_aliases(target) if a]
    elif target:
        names = [str(target)]
    else:
        _log(log, 'cua switch_to_chat: no target')
        return False
    if not names:
        _log(log, 'cua switch_to_chat: empty aliases')
        return False

    # 1. Direct click if the target is already in the visible list.
    tree = _cua_get_tree_markdown(pid, window_id, log=log)
    if not tree:
        return False
    for name in names:
        idx = _find_session_listitem(tree, name)
        if idx is not None:
            _log(log, 'cua switch_to_chat direct-click target=%r element=%s' % (name, idx))
            if _cua_click_element(pid, window_id, idx, log=log):
                # Qt WeChat's ListItem often ignores background UIA Invoke,
                # so verify and fall back to a real foreground mouse click.
                time.sleep(0.25)
                if _uia_current_chat_matches(target, log=log):
                    return True
                _log(log, 'cua switch_to_chat: direct UIA Invoke did not switch, trying real click')
                ctrl = _cua_find_session_control_uiautomation(window_id, names)
                if ctrl and _cua_click_control_real(ctrl, window_id, log=log):
                    time.sleep(0.35)
                    if _uia_current_chat_matches(target, log=log):
                        return True
            # If both click paths failed, continue to search fallback.
            break

    # 2. Search-box fallback. NEVER press Enter.
    search_idx = _find_search_edit(tree)
    if search_idx is None:
        _log(log, 'cua switch_to_chat: search Edit not found in tree')
        return False
    if not _cua_click_element(pid, window_id, search_idx, log=log):
        return False
    time.sleep(0.15)
    primary = names[0]
    # WeChat's search Edit is exposed via UIA ValuePattern.  PostMessage
    # WM_CHAR only lands when the Edit has focus, and Qt's accessibility
    # bridge often does not hand focus to an Edit on click.  Using
    # set_value goes through IUIAutomationValuePattern directly, which
    # is the canonical Win32 write path and is what Qt's bridge honours.
    if not _cua_set_value(pid, window_id, search_idx, primary, log=log):
        # Last-resort fallback: try keystroke-based typing in case the
        # focus did end up on the Edit after all.
        if not _cua_type_text(pid, window_id, primary, log=log):
            return False
    time.sleep(0.25)
    tree2 = _cua_get_tree_markdown(pid, window_id, log=log)
    if not tree2:
        return False
    for name in names:
        idx = _find_dropdown_candidate(tree2, name)
        if idx is not None:
            _log(log, 'cua switch_to_chat dropdown-click target=%r element=%s' % (name, idx))
            # Dropdown ListItems have the same Qt Invoke issue; use real click.
            if _cua_click_element(pid, window_id, idx, log=log):
                time.sleep(0.25)
                if _uia_current_chat_matches(target, log=log):
                    return True
                ctrl = _cua_find_session_control_uiautomation(window_id, names)
                if ctrl and _cua_click_control_real(ctrl, window_id, log=log):
                    time.sleep(0.35)
                    if _uia_current_chat_matches(target, log=log):
                        return True
            break
    _log(log, 'cua switch_to_chat: no dropdown candidate for %r in tree' % primary)
    return False

def _cua_find_active_session_item_uiautomation(hwnd: int, names, log: Optional[LogFn] = None):
    """Return the active/selected session ListItem in the main WeChat window.

    First tries the UIA SelectionItemPattern, then falls back to matching by
    ``names`` so callers can tolerate remark/original-name mismatches.
    """
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(hwnd)
        if win is None:
            return None

        def is_session(ctrl):
            return ctrl.ControlTypeName == 'ListItemControl' and str(ctrl.AutomationId).startswith(_SESSION_ID_PREFIX)

        # Try the selection pattern first (works when the target row is highlighted).
        for ctrl in _walk_uia_controls(win, limit=900):
            if not is_session(ctrl):
                continue
            try:
                pat = ctrl.GetPattern(auto.PatternId.SelectionItemPattern)
                if pat and pat.IsSelected:
                    return ctrl
            except Exception:
                pass

        # Fallback: fuzzy match the displayed session text against any alias.
        for ctrl in _walk_uia_controls(win, limit=900):
            if not is_session(ctrl):
                continue
            text = _control_text(ctrl)
            if any(target_name_matches(name, text) for name in names if name):
                return ctrl
    except Exception as e:
        _log(log, 'cua find_active_session failed: %r' % e)
    return None


def _cua_list_wechat_window_ids() -> set:
    """Return the set of (pid, hwnd) for usable main WeChat top-level windows."""
    import json
    try:
        result = subprocess.run(
            ['cua-driver', 'list_windows', '--json'],
            capture_output=True, text=True, check=False, timeout=10,
        )
        data = json.loads(result.stdout or '{}')
    except Exception:
        return set()
    out = set()
    for w in data.get('windows') or []:
        app_name = (w.get('app_name') or '').lower()
        if app_name not in ('weixin.exe', 'wechat.exe'):
            continue
        try:
            out.add((int(w.get('pid', 0)), int(w.get('window_id', 0))))
        except Exception:
            continue
    return out


def _cua_window_area(pid: int, window_id: int) -> Optional[int]:
    """Return the pixel area of a window from cua-driver's list_windows output."""
    import json
    try:
        result = subprocess.run(
            ['cua-driver', 'list_windows', '--json'],
            capture_output=True, text=True, check=False, timeout=10,
        )
        data = json.loads(result.stdout or '{}')
        for w in data.get('windows') or []:
            if int(w.get('pid', 0)) == pid and int(w.get('window_id', 0)) == window_id:
                bounds = w.get('bounds') or {}
                return int(bounds.get('width', 0)) * int(bounds.get('height', 0))
    except Exception:
        pass
    return None


def _cua_find_any_separate_window(main_hwnd: int, log: Optional[LogFn] = None) -> Optional[Tuple[int, int]]:
    """Return the largest usable WeChat window that is not ``main_hwnd``.

    Used after a pop-out when the title does not match any known alias.
    """
    import json
    result = _cua_run(['cua-driver', 'list_windows'], timeout=10, log=log)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except Exception:
        return None
    best = None
    for w in data.get('windows') or []:
        window_id = w.get('window_id')
        app_name = (w.get('app_name') or '').lower()
        if not _is_main_wechat_process(w.get('app_name')):
            continue
        bounds = w.get('bounds') or {}
        try:
            area = int(bounds.get('width', 0)) * int(bounds.get('height', 0))
        except Exception:
            area = 0
        if best is None or area > best[0]:
            best = (area, int(w.get('pid', 0)), int(window_id))
    if best:
        return best[1], best[2]
    return None
def _cua_pop_out_chat(target: Optional[dict], cfg: Optional[dict] = None,
                      log: Optional[LogFn] = None) -> Optional[Tuple[int, int]]:
    """Pop out an independent chat window for ``target`` from the main WeChat window.

    WeChat's left conversation list shows the user-defined *remark* name for a
    group, which may differ from the original group name used as the popped-out
    window title.  We therefore avoid relying on a direct title match and
    instead:

    1. Foreground the main WeChat window (saving/restoring the previous
       foreground window and cursor position).
    2. Search for the target by its primary alias (name/username/etc.).
    3. Click the top search result to activate the chat.
       After the click the target session is already selected in the left
       conversation list (usually at the top) and highlighted.
    4. Double-click the active session row in the left conversation list.
       Only a real mouse double click reliably triggers WeChat's
       "pop out independent chat window" gesture.
    5. Poll for the new independent window and return it.

    Returns ``(pid, hwnd)`` on success, or ``None`` on failure.
    """
    import time
    cfg = cfg or {}
    if not target:
        _log(log, 'cua pop_out_chat: no target')
        return None

    main_info = _cua_find_wechat_window(log=log)
    if not main_info:
        _log(log, 'cua pop_out_chat: main WeChat window not found')
        return None
    pid, window_id = main_info

    names = [a for a in target_aliases(target) if a]
    if not names:
        _log(log, 'cua pop_out_chat: no target aliases')
        return None

    fg_before = _cua_save_foreground_window()
    try:
        if not _force_foreground_window(window_id, log=log):
            _log(log, 'cua pop_out_chat: failed to foreground main window')
            return None
        time.sleep(0.2)

        try:
            import uiautomation as auto
        except Exception as e:
            _log(log, 'cua pop_out_chat: uiautomation unavailable: %r' % e)
            return None

        win = auto.ControlFromHandle(window_id)
        if win is None:
            _log(log, 'cua pop_out_chat: cannot get UIA root for main window')
            return None

        # Locate the search edit by its Chinese (or English) label.
        search_edit = None
        for name in ('搜索', 'Search'):
            search_edit = win.EditControl(Name=name)
            if search_edit.Exists(0):
                break
        if search_edit is None or not search_edit.Exists(0):
            _log(log, 'cua pop_out_chat: search Edit not found')
            return None

        primary = names[0]
        search_edit.Click()
        time.sleep(0.05)
        search_edit.SendKeys('{Ctrl}a')
        search_edit.SendKeys('{Delete}')
        time.sleep(0.1)
        search_edit.Click()
        time.sleep(0.05)
        search_edit.SendKeys(primary)
        _log(log, 'cua pop_out_chat: searched for %r' % primary)
        time.sleep(0.8)

        # Click the top real search candidate.  WeChat exposes a
        # ``search_item_\u003cdisplay_name\u003e`` row that maps directly to the
        # session-list entry (this is usually the remark name).  Prefer that;
        # fall back to a plain name match otherwise.
        candidate = None
        session_display_name = ''
        search_item_prefix = 'search_item_'
        for ctrl in _walk_uia_controls(win, limit=500):
            if ctrl.ControlTypeName != 'ListItemControl':
                continue
            aid = str(ctrl.AutomationId)
            if aid.startswith(_SESSION_ID_PREFIX):
                continue
            if aid.startswith(search_item_prefix):
                candidate = ctrl
                session_display_name = aid[len(search_item_prefix):]
                break
        if candidate is None:
            for ctrl in _walk_uia_controls(win, limit=500):
                if ctrl.ControlTypeName != 'ListItemControl':
                    continue
                text = _control_text(ctrl)
                aid = str(ctrl.AutomationId)
                if aid.startswith(_SESSION_ID_PREFIX) or aid.startswith(search_item_prefix):
                    continue
                if not text or text.startswith('搜索网络结果') or '搜索网络结果' in text:
                    continue
                if any(target_name_matches(name, text) for name in names):
                    candidate = ctrl
                    break
        if candidate is None:
            _log(log, 'cua pop_out_chat: no search candidate for %r' % primary)
            return None

        _log(log, 'cua pop_out_chat: clicking search candidate %r display=%r' % (_control_text(candidate), session_display_name))
        candidate.Click()
        time.sleep(0.8)

        # After clicking the search result the target chat is active and the
        # matching row appears at the top of the left conversation list.  Do
        # *not* clear the search box (Esc minimises the window and clicking the
        # search box opens a dropdown that covers the list).  Find the active
        # session row and double-click it with the real mouse.
        session_item = None
        if session_display_name:
            session_item = win.ListItemControl(AutomationId=_SESSION_ID_PREFIX + session_display_name)
            if not session_item.Exists(0):
                session_item = None
        if session_item is None:
            session_item = _cua_find_active_session_item_uiautomation(window_id, names, log=log)
        if session_item is None:
            # The chat may be active even though we could not locate the row;
            # attempt to derive the title from the right pane and retry.
            try:
                state = _uia_collect_state(window_id, target=target, limit=500)['state']
                chat_title = str(state.get('current_chat_title') or '').strip()
                if chat_title:
                    chat_title = chat_title.split()[0]
                    session_item = win.ListItemControl(AutomationId=_SESSION_ID_PREFIX + chat_title)
                    if not session_item.Exists(0):
                        session_item = None
            except Exception as e:
                _log(log, 'cua pop_out_chat: title-derived session lookup failed: %r' % e)
        if session_item is None:
            _log(log, 'cua pop_out_chat: active session item not found')
            return None

        _log(log, 'cua pop_out_chat: double-clicking session %r' % str(session_item.AutomationId))

        # Snapshot the current WeChat windows *before* the double click so we can
        # detect the newly-popped-out window afterwards.  The new window title
        # follows the display/remark name, which may differ from target.name.
        before = _cua_list_wechat_window_ids()
        if not _cua_double_click_control_real(session_item, window_id, log=log):
            _log(log, 'cua pop_out_chat: double-click failed')
            return None

        # Wait for a new independent window to appear and return it.
        deadline = time.time() + 8.0
        sep = None
        while time.time() < deadline:
            time.sleep(0.25)
            after = _cua_list_wechat_window_ids()
            new_ids = after - before
            if new_ids:
                # Return the largest new window.
                best = None
                for (wpid, whwnd) in new_ids:
                    area = _cua_window_area(wpid, whwnd) or 0
                    if best is None or area > best[0]:
                        best = (area, wpid, whwnd)
                if best and best[0] >= _MIN_USABLE_WIDTH * _MIN_USABLE_HEIGHT:
                    sep = (best[1], best[2])
                    _log(log, 'cua pop_out_chat: detected new window pid=%s hwnd=%s' % sep)
                    break
            # Fallback to title-based matching.
            sep = _cua_find_separate_chat_window(target, cfg=cfg, log=log)
            if sep:
                break
        if sep:
            _log(log, 'cua pop_out_chat: independent window appeared pid=%s hwnd=%s' % sep)
            return sep
        _log(log, 'cua pop_out_chat: timed out waiting for independent window')
        return None
    finally:
        if fg_before and fg_before != window_id:
            _cua_restore_foreground_window(fg_before, log=log)

def _cua_find_send_button(tree_markdown: str):
    """Return the element_index of the send button, or None.

    The visible send button is the small paper-plane / "发送" button
    in the message-input area.  When present, clicking it is more
    reliable than PostMessage(WM_KEYDOWN, return) -- the Enter key
    can be intercepted by Qt shortcut handling, which is what
    triggers the "独立化当前聊天" behaviour when WeChat is in a
    weird state.
    """
    import re
    if not tree_markdown:
        return None
    for idx, line in _parse_tree_elements(tree_markdown):
        if 'Button' not in line:
            continue
        if '发送' in line or 'Send' in line or 'send' in line:
            return idx
    return None


def send_reply_cua(text: str, target: Optional[dict] = None, cfg: Optional[dict] = None,
                   log: Optional[LogFn] = None) -> SendResult:
    """Send reply using CUA (Computer Use Agent) driver - background, no focus steal.

    This sender uses the cua-driver CLI to interact with WeChat via UIA/PostMessage
    without bringing the window to foreground. Suitable for Qt-based WeChat 4.x.

    Strategy:
    1. Find WeChat window via cua-driver (filters out off-screen
       placeholders; falls back to bring_to_front if the main window
       is minimised).
    2. Get window state (UIA tree + screenshot).
    3. If target chat is specified, switch to it via the UIA tree:
       direct-click a visible ListItem by id when possible, else use
       the search Edit and click the dropdown result.  We never
       press Enter -- Enter on the search Edit opens the WeChat
       web search, not the chat.
    4. Click the message input field at screenshot-relative
       coordinates (the input field is below the chat list and Qt
       does not expose it as a stable UIA Edit).
    5. Clear any stale content, then type the reply.
    6. Send: prefer clicking the visible send Button (found via the
       UIA tree) and only fall back to press_key('return') when no
       such Button is present.  Enter is risky because Qt can route
       it to window-management shortcuts in the minimised/main
       placeholder surface, which is what previously triggered the
       "独立化当前聊天" side-effect.

    Returns SendResult with ok=True if all steps completed without exception.
    Note: This is NOT DB-confirmed; caller should use confirm callback.
    """
    import time
    t0 = time.time()

    # Step 1: Find WeChat window
    window_info = _cua_find_wechat_window(log=log)
    if not window_info:
        return SendResult(ok=False, mode='cua', attempted=['cua_find_window'],
                         reason='cua_wechat_window_not_found')

    pid, window_id = window_info
    attempted = ['cua_find_window']

    # Step 2: Get window state.
    state = _cua_get_window_state(pid, window_id, log=log)
    if not state:
        return SendResult(ok=False, mode='cua', attempted=attempted,
                         reason='cua_get_window_state_failed')

    attempted.append('cua_get_window_state')
    element_count = state.get('element_count', 0)
    _log(log, 'cua window state elements=%s' % element_count)

    # Step 3: Switch to the target chat via the UIA tree, if a target
    # was supplied.  The tree changes after switching, so we re-fetch
    # the state to get fresh screenshot dimensions and a fresh
    # element_index space.
    if target:
        if not _cua_switch_to_chat(pid, window_id, target, log=log):
            return SendResult(ok=False, mode='cua', attempted=attempted + ['cua_switch_to_chat'],
                             reason='cua_switch_to_chat_failed',
                             detail={'target': target})
        attempted.append('cua_switch_to_chat')
        time.sleep(0.2)
        state = _cua_get_window_state(pid, window_id, log=log)
        if not state:
            return SendResult(ok=False, mode='cua', attempted=attempted,
                             reason='cua_get_window_state_failed_after_switch')
        element_count = state.get('element_count', 0)
        _log(log, 'cua window state after switch elements=%s' % element_count)

    # Step 4: Determine input field click coordinates.  WeChat's input
    # field is exposed in the tree but cua-driver's element_index cache
    # does not survive across separate CLI invocations, so we click by
    # screenshot-relative coordinates instead.  The input field sits at
    # the bottom centre of the chat area.
    screenshot_height = state.get('screenshot_height', 1390)
    screenshot_width = state.get('screenshot_width', 1748)
    input_coords = (int(screenshot_width * 0.5), int(screenshot_height * 0.88))
    _log(log, 'cua input coords=%s screenshot=%sx%s' % (input_coords, screenshot_width, screenshot_height))

    # Step 5: Click input field, clear any leftover text, then type.
    if not _cua_click_coordinate(pid, window_id, input_coords[0], input_coords[1], log=log):
        return SendResult(ok=False, mode='cua', attempted=attempted,
                         reason='cua_input_click_failed')
    attempted.append('cua_click_input')
    time.sleep(0.15)

    for _ in range(10):
        _cua_press_key(pid, window_id, 'backspace', log=log)
    time.sleep(0.05)

    if not _cua_type_text(pid, window_id, text, log=log):
        return SendResult(ok=False, mode='cua', attempted=attempted,
                         reason='cua_type_text_failed')
    attempted.append('cua_type_text')
    time.sleep(0.1)

    # Step 6: Send.  Prefer clicking the visible send Button with the real
    # mouse cursor; Qt WeChat's Button controls ignore background UIA
    # Invoke just like its ListItems.  Fall back to cua-driver's
    # element_index click if the UIA control cannot be found, and finally
    # to Enter only if no Button is exposed.
    send_ok = False
    tree_markdown = ''
    state_after = _cua_get_window_state(pid, window_id, log=log)
    if state_after:
        tree_markdown = state_after.get('tree_markdown') or ''
    send_btn_idx = _cua_find_send_button(tree_markdown)
    send_ctrl = _find_uia_send_button(window_id, log=log)
    if send_ctrl is not None:
        if _cua_click_control_real(send_ctrl, window_id, log=log):
            attempted.append('cua_click_send_button')
            send_ok = True
    elif send_btn_idx is not None and _cua_click_element(pid, window_id, send_btn_idx, log=log):
        attempted.append('cua_click_send_button')
        send_ok = True
    else:
        # Last-resort fallback.  When minimised, this is the path
        # that previously triggered "独立化当前聊天"; we accept the
        # risk because the click-input path also runs against a
        # minimised surface in that case.
        if _cua_press_key(pid, window_id, 'return', log=log):
            attempted.append('cua_press_enter')
            send_ok = True
    dt = time.time() - t0
    _log(log, 'cua send completed ok=%s attempted=%s dt=%.3fs' % (send_ok, attempted, dt))

    return SendResult(
        ok=send_ok,
        mode='cua',
        attempted=attempted,
        reason='cua_send_completed' if send_ok else 'cua_send_failed',
        detail={'pid': pid, 'window_id': window_id, 'element_count': element_count, 'dt': dt}
    )


def _cua_restore_window_no_activate(hwnd: int, log: Optional[LogFn] = None) -> bool:
    """Restore a minimised window without stealing foreground.

    Deprecated for separate-window sends: Qt WeChat accepts UIA SetValue
    while the window is minimised, so restoring is unnecessary and avoids
    a visible pop-up.  Kept for callers that explicitly need it.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.IsIconic(hwnd):
            return True
        SW_SHOWNOACTIVATE = 4
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        _log(log, 'restored minimised window hwnd=%s' % hwnd)
        return True
    except Exception as e:
        _log(log, 'restore window failed: %r' % e)
        return False


def _cua_save_foreground_window() -> int:
    """Return the current foreground window handle (0 if none)."""
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def _cua_restore_foreground_window(hwnd: int, log: Optional[LogFn] = None) -> bool:
    """Return foreground to ``hwnd`` without raising or restoring it unnecessarily.

    Uses AllowSetForegroundWindow and, if needed, AttachThreadInput so a
    background service process can still hand focus back to the user.
    """
    if not hwnd:
        return False
    try:
        import ctypes
        import ctypes.wintypes
        import win32api
        import win32gui
        import win32con
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return False
        if user32.GetForegroundWindow() == hwnd:
            return True
        user32.AllowSetForegroundWindow(ctypes.wintypes.DWORD(-1))
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            _log(log, 'restore foreground SetForegroundWindow warn: %r' % (e,))
        time.sleep(0.02)
        if user32.GetForegroundWindow() == hwnd:
            _log(log, 'restored foreground hwnd=%s' % hwnd)
            return True
        cur_tid = win32api.GetCurrentThreadId()
        fg_hwnd = user32.GetForegroundWindow()
        fg_tid = win32gui.GetWindowThreadProcessId(fg_hwnd)[0]
        if fg_tid != cur_tid:
            user32.AttachThreadInput(fg_tid, cur_tid, True)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                _log(log, 'restore foreground attach SetForegroundWindow warn: %r' % (e,))
            user32.AttachThreadInput(fg_tid, cur_tid, False)
        time.sleep(0.02)
        if user32.GetForegroundWindow() == hwnd:
            _log(log, 'restored foreground via attach hwnd=%s' % hwnd)
            return True
        _log(log, 'WARN: could not restore foreground hwnd=%s current_fg=%s' % (hwnd, user32.GetForegroundWindow()))
        return False
    except Exception as e:
        _log(log, 'restore foreground failed: %r' % e)
        return False


def _cua_set_input_value_uiautomation(window_id: int, text: str,
                                      log: Optional[LogFn] = None) -> bool:
    """Use raw UIA ValuePattern to write text into WeChat's input Edit.

    cua-driver's ``set_value`` CLI currently returns ``Element N not in cache``
    across separate CLI invocations, so we fall back to the Python
    ``uiautomation`` library which talks UIA directly.
    """
    try:
        import uiautomation as auto
        # Top-level window handle is the same as cua-driver's window_id.
        win = auto.ControlFromHandle(window_id)
        if not win or not win.Exists(0):
            _log(log, 'uiautomation window not found hwnd=%s' % window_id)
            return False
        edit = win.EditControl(AutomationId='chat_input_field')
        if not edit.Exists(0):
            _log(log, 'uiautomation input edit not found')
            return False
        vp = edit.GetValuePattern()
        if not vp:
            _log(log, 'uiautomation ValuePattern not available')
            return False
        vp.SetValue(text)
        _log(log, 'uiautomation set_value ok len=%s' % len(text))
        return True
    except Exception as e:
        _log(log, 'uiautomation set_value failed: %r' % e)
        return False


def _cua_window_title_from_handle(pid: int, window_id: int) -> Optional[str]:
    """Return the title of a window from cua-driver's list_windows output."""
    import json
    try:
        result = subprocess.run(
            ['cua-driver', 'list_windows', '--json'],
            capture_output=True, text=True, check=False, timeout=10,
        )
        data = json.loads(result.stdout or '{}')
        for w in data.get('windows') or []:
            if int(w.get('pid', 0)) == pid and int(w.get('window_id', 0)) == window_id:
                return str(w.get('title') or '')
    except Exception:
        pass
    return None
def send_reply_cua_separate_window(text: str, target: Optional[dict] = None,
                                   cfg: Optional[dict] = None,
                                   log: Optional[LogFn] = None) -> SendResult:
    """Send through an already-open independent chat window using CUA background input."""
    import time
    t0 = time.time()
    cfg = cfg or {}

    window_info = _cua_find_separate_chat_window(target, cfg=cfg, log=log)
    attempted = ['cua_find_separate_window']
    pop_out_title: Optional[str] = None
    if not window_info:
        _log(log, 'cua separate window not found; trying pop-out from main window')
        window_info = _cua_pop_out_chat(target, cfg=cfg, log=log)
        if not window_info:
            return SendResult(ok=False, mode='cua_separate_window', attempted=attempted + ['cua_pop_out_chat'],
                             reason='cua_separate_window_not_found',
                             detail={'target': target})
        attempted.append('cua_pop_out_chat')
        # The popped-out window title follows the display/remark name, which may
        # differ from target.name.  Capture the actual title so subsequent
        # retries/reconnects can use it.
        try:
            pop_out_title = _cua_window_title_from_handle(window_info[0], window_info[1])
        except Exception as e:
            _log(log, 'cua separate window: could not read popped-out title: %r' % e)

    pid, window_id = window_info

    fg_before = _cua_save_foreground_window()

    def _restore():
        if fg_before and fg_before != window_id:
            _cua_restore_foreground_window(fg_before, log=log)

    state = _cua_get_window_state(pid, window_id, log=log)
    if not state:
        _restore()
        return SendResult(ok=False, mode='cua_separate_window', attempted=attempted,
                         reason='cua_get_window_state_failed')
    attempted.append('cua_get_window_state')
    element_count = state.get('element_count', 0)
    tree_markdown = state.get('tree_markdown') or ''

    # Prefer raw UIA ValuePattern: cua-driver set_value CLI loses element
    # cache across invocations, so uiautomation is more reliable here.
    # We intentionally do *not* restore the window from a minimised state:
    # Qt WeChat accepts ValuePattern writes while minimised, so keeping it
    # minimised avoids any visible pop-up.
    if _cua_set_input_value_uiautomation(window_id, text, log=log):
        attempted.append('cua_set_input_value_uiautomation')
    else:
        input_idx = _find_chat_input_edit(tree_markdown)
        if input_idx is None:
            _restore()
            return SendResult(ok=False, mode='cua_separate_window', attempted=attempted,
                             reason='cua_input_not_found')
        if not _cua_set_value(pid, window_id, input_idx, text, log=log):
            _restore()
            return SendResult(ok=False, mode='cua_separate_window', attempted=attempted,
                             reason='cua_set_input_value_failed')
        attempted.append('cua_set_input_value')
    time.sleep(0.1)
    if not _cua_press_key(pid, window_id, 'return', log=log):
        _restore()
        return SendResult(ok=False, mode='cua_separate_window', attempted=attempted,
                         reason='cua_press_enter_failed')
    attempted.append('cua_press_enter')
    _restore()
    dt = time.time() - t0
    _log(log, 'cua separate send completed attempted=%s dt=%.3fs' % (attempted, dt))
    return SendResult(
        ok=True,
        mode='cua_separate_window',
        attempted=attempted,
        reason='cua_separate_send_completed',
        detail={'pid': pid, 'window_id': window_id, 'element_count': element_count, 'dt': dt,
                'fg_restored': fg_before != window_id}
    )


def open_chat_from_visible_list(target: Optional[dict] = None, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> bool:
    aliases = target_aliases(target)
    target_name = aliases[0] if aliases else ''
    if not target_name:
        return False
    t0 = time.time()
    ljqCtrl = _load_ljqctrl(cfg)
    if ljqCtrl is None:
        _log(log, 'UI list-ocr failed reason=ljqCtrl_unavailable')
        return False
    ocr_utils = _load_ocr_utils(cfg)
    if ocr_utils is None:
        _log(log, 'UI list-ocr failed reason=ocr_utils_unavailable')
        return False
    try:
        from PIL import ImageGrab
        import win32gui
        hwnd = find_wechat_hwnd()
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
    if not _safe_clipboard_copy(pyperclip, target_name, log=log, label='UI search target'):
        return False
    ljqCtrl.Press('ctrl+v')
    time.sleep(0.45)
    ljqCtrl.Click(first_result_x, first_result_y)
    time.sleep(0.35)
    return True


def _physical_input_and_send(hwnd, text: str, target_name: str, ljqCtrl, pyperclip, log: Optional[LogFn] = None) -> bool:
    import win32gui
    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    x = c0[0] + int(client[2] * 0.72)
    y = c0[1] + int(client[3] * 0.88)
    send_x = c0[0] + int(client[2] * 0.92)
    send_y = c0[1] + int(client[3] * 0.94)
    _log(log, 'UI physical-input hwnd=%s target=%r c0=%s client=%s input_xy=(%s,%s) send_xy=(%s,%s) dpi=%s' % (
        hwnd, target_name, c0, client, x, y, send_x, send_y, getattr(ljqCtrl, 'dpi_scale', None)))
    ljqCtrl.Click(x, y)
    time.sleep(0.15)
    ljqCtrl.Press('ctrl+a')
    time.sleep(0.05)
    if not _safe_clipboard_copy(pyperclip, text, log=log, label='physical input text'):
        return False
    ljqCtrl.Press('ctrl+v')
    time.sleep(0.25)
    ljqCtrl.Press('enter')
    time.sleep(0.25)
    ljqCtrl.Click(send_x, send_y)
    return True


def _find_send_button_coords(hwnd, cfg=None):
    """Return approximate send-button screen coordinates for the given WeChat hwnd."""
    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    x = c0[0] + int(client[2] * 0.92)
    y = c0[1] + int(client[3] * 0.94)
    return (x, y)


def _ensure_chat_area_focused(cfg=None, log=None):
    """Placeholder for ensuring chat area focus; may be expanded later."""
    pass


def send_reply_foreground(text: str, target: Optional[dict] = None, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> SendResult:
    result = SendResult(ok=False, mode='foreground')
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    import win32gui, win32con
    import pyperclip
    ljqCtrl = _load_ljqctrl(cfg)
    if ljqCtrl is None:
        _log(log, 'send_reply_foreground failed reason=ljqCtrl_unavailable')
        result.reason = 'ljqCtrl_unavailable'
        return result
    hwnd = find_wechat_hwnd()
    fg_ok = _force_foreground_window(hwnd, log=log)
    result.detail['foreground_ok'] = fg_ok
    if not fg_ok:
        _log(log, 'WARN: could not bring WeChat to foreground; UIA/physical clicks may target the wrong window')
    time.sleep(0.15)

    aliases = target_aliases(target)
    target_name = aliases[0] if aliases else ''
    if cfg is None:
        cfg = {}
    strategy = str(cfg.get('send_strategy') or 'current_or_pyweixin_uia_then_ocr_search_physical')
    prefer_pyweixin = ('pyweixin' in strategy) or bool(cfg.get('pyweixin_first'))
    uia_available = _uia_is_effectively_available(hwnd, log=log)
    # If we couldn't bring WeChat to foreground, absolute-screen UIA clicks will
    # land on whatever window IS in front.  Treat UIA as disabled in that case.
    uia_enabled = ('uia' in strategy) and uia_available and fg_ok
    result.detail['uia_available_tree'] = uia_available
    result.detail['uia_effectively_available'] = uia_enabled
    result.detail['pyweixin_first'] = prefer_pyweixin
    _log(log, 'send_strategy=%r target_name=%r hwnd=%s uia_enabled=%s pyweixin_first=%s' % (strategy, target_name, hwnd, uia_enabled, prefer_pyweixin))

    # --- 0. Prefer verified pyweixin UIA send path when requested. ---
    if target_name and prefer_pyweixin:
        result.attempted.append('pyweixin_uia_send')
        pyweixin_sent = _pyweixin_send_to_friend(target, text, cfg=cfg, log=log)
        result.detail['pyweixin_send'] = pyweixin_sent
        if pyweixin_sent:
            result.ok = True
            result.reason = 'pyweixin_uia_sent'
            _log(log, 'send_reply_foreground result=%s reason=%s attempted=%s detail=%s' % (
                result.ok, result.reason, result.attempted, {k:v for k,v in result.detail.items() if not callable(v)}))
            return result

    # --- 1. Ensure target chat is open (UIA when effective, otherwise OCR/coordinate fallback) ---
    current_verified = False
    if target_name and uia_enabled:
        current_verified = _uia_current_chat_matches(target, log=log)
        result.attempted.append('uia_current_check')
        result.detail['uia_current_matched'] = current_verified
        if current_verified:
            _log(log, 'UIA current chat matches target=%r' % target_name)
    elif target_name and 'uia' in strategy:
        result.attempted.append('uia_skipped_unavailable')
        result.detail['uia_current_matched'] = None

    if target_name and not current_verified:
        opened = False
        # 1a. UIA visible conversation-list select. Do NOT use the WeChat search
        # box here: recent WeChat builds may route it into built-in web search.
        if uia_enabled:
            result.attempted.append('uia_visible_list')
            opened = _uia_open_chat_from_visible_list(target, cfg=cfg, log=log)
            result.detail['uia_open_chat'] = opened

        # 1b. OCR visible list fallback
        if not opened and 'ocr' in strategy:
            result.attempted.append('ocr_visible_list')
            opened = open_chat_from_visible_list(target, cfg=cfg, log=log)

        # 1c. Coordinate search fallback is disabled by default. It can open
        # WeChat's network search surface instead of the target chat.
        if not opened and bool(cfg.get('allow_search_fallback', False)) and 'search' in strategy:
            result.attempted.append('search_fallback')
            _open_chat_by_search(hwnd, target_name, ljqCtrl, pyperclip, log=log)

        if not opened and 'uia' not in strategy and 'ocr' not in strategy and 'search' not in strategy:
            result.reason = 'target_not_switched_strategy_disabled'
    elif not target_name:
        result.attempted.append('current_chat_no_target')
        _log(log, 'UI target missing; assume current chat')

    # --- 2. Type and send (UIA when effective, otherwise physical) ---
    sent = False
    if uia_enabled:
        sent = _uia_input_and_send(text, target=target, cfg=cfg, log=log)
        result.detail['uia_input_send'] = sent
        if sent:
            result.attempted.append('uia_input_send')
        else:
            result.detail['uia_input_blocked_or_failed'] = True
    elif 'uia' in strategy:
        result.detail['uia_input_send'] = None

    if not sent:
        if uia_enabled and result.detail.get('uia_input_blocked_or_failed') and target_name:
            rechecked = _uia_current_chat_matches(target, log=log)
            result.detail['uia_rechecked_before_physical'] = rechecked
            if not rechecked:
                result.reason = 'target_mismatch_blocked'
                _log(log, 'physical fallback blocked reason=target_mismatch target=%r' % target_name)
                _log(log, 'send_reply_foreground result=%s reason=%s attempted=%s detail=%s' % (
                    False, result.reason, result.attempted, {k:v for k,v in result.detail.items() if not callable(v)}))
                return result
        result.attempted.append('physical_input')
        sent = _physical_input_and_send(hwnd, text, target_name, ljqCtrl, pyperclip, log=log)

    result.ok = sent
    result.reason = ('uia_sent' if result.detail.get('uia_input_send') else 'physical_fallback_sent')
    _log(log, 'send_reply_foreground result=%s reason=%s attempted=%s detail=%s' % (
        result.ok, result.reason, result.attempted, {k:v for k,v in result.detail.items() if not callable(v)}))
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
    mode = (mode or cfg.get('send_mode') or 'foreground').lower()
    confirm_timeout = float(cfg.get('send_confirm_timeout') or 5.0)

    if mode in ('backend', 'backend_only', 'background'):
        attempted = send_reply_backend(text, log=log)
        result = SendResult(ok=bool(attempted), mode=mode, attempted=['backend'], reason='backend_attempted')
        return _confirm_result(result, target, before_local_id, text, confirm, confirm_timeout)

    if mode in ('cua', 'cua_background', 'background_cua'):
        result = send_reply_cua(text, target=target, cfg=cfg, log=log)
        return _confirm_result(result, target, before_local_id, text, confirm, confirm_timeout)


    if mode in ('cua_separate_window', 'cua_window', 'separate_window_cua'):
        result = send_reply_cua_separate_window(text, target=target, cfg=cfg, log=log)
        return _confirm_result(result, target, before_local_id, text, confirm, confirm_timeout)
    if mode in ('foreground', 'front'):
        result = send_reply_foreground(text, target=target, cfg=cfg, log=log)
        result = _confirm_result(result, target, before_local_id, text, confirm, confirm_timeout)
        # pyweixin/pywinauto reports success once it has pasted and pressed Alt+S,
        # but it has no post-send acknowledgement.  On local WeChat Qt this can be
        # a false positive (Edit exists, Alt+S/focus does not actually send).  If
        # DB confirmation says the message did not appear, retry once through the
        # normal foreground physical path instead of returning a silent failure.
        if (not result.ok) and ('pyweixin_uia_send' in (result.attempted or [])):
            retry_cfg = dict(cfg or {})
            retry_cfg['pyweixin_first'] = False
            retry_cfg['send_strategy'] = str(retry_cfg.get('send_strategy') or '').replace('pyweixin_', '').replace('pyweixin', '') or 'current_or_ocr_search_physical'
            _log(log, 'pyweixin send not confirmed, fallback foreground physical reason=%s' % result.reason)
            retry = send_reply_foreground(text, target=target, cfg=retry_cfg, log=log)
            retry.mode = mode
            retry.attempted = list(result.attempted or []) + ['confirm_failed_retry'] + list(retry.attempted or [])
            retry.detail['pyweixin_unconfirmed_reason'] = result.reason
            return _confirm_result(retry, target, before_local_id, text, confirm, confirm_timeout)
        return result

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
