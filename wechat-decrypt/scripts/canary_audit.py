#!/usr/bin/env python3
"""Canary observation-window audit for the reliable pipeline.

Runs the three Stage 4 canary checks against a pipeline SQLite DB and prints
a PASS/FAIL report. Read-only; never echoes message or reply text (ids,
counts, statuses, KB ids, and classified error fingerprints only).

Observation window: canary acceptance is judged ONLY on rows inside an
explicit window (`--since` unix ts, `--min-job-id`). Every job inside the
window must be fully verifiable -- a job whose trace cannot be inspected
(corrupt/missing provenance, malformed allowlist) FAILS the KB check.
Historical out-of-window rows are counted separately and never participate
in the window verdict.

Checks:
  1. silent loss   -- window rows all reach a recorded terminal state
                      (jobs: done/failed/timeout/escalated; outbox:
                      sent/dead_letter; events: turned). Non-terminal rows
                      are failures only when past their own schedule + grace
                      (`--grace-seconds`, default 900): outbox `sending` vs
                      lease_until, outbox retry/pending vs next_attempt_at,
                      job `running` vs min(lease_until, deadline_at), else
                      created_at. In-schedule rows are in_flight info.
  2. duplicates    -- at most one `sent` outbox row per window job, and
                      every sent row carries confirmed=true.
  3. unauthorized KB access -- per window job, kb_ids in the persisted
                      provenance must be a subset of the *at-enqueue*
                      snapshot allowlist (events[-1].message._allowed_kb_ids
                      written by build_event_payload and carried verbatim
                      through turn materialization). Missing/malformed trace
                      or allowlist inside the window FAILS (fail-closed).

Usage:
  python scripts/canary_audit.py --db PATH [--target-id ID ...]
      [--since UNIX_TS] [--min-job-id N] [--grace-seconds S]
Exit code is non-zero when any check fails.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time

JOB_TERMINAL = ('done', 'failed', 'timeout', 'escalated')
OUTBOX_TERMINAL = ('sent', 'dead_letter')

_ERR_CATEGORIES = (
    ('quarantined', 'quarantined'),
    ('required knowledge search', 'knowledge_gate'),
    ('provider result failed', 'provider_failed'),
    ('provider run failed', 'provider_failed'),
    ('provider returned non-agentresult', 'provider_failed'),
    ('normalization failed', 'provider_failed'),
    ('no runnable provider', 'provider_failed'),
    ('test_mode_target_rejected', 'test_mode_rejected'),
    ('target not configured', 'target_not_configured'),
    ('timeoutexpired', 'cua_timeout'),
    ('confirm_error', 'confirm_error'),
    ('send_reply_detailed raised', 'send_exception'),
)


def _tgt(targets, prefix=''):
    if not targets:
        return ''
    ph = ','.join('?' * len(targets))
    return ' AND %starget_id IN (%s)' % (prefix, ph)


def _rows(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args)]


def _win(win, kind):
    """Window predicate sql + params for one table kind."""
    sql, params = '', []
    if win.get('since') is not None:
        sql += ' AND created_at >= ?'
        params.append(float(win['since']))
    min_job = int(win.get('min_job_id') or 0)
    if min_job > 0:
        if kind == 'job':
            sql += ' AND id >= ?'
            params.append(min_job)
        elif kind == 'outbox':
            sql += ' AND job_id >= ?'
            params.append(min_job)
    return sql, params


def _iso(ts):
    if ts is None:
        return None
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(ts)))


def _err_fingerprint(err):
    """Privacy-safe error identity: fixed category + sha256 prefix, never text."""
    low = (err or '').lower()
    category = 'other'
    for needle, label in _ERR_CATEGORIES:
        if needle in low:
            category = label
            break
    digest = hashlib.sha256((err or '').encode('utf-8')).hexdigest()[:8]
    return category, digest


def _terminal_rows(con, sql, args):
    out = []
    for r in _rows(con, sql, args):
        category, digest = _err_fingerprint(r.get('error'))
        row = {'id': r['id'], 'target_id': r['target_id'],
               'error_category': category, 'error_sha256_8': digest}
        if 'status' in r:
            row['status'] = r['status']
        if 'job_id' in r:
            row['job_id'] = r['job_id']
        out.append(row)
    return out


def _event_message(snapshot):
    """Mirror reliable_worker._event_message: inner WeChat message dict."""
    if not isinstance(snapshot, dict):
        return {}
    inner = snapshot.get('message')
    if isinstance(inner, dict):
        return inner
    return snapshot


def _is_valid_snapshot(snapshot):
    """Mirror reliable_worker._is_valid_snapshot EXACTLY.

    The worker promotes `_allowed_kb_ids` from the LAST worker-valid
    snapshot, so the audit must evaluate that same snapshot -- never an
    earlier one that merely happens to carry the key.
    """
    if not isinstance(snapshot, dict):
        return False
    msg = _event_message(snapshot)
    known_keys = (
        'message_content', 'sender_username', 'sender_display_name',
        'image_path', 'session_image_paths', 'local_type', 'mention_name',
    )
    if not any(key in msg for key in known_keys):
        return False
    has_text = bool(str(msg.get('message_content') or '').strip())
    has_image = bool(str(msg.get('image_path') or '').strip()) or bool(
        msg.get('session_image_paths')
    )
    return has_text or has_image


def _stuck(row, now, grace_seconds, kind):
    """A non-terminal row is stuck only after its OWN schedule + grace.

    - outbox `sending`: lease_until + grace (fallback created_at)
    - outbox `retry`/`pending`: next_attempt_at + grace (fallback created_at)
    - job `running`: min(lease_until, deadline_at) + grace, so a job past its
      execution deadline is flagged even with a renewed lease
    - everything else: created_at + grace. next_attempt_at is outbox-only.
    """
    status = row.get('status')
    created = row.get('created_at') or 0.0
    if kind == 'outbox' and status == 'sending':
        basis = row.get('lease_until') or created
    elif kind == 'outbox' and status in ('retry', 'pending'):
        naa = row.get('next_attempt_at')
        basis = naa if naa is not None else created
    elif kind == 'job' and status == 'running':
        limits = [v for v in (row.get('lease_until'), row.get('deadline_at')) if v is not None]
        basis = min(limits) if limits else created
    else:
        basis = created
    return now > float(basis) + float(grace_seconds)


def check_silent_loss(con, targets, now, grace_seconds, win):
    tgt, tgt_t = _tgt(targets), _tgt(targets, 't.')
    job_ph = ','.join("'%s'" % s for s in JOB_TERMINAL)
    ob_ph = ','.join("'%s'" % s for s in OUTBOX_TERMINAL)
    w_ev, p_ev = _win(win, 'event')
    w_job, p_job = _win(win, 'job')
    w_ob, p_ob = _win(win, 'outbox')

    stuck_events, flight_events = [], 0
    for r in _rows(con, "SELECT id, target_id, local_id, status, created_at FROM inbound_events WHERE (turn_id IS NULL OR status != 'turned')" + tgt + w_ev, targets + p_ev):
        if _stuck(r, now, grace_seconds, 'event'):
            stuck_events.append({'id': r['id'], 'target_id': r['target_id'], 'local_id': r['local_id']})
        else:
            flight_events += 1

    stuck_turns, flight_turns = [], 0
    for r in _rows(con, "SELECT t.id, t.target_id, t.created_at FROM turns t LEFT JOIN turn_jobs j ON j.turn_id=t.id WHERE j.id IS NULL" + tgt_t + w_ev.replace('created_at', 't.created_at'), targets + p_ev):
        r['status'] = 'ready'
        if _stuck(r, now, grace_seconds, 'turn'):
            stuck_turns.append({'id': r['id'], 'target_id': r['target_id']})
        else:
            flight_turns += 1

    stuck_jobs, flight_jobs = [], 0
    for r in _rows(con, "SELECT id, target_id, status, created_at, lease_until, deadline_at FROM turn_jobs WHERE status NOT IN (%s)" % job_ph + tgt + w_job, targets + p_job):
        if _stuck(r, now, grace_seconds, 'job'):
            stuck_jobs.append({'id': r['id'], 'target_id': r['target_id'], 'status': r['status']})
        else:
            flight_jobs += 1

    stuck_ob, flight_ob = [], 0
    for r in _rows(con, "SELECT id, job_id, target_id, status, created_at, lease_until, next_attempt_at FROM send_outbox WHERE status NOT IN (%s)" % ob_ph + tgt + w_ob, targets + p_ob):
        if _stuck(r, now, grace_seconds, 'outbox'):
            stuck_ob.append({'id': r['id'], 'job_id': r['job_id'], 'target_id': r['target_id'], 'status': r['status']})
        else:
            flight_ob += 1

    findings = {
        'orphan_events': stuck_events, 'turns_without_job': stuck_turns,
        'non_terminal_jobs': stuck_jobs, 'non_terminal_outbox': stuck_ob,
    }
    info = {
        'in_flight_events': flight_events, 'in_flight_turns': flight_turns,
        'in_flight_jobs': flight_jobs, 'in_flight_outbox': flight_ob,
        'failed_jobs': _terminal_rows(con, "SELECT id, target_id, status, error FROM turn_jobs WHERE status IN ('failed','timeout')" + tgt + w_job + " ORDER BY id", targets + p_job),
        'dead_letters': _terminal_rows(con, "SELECT id, job_id, target_id, error FROM send_outbox WHERE status='dead_letter'" + tgt + w_ob + " ORDER BY id", targets + p_ob),
    }
    return all(not v for v in findings.values()), findings, info


def check_duplicates(con, targets, win):
    tgt = _tgt(targets)
    w_ob, p_ob = _win(win, 'outbox')
    dup = _rows(con, "SELECT job_id, COUNT(*) c, GROUP_CONCAT(id) ids FROM send_outbox WHERE status='sent'" + tgt + w_ob + " GROUP BY job_id HAVING c>1", targets + p_ob)
    sent = _rows(con, "SELECT id, job_id, result_json FROM send_outbox WHERE status='sent'" + tgt + w_ob + " ORDER BY id", targets + p_ob)
    unconfirmed = []
    for r in sent:
        try:
            res = json.loads(r['result_json'] or '{}')
        except Exception:
            res = {}
        if res.get('confirmed') is not True:
            unconfirmed.append(r['id'])
    findings = {'jobs_with_multiple_sent': dup, 'sent_unconfirmed': unconfirmed}
    return not dup and not unconfirmed, findings, {'sent_total': len(sent)}


def check_kb_authorization(con, targets, win):
    """Fail-closed over window jobs: every window job must be verifiable.

    Inside the window a NULL/empty provenance_json, a missing `provenance`
    key, malformed JSON/types, or a malformed/missing at-enqueue allowlist
    all land in `unverifiable_jobs` and FAIL the check. Out-of-window
    (historical, pre-tracking) rows are only counted.
    """
    tgt = _tgt(targets)
    w_job, p_job = _win(win, 'job')
    violations = []
    unverifiable = []
    examined = 0
    out_of_window = 0
    in_window_ids = set()
    for r in _rows(con, "SELECT id FROM turn_jobs WHERE 1=1" + tgt + w_job, targets + p_job):
        in_window_ids.add(r['id'])
    for row in _rows(con, "SELECT id, provenance_json, payload_json FROM turn_jobs WHERE 1=1" + tgt + " ORDER BY id", targets):
        if row['id'] not in in_window_ids:
            out_of_window += 1
            continue

        def bad():
            unverifiable.append(row['id'])

        raw_prov = row['provenance_json']
        if raw_prov is None or not str(raw_prov).strip():
            bad()  # window jobs must carry a worker trace summary
            continue
        try:
            prov = json.loads(raw_prov)
            pay = json.loads(row['payload_json'] or '')
        except Exception:
            bad()
            continue
        if not isinstance(prov, dict) or not isinstance(pay, dict):
            bad()
            continue
        if 'provenance' not in prov:
            bad()  # worker summary contract always includes the key
            continue
        events = pay.get('events')
        if not isinstance(events, list) or not events:
            bad()
            continue
        allowed = None
        malformed = False
        valid_snapshots = []
        for ev in events:
            if not isinstance(ev, dict):
                # Outside the durable contract; the worker would skip it,
                # but a security audit cannot verify such a job.
                malformed = True
                break
            snap = ev.get('message')
            if _is_valid_snapshot(snap):
                valid_snapshots.append(snap)
        if not malformed:
            if not valid_snapshots:
                malformed = True
            else:
                # Mirror the worker: scope comes from the LAST valid
                # snapshot only.
                last = valid_snapshots[-1]
                if '_allowed_kb_ids' not in last:
                    malformed = True
                else:
                    raw_allowed = last['_allowed_kb_ids']
                    if not isinstance(raw_allowed, list):
                        malformed = True
                    else:
                        # Every scope entry must be a non-empty string;
                        # compare stripped values so ' leann.bus ' cannot
                        # bypass or miss.
                        normalized = []
                        for kb in raw_allowed:
                            if not isinstance(kb, str) or not kb.strip():
                                malformed = True
                                break
                            normalized.append(kb.strip())
                        if not malformed:
                            allowed = normalized
        if malformed or allowed is None:
            bad()  # window jobs must carry the at-enqueue KB scope snapshot
            continue
        inner = prov.get('provenance')
        if not isinstance(inner, list):
            bad()
            continue
        hits = []
        for item in inner:
            if not isinstance(item, dict):
                malformed = True
                break
            kb = item.get('kb_id')
            if not isinstance(kb, str) or not kb.strip():
                malformed = True
                break
            hits.append(kb.strip())
        if malformed:
            bad()
            continue
        if hits:
            examined += 1
            bad_hits = [h for h in hits if h not in allowed]
            if bad_hits:
                violations.append({'job_id': row['id'], 'hits': bad_hits, 'allowed': allowed})
    ok = not violations and not unverifiable
    info = {'jobs_with_kb_hits': examined, 'out_of_window_jobs': out_of_window}
    return ok, {'violations': violations, 'unverifiable_jobs': unverifiable}, info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--db', required=True, help='pipeline sqlite path')
    ap.add_argument('--target-id', action='append', default=[],
                    help='restrict to target_id (repeatable); default audits all')
    ap.add_argument('--since', type=float, default=None,
                    help='observation window start (unix ts) on created_at')
    ap.add_argument('--min-job-id', type=int, default=0,
                    help='observation window start as minimum turn_jobs.id (outbox: job_id)')
    ap.add_argument('--grace-seconds', type=float, default=900.0,
                    help='in-flight grace window for stuck detection (default 900)')
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    targets = list(args.target_id)
    now = time.time()
    win = {'since': args.since, 'min_job_id': args.min_job_id}
    checks = [
        ('silent loss', lambda c, t: check_silent_loss(c, t, now, args.grace_seconds, win)),
        ('unconfirmed duplicate sends', lambda c, t: check_duplicates(c, t, win)),
        ('unauthorized KB access', lambda c, t: check_kb_authorization(c, t, win)),
    ]
    all_ok = True
    for name, fn in checks:
        ok, findings, info = fn(con, targets)
        all_ok = all_ok and ok
        print('[%s] %s' % ('PASS' if ok else 'FAIL', name))
        print('  info: %s' % json.dumps(info, ensure_ascii=False))
        if not ok:
            print('  findings: %s' % json.dumps(findings, ensure_ascii=False))
    # Boundary evidence: actual first/last row times per chain table inside
    # the window, so the reviewer can confirm --since covers the canary.
    boundary = {}
    for table, kind in (('inbound_events', 'event'), ('turns', 'event'), ('turn_jobs', 'job'), ('send_outbox', 'outbox')):
        w_sql, w_params = _win(win, kind)
        sql = "SELECT COUNT(*) c, MIN(created_at) first, MAX(created_at) last FROM %s WHERE 1=1" % table + _tgt(targets) + w_sql
        row = _rows(con, sql, targets + w_params)[0]
        boundary[table] = {
            'rows': row['c'],
            'first': _iso(row['first']), 'last': _iso(row['last']),
        }
    print('window: since=%s min_job_id=%s' % (_iso(win.get('since')), win.get('min_job_id') or 0))
    print('window rows: %s' % json.dumps(boundary, ensure_ascii=False))
    print('RESULT: %s' % ('PASS' if all_ok else 'FAIL'))
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
