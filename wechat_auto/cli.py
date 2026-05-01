#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified CLI entry-point for wechat_auto package.

Bridges the pip-installed console script `wechat-auto` (sub-command style)
to the manage_targets.py sub-command API.
"""
import argparse
import sys
from pathlib import Path

# Ensure wechat-decrypt vendored modules are on sys.path when installed as package
_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "wechat-decrypt"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from manage_targets import main as _manage_main  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wechat-auto",
        description="WeChat automation CLI – target management, monitor control, send",
    )
    sub = p.add_subparsers(dest="command", help="Available commands")

    # --- Monitor / process control ---
    sub.add_parser("start", help="Start the WeChat monitor background process")
    sub.add_parser("stop", help="Stop the WeChat monitor background process")
    sub.add_parser("restart", help="Restart the monitor")
    sub.add_parser("status", help="Show monitor process status")

    # scan = discover
    scan_p = sub.add_parser("scan", help="Scan new chats (alias: discover)")
    scan_p.add_argument("--include-contacts", action="store_true")
    scan_p.add_argument("--dry-run", action="store_true")
    sub.add_parser("discover")
    ls_p = sub.add_parser("ls", help="List groups with status (alias: list)")
    ls_p.add_argument("--kind", choices=["all", "pending", "enabled", "disabled"], default="all")
    ls_p.add_argument("--json", action="store_true")
    sub.add_parser("list")

    # --- Enable / disable ---
    on_p = sub.add_parser("on", help="Enable a target (alias: enable)")
    on_p.add_argument("key", nargs="?", help="group name or username; omit to show pending")
    on_p.add_argument("--wiki", action="append", default=[])
    sub.add_parser("enable", help="Alias for on")

    off_p = sub.add_parser("off", help="Disable a target (alias: disable)")
    off_p.add_argument("key")
    sub.add_parser("disable", help="Alias for off")

    re_p = sub.add_parser("re", help="Re-enable a target (alias: reenable)")
    re_p.add_argument("key")
    sub.add_parser("reenable", help="Alias for re")

    # --- Knowledge bases ---
    sub.add_parser("kb-list", help="List knowledge bases (aliases: kbs, wiki-list)")
    sub.add_parser("kbs")
    sub.add_parser("wiki-list")

    kb_add = sub.add_parser("kb-add", help="Add/update KB alias (aliases: wiki-add)")
    kb_add.add_argument("id")
    kb_add.add_argument("--type", choices=["getnote", "local", "hook"], default="getnote")
    kb_add.add_argument("--kid", dest="knowledge_base_id")
    kb_add.add_argument("--path")
    kb_add.add_argument("--description", "--desc", dest="description", default="")
    kb_add.add_argument("--executable")
    kb_add.add_argument("--replace", action="store_true")
    sub.add_parser("wiki-add")

    kb_local = sub.add_parser("kb-local", help="Create local KB (aliases: wiki-local)")
    kb_local.add_argument("id")
    kb_local.add_argument("--description", "--desc", dest="description", default="")
    kb_local.add_argument("--replace", action="store_true")
    sub.add_parser("wiki-local")

    kb_imp = sub.add_parser("kb-import", help="Import files into KB (aliases: wiki-import)")
    kb_imp.add_argument("id")
    kb_imp.add_argument("source")
    sub.add_parser("wiki-import")

    sub.add_parser("kb-open", help="Open KB directory (aliases: wiki-open)")
    sub.add_parser("wiki-open")
    sub.add_parser("kb-info", help="Show KB details (aliases: wiki-info)")
    sub.add_parser("wiki-info")

    kb = sub.add_parser("kb", help="Bind KB to target (aliases: bind-wiki)")
    kb.add_argument("key")
    kb.add_argument("wiki", nargs="+")
    kb.add_argument("--replace", action="store_true")
    sub.add_parser("bind-wiki")

    # --- DB lifecycle ---
    sub.add_parser("init", help="Init config.json (aliases: cfg)")
    sub.add_parser("cfg")
    sub.add_parser("key", help="Extract DB keys (aliases: keys)")
    sub.add_parser("keys")

    dec_p = sub.add_parser("dec", help="Decrypt DBs (aliases: decrypt)")
    dec_p.add_argument("--db")
    dec_p.add_argument("--out-dir")
    dec_p.add_argument("--keys-file")
    dec_p.add_argument("--db-dir")
    sub.add_parser("decrypt")

    setup_p = sub.add_parser("setup", help="init+keys+decrypt (aliases: bootstrap)")
    setup_p.add_argument("--admin", action="store_true")
    sub.add_parser("bootstrap")

    rf_p = sub.add_parser("refresh", help="Refresh enabled targets (aliases: rf)")
    rf_p.add_argument("--force", action="store_true")
    sub.add_parser("rf")

    # --- Info ---
    sub.add_parser("version", help="Show package version")

    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd_name = getattr(args, 'command', None)

    if cmd_name == "version":
        from wechat_auto import __version__
        print(__version__)
        return 0
    if cmd_name is None:
        parser.print_help()
        return 0

    cmd = [cmd_name]

    if cmd_name in ("on", "enable") and getattr(args, 'key', None):
        cmd.append(args.key)
        for w in getattr(args, 'wiki', []):
            cmd += ["--wiki", w]

    elif cmd_name in ("off", "disable", "re", "reenable"):
        cmd.append(getattr(args, 'key', ''))

    elif cmd_name in ("kb-add", "wiki-add"):
        cmd.append(args.id)
        if args.type and args.type != "getnote":
            cmd += ["--type", args.type]
        if getattr(args, 'knowledge_base_id', None):
            cmd += ["--kid", args.knowledge_base_id]
        if getattr(args, 'path', None):
            cmd += ["--path", args.path]
        if getattr(args, 'description', None):
            cmd += ["--description", args.description]
        if getattr(args, 'executable', None):
            cmd += ["--executable", args.executable]
        if getattr(args, 'replace', False):
            cmd.append("--replace")

    elif cmd_name in ("kb-local", "wiki-local"):
        cmd.append(args.id)
        if getattr(args, 'description', None):
            cmd += ["--description", args.description]
        if getattr(args, 'replace', False):
            cmd.append("--replace")

    elif cmd_name in ("kb-import", "wiki-import"):
        cmd += [args.id, args.source]

    elif cmd_name in ("kb", "bind-wiki"):
        cmd.append(getattr(args, 'key', ''))
        cmd.extend(getattr(args, 'wiki', []))
        if getattr(args, 'replace', False):
            cmd.append("--replace")

    elif cmd_name in ("scan", "discover"):
        if getattr(args, 'include_contacts', False):
            cmd.append("--include-contacts")
        if getattr(args, 'dry_run', False):
            cmd.append("--dry-run")

    elif cmd_name in ("ls", "list"):
        kind = getattr(args, 'kind', 'all')
        if kind != 'all':
            cmd += ["--kind", kind]
        if getattr(args, 'json', False):
            cmd.append("--json")

    elif cmd_name in ("dec", "decrypt"):
        if getattr(args, 'target_db', None):
            cmd += ["--db", args.target_db]
        if getattr(args, 'out_dir', None):
            cmd += ["--out-dir", args.out_dir]
        if getattr(args, 'keys_file', None):
            cmd += ["--keys-file", args.keys_file]
        if getattr(args, 'db_dir', None):
            cmd += ["--db-dir", args.db_dir]

    elif cmd_name in ("setup", "bootstrap"):
        if getattr(args, 'admin', False):
            cmd.append("--admin")

    elif cmd_name in ("refresh", "rf"):
        if getattr(args, 'force', False):
            cmd.append("--force")

    return _manage_main(cmd)


if __name__ == "__main__":
    raise SystemExit(main())