#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Config-driven low-load WeChat group monitor.
# - multi target groups/chats
# - multi message_N.db support
# - per-target Msg_xxx table and last_local_id
# - default mode is read-only: it DOES NOT run full decrypt each cycle
# - optional --sync-on-start / --sync-each-cycle kept for manual testing only
# Config file: wechat_bot_targets.json

import argparse
import ctypes
import os
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from reply_engine import DEFAULT_TRIGGERS, generate_reply

ROOT = Path(__file__).resolve().parent
MEMORY = (ROOT.parent.parent / 'memory').resolve()
CONFIG_PATH = ROOT / 'wechat_bot_targets.json'
DECRYPTED_MESSAGE_DIR = ROOT / 'decrypted' / 'message'
WECHAT_HWND_HINT = 67810
LOG = ROOT / 'wechat_bot_monitor.log'
STOP_FILE = ROOT / 'wechat_bot_monitor.stop'

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + str(msg)
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def msg_table(username):
    return 'Msg_' + hashlib.md5(str(username).encode('utf-8')).hexdigest()


def load_config(path=CONFIG_PATH):
    if not path.exists():
        raise FileNotFoundError('missing config: %s' % path)
    cfg = json.loads(path.read_text(encoding='utf-8'))
    cfg.setdefault('poll_interval', 3)
    cfg.setdefault('default_triggers', DEFAULT_TRIGGERS[:])
    cfg.setdefault('default_reply_template', '')
    cfg.setdefault('wiki_dir', str(ROOT / 'wiki'))
    cfg.setdefault('reply_engine', {})
    cfg.setdefault('context_limit', 12)
    cfg.setdefault('send_mode', 'backend_only')
    cfg.setdefault('stop_file', str(STOP_FILE))
    cfg.setdefault('targets', [])
    for t in cfg['targets']:
        t.setdefault('enabled', True)
        if not t.get('table') and t.get('username'):
            t['table'] = msg_table(t['username'])
        t.setdefault('db', 'message_0.db')
        t.setdefault('last_local_id', 0)
        t.setdefault('triggers', [])
        t.setdefault('reply_template', '')
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def run_decrypt():
    t0 = time.time()
    r = subprocess.run([sys.executable, 'admin_extract_and_decrypt.py'], cwd=str(ROOT),
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    return r.returncode, time.time() - t0, (r.stdout or '')[-500:], (r.stderr or '')[-500:]


def run_fast_refresh(config_path=CONFIG_PATH, force=False):
    t0 = time.time()
    try:
        from fast_refresh_targets import refresh_targets
        results = refresh_targets(config_path, force=force)
        dt = time.time() - t0
        changed = sum(1 for r in results if r.get('status') == 'refreshed')
        failed = [r for r in results if r.get('status') not in ('unchanged', 'refreshed')]
        summary = ','.join('%s:%s' % (r.get('rel'), r.get('status')) for r in results)
        return 0 if not failed else 1, dt, changed, len(failed), summary
    except Exception as e:
        return 1, time.time() - t0, 0, 1, repr(e)


def clean_value(v):
    if isinstance(v, (bytes, bytearray)):
        return '<bytes %d>' % len(v)
    return v


def row_to_dict(row):
    d = {k: clean_value(row[k]) for k in row.keys()}
    ts = d.get('create_time') or 0
    try:
        d['create_time_local'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(ts))) if int(ts) else ''
    except Exception:
        d['create_time_local'] = ''
    return d


def table_exists(con, table):
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone() is not None


