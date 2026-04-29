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
from wechat_sender import send_reply_detailed

ROOT = Path(__file__).resolve().parent
MEMORY = (ROOT.parent.parent / 'memory').resolve()
CONFIG_PATH = ROOT / 'wechat_bot_targets.json'
DECRYPTED_MESSAGE_DIR = ROOT / 'decrypted' / 'message'
DECRYPTED_CONTACT_DB = ROOT / 'decrypted' / 'contact' / 'contact.db'
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
    cfg.setdefault('send_strategy', 'current_or_uia_then_ocr_search_physical')
    cfg.setdefault('uia_probe_enabled', True)
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


def load_contact_name_map(db_path=DECRYPTED_CONTACT_DB):
    """Return username -> display name from the decrypted contact DB.

    Group messages in message DB are stored as "username:\ncontent".  That
    username is stable but not suitable for a human-facing @ prefix, so resolve
    it to remark/nick_name before passing the message to reply_engine.
    """
    p = Path(db_path)
    if not p.exists():
        return {}
    con = sqlite3.connect('file:%s?mode=ro' % p.as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    try:
        if not table_exists(con, 'contact'):
            return {}
        names = {}
        for row in con.execute('select username, remark, nick_name, alias from contact'):
            username = str(row['username'] or '').strip()
            if not username:
                continue
            display = str(row['remark'] or row['nick_name'] or row['alias'] or username).strip()
            names[username] = display or username
        return names
    except Exception as e:
        log('warn load contact names failed: %r' % (e,))
        return {}
    finally:
        con.close()


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
    t0 = time.time()
    deadline = t0 + max(0.5, float(timeout or 5.0))
    attempts = 0
    refresh_total = 0.0
    lookup_total = 0.0
    last_summary = ''
    while time.time() < deadline:
        attempts += 1
        refresh_t0 = time.time()
        rc, refresh_dt, changed, failed, summary = run_fast_refresh(config_path)
        refresh_total += time.time() - refresh_t0
        last_summary = summary
        lookup_t0 = time.time()
        sent = has_self_sent_after(t, before_local_id, text)
        lookup_total += time.time() - lookup_t0
        if sent:
            log('send DB-confirmed target=%s before=%s sent_local_id=%s elapsed=%.3fs attempts=%d refresh_total=%.3fs lookup_total=%.3fs last_refresh=rc%s/changed%s/failed%s/%s content=%r' % (
                t.get('name'), before_local_id, sent.get('local_id'), time.time() - t0, attempts, refresh_total, lookup_total,
                rc, changed, failed, last_summary, sent.get('message_content')))
            return True
        time.sleep(0.5)
    log('send NOT DB-confirmed target=%s before=%s timeout=%.1fs elapsed=%.3fs attempts=%d refresh_total=%.3fs lookup_total=%.3fs last_refresh=%s' % (
        t.get('name'), before_local_id, float(timeout or 5.0), time.time() - t0, attempts, refresh_total, lookup_total, last_summary))
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



def send_reply(text, mode=None, target=None, before_local_id=None, cfg=None, config_path=CONFIG_PATH):
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
    )
    log('send result ok=%s mode=%s reason=%s confirmed=%s attempted=%s detail_keys=%s' % (
        result.ok, result.mode, result.reason, result.confirmed, ','.join(result.attempted), ','.join(sorted(result.detail.keys()))))
    return bool(result)


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

    runtime_min_last_local_id = {}
    contact_names = load_contact_name_map()
    log('contact display names loaded=%d' % len(contact_names))
    for t in targets:
        latest = fetch_latest_for_target(t)
        if int(t.get('last_local_id') or 0) <= 0 and latest:
            t['last_local_id'] = int(latest.get('local_id') or 0)
        target_key = '%s|%s|%s' % (t.get('db'), t.get('table'), t.get('username'))
        runtime_min_last_local_id[target_key] = int(t.get('last_local_id') or 0)
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
                log('new msg target=%s local_id=%s content=%r' % (t.get('name'), lid, m.get('message_content')))
                if is_trigger(cfg, t, m):
                    hit_count += 1
                    context_limit = int(cfg.get('context_limit') or 12)
                    context_t0 = time.time()
                    ctx = fetch_context_for_target(t, upto_local_id=lid, limit=context_limit)
                    context_dt = time.time() - context_t0
                    if ctx:
                        m['context_messages'] = ctx
                    gen_t0 = time.time()
                    decision = generate_reply(m, t, cfg)
                    gen_dt = time.time() - gen_t0
                    text = decision.reply_text
                    log('trigger hit target=%s local_id=%s ctx=%d ctx=%.3fs gen=%.3fs intent=%s risk=%s need_human=%s reason=%s reply=%r' % (
                        t.get('name'), lid, len(ctx), context_dt, gen_dt, decision.intent, decision.risk_level, decision.need_human, decision.reason, text))
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
