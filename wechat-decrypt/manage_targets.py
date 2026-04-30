#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI for WeChat bot target discovery and management."""
import argparse
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import target_registry as reg


def safe_print(text=""):
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(str(text).encode(enc, errors="replace").decode(enc, errors="replace"))


def print_json(obj):
    safe_print(json.dumps(obj, ensure_ascii=False, indent=2))


def print_table(rows, fields):
    if not rows:
        safe_print("(empty)")
        return
    widths = []
    for f in fields:
        widths.append(max(len(f), *(len(str(r.get(f, ""))) for r in rows)))
    safe_print("  ".join(f.ljust(widths[i]) for i, f in enumerate(fields)))
    safe_print("  ".join("-" * w for w in widths))
    for r in rows:
        safe_print("  ".join(str(r.get(f, "")).ljust(widths[i]) for i, f in enumerate(fields)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Manage WeChat bot monitored targets.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="discover new chats into pending candidates")
    p.add_argument("--include-contacts", action="store_true", help="also add 1:1 contacts; default groups only")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="list targets/candidates")
    p.add_argument("--kind", choices=["all", "targets", "candidates", "pending"], default="all")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("enable", help="enable a pending candidate by name or username")
    p.add_argument("key")
    p.add_argument("--wiki", action="append", default=[], help="knowledge base id; can repeat")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("disable", help="disable a configured target")
    p.add_argument("key")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reenable", help="re-enable a configured target")
    p.add_argument("key")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("bind-wiki", help="bind one or more knowledge bases to a target")
    p.add_argument("key")
    p.add_argument("wiki", nargs="+")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "discover":
            out = reg.discover_candidates(include_contacts=args.include_contacts)
            if args.json:
                print_json(out)
            else:
                safe_print("discovered={discovered} added={added} updated={updated} pending={pending}".format(**out))
            return 0
        if args.cmd == "list":
            out = reg.list_items()
            if args.json:
                print_json(out)
            else:
                if args.kind in ("all", "targets"):
                    rows = [{"enabled": t.get("enabled", True), "name": t.get("name"), "username": t.get("username"), "db": t.get("db"), "last_local_id": t.get("last_local_id"), "knowledge_bases": ",".join(t.get("knowledge_bases") or [])} for t in out["targets"]]
                    safe_print("\n[target]")
                    print_table(rows, ["enabled", "name", "username", "db", "last_local_id", "knowledge_bases"])
                if args.kind in ("all", "candidates", "pending"):
                    cands = out["candidates"]
                    if args.kind == "pending":
                        cands = [c for c in cands if c.get("status") == "pending"]
                    rows = [{"status": c.get("status"), "type": c.get("type"), "name": c.get("name"), "username": c.get("username"), "db": c.get("db"), "last_local_id": c.get("last_local_id"), "last_message_time": c.get("last_message_time_text")} for c in cands]
                    safe_print("\n[candidate]")
                    print_table(rows, ["status", "type", "name", "username", "db", "last_local_id", "last_message_time"])
            return 0
        if args.cmd == "enable":
            out = reg.enable_candidate(args.key, knowledge_bases=args.wiki)
            print_json(out) if args.json else safe_print("enabled target: %s (%s)" % (out.get("name"), out.get("username")))
            return 0
        if args.cmd == "disable":
            out = reg.set_enabled(args.key, False)
            print_json(out) if args.json else safe_print("disabled target: %s (%s)" % (out.get("name"), out.get("username")))
            return 0
        if args.cmd == "reenable":
            out = reg.set_enabled(args.key, True)
            print_json(out) if args.json else safe_print("enabled target: %s (%s)" % (out.get("name"), out.get("username")))
            return 0
        if args.cmd == "bind-wiki":
            out = reg.bind_wiki(args.key, args.wiki, replace=args.replace)
            print_json(out) if args.json else safe_print("knowledge_bases: %s" % ",".join(out.get("knowledge_bases") or []))
            return 0
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
