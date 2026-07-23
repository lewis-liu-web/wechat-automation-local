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
from typing import Optional

from file_lock import InterProcessLock, lock_path_for_db


def _db_lock(db_path):
    """Return an InterProcessLock for a decrypted message DB path."""
    return InterProcessLock(str(lock_path_for_db(Path(db_path))), timeout=30.0)

try:
    import zstandard as _zstd
    _ZSTD_DCTX = _zstd.ZstdDecompressor()
except Exception:  # pragma: no cover
    _zstd = None
    _ZSTD_DCTX = None

from reply_engine import DEFAULT_TRIGGERS, precheck, postcheck, resolve_target_kb_ids
from wechat_sender import send_reply_detailed
import admin_commands as _admin_commands
try:
    import reliable_pipeline as _pipeline
except Exception:  # pragma: no cover
    _pipeline = None
try:
    import event_log as _event_log
except Exception:  # pragma: no cover
    _event_log = None


def _record_event(kind, target=None, sender=None, payload=None):
    if _event_log is None:
        return
    try:
        _event_log.log_event(kind, target=target, sender=sender, payload=payload or {})
    except Exception:
        # never let telemetry break the monitor loop
        pass




_PER_MESSAGE_REASONS = {
    'durable_ingress',
    'self_sent_skip',
    'admin_command',
    'target_muted_skip',
    'reliable_pipeline_sender_not_allowed',
    'precheck_boundary',
    'trigger_skip',
}


def _advance_cursor(t, new_cursor, branch_name=None):
    """Advance target cursor and record a per-reason coverage trace.

    reason (branch_name) describes why the cursor advanced (e.g.
    admin_command, self_sent_skip, durable_ingress, thin_monitor,
    deferred_flush).  The trace is consumed by _audit_cursor_coverage after a
    successful save_config to verify that every local_id between the previously
    saved cursor and the new cursor has been accounted for.

    Per-message reasons (durable_ingress, self_sent_skip, admin_command,
    target_muted_skip, reliable_pipeline_sender_not_allowed, precheck_boundary)
    are expected to advance exactly one local_id at a time because they correspond
    to a single fetched message.  Aggregation reasons (startup_advance,
    runtime_min_sync, aggregator_*, deferred_flush, thin_monitor,
    image_only_no_task, decision_skip) may advance over a range of local_ids that
    have been aggregated or buffered.  An unexpected per-message jump records only
    the single expected local_id so that the gap audit reports the skipped IDs.
    """
    old = int(t.get('last_local_id') or 0)
    new = int(new_cursor)
    if new > old:
        key = _target_key(t)
        state = _CURSOR_AUDIT.setdefault(key, {'prev': 0, 'trace': []})
        trace = state['trace']
        if branch_name in _PER_MESSAGE_REASONS and new != old + 1:
            log('cursor_coverage_unexpected_jump target=%s reason=%s old=%s new=%s expected=%s' % (
                t.get('name'), branch_name, old, new, old + 1))
            # Do not record a coverage trace for an unexpected per-message
            # jump; the whole skipped range will be reported as a coverage gap
            # during audit.
        else:
            trace_start = old + 1
            trace_end = new
            if trace and trace[-1][2] == branch_name and trace[-1][1] + 1 == trace_start:
                trace[-1][1] = trace_end
            else:
                trace.append([trace_start, trace_end, branch_name])
        t['last_local_id'] = new
        log('cursor_transition target=%s target_key=%s local_id=%s from=%s to=%s reason=%s' % (
            t.get('name'), key, new, old, new, branch_name or 'unknown'))
        return True
    return False


_CURSOR_AUDIT = {}


def _target_key(t):
    return '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))


def _init_cursor_baseline(t):
    """Set the audit baseline to the target's current cursor so that coverage
    auditing only concerns cursor movement that happens in this process.
    Existing historical cursor positions are accepted without requiring a trace.
    """
    state = _CURSOR_AUDIT.setdefault(_target_key(t), {'prev': 0, 'trace': []})
    state['prev'] = int(t.get('last_local_id') or 0)


def _audit_cursor_coverage(targets, *, consume=False):
    """Verify that every local_id between the previous saved cursor and the
    current cursor is covered by a recorded branch trace.  Logs a warning with a
    bounded sample when gaps are found.  Never includes message content in the
    log; only cursor ranges and a bounded sample of up to 50 local_ids are emitted.

    When consume=True the trace is cleared and the baseline is advanced to the
    current cursor; this must be called only after a successful save_config.  When
    consume=False the audit is logged but the trace and baseline are preserved so
    that no-save-state cycles do not lose coverage state.
    """
    for t in targets:
        key = _target_key(t)
        state = _CURSOR_AUDIT.setdefault(key, {'prev': 0, 'trace': []})
        cur = int(t.get('last_local_id') or 0)
        prev = state['prev']
        if cur <= prev:
            continue
        trace = sorted(state['trace'])
        missing_intervals = []
        pos = prev + 1
        for start, end, branch in trace:
            if end < pos:
                continue
            if start > pos:
                gap_end = min(start - 1, cur)
                if gap_end >= pos:
                    missing_intervals.append((pos, gap_end))
                pos = gap_end + 1
            if pos > cur:
                break
            pos = end + 1
        if pos <= cur:
            missing_intervals.append((pos, cur))
        missing_count = sum(e - s + 1 for s, e in missing_intervals)
        sample = []
        for s, e in missing_intervals:
            for lid in range(s, e + 1):
                if len(sample) >= 50:
                    break
                sample.append(lid)
            if len(sample) >= 50:
                break
        if missing_count:
            log('cursor_coverage_gap target=%s before=%s after=%s missing_count=%s sample=%s' % (
                t.get('name'), prev, cur, missing_count, ','.join(str(x) for x in sample)))
        if consume:
            state['prev'] = cur
            state['trace'] = []
def _decrypt_image_for_message(m, t, cfg):
    """Decrypt an image message and attach its local path.

    Thin-monitor targets still need the local image file path so the agent
    can read it (either through a multimodal provider or the decode_image MCP
    tool).  They do not keep pending image state for fallback triggers.
    """
    lid = int(m.get('local_id') or 0)
    if int(m.get('local_type') or 0) != 3:
        return None
    # Reuse an already-decrypted path from the main loop image block.
    existing = m.get('image_path')
    if existing:
        return str(existing)
    try:
        from image_handler import process_image_message
        img_path = process_image_message(
            m, t, cfg,
            packed_info_data_bytes=m.get('packed_info_data'),
        )
        if img_path:
            m['image_path'] = img_path
            log('image decrypted target=%s local_id=%s path=%s' % (t.get('name'), lid, img_path))
            return img_path
        else:
            log('image decrypt failed target=%s local_id=%s' % (t.get('name'), lid))
    except Exception as e:
        log('image decrypt exception target=%s local_id=%s error=%r' % (t.get('name'), lid, e))
    return None



def _advance_startup_cursors(targets, cfg):
    """Advance target cursors to the current DB max on startup.

    Mutates targets in place and returns a dict mapping target_key -> last_local_id.
    Controlled by ``monitor.advance_cursor_on_start`` (default True).

    Always skips offline backlog when enabled — independent of whether the
    reliable pipeline will process new messages.  Processing is still gated
    per-message; this only prevents reply storms after stop/start.
    """
    advance = bool((cfg or {}).get('monitor', {}).get('advance_cursor_on_start', True))
    runtime_min = {}
    for t in targets:
        latest = fetch_latest_for_target(t)
        latest_id = int(latest.get('local_id') or 0) if latest else 0
        current_id = int(t.get('last_local_id') or 0)
        if advance and latest_id > current_id:
            log('startup cursor advance target=%s from %s to %s (skip history while stopped)' % (t.get('name'), current_id, latest_id))
            _advance_cursor(t, latest_id, 'startup_advance')
        target_key = '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))
        runtime_min[target_key] = int(t.get('last_local_id') or 0)
        log('baseline target=%s db=%s table=%s last_local_id=%s latest=%s' % (
            t.get('name'), t.get('db'), t.get('table'), t.get('last_local_id'), json.dumps(latest, ensure_ascii=False)))
    return runtime_min