def fetch_new_for_db(db_name, targets):
    # Return list of (target, msg) for enabled targets in one message_N.db.
    db_path = DECRYPTED_MESSAGE_DIR / db_name
    if not db_path.exists():
        log('warn db missing %s' % db_path)
        return []
    con = sqlite3.connect('file:%s?mode=ro' % db_path.as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    out = []
    try:
        for t in targets:
            table = t.get('table')
            if not table:
                log('warn target missing table name=%r username=%r' % (t.get('name'), t.get('username')))
                continue
            if not table_exists(con, table):
                log('warn table missing db=%s target=%s table=%s' % (db_name, t.get('name'), table))
                continue
            last_id = int(t.get('last_local_id') or 0)
            sql = f'''select local_id, server_id, local_type, sort_seq, real_sender_id,
                             create_time, status, message_content, source
                      from "{table}"
                      where local_id > ?
                      order by local_id asc
                      limit 50'''
            for row in con.execute(sql, (last_id,)):
                out.append((t, row_to_dict(row)))
    finally:
        con.close()
    return out


def fetch_latest_for_target(t):
    db_path = DECRYPTED_MESSAGE_DIR / t.get('db', 'message_0.db')
    table = t.get('table')
    if not db_path.exists() or not table:
        return None
    con = sqlite3.connect('file:%s?mode=ro' % db_path.as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    try:
        if not table_exists(con, table):
            return None
        row = con.execute(f'select local_id, server_id, local_type, sort_seq, real_sender_id, create_time, status, message_content, source from "{table}" order by local_id desc limit 1').fetchone()
        return row_to_dict(row) if row else None
    finally:
        con.close()


def latest_local_id_for_target(t):
    latest = fetch_latest_for_target(t)
    try:
        return int((latest or {}).get('local_id') or 0)
    except Exception:
        return 0


def has_self_sent_after(t, after_local_id, text_hint=None):
    """Return the first self-sent message after after_local_id, if any."""
    db_path = DECRYPTED_MESSAGE_DIR / t.get('db', 'message_0.db')
    table = t.get('table')
    if not db_path.exists() or not table:
        return None
    con = sqlite3.connect('file:%s?mode=ro' % db_path.as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    try:
        if not table_exists(con, table):
            return None
        rows = con.execute(
            f'select local_id, server_id, local_type, sort_seq, real_sender_id, create_time, status, message_content, source from "{table}" where local_id > ? and status = 2 order by local_id asc limit 8',
            (int(after_local_id or 0),)
        ).fetchall()
        hint = (text_hint or '').strip()
        for row in rows:
            d = row_to_dict(row)
            content = str(d.get('message_content') or '')
            if not hint or content == hint or hint[:20] in content:
                return d
        return row_to_dict(rows[-1]) if rows else None
    finally:
        con.close()


def wait_sent_confirmation(t, before_local_id, text, config_path=CONFIG_PATH, timeout=5.0):
    """Refresh decrypted DB briefly and confirm a new self-sent row exists."""
    deadline = time.time() + max(0.5, float(timeout or 5.0))
    while time.time() < deadline:
        run_fast_refresh(config_path)
        sent = has_self_sent_after(t, before_local_id, text)
        if sent:
            log('send DB-confirmed target=%s before=%s sent_local_id=%s content=%r' % (
                t.get('name'), before_local_id, sent.get('local_id'), sent.get('message_content')))
            return True
        time.sleep(0.5)
    log('send NOT DB-confirmed target=%s before=%s timeout=%.1fs' % (t.get('name'), before_local_id, float(timeout or 5.0)))
    return False


def fetch_context_for_target(t, upto_local_id=None, limit=12):
    """Fetch recent readable messages for prompt context, oldest first.

    The trigger message is included as the last item.  We keep this read-only
    and lightweight: it only reads the decrypted target Msg_xxx table that is
    already refreshed by fast_refresh_targets.
    """
    db_path = DECRYPTED_MESSAGE_DIR / t.get('db', 'message_0.db')
    table = t.get('table')
    if not db_path.exists() or not table:
        return []
    con = sqlite3.connect('file:%s?mode=ro' % db_path.as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    try:
        if not table_exists(con, table):
            return []
        params = []
        where = ''
        if upto_local_id is not None:
            where = 'where local_id <= ?'
            params.append(int(upto_local_id))
        sql = 'select local_id, real_sender_id, create_time, status, message_content from "%s" %s order by local_id desc limit ?' % (table, where)
        params.append(max(1, int(limit)))
        rows = [row_to_dict(r) for r in con.execute(sql, params)]
        out = []
        for r in reversed(rows):
            txt = r.get('message_content')
            if not isinstance(txt, str) or not txt.strip():
                continue
            txt = txt.replace('\r\n', '\n').strip()
            # Group messages often look like "wxid_xxx:\ncontent"; keep both
            # sender id and content so the LLM can resolve short references.
            if len(txt) > 500:
                txt = txt[:500] + '…'
            r['message_content'] = txt
            out.append(r)
        return out
    finally:
        con.close()


def is_trigger(cfg, target, msg):
    if int(msg.get('status') or 0) == 2:  # skip self-sent messages
        return False
    text = str(msg.get('message_content') or '')
    triggers = target.get('triggers') or cfg.get('default_triggers') or []
    return any(k and k in text for k in triggers)


def reply_text(cfg, target):
    return target.get('reply_template') or cfg.get('default_reply_template') or ''


def find_wechat_hwnd():
    import win32gui
    try:
        if win32gui.IsWindow(WECHAT_HWND_HINT) and win32gui.GetWindowText(WECHAT_HWND_HINT) == '微信':
            return WECHAT_HWND_HINT
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


def _find_descendant_windows(hwnd):
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
    # Qt builds differ: try keydown/up with scan code and WM_CHAR CR/LF.
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0x001C0001)
    win32gui.PostMessage(hwnd, win32con.WM_CHAR, 0x0D, 0x001C0001)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0xC01C0001)


def send_reply_backend(text):
    """Paste/send without bringing WeChat to foreground.

    Qt WeChat may ignore pure background keyboard messages on some builds.  This
    function is intentionally best-effort and *does not* fall back to foreground
    unless the caller explicitly chooses a foreground-capable send_mode.
    """
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
    children = _find_descendant_windows(hwnd)
    log('backend send try hwnd=%s fg=%s client=%s input=(%s,%s) send=(%s,%s) children=%d' % (
        hwnd, fg, client, input_x, input_y, send_x, send_y, len(children)))

    targets = [hwnd] + [c[0] for c in children]
    # Click input area, paste, then try both Enter and clicking the Send button.
    # This is best-effort only: recent Qt WeChat often rejects background input.
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
    log('backend send attempted; foreground_preserved=%s (not DB-confirmed)' % preserved)
    return preserved


def _target_name_matches_ocr(target_name, ocr_text):
    """Fuzzy match target chat title against OCR text from the conversation list."""
    if not target_name or not ocr_text:
        return False
    def norm(s):
        return ''.join(ch for ch in str(s) if not ch.isspace() and ch not in '.…·-_:：[]【】()（）')
    a = norm(target_name)
    b = norm(ocr_text)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    # OCR/list often truncates long group names, e.g. "重生之我在重庆..".
    return len(a) >= 6 and a[:6] in b


def _open_chat_from_visible_list(hwnd, target_name, ljqCtrl):
    """Open target chat by OCR-ing the visible conversation list; return True on hit."""
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
        # Left conversation list only: avoid full-screen screenshots per SOP.
        x1 = c0[0] + 40
        y1 = c0[1] + 70
        x2 = c0[0] + min(360, max(260, int(client[2] * 0.34)))
        y2 = c0[1] + min(client[3] - 20, 620)
        img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
        res = ocr_utils.ocr_image(img, enhance=False, engine='rapid')
        for d in res.get('details', []) or []:
            txt = d.get('text') or ''
            if _target_name_matches_ocr(target_name, txt):
                box = d.get('bbox') or []
                if len(box) >= 4:
                    cy = sum(float(p[1]) for p in box[:4]) / 4.0
                else:
                    cy = 0
                # Click row center, not the exact glyph; conversation rows are ~74px high.
                click_x = c0[0] + 170
                click_y = int(y1 + cy)
                log('UI list-ocr hit target=%r text=%r click_xy=(%s,%s) dt=%.3fs' % (
                    target_name, txt, click_x, click_y, time.time() - t0))
                ljqCtrl.Click(click_x, click_y)
                time.sleep(0.20)
                return True
        log('UI list-ocr miss target=%r text=%r dt=%.3fs' % (target_name, res.get('text', '')[:120], time.time() - t0))
    except Exception as e:
        log('UI list-ocr failed target=%r err=%r dt=%.3fs' % (target_name, e, time.time() - t0))
    return False


def send_reply_foreground(text, target=None):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
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
        log('SetForegroundWindow warn: %r' % (e,))
    time.sleep(0.25)

    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))

    # Multi-target bot: never assume the desired chat is the first item in the
    # session list.  Open the target chat explicitly through WeChat search.
    # Coordinates are client-relative physical pixels because this process is
    # DPI-aware and ljqCtrl expects physical coords in this environment.
    target_name = (target or {}).get('name') or (target or {}).get('username') or ''
    if target_name:
        opened = _open_chat_from_visible_list(hwnd, target_name, ljqCtrl)
        if not opened:
            search_x = c0[0] + 150
            search_y = c0[1] + 38
            first_result_x = c0[0] + 170
            first_result_y = c0[1] + 115
            log('UI search fallback target=%r hwnd=%s search_xy=(%s,%s) result_xy=(%s,%s) client=%s dpi=%s' % (
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
    else:
        log('UI target missing; fallback to currently selected chat')

    client = win32gui.GetClientRect(hwnd)
    c0 = win32gui.ClientToScreen(hwnd, (0, 0))
    x = c0[0] + int(client[2] * 0.72)
    y = c0[1] + int(client[3] * 0.88)
    send_x = c0[0] + int(client[2] * 0.92)
    send_y = c0[1] + int(client[3] * 0.94)
    log('UI click input hwnd=%s target=%r c0=%s client=%s input_xy=(%s,%s) send_xy=(%s,%s) dpi=%s' % (hwnd, target_name, c0, client, x, y, send_x, send_y, getattr(ljqCtrl, 'dpi_scale', None)))
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


def send_reply(text, mode=None, target=None, before_local_id=None, cfg=None, config_path=CONFIG_PATH):
    mode = (mode or 'backend_only').lower()
    confirm_timeout = float((cfg or {}).get('send_confirm_timeout') or 5.0)

    def confirmed_or(value):
        if target is None or before_local_id is None:
            return bool(value)
        return wait_sent_confirmation(target, before_local_id, text, config_path=config_path, timeout=confirm_timeout)

    if mode in ('backend', 'backend_only', 'background'):
        attempted = send_reply_backend(text)
        return confirmed_or(attempted)
    if mode in ('foreground', 'front'):
        attempted = send_reply_foreground(text, target=target)
        return confirmed_or(attempted)
    if mode in ('backend_then_foreground', 'background_then_foreground'):
        try:
            if send_reply_backend(text) and confirmed_or(True):
                return True
            log('backend send not confirmed, fallback foreground')
        except Exception as e:
            log('backend send failed, fallback foreground: %r' % (e,))
        attempted = send_reply_foreground(text, target=target)
        return confirmed_or(attempted)
    raise ValueError('unknown send_mode: %s' % mode)


def enabled_targets(cfg):
    return [t for t in cfg.get('targets', []) if t.get('enabled', True)]


def group_targets_by_db(targets):
    grouped = {}
    for t in targets:
        grouped.setdefault(t.get('db', 'message_0.db'), []).append(t)
    return grouped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=str(CONFIG_PATH))
    ap.add_argument('--interval', type=float, default=None)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--baseline-only', action='store_true')
    ap.add_argument('--no-save-state', action='store_true', help='do not write last_local_id back to config')
    ap.add_argument('--sync-on-start', action='store_true', help='run decrypt once before baseline; heavier but refreshes DB')
    ap.add_argument('--sync-each-cycle', action='store_true', help='HEAVY: run decrypt every poll cycle; not recommended')
    ap.add_argument('--no-fast-refresh', action='store_true', help='disable lightweight target DB refresh each cycle')
    ap.add_argument('--fast-refresh-force-start', action='store_true', help='force lightweight refresh before baseline')
    args = ap.parse_args()

    config_path = Path(args.config).resolve()
    cfg = load_config(config_path)
    interval = float(args.interval if args.interval is not None else cfg.get('poll_interval', 3))
    targets = enabled_targets(cfg)
    log('monitor start targets=%d dbs=%s interval=%.1fs dry_run=%s sync_on_start=%s sync_each_cycle=%s fast_refresh=%s' % (
        len(targets), sorted(group_targets_by_db(targets).keys()), interval, args.dry_run, args.sync_on_start, args.sync_each_cycle, not args.no_fast_refresh))

    if args.sync_on_start:
        rc, dt, so, se = run_decrypt()
        if rc != 0:
            log('initial decrypt failed rc=%s stdout=%s stderr=%s' % (rc, so, se))
            return 2
        log('initial sync done dt=%.3fs' % dt)

    if not args.no_fast_refresh:
        rc, dt, changed, failed, summary = run_fast_refresh(config_path, force=args.fast_refresh_force_start)
        log('initial fast-refresh rc=%s dt=%.3fs changed=%s failed=%s %s' % (rc, dt, changed, failed, summary))

    for t in targets:
        latest = fetch_latest_for_target(t)
        if int(t.get('last_local_id') or 0) <= 0 and latest:
            t['last_local_id'] = int(latest.get('local_id') or 0)
        log('baseline target=%s db=%s table=%s last_local_id=%s latest=%s' % (
            t.get('name'), t.get('db'), t.get('table'), t.get('last_local_id'), json.dumps(latest, ensure_ascii=False)))
    if not args.no_save_state:
        save_config(cfg, config_path)
    if args.baseline_only:
        return 0

    stop_path = Path(cfg.get('stop_file') or STOP_FILE)
    if stop_path.exists():
        try:
            stop_path.unlink()
        except Exception:
            pass

    while True:
        if stop_path.exists():
            log('stop file detected: %s' % stop_path)
            try:
                stop_path.unlink()
            except Exception:
                pass
            break
        cycle_t0 = time.time()
        sync_dt = 0.0
        refresh_changed = 0
        refresh_failed = 0
        if args.sync_each_cycle:
            rc, sync_dt, so, se = run_decrypt()
            if rc != 0:
                log('decrypt failed rc=%s dt=%.3f stderr=%s' % (rc, sync_dt, se))
                if args.once:
                    break
                time.sleep(max(0.0, interval - (time.time() - cycle_t0)))
                continue
        elif not args.no_fast_refresh:
            rc, sync_dt, refresh_changed, refresh_failed, summary = run_fast_refresh(config_path)
            if rc != 0:
                log('fast-refresh warn rc=%s dt=%.3f changed=%s failed=%s %s' % (rc, sync_dt, refresh_changed, refresh_failed, summary))

        # Reload config each cycle so adding/removing groups does not require restart.
        cfg = load_config(config_path)
        targets = enabled_targets(cfg)
        new_count = 0
        hit_count = 0
        for db_name, db_targets in group_targets_by_db(targets).items():
            for t, m in fetch_new_for_db(db_name, db_targets):
                new_count += 1
                lid = int(m.get('local_id') or 0)
                advance_state = True
                log('new msg target=%s local_id=%s content=%r' % (t.get('name'), lid, m.get('message_content')))
                if is_trigger(cfg, t, m):
                    hit_count += 1
                    context_limit = int(cfg.get('context_limit') or 12)
                    ctx = fetch_context_for_target(t, upto_local_id=lid, limit=context_limit)
                    if ctx:
                        m['context_messages'] = ctx
                    gen_t0 = time.time()
                    decision = generate_reply(m, t, cfg)
                    gen_dt = time.time() - gen_t0
                    text = decision.reply_text
                    log('trigger hit target=%s local_id=%s ctx=%d gen=%.3fs intent=%s risk=%s need_human=%s reason=%s reply=%r' % (
                        t.get('name'), lid, len(ctx), gen_dt, decision.intent, decision.risk_level, decision.need_human, decision.reason, text))
                    if not decision.should_reply or not text:
                        log('decision: skip send')
                    elif args.dry_run:
                        log('dry-run: skip send')
                    else:
                        t0 = time.time()
                        ok = send_reply(text, cfg.get('send_mode'), target=t, before_local_id=lid, cfg=cfg, config_path=config_path)
                        log('reply send attempted ok=%s cost=%.3fs' % (ok, time.time() - t0))
                        if not ok:
                            # Do not pretend a message was delivered.  Still advance the cursor
                            # by default to avoid repeatedly pasting the same failed reply every
                            # poll; the failed local_id is recorded for manual/native retry.
                            t['last_send_failed_local_id'] = lid
                            t['last_send_failed_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                            if not cfg.get('advance_on_send_failure', True):
                                advance_state = False
                                log('send failed; state NOT advanced due advance_on_send_failure=false')
                            else:
                                log('send failed; state advanced but failure recorded')
                        else:
                            t.pop('last_send_failed_local_id', None)
                            t.pop('last_send_failed_at', None)
                if advance_state:
                    t['last_local_id'] = max(int(t.get('last_local_id') or 0), lid)
        if new_count and not args.no_save_state:
            save_config(cfg, config_path)
        mode = 'sync-each-cycle' if args.sync_each_cycle else ('read-only' if args.no_fast_refresh else 'fast-refresh')
        log('cycle done mode=%s sync=%.3fs total=%.3fs targets=%d new=%d hits=%d refresh_changed=%d refresh_failed=%d' % (
            mode, sync_dt, time.time() - cycle_t0, len(targets), new_count, hit_count, refresh_changed, refresh_failed))
        if args.once:
            break
        time.sleep(max(0.0, interval - (time.time() - cycle_t0)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
