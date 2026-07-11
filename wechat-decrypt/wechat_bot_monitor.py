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

try:
    import zstandard as _zstd
    _ZSTD_DCTX = _zstd.ZstdDecompressor()
except Exception:  # pragma: no cover
    _zstd = None
    _ZSTD_DCTX = None

from reply_engine import DEFAULT_TRIGGERS, generate_reply, precheck, postcheck, _thin_monitor_enabled
from reply_decision import decide as reply_decision_decide
from wechat_sender import send_reply_detailed
from message_aggregator import (
    ingest_event,
    event_from_monitor_message,
    flush_all_pending,
    flush_due,
    has_open_window,
    AggregatedTurn,
)
import admin_commands as _admin_commands
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


def _build_thin_monitor_aggregated_message(t, m, cfg):
    """Build an aggregated message + event_context for the thin-monitor path."""
    event = event_from_monitor_message(m, t)
    policy = resolve_target_policy(cfg, t)
    ctx_policy = policy.get('context_policy') or {}
    context_limit = int(ctx_policy.get('max_messages') or cfg.get('context_limit') or 30)
    ctx, event_context = build_event_context(t, m, cfg, limit=context_limit)
    event_context['target_policy'] = policy
    event_context['trigger_matched'] = False
    event_context['in_session'] = False
    event_context['sender'] = m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or ''
    event_context['sender_username'] = m.get('sender_username') or ''
    event_context['sender_display_name'] = m.get('sender_display_name') or ''
    event_context['mention_name'] = m.get('mention_name') or m.get('sender_display_name') or ''
    # Thin-monitor does not manage sessions; keep the event_context schema
    # compatible with downstream consumers while reporting a fixed idle state.
    event_context['session_active'] = False
    event_context['session_state'] = 'idle'
    event_context['session_turns'] = 0

    turn = ingest_event(
        event,
        trigger_matched=False,
        in_session=False,
        event_context=event_context,
        target=t,
        config=cfg,
    )
    if turn is None:
        return None, event_context, None

    agg_msg = turn.to_generate_reply_message()
    agg_msg['target_policy'] = policy
    agg_msg['event_context'] = event_context
    return agg_msg, event_context, turn


def _advance_startup_cursors(targets, cfg):
    """Advance target cursors to the current DB max on startup.

    Mutates targets in place and returns a dict mapping target_key -> last_local_id.
    Controlled by ``monitor.advance_cursor_on_start`` (default True).
    """
    advance = bool((cfg or {}).get('monitor', {}).get('advance_cursor_on_start', True))
    runtime_min = {}
    for t in targets:
        latest = fetch_latest_for_target(t)
        latest_id = int(latest.get('local_id') or 0) if latest else 0
        current_id = int(t.get('last_local_id') or 0)
        if advance and latest_id > current_id:
            log('startup cursor advance target=%s from %s to %s (skip history while stopped)' % (t.get('name'), current_id, latest_id))
            t['last_local_id'] = latest_id
        target_key = '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))
        runtime_min[target_key] = int(t.get('last_local_id') or 0)
        log('baseline target=%s db=%s table=%s last_local_id=%s latest=%s' % (
            t.get('name'), t.get('db'), t.get('table'), t.get('last_local_id'), json.dumps(latest, ensure_ascii=False)))
    return runtime_min