# Target keys that were enabled on the previous main-loop cycle. Used to detect
# hot re-enable while monitor stays running and snap cursors past offline backlog.
_PREV_ENABLED_TARGET_KEYS = None

def _ensure_sender_mention(text, msg, contact_names=None):
    reply = str(text or '').strip()
    sender_username = extract_group_sender_username(msg.get('message_content'))
    sender_display = (contact_names or {}).get(sender_username) if sender_username else ''
    name = str(
        msg.get('mention_name')
        or msg.get('sender_display_name')
        or sender_display
        or msg.get('sender_username')
        or msg.get('sender')
        or ''
    ).strip().lstrip('@')
    if not reply or not name:
        return reply
    expected = '@' + name
    if reply.startswith(expected):
        return reply if reply.startswith(expected + ' ') else expected + ' ' + reply[len(expected):].strip()
    if reply.startswith('@'):
        import re
        reply = re.sub(r'^@\S+\s*', '', reply, count=1).strip()
    return (expected + ' ' + reply).strip()

ROOT = Path(__file__).resolve().parent
MEMORY = (ROOT.parent.parent / 'memory').resolve()
CONFIG_PATH = ROOT / 'wechat_bot_targets.json'
DECRYPTED_MESSAGE_DIR = ROOT / 'decrypted' / 'message'
DECRYPTED_CONTACT_DB = ROOT / 'decrypted' / 'contact' / 'contact.db'
WECHAT_HWND_HINT = 67810
LOG = ROOT / 'wechat_bot_monitor.log'
STOP_FILE = ROOT / 'wechat_bot_monitor.stop'

SESSION_WINDOW_DEFAULT = 60  # seconds; group chats should be conservative by default

# Active session state per user per target.
# Key: target+sender, Value: {'expires_at': float}
_active_sessions = {}
_CONFIG_CACHE: dict = {}  # str(path) -> (mtime_ns: int, cfg: dict)
_CONTACT_CACHE: dict = {}  # str(path) -> (mtime_ns: int, names: dict[str, str])


def _invalidate_config_cache(path) -> None:
    try:
        _CONFIG_CACHE.pop(str(Path(path).resolve()), None)
    except Exception:
        pass


def _invalidate_contact_cache(path) -> None:
    try:
        _CONTACT_CACHE.pop(str(Path(path).resolve()), None)
    except Exception:
        pass


MODE_DEFAULTS = {
    'group_assistant': {
        'reply_policy': 'balanced',
        'session_policy': {'timeout_seconds': 60, 'max_turns': 3, 'require_followup_intent': True},
        'context_policy': {'time_window_seconds': 90, 'max_messages': 30, 'sender_recent_limit': 5, 'include_bot_recent': True},
    },
    'customer_service': {
        'reply_policy': 'knowledge_grounded',
        'session_policy': {'timeout_seconds': 120, 'max_turns': 5, 'require_followup_intent': True},
        'context_policy': {'time_window_seconds': 120, 'max_messages': 40, 'sender_recent_limit': 6, 'include_bot_recent': True},
    },
}


CLOSE_SESSION_PATTERNS = ['谢谢', '谢了', '好了', '好啦', '明白', '懂了', '不用了', '没事了', '解决了', '不需要', '算了']

# Recent image paths per sender per target, used when a text message triggers after an image.
# Key: target+sender, Value: [{'path': str, 'time': float}, ...]
_pending_images = {}

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[attr-defined]
except Exception:
    pass


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + str(msg)
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def _deep_merge(base, override):
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_target_policy(cfg, target):
    """Return normalized product policy for a target.

    Keep product entities intentionally small: mode decides business behavior;
    text/image/voice/file are input capabilities, not separate modes.
    """
    mode = str(target.get('mode') or cfg.get('mode') or 'group_assistant').lower()
    if mode == 'personal_assistant' or mode not in MODE_DEFAULTS:
        mode = 'group_assistant'
    policy = _deep_merge(MODE_DEFAULTS[mode], {})
    policy = _deep_merge(policy, cfg.get('policy') or {})
    policy = _deep_merge(policy, target.get('policy') or {})
    for key in ('reply_policy', 'session_policy', 'context_policy', 'trigger_policy', 'input_capabilities', 'image_policy'):
        if key in cfg:
            if isinstance(cfg[key], dict) and isinstance(policy.get(key), dict):
                policy[key] = _deep_merge(policy[key], cfg[key])
            else:
                policy[key] = cfg[key]
        if key in target:
            if isinstance(target[key], dict) and isinstance(policy.get(key), dict):
                policy[key] = _deep_merge(policy[key], target[key])
            else:
                policy[key] = target[key]
    policy.setdefault('input_capabilities', {'text': True, 'image': True, 'voice': False, 'file': False})
    policy.setdefault('image_policy', {'bind_window_seconds': 90, 'max_pending_images': 5, 'trigger_behavior': 'mention_or_active_session'})
    policy['mode'] = mode
    return policy


def _message_text(msg):
    return str((msg or {}).get('message_content') or '').strip()




def _looks_like_session_close(text):
    low = str(text or '').lower()
    return any(p.lower() in low for p in CLOSE_SESSION_PATTERNS)


def msg_table(username):
    return 'Msg_' + hashlib.md5(str(username).encode('utf-8')).hexdigest()


def _session_key(t, msg):
    """Unique key for session: target + sender.

    Always uses real_sender_id (numeric id, stable across messages)
    so that image messages (which may not have sender_username set)
    and trigger messages (which do) share the same key.
    Falls back to sender_username only when real_sender_id is missing.
    """
    sender = msg.get('real_sender_id')
    if sender is None:
        sender = msg.get('sender_username')
        if not sender:
            sender = msg.get('sender') or ''
    key = '%s|%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'), sender)
    log('session_key target=%s sender_field=%s real_sender_id=%s sender_username=%s key=%s' % (
        t.get('name'),
        msg.get('sender') or '',
        msg.get('real_sender_id'),
        msg.get('sender_username') or '',
        key))
    return key


def _clear_expired_sessions(cfg):
    """Remove sessions and pending images that have expired."""
    now = time.time()
    group_policy = resolve_target_policy(cfg, {})
    window = float((group_policy.get('image_policy') or {}).get('bind_window_seconds') or cfg.get('session_window') or SESSION_WINDOW_DEFAULT)
    # Clear expired sessions
    expired = [k for k, v in _active_sessions.items() if now > v.get('expires_at', 0)]
    for k in expired:
        log('session_expired key=%s' % k)
        del _active_sessions[k]
    # Clear expired pending images (same window as session)
    for key, items in list(_pending_images.items()):
        fresh = [item for item in items if now - item.get('time', 0) <= window]
        if fresh:
            _pending_images[key] = fresh
        else:
            log('pending_images_expired key=%s' % key)
            del _pending_images[key]


def _is_in_session(t, msg, cfg):
    """Check if sender can continue an active session for this target."""
    key = _session_key(t, msg)
    sess = _active_sessions.get(key)
    policy = resolve_target_policy(cfg, t)
    session_policy = policy.get('session_policy') or {}
    text = _message_text(msg)
    if sess and time.time() < sess.get('expires_at', 0):
        if _looks_like_session_close(text):
            log('session_close key=%s reason=close_intent text=%r' % (key, text[:80]))
            _active_sessions.pop(key, None)
            return False
        max_turns = int(session_policy.get('max_turns') or 3)
        turns = int(sess.get('turns') or 0)
        if max_turns > 0 and turns >= max_turns:
            log('session_miss key=%s reason=max_turns turns=%s max=%s' % (key, turns, max_turns))
            _active_sessions.pop(key, None)
            return False
        require_followup = bool(session_policy.get('require_followup_intent', True))
        if not require_followup:
            log('session_miss key=%s reason=session_followup_disabled' % key)
            return False
        has_images = bool(msg.get('image_path') or msg.get('session_image_paths'))
        if not text and not has_images:
            log('session_miss key=%s reason=empty_text' % key)
            return False
        timeout = float(session_policy.get('timeout_seconds') or cfg.get('session_window') or SESSION_WINDOW_DEFAULT)
        sess['turns'] = turns + 1
        sess['last_message_at'] = time.time()
        sess['expires_at'] = time.time() + timeout
        log('session_hit key=%s turns=%s expires_in=%.0fs image=%s' % (
            key, sess['turns'], sess['expires_at'] - time.time(), has_images))
        return True


