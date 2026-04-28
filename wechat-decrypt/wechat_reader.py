#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only helper for decrypted WeChat 4.x DBs. Does not read keys."""
import argparse, hashlib, json, os, sqlite3, time, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from typing import Any, Dict, List
BASE = os.path.join(os.path.dirname(__file__), 'decrypted')

def db_path(rel: str) -> str:
    return os.path.join(BASE, rel)

def connect_ro(rel: str) -> sqlite3.Connection:
    p = db_path(rel)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con

def clean(v: Any, max_len: int = 500) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return f'<bytes {len(v)}>'
    if isinstance(v, str):
        return v if len(v) <= max_len else v[:max_len] + '…'
    return v

def rowdict(r: sqlite3.Row) -> Dict[str, Any]:
    return {k: clean(r[k]) for k in r.keys()}

def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None

def list_sessions(limit: int = 20) -> List[Dict[str, Any]]:
    con = connect_ro('session/session.db')
    sql = """select username, unread_count, summary, last_timestamp, sort_timestamp,
                    last_msg_sender, last_sender_display_name, last_msg_locald_id, last_msg_type, last_msg_sub_type
             from SessionTable order by sort_timestamp desc limit ?"""
    rows = [rowdict(r) for r in con.execute(sql, (limit,))]
    con.close()
    for r in rows:
        u = r.get('username') or ''
        r['msg_table'] = 'Msg_' + hashlib.md5(u.encode('utf-8')).hexdigest()
        ts = r.get('last_timestamp') or 0
        try:
            r['last_time_local'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(ts))) if int(ts) else ''
        except Exception:
            r['last_time_local'] = ''
    return rows

def recent_messages(username: str, limit: int = 20) -> Dict[str, Any]:
    table = 'Msg_' + hashlib.md5(username.encode('utf-8')).hexdigest()
    con = connect_ro('message/message_0.db')
    if not table_exists(con, table):
        existing = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name like 'Msg_%' order by name")]
        con.close()
        return {'username': username, 'msg_table': table, 'exists': False, 'existing_msg_tables': existing}
    sql = f"""select local_id, server_id, local_type, sort_seq, real_sender_id, create_time,
                     status, message_content, compress_content, WCDB_CT_message_content, source
              from \"{table}\" order by sort_seq desc, local_id desc limit ?"""
    rows = [rowdict(r) for r in con.execute(sql, (limit,))]
    con.close()
    for r in rows:
        ts = r.get('create_time') or 0
        try:
            r['create_time_local'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(ts))) if int(ts) else ''
        except Exception:
            r['create_time_local'] = ''
    return {'username': username, 'msg_table': table, 'exists': True, 'messages': rows}

def main():
    ap = argparse.ArgumentParser(description='Read decrypted WeChat DBs')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('sessions')
    s.add_argument('--limit', type=int, default=20)
    r = sub.add_parser('recent')
    r.add_argument('username')
    r.add_argument('--limit', type=int, default=20)
    args = ap.parse_args()
    out = list_sessions(args.limit) if args.cmd == 'sessions' else recent_messages(args.username, args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
if __name__ == '__main__':
    main()
