#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI for WeChat bot target discovery and management."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import target_registry as reg

ROOT = Path(__file__).resolve().parent
MONITOR_SCRIPT = ROOT / "wechat_bot_monitor.py"
STOP_FILE = ROOT / (reg.load_config().get("stop_file") or "wechat_bot_monitor.stop")


# ------------------------------------------------------------------
# Monitor process helpers
# ------------------------------------------------------------------
def _monitor_pids():
    out = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'wechat_bot_monitor\\.py' -and $_.Name -match '^pythonw?\\.exe$' } | Select-Object ProcessId | ForEach-Object { $_.ProcessId }"
        ],
        capture_output=True, text=True, encoding="utf-8",
    )
    pids = [int(x) for x in (out.stdout or "").strip().split() if x.strip().isdigit()]
    return pids


def _is_monitor_running():
    return bool(_monitor_pids())


def cmd_start(json_mode=False):
    if _is_monitor_running():
        msg = {"ok": True, "running": True, "message": "微信自动监听已在运行。"}
        if json_mode:
            safe_print(json.dumps(msg, ensure_ascii=False))
        else:
            safe_print(msg["message"])
        return 0
    if STOP_FILE.exists():
        STOP_FILE.unlink(missing_ok=True)
    pythonw = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Lewis\AppData\Local")) / "Programs" / "Python" / "Python312" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen(
        [str(pythonw), str(MONITOR_SCRIPT)],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    running = _is_monitor_running()
    msg = {"ok": running, "running": running, "message": "已启动微信自动监听。" if running else "启动中，请稍候再查看状态。"}
    if json_mode:
        safe_print(json.dumps(msg, ensure_ascii=False))
    else:
        safe_print(msg["message"])
    return 0 if running else 1