def _maybe_expire_session_on_close_cue(t, msg, decision_plan, event_context):
    """Expire the active session when a close cue is suppressed to silent.

    The trigger branch in the main loop calls _activate_session and skips
    _is_in_session(), so a message that both matches a trigger and looks like
    a session-close cue would leave the session open. This helper closes it
    after the decision layer has returned silent for a close cue.
    """
    if decision_plan.reply_mode != 'silent':
        return
    if not (event_context or {}).get('session_active'):
        return
    text = _message_text(msg)
    if not _looks_like_session_close(text):
        return
    key = _session_key(t, msg)
    if key in _active_sessions:
        log('session_close key=%s reason=close_cue_after_decision text=%r' % (key, text[:80]))
        del _active_sessions[key]
    log('session_miss key=%s active_keys=%s' % (key, list(_active_sessions.keys())))
    return False


def _activate_session(t, msg, cfg):
    """Activate or refresh a session window for this sender."""
    policy = resolve_target_policy(cfg, t)
    session_policy = policy.get('session_policy') or {}
    window = float(session_policy.get('timeout_seconds') or cfg.get('session_window') or SESSION_WINDOW_DEFAULT)
    key = _session_key(t, msg)
    prev = _active_sessions.get(key) or {}
    _active_sessions[key] = {
        'expires_at': time.time() + window,
        'last_message_at': time.time(),
        'turns': int(prev.get('turns') or 0) + 1,
        'mode': policy.get('mode'),
        'state': 'active',
    }
    log('session_activated key=%s mode=%s turns=%s window=%.0fs expires_at=%s' % (
        key, policy.get('mode'), _active_sessions[key]['turns'], window,
        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_active_sessions[key]['expires_at']))))


