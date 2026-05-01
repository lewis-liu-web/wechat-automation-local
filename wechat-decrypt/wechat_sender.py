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
    return {
        'root': root,
        'controls': controls,
        'texts': texts,
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
        _log(log, 'UIA current-check state=%r' % state)
        return bool(state.get('target_matched'))
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
        pyperclip.copy(target_name)
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


def _uia_input_and_send(text: str, cfg: Optional[dict] = None, log: Optional[LogFn] = None) -> bool:
    t0 = time.time()
    try:
        ljqCtrl = _load_ljqctrl(cfg)
        if ljqCtrl is None:
            _log(log, 'UIA input-send failed reason=ljqCtrl_unavailable')
            return False
        import pyperclip
        hwnd = find_wechat_hwnd()
        edit = _find_uia_message_input(hwnd, log=log)
        if not edit or not _click_control(edit, ljqCtrl, log=log, label='message_input'):
            _log(log, 'UIA input-send failed reason=no_input dt=%.3fs' % (time.time() - t0))
            return False
        time.sleep(0.12)
        ljqCtrl.Press('ctrl+a')
        time.sleep(0.03)
        pyperclip.copy(text)
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
    pyperclip.copy(target_name)
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
    pyperclip.copy(text)
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
    strategy = str(cfg.get('send_strategy') or 'current_or_pyweixin_uia_then_ocr_search_physical')
    prefer_pyweixin = ('pyweixin' in strategy) or bool(cfg.get('pyweixin_first'))
    uia_enabled = ('uia' in strategy) and _uia_is_effectively_available(hwnd, log=log)
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
        # 1a. UIA search & select
        if uia_enabled:
            result.attempted.append('uia_search')
            opened = _uia_open_chat_by_search(target, cfg=cfg, log=log)
            result.detail['uia_open_chat'] = opened

        # 1b. OCR visible list fallback
        if not opened and 'ocr' in strategy:
            result.attempted.append('ocr_visible_list')
            opened = open_chat_from_visible_list(target, cfg=cfg, log=log)

        # 1c. Coordinate search fallback
        if not opened and 'search' in strategy:
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
        sent = _uia_input_and_send(text, cfg=cfg, log=log)
        result.detail['uia_input_send'] = sent
        if sent:
            result.attempted.append('uia_input_send')
    elif 'uia' in strategy:
        result.detail['uia_input_send'] = None

    if not sent:
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