def cmd_stop(json_mode=False):
    if STOP_FILE.exists():
        STOP_FILE.unlink(missing_ok=True)
    STOP_FILE.write_text("stop requested at %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    msg = {"ok": True, "running": False, "message": "已请求停止微信自动监听。监听进程会在下一轮轮询内退出。"}
    if json_mode:
        safe_print(json.dumps(msg, ensure_ascii=False))
    else:
        safe_print(msg["message"])
    return 0


def cmd_restart(json_mode=False):
    cmd_stop(json_mode=json_mode)
    waited = 0
    while _is_monitor_running() and waited < 12:
        time.sleep(0.5)
        waited += 1
    return cmd_start(json_mode=json_mode)


def cmd_status(json_mode=False):
    pids = _monitor_pids()
    running = bool(pids)
    out = {"ok": True, "running": running, "pids": pids}
    if json_mode:
        safe_print(json.dumps(out, ensure_ascii=False))
    else:
        if running:
            safe_print("监听进程运行中，PID: %s" % ",".join(str(x) for x in pids))
        else:
            safe_print("监听进程未运行。")
    return 0


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------
def safe_print(text=""):
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(str(text).encode(enc, errors="replace").decode(enc, errors="replace"))


def print_json(obj):
    safe_print(json.dumps(obj, ensure_ascii=False, indent=2))


def print_table(rows, fields, htransform=None):
    if not rows:
        safe_print("(empty)")
        return
    htransform = htransform or {}
    widths = []
    for f in fields:
        label = htransform.get(f, f)
        widths.append(max(len(label), *(len(str(r.get(f, ""))) for r in rows)))
    safe_print("  ".join((htransform.get(f, f)).ljust(widths[i]) for i, f in enumerate(fields)))
    safe_print("  ".join("-" * w for w in widths))
    for r in rows:
        safe_print("  ".join(str(r.get(f, "")).ljust(widths[i]) for i, f in enumerate(fields)))


# ------------------------------------------------------------------
# Main CLI
# ------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Manage WeChat bot monitored targets.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # scan = discover
    p = sub.add_parser("scan", aliases=["discover"], help="scan new chats into pending candidates")
    p.add_argument("--include-contacts", action="store_true", help="also add 1:1 contacts; default groups only")
    p.add_argument("--json", action="store_true")

    # ls = list
    p = sub.add_parser("ls", aliases=["list"], help="list groups with listen status")
    p.add_argument("--kind", choices=["all", "pending", "enabled", "disabled"], default="all")
    p.add_argument("--json", action="store_true")

    # on = enable
    p = sub.add_parser("on", aliases=["enable"], help="enable a pending candidate by name or username")
    p.add_argument("key")
    p.add_argument("--wiki", action="append", default=[], help="knowledge base id; can repeat")
    p.add_argument("--json", action="store_true")

    # off = disable
    p = sub.add_parser("off", aliases=["disable"], help="disable a configured target")
    p.add_argument("key")
    p.add_argument("--json", action="store_true")

    # re = reenable
    p = sub.add_parser("re", aliases=["reenable"], help="re-enable a configured target")
    p.add_argument("key")
    p.add_argument("--json", action="store_true")

    # kb = bind-wiki
    p = sub.add_parser("kb", aliases=["bind-wiki"], help="bind one or more knowledge bases to a target")
    p.add_argument("key")
    p.add_argument("wiki", nargs="+")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--json", action="store_true")

    # process control
    p = sub.add_parser("start", help="start monitor process")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("stop", help="stop monitor process")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("restart", help="restart monitor process")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status", help="show monitor process status")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    cmd = args.cmd

    try:
        # -- process control --
        if cmd in ("start",):
            return cmd_start(json_mode=args.json)
        if cmd in ("stop",):
            return cmd_stop(json_mode=args.json)
        if cmd in ("restart",):
            return cmd_restart(json_mode=args.json)
        if cmd in ("status",):
            return cmd_status(json_mode=args.json)

        # -- scan / discover --
        if cmd in ("scan", "discover"):
            out = reg.discover_candidates(include_contacts=args.include_contacts)
            if args.json:
                print_json(out)
            else:
                safe_print("discovered={discovered} added={added} updated={updated} pending={pending}".format(**out))
            return 0

        # -- ls / list --
        if cmd in ("ls", "list"):
            rows = reg.list_groups()
            if args.kind == "pending":
                rows = [r for r in rows if r.get("status") == "pending"]
            elif args.kind == "enabled":
                rows = [r for r in rows if r.get("listen_enabled")]
            elif args.kind == "disabled":
                rows = [r for r in rows if r.get("status") not in ("pending",) and not r.get("listen_enabled")]
            if args.json:
                print_json({"kind": args.kind, "count": len(rows), "groups": rows})
            else:
                htransform = {
                    "status": "状态",
                    "listen_enabled": "监听",
                    "name": "名称",
                    "username": "微信ID",
                    "db": "数据库",
                    "last_local_id": "最后消息ID",
                    "last_message_time": "最后消息时间",
                    "knowledge_bases": "知识库",
                }
                print_table(rows, ["status", "listen_enabled", "name", "username", "db", "last_local_id", "last_message_time", "knowledge_bases"], htransform=htransform)
            return 0

        # -- on / enable --
        if cmd in ("on", "enable"):
            out = reg.enable_candidate(args.key, knowledge_bases=args.wiki)
            if args.json:
                print_json(out)
            else:
                safe_print("已启用目标: %s (%s)" % (out.get("name"), out.get("username")))
                safe_print("\n提示：新群仅在 monitor 启动后、或已运行中读取到该目标时生效。")
                if _is_monitor_running():
                    safe_print("监听进程运行中，无需额外操作（通常30秒内生效）。")
                else:
                    safe_print("监听进程未运行，请执行以下命令启动：")
                    safe_print("  python manage_targets.py start")
            return 0

        # -- off / disable --
        if cmd in ("off", "disable"):
            out = reg.set_enabled(args.key, False)
            if args.json:
                print_json(out)
            else:
                safe_print("已禁用目标: %s (%s)" % (out.get("name"), out.get("username")))
                if _is_monitor_running():
                    safe_print("监听进程运行中，变更会在下一轮轮询生效。")
                else:
                    safe_print("监听进程未运行。")
            return 0

        # -- re / reenable --
        if cmd in ("re", "reenable"):
            out = reg.set_enabled(args.key, True)
            if args.json:
                print_json(out)
            else:
                safe_print("已重新启用目标: %s (%s)" % (out.get("name"), out.get("username")))
                if _is_monitor_running():
                    safe_print("监听进程运行中，变更会在下一轮轮询生效。")
                else:
                    safe_print("监听进程未运行，请执行：python manage_targets.py start")
            return 0

        # -- kb / bind-wiki --
        if cmd in ("kb", "bind-wiki"):
            out = reg.bind_wiki(args.key, args.wiki, replace=args.replace)
            if args.json:
                print_json(out)
            else:
                safe_print("knowledge_bases: %s" % ",".join(out.get("knowledge_bases") or []))
            return 0

        safe_print("Unknown command: %s" % cmd)
        return 1
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())