def _read_and_init_config(path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    cfg.setdefault('poll_interval', 3)
    cfg.setdefault('default_triggers', DEFAULT_TRIGGERS[:])
    cfg.setdefault('default_response_mode', 'trigger')
    cfg.setdefault('default_reply_template', '')
    cfg.setdefault('wiki_dir', str(ROOT / 'wiki'))
    cfg.setdefault('reply_engine', {})
    cfg.setdefault('context_limit', 12)
    cfg.setdefault('send_mode', 'cua_separate_window')
    cfg.setdefault('send_strategy', 'current_or_uia_then_ocr_search_physical')
    cfg.setdefault('uia_probe_enabled', True)
    cfg.setdefault('stop_file', str(STOP_FILE))
    cfg.setdefault('targets', [])
    # Merge image/DB settings from config.json (decryptor config) if available
    try:
        from config import CONFIG_FILE
        import json as _json
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                db_cfg = _json.load(f)
            # Copy keys needed by image_handler and other utilities
            for key in ('db_dir', 'decrypted_dir', 'image_aes_key', 'image_xor_key', 'decoded_images_dir'):
                if key in db_cfg and key not in cfg:
                    cfg[key] = db_cfg[key]
            # Also expose wechat_data_dir for backward compatibility
            # wechat_data_dir should be the parent of db_dir (which ends with /db_storage)
            if 'db_dir' in db_cfg and 'wechat_data_dir' not in cfg:
                db_dir = db_cfg['db_dir']
                if db_dir.endswith('db_storage') or db_dir.endswith('db_storage/'):
                    cfg['wechat_data_dir'] = os.path.dirname(db_dir)
                else:
                    cfg['wechat_data_dir'] = db_dir
    except Exception:
        pass
    for t in cfg['targets']:
        t.setdefault('enabled', True)
        if not t.get('table') and t.get('username'):
            t['table'] = msg_table(t['username'])
        t.setdefault('db', 'message_0.db')
        t.setdefault('last_local_id', 0)
        t.setdefault('triggers', [])
        t.setdefault('response_mode', 'trigger')
        t.setdefault('reply_template', '')
    return cfg

def _apply_decrypted_dirs(cfg: dict) -> None:
    """Point module-level decrypted DB paths to the configured directory.

    The monitor may be launched from a checkout directory while config.json
    points decrypted output elsewhere (e.g. a dedicated data directory).
    Without this, fetch_new_for_db reads stale local copies.
    """
    global DECRYPTED_MESSAGE_DIR, DECRYPTED_CONTACT_DB
    ddir = cfg.get('decrypted_dir')
    if not ddir:
        return
    dpath = Path(ddir).resolve()
    DECRYPTED_MESSAGE_DIR = dpath / 'message'
    DECRYPTED_CONTACT_DB = dpath / 'contact' / 'contact.db'


def load_config(path=CONFIG_PATH):
    """Load and initialize wechat_bot_targets.json, cached by mtime.

    The main loop calls this every poll cycle.  When the file mtime is unchanged
    we return the previously initialized dict, avoiding JSON parse + setdefault
    + per-target table derivation on every cycle.  External writes
    (manage_targets, control_api) are detected via st_mtime_ns and the cache is
    rebuilt.  save_config() invalidates the cache entry before writing.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError('missing config: %s' % p)
    try:
        mtime_ns = p.stat().st_mtime_ns
    except OSError:
        mtime_ns = -1
    key = str(p.resolve())
    cached = _CONFIG_CACHE.get(key)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    cfg = _read_and_init_config(p)
    _CONFIG_CACHE[key] = (mtime_ns, cfg)
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    _invalidate_config_cache(path)
    p = Path(path)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(p)



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


def _decode_message_content(content, ct):
    """Return plaintext for message_content. ct is WCDB_CT_message_content.

    - 4: zstd-compressed BLOB -> decompress -> utf-8 string
    - 0 / None: pass through (may be str or bytes)
    - bytes (any other ct) -> utf-8 with errors='replace'
    - str -> return as-is

    On zstd failure: warn and return '' (do not raise; one bad message must
    not kill the monitor loop).
    """
    if ct == 4 and isinstance(content, (bytes, bytearray)):
        if _ZSTD_DCTX is None:
            log('warn zstd content present but zstandard not installed')
            return ''
        try:
            return _ZSTD_DCTX.decompress(content).decode('utf-8', errors='replace')
        except Exception as e:
            log('warn zstd decompress failed: %r' % (e,))
            return ''
    if isinstance(content, (bytes, bytearray)):
        return content.decode('utf-8', errors='replace')
    return content


def row_to_dict(row):
    ct = None
    if 'WCDB_CT_message_content' in row.keys():
        try:
            ct = int(row['WCDB_CT_message_content'] or 0)
        except Exception:
            ct = 0
    d = {}
    for k in row.keys():
        v = row[k]
        if k == 'message_content' and ct is not None:
            v = _decode_message_content(v, ct)
        elif isinstance(v, (bytes, bytearray)):
            v = '<bytes %d>' % len(v)
        d[k] = v
    ts = d.get('create_time') or 0
    try:
        d['create_time_local'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(ts))) if int(ts) else ''
    except Exception:
        d['create_time_local'] = ''
    return d


def table_exists(con, table):
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone() is not None


def load_contact_name_map(db_path=DECRYPTED_CONTACT_DB):
    """Return username -> display name from the decrypted contact DB, cached.

    Group messages in message DB are stored as "username:\ncontent".  That
    username is stable but not suitable for a human-facing @ prefix, so resolve
    it to remark/nick_name before passing the message to reply_engine.

    The main loop calls this every poll cycle.  We cache the dict keyed by
    file mtime_ns; on a hit we skip the SQLite open and full-table scan.
    The contact DB only changes when fast_refresh_targets decrypts it, so
    a stable mtime means the result is stable.  Returning a fresh copy on
    hit keeps the contract that callers may mutate the dict without
    poisoning the cache.
    """
    p = Path(db_path)
    if not p.exists():
        return {}
    try:
        mtime_ns = p.stat().st_mtime_ns
    except OSError:
        mtime_ns = -1
    key = str(p.resolve())
    cached = _CONTACT_CACHE.get(key)
    if cached is not None and cached[0] == mtime_ns:
        return dict(cached[1])
    try:
        con = sqlite3.connect('file:%s?mode=ro' % p.as_posix(), uri=True)
        con.row_factory = sqlite3.Row
    except Exception as e:
        log('warn load contact names failed: %r' % (e,))
        return {}
    try:
        if not table_exists(con, 'contact'):
            names = {}
        else:
            names = {}
            for row in con.execute('select username, remark, nick_name, alias from contact'):
                username = str(row['username'] or '').strip()
                if not username:
                    continue
                display = str(row['remark'] or row['nick_name'] or row['alias'] or username).strip()
                names[username] = display or username
    except Exception as e:
        log('warn load contact names failed: %r' % (e,))
        return {}
    finally:
        con.close()
    _CONTACT_CACHE[key] = (mtime_ns, names)
    return dict(names)


def extract_group_sender_username(message_content):
    text = str(message_content or '')
    if ':\n' not in text:
        return ''
    prefix = text.split(':\n', 1)[0].strip()
    # Avoid treating ordinary prose as a username; WeChat ids/usernames do not
    # contain whitespace and are stored as a short prefix before the newline.
    if not prefix or len(prefix) > 128 or any(ch.isspace() for ch in prefix):
        return ''
    return prefix


def enrich_sender_display_name(msg, contact_names):
    sender = extract_group_sender_username(msg.get('message_content'))
    if not sender:
        return msg
    display = (contact_names or {}).get(sender) or sender
    msg['sender_username'] = sender
    msg['sender_display_name'] = display
    msg['mention_name'] = display
    return msg


def fetch_new_for_db(db_name, targets):
    # Return list of (target, msg) for enabled targets in one message_N.db.
    db_path = DECRYPTED_MESSAGE_DIR / db_name
    if not db_path.exists():
        log('warn db missing %s' % db_path)
        return []
    with _db_lock(db_path):
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
                                 create_time, status, message_content, source, packed_info_data,
                                 WCDB_CT_message_content
                          from "{table}"
                          where local_id > ?
                          order by local_id asc
                          limit 50'''
                for row in con.execute(sql, (last_id,)):
                    d = row_to_dict(row)
                    # Keep packed_info_data as raw bytes for image MD5 extraction
                    raw_val = row['packed_info_data']
                    if raw_val is not None:
                        d['packed_info_data'] = raw_val
                    out.append((t, d))
        finally:
            con.close()
        return out


def fetch_latest_for_target(t):
    db_path = DECRYPTED_MESSAGE_DIR / t.get('db', 'message_0.db')
    table = t.get('table')
    if not db_path.exists() or not table:
        return None
    with _db_lock(db_path):
        con = sqlite3.connect('file:%s?mode=ro' % db_path.as_posix(), uri=True)
        con.row_factory = sqlite3.Row
        try:
            if not table_exists(con, table):
                return None
            row = con.execute(f'select local_id, server_id, local_type, sort_seq, real_sender_id, create_time, status, message_content, source, packed_info_data, WCDB_CT_message_content from "{table}" order by local_id desc limit 1').fetchone()
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
    with _db_lock(db_path):
        con = sqlite3.connect('file:%s?mode=ro' % db_path.as_posix(), uri=True)
        con.row_factory = sqlite3.Row
        try:
            if not table_exists(con, table):
                return None
            rows = con.execute(
                f'select local_id, server_id, local_type, sort_seq, real_sender_id, create_time, status, message_content, source, packed_info_data, WCDB_CT_message_content from "{table}" where local_id > ? and status = 2 order by local_id asc limit 8',
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


def _target_raw_db_path(t, cfg) -> Optional[Path]:
    """Return the encrypted (raw) DB path for a target, or None if unknown.

    The fast-refresh tool treats the target's ``db`` field as either a bare
    filename (e.g. ``message_0.db``) or a relative path inside the WeChat
    db_storage tree (e.g. ``bizchat/biz_message_0.db``).  Bare filenames are
    resolved under the canonical ``message/`` subdirectory.  This mirrors
    ``fast_refresh_targets.normalize_target_db`` so the two stay in sync.
    """
    db_dir = (cfg or {}).get('db_dir')
    if not db_dir:
        return None
    rel = str(t.get('db') or 'message_0.db').replace('\\', '/').lstrip('/')
    if '/' not in rel:
        rel = 'message/' + rel
    norm = os.path.normpath(rel)
    if os.path.isabs(norm) or norm.startswith('..') or not norm.endswith('.db'):
        return None
    return Path(db_dir) / norm


def _read_db_dir() -> Optional[str]:
    """Return the raw WeChat db_storage path from config.json, or None.

    We intentionally bypass ``load_config`` here: the wait path runs inside
    a tight confirm loop and we only need a single field.  On any failure
    (missing config, import error) we return None and the caller falls back
    to refreshing every cycle, which is the safe default.
    """
    try:
        from config import CONFIG_FILE
    except Exception:
        return None
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        db_dir = cfg.get('db_dir')
        return db_dir if isinstance(db_dir, str) and db_dir else None
    except Exception:
        return None



def wait_sent_confirmation(t, before_local_id, text, config_path=CONFIG_PATH, timeout=5.0):
    """Wait until a new self-sent row shows up in the decrypted DB.

    Refresh policy: the raw (encrypted) DB only changes when WeChat itself
    writes a new message.  We stat the raw DB mtime and skip the
    ``fast_refresh_targets`` step when it is unchanged, so an in-flight send
    confirmation does not redundantly re-decrypt all targets every 0.5s.
    If the raw DB mtime moves (or the raw file is missing) we do a refresh.
    """
    t0 = time.time()
    deadline = t0 + max(0.5, float(timeout or 5.0))
    attempts = 0
    refresh_total = 0.0
    refresh_skipped = 0
    lookup_total = 0.0
    last_summary = ''
    last_rc = 0
    last_changed = 0
    raw_path = _target_raw_db_path(t, {'db_dir': _read_db_dir()})
    last_raw_mtime_ns: Optional[int] = None
    while time.time() < deadline:
        attempts += 1
        cur_mtime_ns: Optional[int] = None
        if raw_path is not None and raw_path.exists():
            try:
                cur_mtime_ns = raw_path.stat().st_mtime_ns
            except OSError:
                cur_mtime_ns = None
        need_refresh = (raw_path is None) or (cur_mtime_ns is None) or (cur_mtime_ns != last_raw_mtime_ns)
        if need_refresh:
            refresh_t0 = time.time()
            rc, refresh_dt, changed, failed, summary = run_fast_refresh(config_path)
            refresh_total += time.time() - refresh_t0
            last_summary = summary
            last_rc, last_changed, last_failed = rc, changed, failed
            if cur_mtime_ns is not None:
                last_raw_mtime_ns = cur_mtime_ns
        else:
            refresh_skipped += 1
        lookup_t0 = time.time()
        sent = has_self_sent_after(t, before_local_id, text)
        lookup_total += time.time() - lookup_t0
        if sent:
            log('send DB-confirmed target=%s before=%s sent_local_id=%s elapsed=%.3fs attempts=%d refresh=%d skipped=%d refresh_total=%.3fs lookup_total=%.3fs last_refresh=rc%s/changed%s/failed%s/%s content=%r' % (
                t.get('name'), before_local_id, sent.get('local_id'), time.time() - t0, attempts, attempts - refresh_skipped, refresh_skipped,
                refresh_total, lookup_total, last_rc, last_changed, last_failed, last_summary, sent.get('message_content')))
            return True
        time.sleep(0.5)
    log('send NOT DB-confirmed target=%s before=%s timeout=%.1fs elapsed=%.3fs attempts=%d refresh=%d skipped=%d refresh_total=%.3fs lookup_total=%.3fs last_refresh=%s' % (
        t.get('name'), before_local_id, float(timeout or 5.0), time.time() - t0, attempts, attempts - refresh_skipped, refresh_skipped,
        refresh_total, lookup_total, last_summary))
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
    with _db_lock(db_path):
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
            sql = 'select local_id, real_sender_id, create_time, status, message_content, packed_info_data, WCDB_CT_message_content from "%s" %s order by local_id desc limit ?' % (table, where)
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


def build_event_context(t, trigger_msg, cfg, limit=None):
    """Build event-centered context for group bot replies.

    Keeps the legacy flat context_messages for compatibility, and adds structured
    slices so the agent can distinguish the trigger, same-sender short history,
    nearby group chatter, recent bot reply, and related images.
    """
    policy = resolve_target_policy(cfg, t)
    ctx_policy = policy.get('context_policy') or {}
    max_messages = int(limit or ctx_policy.get('max_messages') or cfg.get('context_limit') or 30)
    sender_recent_limit = int(ctx_policy.get('sender_recent_limit') or 5)
    window_seconds = int(ctx_policy.get('time_window_seconds') or 90)
    all_rows = fetch_context_for_target(t, upto_local_id=trigger_msg.get('local_id'), limit=max_messages)
    trigger_lid = int(trigger_msg.get('local_id') or 0)
    trigger_ts = int(trigger_msg.get('create_time') or 0)
    sender = trigger_msg.get('sender_username')
    if not sender:
        sender = trigger_msg.get('real_sender_id')
        if sender is None:
            sender = trigger_msg.get('sender') or ''

    def row_sender(row):
        return extract_group_sender_username(row.get('message_content')) or row.get('real_sender_id') or row.get('sender') or ''

    time_window_messages = []
    sender_recent = []
    bot_recent = []
    for row in all_rows:
        try:
            lid = int(row.get('local_id') or 0)
            ts = int(row.get('create_time') or 0)
        except Exception:
            lid, ts = 0, 0
        if lid == trigger_lid:
            continue
        if trigger_ts and ts and 0 <= (trigger_ts - ts) <= window_seconds:
            time_window_messages.append(row)
        if sender and str(row_sender(row)) == str(sender):
            sender_recent.append(row)
        if int(row.get('status') or 0) == 2:
            bot_recent.append(row)

    event_context = {
        'mode': policy.get('mode'),
        'reply_policy': policy.get('reply_policy'),
        'trigger_message': trigger_msg,
        'time_window_seconds': window_seconds,
        'time_window_messages': time_window_messages[-max_messages:],
        'sender_recent': sender_recent[-sender_recent_limit:],
        'bot_recent': bot_recent[-1:] if (ctx_policy.get('include_bot_recent') is not False) else [],
        'related_images': list(trigger_msg.get('session_image_paths') or ([] if not trigger_msg.get('image_path') else [trigger_msg.get('image_path')])),
    }
    return all_rows, event_context


import re

def _match_triggers(text: str, triggers: list) -> bool:
    """Exact trigger matching. @mentions must match whole word like WeChat @attention."""
    text = str(text or '')
    for trig in triggers:
        if not trig:
            continue
        if trig.startswith('@'):
            # @mention exact match: @nickname followed by space/sep or end of string
            # WeChat uses \u2005 (four-per-em space) after @mentions
            pattern = re.escape(trig) + r'(?:[\s\u2005]|$)'
            if re.search(pattern, text):
                return True
        else:
            # Non-@ triggers: substring match
            if trig in text:
                return True
    return False


def is_trigger(cfg, target, msg):
    """Unified trigger check for all message types.

    Modes:
      - 'trigger' (default): only respond when a trigger keyword matches.
        Empty trigger list means NO response.
      - 'free': respond to all messages (no trigger check).
    """
    if int(msg.get('status') or 0) == 2:  # skip self-sent messages
        return False

    response_mode = (target.get('response_mode') or cfg.get('default_response_mode') or 'trigger').lower()
    if response_mode == 'free':
        return True

    # trigger mode
    triggers = target.get('triggers')
    if triggers is None:
        triggers = cfg.get('default_triggers', [])
    if not triggers:
        return False  # empty trigger list = no response

    text = str(msg.get('message_content') or '')
    return _match_triggers(text, triggers)


def should_enter_durable(cfg, target, msg):
    """Return (enter, trigger_hit, session_active) for durable ingress gate.

    free mode: always enter (via is_trigger).
    trigger mode: enter on keyword match OR active multi-turn session.
    empty triggers + trigger mode: never enter unless session already open.
    """
    session_active = bool(_is_in_session(target, msg, cfg))
    trigger_hit = bool(is_trigger(cfg, target, msg))
    return (trigger_hit or session_active), trigger_hit, session_active


def reply_text(cfg, target):
    return target.get('reply_template') or cfg.get('default_reply_template') or ''



def send_reply(text, mode=None, target=None, before_local_id=None, cfg=None, config_path=CONFIG_PATH, mention_name=None):
    """Compatibility wrapper around wechat_sender with DB confirmation."""
    result = send_reply_detailed(
        text,
        mode=mode,
        target=target,
        before_local_id=before_local_id,
        cfg=cfg,
        confirm=lambda t, lid, body, timeout: wait_sent_confirmation(
            t, lid, body, config_path=config_path, timeout=timeout),
        log=log,
        mention_name=mention_name,
    )
    log('send result ok=%s mode=%s reason=%s confirmed=%s attempted=%s detail_keys=%s' % (
        result.ok, result.mode, result.reason, result.confirmed, ','.join(result.attempted), ','.join(sorted(result.detail.keys()))))
    return bool(result)


def _send_image_task_missing_guide(turn, cfg, config_path=CONFIG_PATH, dry_run=False):
    event_context = turn.event_context or {}
    sender = event_context.get('sender_display_name') or event_context.get('mention_name') or event_context.get('sender_username') or ''
    mention = f"@{sender} " if sender else ""
    guide_text = f"{mention}缺少图片处理任务描述，请重新发送图片+任务描述"
    target = turn.target or {}
    log('aggregator_image_only_no_description target=%s local_id=%s sender=%s' % (
        target.get('name'), turn.end_local_id, sender))
    if dry_run:
        log('dry-run: skip image-only guide send')
        return True
    t0 = time.time()
    ok = send_reply(guide_text, cfg.get('send_mode'), target=target, before_local_id=turn.end_local_id, cfg=cfg, config_path=config_path)
    log('reply send attempted ok=%s cost=%.3fs' % (ok, time.time() - t0))
    return bool(ok)


def enabled_targets(cfg):
    return [t for t in cfg.get('targets', []) if t.get('enabled', True)]


def group_targets_by_db(targets):
    grouped = {}
    for t in targets:
        grouped.setdefault(t.get('db', 'message_0.db'), []).append(t)
    return grouped


def _clean_command_text(msg):
    text = str((msg or {}).get('message_content') or '')
    prefix = extract_group_sender_username(text)
    if prefix and text.startswith(prefix + ':\n'):
        text = text[len(prefix) + 2:]
    return text.strip()


def _try_handle_admin_command(m, t, cfg, config_path, dry_run=False, contact_names=None):
    sender = m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or ''
    sender_display_name = m.get('sender_display_name') or (contact_names or {}).get(sender) or ''
    clean_text = _clean_command_text(m)
    try:
        result = _admin_commands.handle_admin_command(clean_text, t, cfg, sender, sender_display_name)
    except Exception as e:
        log('admin_command handler exception target=%s local_id=%s error=%r' % (t.get('name'), m.get('local_id'), e))
        return False
    if not result.handled:
        return False
    # Disabled-aware short-circuit: command recognized (handler ran) but no t.update,
    # no send_reply, no cursor advance, no config save. The message replays on
    # next cycle once re-enabled so the mutation applies exactly once.
    if not _reliable_pipeline_globally_enabled(cfg):
        lid = int(m.get('local_id') or 0)
        _record_event('admin_command_recognized_globally_disabled',
                      target=t.get('name'), sender=sender,
                      payload={'local_id': lid, 'action': getattr(result, 'action', '')})
        return True
    lid = int(m.get('local_id') or 0)
    if result.target_updates:
        t.update(result.target_updates)
    if result.reply_text and not dry_run:
        send_reply(
            result.reply_text,
            cfg.get('send_mode'),
            target=t,
            before_local_id=lid,
            cfg=cfg,
            config_path=config_path,
            mention_name=None,
        )
    _record_event(
        'admin_command',
        target=t.get('name'),
        sender=sender,
        payload={'local_id': lid, 'action': result.action, 'log_reason': result.log_reason},
    )
    _advance_cursor(t, lid, 'admin_command')
    if result.save_config:
        save_config(cfg, config_path)
        _audit_cursor_coverage([t], consume=True)
    return True


# --- Durable ingress helper for the thin-monitor / reliable-pipeline path ---
#
# Stage 1: this helper owns ingress only.  It does NOT run reply_engine,
# send_reply, reply_decision, message_aggregator, or any KB retrieval.
# All decisions are durable: every state transition survives a crash/restart.

# Test-friendly alias: tests patch ``monitor.pipeline.persist_inbound_event``.
pipeline = _pipeline



def _is_allowed_pipeline_sender(t, m, cfg):
    """Return True when the event sender is allowed for this reliable target.

    The allowlist is active only when ``cfg['reliable_pipeline']['test_target_only']``
    is True AND the target has a non-empty ``reliable_pipeline_allowed_senders``
    list.  An empty/missing allowlist means no restriction so the normal pipeline
    is never accidentally frozen.  Sender resolution matches
    ``_resolve_pipeline_identifiers`` (sender_username → real_sender_id → sender).
    Matching is case-insensitive to tolerate wxid casing drift.
    """
    if not isinstance(cfg, dict) or not isinstance(t, dict):
        return True
    rp = cfg.get('reliable_pipeline') or {}
    if rp.get('test_target_only') is not True:
        return True
    allowed = t.get('reliable_pipeline_allowed_senders')
    if not isinstance(allowed, list) or not allowed:
        return True
    if not isinstance(m, dict):
        return False
    sender_id = (
        str(m.get('sender_username') or '').strip()
        or str(m.get('real_sender_id') or '').strip()
        or str(m.get('sender') or '').strip()
    )
    if not sender_id:
        return False
    allowed_set = set(str(x).strip().casefold() for x in allowed if str(x).strip())
    return sender_id.strip().casefold() in allowed_set



def _resolve_pipeline_identifiers(t, m):
    """Derive the (event_id, target_id, group_key, sender_id, local_id) tuple.

    Returns None when any required field is missing; the caller MUST refuse to
    advance the cursor in that case.  The sender chain mirrors
    ``_session_key`` so a single sender resolves to the same identity in both
    the legacy aggregator and the new durable path.
    """
    if not isinstance(m, dict) or not isinstance(t, dict):
        return None
    local_id = int(m.get('local_id') or 0)
    if local_id <= 0:
        return None
    target_username = str(t.get('username') or '').strip()
    if not target_username:
        return None
    sender_id = (
        str(m.get('sender_username') or '').strip()
        or str(m.get('real_sender_id') or '').strip()
        or str(m.get('sender') or '').strip()
    )
    if not sender_id:
        return None
    event_id = _pipeline.source_event_id(t, m)
    if not event_id:
        return None
    return {
        'event_id': event_id,
        'target_id': target_username,
        'group_key': target_username,
        'sender_id': sender_id,
        'local_id': local_id,
    }


def build_event_payload(t, m, *, cfg=None, config_path=None, event_context=None, target_policy=None):
    """Build the durable payload for an inbound event.

    Includes enough message / target / event-context context for downstream
    workers to produce a reply later, but deliberately performs NO KB
    pre-fetching, NO reply-decision, and NO LLM call.  KB retrieval is owned
    exclusively by the worker, not the ingress path.
    """
    allowed_kb_ids = resolve_target_kb_ids(cfg, t) if cfg and t else []
    payload = {
        'schema_version': _pipeline.SCHEMA_VERSION,
        'message': {
            'local_id': int((m or {}).get('local_id') or 0),
            'real_sender_id': (m or {}).get('real_sender_id'),
            'sender_username': (m or {}).get('sender_username') or '',
            'sender_display_name': (m or {}).get('sender_display_name') or '',
            'mention_name': (m or {}).get('mention_name') or '',
            'message_content': (m or {}).get('message_content') or '',
            'local_type': int((m or {}).get('local_type') or 0),
            'create_time': (m or {}).get('create_time') or 0,
            'image_path': (m or {}).get('image_path') or '',
            'session_image_paths': list((m or {}).get('session_image_paths') or []),
        },
        'target': {
            'name': (t or {}).get('name') or '',
            'username': (t or {}).get('username') or '',
            'db': (t or {}).get('db') or '',
            'table': (t or {}).get('table') or '',
            'dedicated_agent_instance_id': (t or {}).get('dedicated_agent_instance_id') or '',
        },
        'event_context': dict(event_context) if event_context else {},
        'target_policy': dict(target_policy) if target_policy else {},
        # Internal authorization context. The worker strips these before
        # serializing the job into the model prompt.
        '_config_path': str(config_path or CONFIG_PATH),
        '_allowed_kb_ids': list(allowed_kb_ids),
    }
    return payload


def _event_already_in_open_window(event_id, target_id, sender_id, db_path):
    """Check whether ``event_id`` is already a member of an open turn window.

    Membership is defined by the persisted event row's integer ``id`` falling
    within the open window's ``[first_event_id, last_event_id]`` range for the
    same ``(target_id, sender_id)`` tuple.  This guards ``add_event_to_window``
    against being called on a duplicate, which would otherwise mutate
    ``due_at`` on the open window and postpone materialization indefinitely.
    """
    if _pipeline is None or not event_id or not target_id or not sender_id:
        return False
    try:
        with _pipeline._connect(db_path) as con:  # type: ignore[attr-defined]
            row = con.execute(
                "SELECT id FROM inbound_events WHERE source_event_id=?",
                (event_id,),
            ).fetchone()
            if not row:
                return False
            event_row_id = int(row["id"])
            window = con.execute(
                """SELECT 1 FROM turn_windows
                   WHERE target_id=? AND sender_id=? AND status='open'
                     AND ? BETWEEN first_event_id AND last_event_id
                   LIMIT 1""",
                (target_id, sender_id, event_row_id),
            ).fetchone()
            return window is not None
    except Exception:
        # On any DB error we conservatively answer False so the caller
        # still attempts add_event_to_window (idempotent at the row level).
        return False


def _reliable_pipeline_globally_enabled(cfg):
    """Strict-is-True enabled check matching control_api._reliable_pipeline_enabled.

    Plain truthiness lets the monitor persist jobs for non-bool config values
    (e.g. ``1``, ``"yes"``) the scheduler rejects. Keep these in lock-step.
    """
    section = (cfg or {}).get('reliable_pipeline') or {}
    return section.get('enabled') is True


def durable_ingress_event(t, m, *, cfg=None, config_path=None, db_path, now=None, event_context=None,
                          target_policy=None):
    """Persist + window-attach + per-cycle drain for one inbound event.

    Ordering is the contract:
      1. ``persist_inbound_event`` (durable). Cursor advances iff this returns.
      2. ``add_event_to_window`` only when the event is not already a member
         of its open window — otherwise we'd mutate ``due_at`` on replay.
      3. ``close_due_windows`` then ``create_jobs_for_ready_turns``.

    Returns a dict the caller can inspect to decide whether to advance its
    ``last_local_id`` cursor.  Exceptions are caught and surfaced as
    ``{'advanced': False, 'error': '...'}`` so a transient storage failure
    cannot corrupt the monitor cursor.
    """
    if not _reliable_pipeline_globally_enabled(cfg):
        return {'advanced': False, 'persisted': False, 'inserted': False,
                'window_attached': False, 'local_id': 0, 'event_id': '',
                'closed_turns': 0, 'created_jobs': 0,
                'error': 'reliable_pipeline globally disabled'}
    # Per-target opt-in remains the Stage-4 rollback switch.  Full cutover
    # deleted the dual-path gate function; this field is the only way to take
    # one target off durable ingress without disabling the global pipeline.
    if not (isinstance(t, dict) and t.get('reliable_pipeline_target') is True):
        return {'advanced': False, 'persisted': False, 'inserted': False,
                'window_attached': False, 'local_id': 0, 'event_id': '',
                'closed_turns': 0, 'created_jobs': 0,
                'error': 'target not opted into reliable pipeline'}


    result = {
        'advanced': False,
        'persisted': False,
        'inserted': False,
        'window_attached': False,
        'local_id': 0,
        'event_id': '',
        'closed_turns': 0,
        'created_jobs': 0,
        'error': '',
    }
    if _pipeline is None:
        result['error'] = 'reliable_pipeline module unavailable'
        return result
    ident = _resolve_pipeline_identifiers(t, m)
    if not ident:
        result['error'] = 'missing required identifier (sender or local_id)'
        return result
    result['event_id'] = ident['event_id']
    result['local_id'] = ident['local_id']

    payload = build_event_payload(
        t, m, cfg=cfg, config_path=config_path, event_context=event_context, target_policy=target_policy,
    )
    received_at = float(m.get('create_time') or 0) or None
    try:
        event_row, inserted = _pipeline.persist_inbound_event(
            event_id=ident['event_id'],
            target_id=ident['target_id'],
            group_key=ident['group_key'],
            sender_id=ident['sender_id'],
            local_id=ident['local_id'],
            payload=payload,
            received_at=received_at,
            db_path=db_path,
        )
    except Exception as e:
        result['error'] = 'persist_inbound_event failed: %r' % (e,)
        log('reliable_pipeline persist failed target=%s local_id=%s error=%r' % (
            (t or {}).get('name'), ident['local_id'], e))
        return result
    result['persisted'] = True
    result['inserted'] = bool(inserted)

    if not inserted:
        # Duplicate replay: the event is already durable. Three cases:
        #   * status == INBOUND_TURNED  → already materialized into a turn;
        #                                 window-attach would reopen a window
        #                                 around a turned event. No-op.
        #   * status == INBOUND_PENDING AND already a member of its open
        #                                 window → pure replay; window-attach
        #                                 would mutate due_at. No-op.
        #   * status == INBOUND_PENDING AND NOT a member → crash between
        #                                 persist and window-attach; we must
        #                                 attach now so the event isn't stranded.
        event_status = str(event_row.get('status') or _pipeline.INBOUND_PENDING)
        if event_status == _pipeline.INBOUND_TURNED:
            result['window_attached'] = False
            log('reliable_pipeline duplicate_turned_noop event=%s local_id=%s' % (
                ident['event_id'], ident['local_id']))
        elif _event_already_in_open_window(
            ident['event_id'], ident['target_id'], ident['sender_id'], db_path,
        ):
            result['window_attached'] = False
            log('reliable_pipeline duplicate_member_noop event=%s local_id=%s' % (
                ident['event_id'], ident['local_id']))
        else:
            try:
                _pipeline.add_event_to_window(
                    ident['event_id'], db_path=db_path, now=now,
                )
                result['window_attached'] = True
            except Exception as e:
                result['error'] = 'add_event_to_window failed: %r' % (e,)
                log('reliable_pipeline window-attach failed event=%s error=%r' % (
                    ident['event_id'], e))
                return result
    else:
        # Fresh event: open or extend its window.
        try:
            _pipeline.add_event_to_window(
                ident['event_id'], db_path=db_path, now=now,
            )
            result['window_attached'] = True
        except Exception as e:
            # Persisted but window-attach failed: caller must retry. We refuse
            # to advance so the next poll replays this event; the membership
            # guard above then re-attaches without disturbing due_at.
            result['error'] = 'add_event_to_window failed: %r' % (e,)
            log('reliable_pipeline window-attach failed event=%s error=%r' % (
                ident['event_id'], e))
            return result

    # Per-cycle drain: close any windows whose debounce has expired, then
    # materialize every ready turn into exactly one queued worker job.
    try:
        turns = _pipeline.close_due_windows(now=now, db_path=db_path)
        jobs = _pipeline.create_jobs_for_ready_turns(db_path=db_path)
    except Exception as e:
        # Drain failure must NOT advance the cursor: the caller will retry
        # the entire ingress atomically. Advancing here would let a later
        # row in the same cycle cursor-past a failed one (see loop fence).
        result['error'] = 'drain failed: %r' % (e,)
        log('reliable_pipeline drain failed event=%s error=%r' % (
            ident['event_id'], e))
        return result
    result['closed_turns'] = len(turns)
    result['created_jobs'] = len(jobs)
    result['advanced'] = True
    return result


def drain_due_pipeline(*, cfg, db_path, now=None):
    """Per-cycle drain for restart-recovered open windows.

    Safe to call when no events have arrived this cycle; that is the point:
    after a crash an open window may have been left in the database with its
    debounce timer long expired, and this is the only place that closes it
    without a new message arriving.
    """
    if not _reliable_pipeline_globally_enabled(cfg):
        return {'materialized_jobs': 0, 'closed_windows': 0, 'error': 'globally disabled'}

    summary = {
        'closed_turns': 0,
        'created_jobs': 0,
        'error': '',
    }
    if _pipeline is None:
        summary['error'] = 'reliable_pipeline module unavailable'
        return summary
    try:
        turns = _pipeline.close_due_windows(now=now, db_path=db_path)
        jobs = _pipeline.create_jobs_for_ready_turns(db_path=db_path)
    except Exception as e:
        summary['error'] = 'drain failed: %r' % (e,)
        log('reliable_pipeline drain_due failed error=%r' % (e,))
        return summary
    summary['closed_turns'] = len(turns)
    summary['created_jobs'] = len(jobs)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=str(CONFIG_PATH))
    ap.add_argument('--interval', type=float, default=None)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-save-state', action='store_true', help='do not write last_local_id back to config')
    ap.add_argument('--sync-on-start', action='store_true', help='run decrypt once before baseline; heavier but refreshes DB')
    ap.add_argument('--sync-each-cycle', action='store_true', help='HEAVY: run decrypt every poll cycle; not recommended')
    ap.add_argument('--no-fast-refresh', action='store_true', help='disable lightweight target refresh each cycle')
    ap.add_argument('--fast-refresh-force-start', action='store_true', help='force lightweight refresh before baseline')
    args = ap.parse_args()

    config_path = Path(args.config).resolve()
    cfg = load_config(config_path)
    _apply_decrypted_dirs(cfg)
    interval = float(args.interval if args.interval is not None else cfg.get('poll_interval', 3))
    targets = enabled_targets(cfg)
    for t in targets:
        _init_cursor_baseline(t)
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

    contact_names = load_contact_name_map()
    log('contact display names loaded=%d' % len(contact_names))
    runtime_min_last_local_id = _advance_startup_cursors(targets, cfg)
    if not args.no_save_state:
        save_config(cfg, config_path)
        _audit_cursor_coverage(targets, consume=True)

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
        contact_names = load_contact_name_map()
        targets = enabled_targets(cfg)
        advance_on_start = bool((cfg or {}).get('monitor', {}).get('advance_cursor_on_start', True))
        global _PREV_ENABLED_TARGET_KEYS
        current_enabled_keys = set()
        prev_enabled_keys = _PREV_ENABLED_TARGET_KEYS
        if prev_enabled_keys is None:
            # First loop after process start: startup advance already ran for
            # the initial set; seed the set so we do not double-snap them.
            prev_enabled_keys = {_target_key(t) for t in targets}
        for t in targets:
            tkey = _target_key(t)
            target_key = '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))
            current_enabled_keys.add(tkey)
            is_new = tkey not in prev_enabled_keys
            if tkey not in _CURSOR_AUDIT:
                _init_cursor_baseline(t)
            if advance_on_start and is_new:
                # Newly enabled (or first seen this process after being off):
                # skip backlog so resume does not storm the group.
                latest_id = latest_local_id_for_target(t)
                current_id = int(t.get('last_local_id') or 0)
                if latest_id > current_id:
                    log('hot-resume cursor advance target=%s from %s to %s (skip backlog while disabled)' % (
                        t.get('name'), current_id, latest_id))
                    _advance_cursor(t, latest_id, 'hot_resume_advance')
                    runtime_min_last_local_id[target_key] = latest_id
                else:
                    runtime_min_last_local_id[target_key] = max(
                        int(runtime_min_last_local_id.get(target_key, 0) or 0), current_id)
        _PREV_ENABLED_TARGET_KEYS = current_enabled_keys
        for t in targets:
            target_key = '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))
            file_last_id = int(t.get('last_local_id') or 0)
            runtime_last_id = int(runtime_min_last_local_id.get(target_key, 0) or 0)
            if file_last_id < runtime_last_id:
                log('warn config cursor regressed target=%s file_last_local_id=%s runtime_last_local_id=%s; using runtime value to avoid replay' % (
                    t.get('name'), file_last_id, runtime_last_id))
                _advance_cursor(t, runtime_last_id, 'runtime_min_sync')
            else:
                runtime_min_last_local_id[target_key] = file_last_id
        new_count = 0
        hit_count = 0
        # Per-cycle fence: once the durable path fails for a target in this
        # cycle, no later row for that target may advance the cursor past
        # the failed one — otherwise a later success could mask the failure
        # and replay the failed row out-of-order on the next cycle.
        _rl_failed_targets = set()
        _rl_db_path = Path(((cfg.get('reliable_pipeline') or {}).get('db_path')
                           or str(ROOT / 'temp' / 'reliable_pipeline.sqlite')))
        for db_name, db_targets in group_targets_by_db(targets).items():
            for t, m in fetch_new_for_db(db_name, db_targets):
                enrich_sender_display_name(m, contact_names)
                new_count += 1
                lid = int(m.get('local_id') or 0)
                _rl_target_key = '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))
                fenced = _rl_target_key in _rl_failed_targets
                if fenced:
                    log('reliable_pipeline fenced target=%s local_id=%s reason=cycle_failure' % (
                        t.get('name'), lid))
                    continue
                if _try_handle_admin_command(m, t, cfg, config_path,
                                            dry_run=args.dry_run, contact_names=contact_names):
                    continue
                # --- per-message reliable_pipeline.enabled guard (fail-closed) ---
                # When the scheduler is disabled, skip + no-advance + no outbound.
                if not _reliable_pipeline_globally_enabled(cfg):
                    log('reliable_pipeline globally disabled target=%s local_id=%s; skip + no-advance' % (
                        t.get('name'), lid))
                    _record_event(
                        'reliable_pipeline_globally_disabled',
                        target=t.get('name'),
                        sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
                        payload={'local_id': lid},
                    )
                    continue
                # Image message: decrypt and attach image_path for durable worker.
                if int(m.get('local_type') or 0) == 3:
                    _decrypt_image_for_message(m, t, cfg)
                if int(m.get('status') or 0) == 2:
                    log('self_sent_skip target=%s local_id=%s' % (t.get('name'), lid))
                    _record_event('self_sent_skip', target=t.get('name'),
                                  sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
                                  payload={'local_id': lid})
                    _advance_cursor(t, lid, 'self_sent_skip')
                    continue
                sender = m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or ''
                if t.get('admin_muted'):
                    log('target_muted target=%s local_id=%s' % (t.get('name'), lid))
                    _record_event('target_muted_skip', target=t.get('name'), sender=sender, payload={'local_id': lid})
                    _advance_cursor(t, lid, 'target_muted_skip')
                    continue
                if not _is_allowed_pipeline_sender(t, m, cfg):
                    log('reliable_pipeline sender_not_allowed target=%s local_id=%s sender=%s' % (
                        t.get('name'), lid, sender))
                    _record_event('reliable_pipeline_sender_not_allowed', target=t.get('name'),
                                      sender=sender,
                                      payload={'local_id': lid})
                    _advance_cursor(t, lid, 'reliable_pipeline_sender_not_allowed')
                    continue
                # --- precheck boundary (local deterministic safety; high-risk routed immediately) ---
                boundary = precheck(str(m.get('message_content') or ''))
                if boundary and getattr(boundary, 'risk_level', '') == 'high':
                    text = _ensure_sender_mention(postcheck(boundary.reply_text), m, contact_names)
                    if text and not args.dry_run:
                        ok = send_reply(text, cfg.get('send_mode'), target=t,
                                       before_local_id=lid, cfg=cfg, config_path=config_path)
                        _record_event('send_ok' if ok else 'send_failed',
                                      target=t.get('name'), sender=sender,
                                      payload={'local_id': lid, 'reason': getattr(boundary, 'reason', ''),
                                               'precheck_boundary': True})
                    _advance_cursor(t, lid, 'precheck_boundary')
                    continue
                # --- trigger / free / active-session gate (Stage-4 cutover residual) ---
                # free: all messages enter durable. trigger: keyword or active session only.
                # Empty triggers + trigger mode: silent (cursor advances, no Hermes job).
                _enter, _trigger_hit, _session_active = should_enter_durable(cfg, t, m)
                if not _enter:
                    log('trigger_skip target=%s local_id=%s sender=%s mode=%s' % (
                        t.get('name'), lid, sender,
                        (t.get('response_mode') or cfg.get('default_response_mode') or 'trigger')))
                    _record_event('trigger_skip', target=t.get('name'), sender=sender,
                                  payload={'local_id': lid, 'session_active': False})
                    _advance_cursor(t, lid, 'trigger_skip')
                    continue
                if _trigger_hit and not _session_active:
                    _activate_session(t, m, cfg)
                # --- durable ingress (single path; survives monitor restarts) ---
                _rl_event_context = {
                    'mode': 'durable_ingress',
                    'local_id': lid,
                    'sender': sender,
                    'session_active': bool(_session_active or _trigger_hit),
                    'trigger_hit': _trigger_hit,
                }
                _rl_result = durable_ingress_event(
                    t, m, cfg=cfg, config_path=config_path, db_path=_rl_db_path,
                    now=time.time(), event_context=_rl_event_context,
                    target_policy=resolve_target_policy(cfg, t),
                )
                if not _rl_result or not _rl_result.get('advanced'):
                    err = (_rl_result or {}).get('error', '')
                    if err == 'target not opted into reliable pipeline':
                        # Target rolled back off durable: no legacy path remains
                        # after Stage 4. Advance so the cursor does not freeze,
                        # but do not fence the cycle (other rows may still run).
                        log('reliable_pipeline target_opted_out target=%s local_id=%s; skip + advance' % (
                            t.get('name'), lid))
                        _record_event(
                            'reliable_pipeline_target_opted_out',
                            target=t.get('name'), sender=sender,
                            payload={'local_id': lid},
                        )
                        _advance_cursor(t, lid, 'reliable_pipeline_target_opted_out')
                    else:
                        _rl_failed_targets.add(_rl_target_key)
                        log('reliable_pipeline ingress_failure target=%s local_id=%s error=%s' % (
                            t.get('name'), lid, err))
                else:
                    _advance_cursor(t, lid, 'durable_ingress')

        # Reliable pipeline per-cycle drain
        if _reliable_pipeline_globally_enabled(cfg):
            drain_due_pipeline(cfg=cfg, db_path=_rl_db_path, now=time.time())
        if new_count and not args.no_save_state:
            save_config(cfg, config_path)
            _audit_cursor_coverage(targets, consume=True)
        else:
            _audit_cursor_coverage(targets, consume=False)
        mode = 'sync-each-cycle' if args.sync_each_cycle else ('read-only' if args.no_fast_refresh else 'fast-refresh')
        log('cycle done mode=%s sync=%.3fs total=%.3fs targets=%d new=%d hits=%d refresh_changed=%d refresh_failed=%d' % (
            mode, sync_dt, time.time() - cycle_t0, len(targets), new_count, hit_count, refresh_changed, refresh_failed))
        if args.once:
            break
        time.sleep(max(0.0, interval - (time.time() - cycle_t0)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