def _handle_thin_monitor_target(t, m, cfg, *, config_path, args, contact_names, lid):
    """Thin-monitor target: decrypt image, aggregate, enqueue to agent, send ack.

    The monitor does not run trigger matching, session management, or the local
    reply decision layer.  Everything except listen/send is delegated to the
    agent side.
    """
    _decrypt_image_for_message(m, t, cfg)
    log('new msg thin target=%s local_id=%s content=%r image=%s' % (
        t.get('name'), lid, m.get('message_content'), m.get('image_path', '')))

    agg_msg, event_context, turn = _build_thin_monitor_aggregated_message(t, m, cfg)
    if turn is None:
        _record_event(
            'thin_monitor_buffered',
            target=t.get('name'),
            sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
            payload={'local_id': lid},
        )
        return lid

    # High-risk requests still get an immediate safety boundary reply locally.
    boundary = precheck(str(m.get('message_content') or ''))
    if boundary and boundary.risk_level == 'high':
        text = _ensure_sender_mention(postcheck(boundary.reply_text), m, contact_names)
        if text and not args.dry_run:
            ok = send_reply(text, cfg.get('send_mode'), target=t, before_local_id=lid, cfg=cfg, config_path=config_path)
            log('thin_pre_boundary_immediate target=%s local_id=%s ok=%s reason=%s' % (
                t.get('name'), lid, ok, boundary.reason))
            _record_event(
                'send_ok' if ok else 'send_failed',
                target=t.get('name'),
                sender=event_context.get('sender'),
                payload={'local_id': lid, 'reason': boundary.reason, 'immediate_boundary': True, 'thin_monitor': True},
            )
        return turn.end_local_id

    gen_t0 = time.time()
    try:
        decision = generate_reply(agg_msg, t, cfg)
    except Exception as exc:
        log('thin_generate_reply_error target=%s local_id=%s error=%r' % (t.get('name'), turn.end_local_id, exc))
        _record_event(
            'thin_monitor_generate_error',
            target=t.get('name'),
            sender=event_context.get('sender'),
            payload={'local_id': turn.end_local_id, 'error': repr(exc)},
        )
        return turn.end_local_id

    gen_dt = time.time() - gen_t0
    text = decision.reply_text
    ctx_len = len(agg_msg.get('context_messages') or [])
    log('thin_monitor_enqueued target=%s local_id=%s ctx=%d gen=%.3fs intent=%s risk=%s need_human=%s reason=%s reply=%r' % (
        t.get('name'), turn.end_local_id, ctx_len, gen_dt, decision.intent, decision.risk_level,
        decision.need_human, decision.reason, text))
    _record_event(
        'thin_monitor_enqueued',
        target=t.get('name'),
        sender=event_context.get('sender'),
        payload={
            'local_id': turn.end_local_id,
            'intent': decision.intent,
            'risk_level': decision.risk_level,
            'reason': decision.reason,
            'has_reply_text': bool(text),
        },
    )
    if decision.should_reply and text and not args.dry_run:
        # For thin-monitor the agent_worker sends the final reply.  The monitor
        # only sends an immediate ack if one was generated (e.g., customer_service mode).
        ok = send_reply(text, cfg.get('send_mode'), target=t, before_local_id=turn.end_local_id, cfg=cfg,
                        config_path=config_path, mention_name=event_context.get('mention_name') or event_context.get('sender_display_name') or event_context.get('sender'))
        log('thin_monitor_ack_sent target=%s local_id=%s ok=%s' % (t.get('name'), turn.end_local_id, ok))
        _record_event(
            'send_ok' if ok else 'send_failed',
            target=t.get('name'),
            sender=event_context.get('sender'),
            payload={'local_id': turn.end_local_id, 'thin_monitor': True, 'ack': True},
        )
    return turn.end_local_id


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
    if result.save_config:
        save_config(cfg, config_path)
    t['last_local_id'] = max(int(t.get('last_local_id') or 0), lid)
    return True


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
    ap.add_argument('--no-fast-refresh', action='store_true', help='disable lightweight target refresh each cycle')
    ap.add_argument('--fast-refresh-force-start', action='store_true', help='force lightweight refresh before baseline')
    args = ap.parse_args()

    config_path = Path(args.config).resolve()
    cfg = load_config(config_path)
    _apply_decrypted_dirs(cfg)
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

    contact_names = load_contact_name_map()
    log('contact display names loaded=%d' % len(contact_names))
    runtime_min_last_local_id = _advance_startup_cursors(targets, cfg)
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
        contact_names = load_contact_name_map()
        targets = enabled_targets(cfg)
        for t in targets:
            target_key = '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))
            file_last_id = int(t.get('last_local_id') or 0)
            runtime_last_id = int(runtime_min_last_local_id.get(target_key, 0) or 0)
            if file_last_id < runtime_last_id:
                log('warn config cursor regressed target=%s file_last_local_id=%s runtime_last_local_id=%s; using runtime value to avoid replay' % (
                    t.get('name'), file_last_id, runtime_last_id))
                t['last_local_id'] = runtime_last_id
            else:
                runtime_min_last_local_id[target_key] = file_last_id
        new_count = 0
        hit_count = 0
        for db_name, db_targets in group_targets_by_db(targets).items():
            for t, m in fetch_new_for_db(db_name, db_targets):
                enrich_sender_display_name(m, contact_names)
                new_count += 1
                lid = int(m.get('local_id') or 0)
                advance_state = True
                # Image message: decrypt and attach image_path for vision processing in reply_engine.
                # Thin-monitor targets do not run local vision/pending-image logic; the agent
                # handles images via its own multimodal provider or the decode_image MCP tool.
                if not _thin_monitor_enabled(cfg, t) and int(m.get('local_type') or 0) == 3:
                    try:
                        from image_handler import process_image_message
                        img_path = process_image_message(
                            m, t, cfg,
                            packed_info_data_bytes=m.get('packed_info_data'),
                        )
                        if img_path:
                            m['image_path'] = img_path
                            log('image decrypted target=%s local_id=%s path=%s' % (t.get('name'), lid, img_path))
                            # Store image path for this sender so a follow-up text query can reference it
                            key = _session_key(t, m)
                            _pending_images.setdefault(key, []).append({'path': img_path, 'time': time.time()})
                            # Keep only the most recent 5 images per sender to avoid unbounded growth
                            _pending_images[key] = _pending_images[key][-5:]
                            log('pending_images stored key=%s sender=%s count=%d' % (key, m.get('real_sender_id') or m.get('sender') or '', len(_pending_images[key])))
                        else:
                            log('image decrypt failed target=%s local_id=%s' % (t.get('name'), lid))
                    except Exception as e:
                        log('image decrypt exception target=%s local_id=%s error=%r' % (t.get('name'), lid, e))
                log('new msg target=%s local_id=%s content=%r image=%s' % (t.get('name'), lid, m.get('message_content'), m.get('image_path', '')))
                if _try_handle_admin_command(m, t, cfg, config_path, dry_run=args.dry_run, contact_names=contact_names):
                    continue
                # Skip messages sent by the bot itself to prevent self-reply loops.
                if int(m.get('status') or 0) == 2:
                    log('self_sent_skip target=%s local_id=%s' % (t.get('name'), lid))
                    if advance_state:
                        t['last_local_id'] = max(int(t.get('last_local_id') or 0), lid)
                    continue
                sender = m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or ''
                if t.get('admin_muted'):
                    log('target_muted target=%s local_id=%s' % (t.get('name'), lid))
                    _record_event('target_muted_skip', target=t.get('name'), sender=sender, payload={'local_id': lid})
                    t['last_local_id'] = max(int(t.get('last_local_id') or 0), lid)
                    continue
                # Thin-monitor path: delegate trigger/session/decision/reply to the agent.
                if _thin_monitor_enabled(cfg, t):
                    new_lid = _handle_thin_monitor_target(
                        t, m, cfg,
                        config_path=config_path,
                        args=args,
                        contact_names=contact_names,
                        lid=lid,
                    )
                    if advance_state:
                        t['last_local_id'] = max(int(t.get('last_local_id') or 0), new_lid)
                    continue
                # --- session-based trigger handling ---
                _clear_expired_sessions(cfg)
                triggered = bool(is_trigger(cfg, t, m))
                in_session = False
                if triggered:
                    _activate_session(t, m, cfg)
                    _record_event(
                        'trigger_matched',
                        target=t.get('name'),
                        sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
                        payload={'local_id': lid, 'reason': 'trigger'},
                    )
                elif _is_in_session(t, m, cfg):
                    log('session_trigger target=%s local_id=%s' % (t.get('name'), lid))
                    in_session = True
                    _record_event(
                        'trigger_matched',
                        target=t.get('name'),
                        sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
                        payload={'local_id': lid, 'reason': 'session'},
                    )
                should_process = triggered or in_session
                if not should_process:
                    current_event = event_from_monitor_message(m, t)
                    if has_open_window(current_event.chat_id, current_event.sender_id):
                        log('aggregator_open_window_append target=%s local_id=%s chat=%s sender=%s' % (
                            t.get('name'), lid, current_event.chat_id, current_event.sender_id))
                        should_process = True
                        in_session = True
                if not should_process:
                    key = _session_key(t, m)
                    text_for_pending = str(m.get('message_content') or '')
                    triggers_for_pending = t.get('triggers')
                    if triggers_for_pending is None:
                        triggers_for_pending = cfg.get('default_triggers', [])
                    pending_trigger = any((trig and trig in text_for_pending) for trig in triggers_for_pending)
                    if pending_trigger and _pending_images.get(key):
                        log('pending_image_trigger_fallback target=%s local_id=%s key=%s' % (t.get('name'), lid, key))
                        triggered = True
                        should_process = True
                if should_process:
                    key = _session_key(t, m)
                    pending = _pending_images.pop(key, None)
                    log('pending_images lookup key=%s sender=%s found=%s' % (key, m.get('real_sender_id') or m.get('sender') or '', bool(pending)))
                    if pending:
                        policy_for_pending = resolve_target_policy(cfg, t)
                        image_policy = policy_for_pending.get('image_policy') or {}
                        window = float(image_policy.get('bind_window_seconds') or cfg.get('session_window') or SESSION_WINDOW_DEFAULT)
                        now = time.time()
                        fresh_paths = [item['path'] for item in pending if now - item.get('time', 0) <= window]
                        log('pending_images fresh_paths=%d total=%d window=%.0fs' % (len(fresh_paths), len(pending), window))
                        if fresh_paths:
                            m['session_image_paths'] = fresh_paths
                event = event_from_monitor_message(m, t)
                # Build minimal event_context for the aggregated turn
                policy = resolve_target_policy(cfg, t)
                ctx_policy = policy.get('context_policy') or {}
                context_limit = int(ctx_policy.get('max_messages') or cfg.get('context_limit') or 30)
                ctx, event_context = build_event_context(t, m, cfg, limit=context_limit)
                event_context['target_policy'] = policy
                event_context['trigger_matched'] = bool(triggered)
                event_context['in_session'] = bool(in_session)
                event_context['sender'] = m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or ''
                event_context['sender_username'] = m.get('sender_username') or ''
                event_context['sender_display_name'] = m.get('sender_display_name') or ''
                event_context['mention_name'] = m.get('mention_name') or m.get('sender_display_name') or ''
                sess_entry = _active_sessions.get(_session_key(t, m)) or {}
                event_context['session_active'] = bool(sess_entry)
                event_context['session_state'] = 'active' if sess_entry else 'idle'
                event_context['session_turns'] = int(sess_entry.get('turns') or 0)

                turn = ingest_event(
                    event,
                    trigger_matched=triggered,
                    in_session=in_session,
                    event_context=event_context,
                    target=t,
                    config=cfg,
                )

                if turn is None:
                    _record_event(
                        'aggregator_buffered',
                        target=t.get('name'),
                        sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
                        payload={'local_id': lid},
                    )
                    if advance_state:
                        t['last_local_id'] = max(int(t.get('last_local_id') or 0), lid)
                    continue

                if not should_process:
                    _record_event(
                        'aggregator_dropped',
                        target=t.get('name'),
                        sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
                        payload={'local_id': lid},
                    )
                    if advance_state:
                        t['last_local_id'] = max(int(t.get('last_local_id') or 0), turn.end_local_id)
                    continue

                # High-risk requests should get an immediate safety boundary reply.
                # This avoids losing the response if a debounce window is interrupted.
                boundary = precheck(str(m.get('message_content') or ''))
                if boundary and boundary.risk_level == 'high':
                    text = _ensure_sender_mention(postcheck(boundary.reply_text), m, contact_names)
                    if text and not args.dry_run:
                        ok = send_reply(text, cfg.get('send_mode'), target=t, before_local_id=lid, cfg=cfg, config_path=config_path)
                        log('pre_boundary_immediate target=%s local_id=%s ok=%s reason=%s' % (
                            t.get('name'), lid, ok, boundary.reason))
                        _record_event(
                            'send_ok' if ok else 'send_failed',
                            target=t.get('name'),
                            sender=m.get('sender_username') or m.get('real_sender_id') or m.get('sender') or '',
                            payload={'local_id': lid, 'reason': boundary.reason, 'immediate_boundary': True},
                        )
                    if advance_state:
                        t['last_local_id'] = max(int(t.get('last_local_id') or 0), lid)
                    continue

                # Window closed – we have an aggregated turn.  Proceed to reply.
                hit_count += 1

                # Image-only without task description: guide the user instead of
                # dispatching an agent job that has nothing to analyze.
                # Exception: if the user explicitly triggered the bot or is in an
                # active session, let the image through so reply_engine can run VLM
                # and KB retrieval on it.
                if turn.image_paths and not turn.has_image_task_description() and not (turn.trigger_matched or turn.in_session):
                    _send_image_task_missing_guide(turn, cfg, config_path=config_path, dry_run=args.dry_run)
                    if advance_state:
                        t['last_local_id'] = max(int(t.get('last_local_id') or 0), turn.end_local_id)
                    continue

                agg_msg = turn.to_generate_reply_message()
                # Keep the aggregated window's own context_messages; the flat
                # historical rows from build_event_context live in event_context.
                # Carry forward the original message's metadata for reply_engine
                agg_msg['target_policy'] = policy
                agg_msg['event_context'] = event_context

                # Trigger-only reply decision layer (on the *aggregated* message)
                decision_plan = reply_decision_decide(t, agg_msg, event_context)
                log('decision target=%s local_id=%s should_trigger=%s reason=%s mode=%s risk=%s confidence=%.2f' % (
                    t.get('name'), turn.end_local_id, decision_plan.should_reply, decision_plan.reason,
                    decision_plan.reply_mode, decision_plan.risk_level, decision_plan.confidence))
                _record_event(
                    'decision',
                    target=t.get('name'),
                    sender=event_context.get('sender'),
                    payload={
                        'local_id': turn.end_local_id,
                        'should_reply': decision_plan.should_reply,
                        'reason': decision_plan.reason,
                        'reply_mode': decision_plan.reply_mode,
                        'risk_level': decision_plan.risk_level,
                        'confidence': decision_plan.confidence,
                    },
                )
                if decision_plan.reply_mode == 'handoff' or decision_plan.risk_level == 'high':
                    _record_event(
                        'risk_detected',
                        target=t.get('name'),
                        sender=event_context.get('sender'),
                        payload={'local_id': turn.end_local_id, 'reason': decision_plan.reason},
                    )
                if not decision_plan.should_reply:
                    _maybe_expire_session_on_close_cue(t, agg_msg, decision_plan, event_context)
                    if advance_state:
                        t['last_local_id'] = max(int(t.get('last_local_id') or 0), turn.end_local_id)
                    continue

                gen_t0 = time.time()
                try:
                    decision = generate_reply(agg_msg, t, cfg)
                    gen_dt = time.time() - gen_t0
                    text = decision.reply_text
                    ctx_len = len(agg_msg.get('context_messages') or [])
                    log('trigger hit target=%s local_id=%s ctx=%d gen=%.3fs intent=%s risk=%s need_human=%s reason=%s reply=%r' % (
                        t.get('name'), turn.end_local_id, ctx_len, gen_dt, decision.intent, decision.risk_level, decision.need_human, decision.reason, text))
                    try:
                        detail = decision.to_dict()
                        detail['target'] = t.get('name')
                        detail['local_id'] = turn.end_local_id
                        detail['context_count'] = ctx_len
                        detail['generate_seconds'] = round(gen_dt, 3)
                        detail['reply_preview'] = (text or '')[:200]
                        log('decision_detail %s' % json.dumps(detail, ensure_ascii=False, default=str))
                    except Exception as e:
                        log('decision_detail_error target=%s local_id=%s error=%r' % (t.get('name'), turn.end_local_id, e))
                    if not decision.should_reply or not text:
                        log('decision: skip send')
                        _record_event(
                            'reply_skipped',
                            target=t.get('name'),
                            sender=event_context.get('sender'),
                            payload={
                                'local_id': turn.end_local_id,
                                'reason': decision.reason,
                                'intent': decision.intent,
                                'risk_level': decision.risk_level,
                            },
                        )
                    elif args.dry_run:
                        log('dry-run: skip send')
                    else:
                        t0 = time.time()
                        ok = send_reply(text, cfg.get('send_mode'), target=t, before_local_id=turn.end_local_id, cfg=cfg, config_path=config_path,
                                        mention_name=event_context.get('mention_name') or event_context.get('sender_display_name') or event_context.get('sender_name') or event_context.get('from_display_name'))
                        cost = time.time() - t0
                        log('reply send attempted ok=%s cost=%.3fs' % (ok, cost))
                        _record_event(
                            'send_ok' if ok else 'send_failed',
                            target=t.get('name'),
                            sender=event_context.get('sender'),
                            payload={'local_id': turn.end_local_id, 'latency': round(cost, 3)},
                        )
                        if not ok:
                            t['last_send_failed_local_id'] = turn.end_local_id
                            t['last_send_failed_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                            if not cfg.get('advance_on_send_failure', True):
                                advance_state = False
                                log('send failed; state NOT advanced due advance_on_send_failure=false')
                            else:
                                log('send failed; state advanced but failure recorded')
                        else:
                            t.pop('last_send_failed_local_id', None)
                            t.pop('last_send_failed_at', None)
                except Exception as e:
                    import traceback as _tb
                    tb_text = _tb.format_exc().replace('\n', ' | ')[:1200]
                    log('reply_pipeline_exception target=%s local_id=%s error=%r traceback=%s' % (
                        t.get('name'), turn.end_local_id, e, tb_text))
                    try:
                        _record_event(
                            'reply_pipeline_exception',
                            target=t.get('name'),
                            sender=event_context.get('sender'),
                            payload={'local_id': turn.end_local_id, 'error': repr(e)[:300]},
                        )
                    except Exception:
                        pass
        # Close conversation windows whose debounce timer elapsed even when no
        # new message arrives.  Without this, a single image + wake word can sit
        # in memory forever waiting for another message to trigger the flush.
        flushed_turns = flush_due()
        for turn in flushed_turns:
            turn_cfg = turn.config or cfg
            turn_target = turn.target or {}
            if turn.target.get('admin_muted'):
                for tt in targets:
                    if tt.get('username') == turn.chat_id:
                        tt['last_local_id'] = max(int(tt.get('last_local_id') or 0), turn.end_local_id)
                        break
                continue
            if turn.image_paths and not turn.has_image_task_description() and not (turn.trigger_matched or turn.in_session):
                _send_image_task_missing_guide(turn, turn_cfg, config_path=config_path, dry_run=args.dry_run)
                for tt in targets:
                    if tt.get('username') == turn.chat_id:
                        tt['last_local_id'] = max(int(t.get('last_local_id') or 0), turn.end_local_id)
                        break
                continue
            try:
                agg_msg = turn.to_generate_reply_message()
                decision = generate_reply(agg_msg, turn_target, turn_cfg)
                text = decision.reply_text
                log('aggregator_flush_due target=%s local_id=%s intent=%s reason=%s reply=%r' % (
                    turn_target.get('name'), turn.end_local_id, decision.intent, decision.reason, text))
                if decision.should_reply and text and not args.dry_run:
                    send_reply(text, turn_cfg.get('send_mode'), target=turn_target, before_local_id=turn.end_local_id, cfg=turn_cfg, config_path=config_path,
                               mention_name=(turn.event_context or {}).get('mention_name') or (turn.event_context or {}).get('sender_display_name') or (turn.event_context or {}).get('sender_name') or (turn.event_context or {}).get('from_display_name'))
                for tt in targets:
                    if tt.get('username') == turn.chat_id:
                        tt['last_local_id'] = max(int(tt.get('last_local_id') or 0), turn.end_local_id)
                        break
            except Exception as e:
                log('aggregator_flush_due_exception target=%s local_id=%s error=%r' % (
                    turn_target.get('name'), turn.end_local_id, e))
        if (new_count or flushed_turns) and not args.no_save_state:
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
