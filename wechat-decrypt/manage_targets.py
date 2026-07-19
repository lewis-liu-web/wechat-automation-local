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

try:
    import event_log as _el
except Exception:  # pragma: no cover
    _el = None


def _audit_event(kind: str, target: str | None = None, payload: dict | None = None) -> None:
    """Best-effort audit write; never raises into the CLI flow."""
    if _el is None:
        return
    try:
        _el.log_event(kind, target=target, payload=payload or {})
    except Exception:
        pass


ROOT = Path(__file__).resolve().parent
MONITOR_SCRIPT = ROOT / "wechat_bot_monitor.py"
STOP_FILE = ROOT / (reg.load_config().get("stop_file") or "wechat_bot_monitor.stop")
GA_ROOT = ROOT.parents[1] if len(ROOT.parents) > 1 else ROOT
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

INIT_SCRIPT = ROOT / "config.py"
KEY_SCRIPT = ROOT / "find_all_keys_windows.py" if (ROOT / "find_all_keys_windows.py").exists() else ROOT / "find_all_keys.py"
DECRYPT_SCRIPT = ROOT / "decrypt_db.py"
ADMIN_SETUP_SCRIPT = ROOT / "admin_extract_and_decrypt.py"


# ------------------------------------------------------------------
# Decrypt / key extraction helpers
# ------------------------------------------------------------------
def _run_script(script, extra_args=None):
    extra_args = list(extra_args or [])
    cmd = [sys.executable, str(script)] + extra_args
    safe_print("$ " + " ".join([Path(cmd[0]).name] + [str(x) for x in cmd[1:]]))
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def cmd_init(json_mode=False):
    """Initialize config.json by loading config.auto-detection."""
    from config import load_config
    cfg = load_config()
    out = {
        "ok": True,
        "db_dir": cfg.get("db_dir"),
        "keys_file": cfg.get("keys_file"),
        "decrypted_dir": cfg.get("decrypted_dir"),
        "message": "微信数据库配置已初始化/确认。",
    }
    if json_mode:
        print_json(out)
    else:
        safe_print(out["message"])
        safe_print("db_dir: %s" % out["db_dir"])
        safe_print("keys_file: %s" % out["keys_file"])
        safe_print("decrypted_dir: %s" % out["decrypted_dir"])
        safe_print("下一步：python manage_targets.py key  # 提取数据库密钥")
        safe_print("或：  python manage_targets.py setup  # 提钥并解密全部数据库")
    return 0


def cmd_key(json_mode=False):
    if json_mode:
        safe_print(json.dumps({"ok": True, "action": "extract_keys", "script": str(KEY_SCRIPT)}, ensure_ascii=False))
    safe_print("开始从微信进程提取数据库密钥；请确保微信已启动并登录。")
    safe_print("注意：控制台/日志不应打印完整密钥；密钥文件仅保留在本机运行目录。")
    return _run_script(KEY_SCRIPT)


def cmd_dec(target_db=None, out_dir=None, keys_file=None, db_dir=None, json_mode=False):
    args = []
    if target_db:
        args += ["--db", target_db]
    if out_dir:
        args += ["--out-dir", out_dir]
    if keys_file:
        args += ["--keys-file", keys_file]
    if db_dir:
        args += ["--db-dir", db_dir]
    if json_mode:
        safe_print(json.dumps({"ok": True, "action": "decrypt", "script": str(DECRYPT_SCRIPT), "args": args}, ensure_ascii=False))
    safe_print("开始解密微信数据库。若提示缺少密钥，请先执行：python manage_targets.py key")
    return _run_script(DECRYPT_SCRIPT, args)

def cmd_decrypt_status(json_mode=False):
    """Show read-only status of key extraction and decryption; never print keys."""
    import config as _config

    if not os.path.exists(_config.CONFIG_FILE):
        result = {"ok": False, "reason": "config_missing"}
        if json_mode:
            print_json(result)
        else:
            safe_print("decrypt-status: config missing")
        return 0

    # load_config may print auto-detect messages even with write_missing=False;
    # suppress them so JSON output stays clean.
    import io as _io
    _stdout_buf = sys.stdout
    try:
        sys.stdout = _io.StringIO()
        cfg = _config.load_config(exit_on_missing=False, write_missing=False)
    finally:
        sys.stdout = _stdout_buf
    db_dir = cfg.get("db_dir", "")
    keys_file = cfg.get("keys_file", "")
    decrypted_dir = cfg.get("decrypted_dir", "")

    keys = {}
    if keys_file and os.path.exists(keys_file):
        try:
            with open(keys_file, encoding="utf-8") as f:
                keys = json.load(f)
        except Exception:
            keys = {}

    keys_count = sum(1 for k in keys if isinstance(k, str) and not k.startswith("_"))
    keys_match_db_dir = False
    if isinstance(keys.get("_db_dir"), str) and isinstance(db_dir, str):
        keys_match_db_dir = os.path.normcase(os.path.normpath(keys["_db_dir"])) == os.path.normcase(os.path.normpath(db_dir))

    decrypted_db_count = 0
    decrypted_total_bytes = 0
    if decrypted_dir and os.path.isdir(decrypted_dir):
        try:
            for root, _dirs, files in os.walk(decrypted_dir):
                for fname in files:
                    if fname.endswith(".db"):
                        fpath = os.path.join(root, fname)
                        try:
                            decrypted_total_bytes += os.path.getsize(fpath)
                            decrypted_db_count += 1
                        except OSError:
                            pass
        except OSError:
            pass

    result = {
        "ok": True,
        "db_dir": db_dir,
        "keys_file": keys_file,
        "keys_count": keys_count,
        "keys_match_db_dir": keys_match_db_dir,
        "decrypted_dir": decrypted_dir,
        "decrypted_db_count": decrypted_db_count,
        "decrypted_total_bytes": decrypted_total_bytes,
    }

    if json_mode:
        print_json(result)
    else:
        safe_print("db_dir: {db_dir}".format(**result))
        safe_print("keys_file: {keys_file}".format(**result))
        safe_print("keys_count: {keys_count}".format(**result))
        safe_print("keys_match_db_dir: {keys_match_db_dir}".format(**result))
        safe_print("decrypted_dir: {decrypted_dir}".format(**result))
        safe_print("decrypted_db_count: {decrypted_db_count}".format(**result))
        safe_print("decrypted_total_bytes: {decrypted_total_bytes}".format(**result))

    return 0


def cmd_setup(admin=False, json_mode=False):
    if admin:
        safe_print("开始管理员一键提钥+解密流程。必要时请用管理员权限运行当前终端。")
        if json_mode:
            safe_print(json.dumps({"ok": True, "action": "admin_setup", "script": str(ADMIN_SETUP_SCRIPT)}, ensure_ascii=False))
        return _run_script(ADMIN_SETUP_SCRIPT)
    safe_print("开始初始化 + 提取密钥 + 解密全部数据库。")
    code = cmd_init(json_mode=False)
    if code != 0:
        return code
    code = cmd_key(json_mode=False)
    if code != 0:
        safe_print("提取密钥失败；请确认微信已启动/登录，必要时以管理员权限运行：python manage_targets.py setup --admin")
        return code
    code = cmd_dec(json_mode=False)
    if code == 0:
        safe_print("完成。下一步可执行：python manage_targets.py scan 发现群；python manage_targets.py start 启动监听。")
    return code


def cmd_refresh(force=False, json_mode=False):
    args = ["--force"] if force else []
    if json_mode:
        args.append("--json")
    return _run_script(ROOT / "fast_refresh_targets.py", args)


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
        creationflags=_SUBPROCESS_FLAGS,
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
    # Wait for the monitor to exit. The monitor only checks STOP_FILE once per
    # poll cycle (poll_interval seconds), so give it enough headroom.
    deadline = time.time() + 30
    while _is_monitor_running() and time.time() < deadline:
        time.sleep(0.5)
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
    p = sub.add_parser("on", aliases=["enable"], help="enable a pending/disabled target by name or username")
    p.add_argument("key", nargs="?", help="group name or username; omit to show pending groups")
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

    # trigger = manage per-target trigger keywords
    p = sub.add_parser("trigger-list", aliases=["tl"], help="list trigger keywords for a target (and the global default_triggers)")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trigger-add", aliases=["ta"], help="add trigger keywords to a target")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("words", nargs="+", help="trigger keywords to add, e.g. @bot #ask")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trigger-remove", aliases=["tr"], help="remove specific trigger keywords from a target")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("words", nargs="+", help="trigger keywords to remove")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trigger-replace", aliases=["tp"], help="replace all per-target trigger keywords")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("words", nargs="+", help="new trigger keywords list")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trigger-clear", aliases=["tc"], help="clear all per-target trigger keywords (fall back to default_triggers)")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("--json", action="store_true")

    # trigger-default-* = manage global default_triggers
    p = sub.add_parser("trigger-default-list", help="list global default trigger keywords")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trigger-default-replace", help="replace global default trigger keywords")
    p.add_argument("words", nargs="+", help="new default trigger keywords")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trigger-default-clear", help="clear global default trigger keywords")
    p.add_argument("--json", action="store_true")


    # legacy alias: `trigger key [words ...] [--set|--remove|--clear]`
    p = sub.add_parser("trigger", aliases=["triggers", "kw", "keyword"], help="legacy alias; prefer trigger-list/add/remove/replace/clear")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("triggers", nargs="*", help="trigger keywords, e.g. #ask #穿越测试")
    p.add_argument("--set", dest="replace", action="store_true", help="replace existing per-target triggers")
    p.add_argument("--remove", "--rm", action="store_true", help="remove the given keywords")
    p.add_argument("--clear", action="store_true", help="clear all per-target triggers")
    p.add_argument("--json", action="store_true")

    # kb-list = list configured knowledge bases
    p = sub.add_parser("kb-list", aliases=["kbs", "wiki-list"], help="list configured knowledge base aliases")
    p.add_argument("--json", action="store_true")

    # kb-add = create/update knowledge base alias
    p = sub.add_parser("kb-add", aliases=["wiki-add"], help="create a knowledge base alias for binding to groups")
    p.add_argument("id", help="alias used by 'kb', e.g. canteen or workdocs")
    p.add_argument("--type", choices=["getnote", "local", "hook"], default="getnote")
    p.add_argument("--kid", "--knowledge-base-id", dest="knowledge_base_id", help="external provider knowledge base id (optional for --type hook; required for getnote)")
    p.add_argument("--path", help="local wiki path for --type local")
    p.add_argument("--description", "--desc", default="")
    p.add_argument("--executable", help="adapter executable path for --type hook/getnote")
    p.add_argument("--scope", default="scene")
    p.add_argument("--limit", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--replace", action="store_true", help="update alias if it already exists")
    p.add_argument("--json", action="store_true")

    # kb-local = create local directory-backed knowledge base
    p = sub.add_parser("kb-local", aliases=["wiki-local"], help="create a local directory-backed knowledge base")
    p.add_argument("id", help="alias used by 'kb', e.g. canteen")
    p.add_argument("--description", "--desc", default="")
    p.add_argument("--replace", action="store_true", help="update alias if it already exists")
    p.add_argument("--json", action="store_true")

    # kb-import = copy files into a local knowledge base
    p = sub.add_parser("kb-import", aliases=["wiki-import"], help="copy a file or directory into a local knowledge base")
    p.add_argument("id", help="local knowledge base alias")
    p.add_argument("source", help="file or directory to copy into the knowledge base")
    p.add_argument("--json", action="store_true")

    # kb-open/info = manage local knowledge base contents
    p = sub.add_parser("kb-open", aliases=["wiki-open"], help="open a local knowledge base directory")
    p.add_argument("id", help="local knowledge base alias")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("kb-info", aliases=["wiki-info"], help="show knowledge base details and file stats")
    p.add_argument("id", help="knowledge base alias")
    p.add_argument("--json", action="store_true")

    # kb = bind-wiki (same-source KBs per target)
    p = sub.add_parser("kb", aliases=["bind-wiki"], help="bind one or more same-source knowledge base aliases to a target")
    p.add_argument("key")
    p.add_argument("wiki", nargs="+")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--json", action="store_true")

    # kb-reindex = force rebuild local KB index
    p = sub.add_parser("kb-reindex", aliases=["wiki-reindex"], help="force rebuild local KB FTS index")
    p.add_argument("id", help="local knowledge base alias")
    p.add_argument("--json", action="store_true")

    # kb-diagnose = diagnose local KB index
    p = sub.add_parser("kb-diagnose", aliases=["wiki-diagnose"], help="diagnose local KB FTS index")
    p.add_argument("id", help="local knowledge base alias")
    p.add_argument("--query", "-q", default="", help="sample query to test against the index")
    p.add_argument("--json", action="store_true")

    # target-* = target CRUD
    p = sub.add_parser("target-show", help="show target details including triggers and knowledge bases")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("target-delete", help="delete a configured target")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("target-field", help="set an arbitrary field on a target")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("field", help="field name to set")
    p.add_argument("value", help="value to set; parsed as JSON if it looks like JSON")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("target-mode", help="set target response mode")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("mode", choices=["group_assistant", "customer_service"], help="response mode")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("target-category", help="set target business category")
    p.add_argument("key", help="target name or WeChat username")
    p.add_argument("category", choices=["user", "admin"], help="business category")
    p.add_argument("--json", action="store_true")

    # kb-enable / kb-disable / kb-delete / kb-search
    p = sub.add_parser("kb-enable", help="enable a knowledge base alias")
    p.add_argument("id", help="knowledge base alias")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("kb-disable", help="disable a knowledge base alias")
    p.add_argument("id", help="knowledge base alias")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("kb-delete", help="delete a knowledge base alias")
    p.add_argument("id", help="knowledge base alias")
    p.add_argument("--remove-files", action="store_true", help="also remove local KB directory files")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("kb-search", help="search a local knowledge base")
    p.add_argument("id", help="local knowledge base alias")
    p.add_argument("query", help="search query")
    p.add_argument("--limit", type=int, default=5, help="max results")
    p.add_argument("--json", action="store_true")


    # db decrypt lifecycle
    p = sub.add_parser("init", aliases=["cfg"], help="initialize/confirm config.json and auto-detected db_dir")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("key", aliases=["keys"], help="extract WeChat database keys from running WeChat process")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("dec", aliases=["decrypt"], help="decrypt WeChat databases")
    p.add_argument("--db", dest="target_db", help="only decrypt one relative DB, e.g. message/message_0.db")
    p.add_argument("--out-dir", help="output directory; default from config.json")
    p.add_argument("--keys-file", help="keys json path; default from config.json")
    p.add_argument("--db-dir", help="raw db_storage path; default from config.json")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("decrypt-status", help="show read-only status of keys and decrypted databases")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("setup", aliases=["bootstrap"], help="init + extract keys + decrypt all databases")
    p.add_argument("--admin", action="store_true", help="run admin_extract_and_decrypt.py one-shot helper")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("refresh", aliases=["rf"], help="refresh only enabled target message DBs")
    p.add_argument("--force", action="store_true")
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
        # -- db decrypt lifecycle --
        if cmd in ("init", "cfg"):
            return cmd_init(json_mode=args.json)
        if cmd in ("key", "keys"):
            return cmd_key(json_mode=args.json)
        if cmd in ("dec", "decrypt"):
            return cmd_dec(target_db=args.target_db, out_dir=args.out_dir, keys_file=args.keys_file, db_dir=args.db_dir, json_mode=args.json)
        if cmd == "decrypt-status":
            return cmd_decrypt_status(json_mode=args.json)
        if cmd in ("setup", "bootstrap"):
            return cmd_setup(admin=args.admin, json_mode=args.json)
        if cmd in ("refresh", "rf"):
            return cmd_refresh(force=args.force, json_mode=args.json)

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
            if not args.key:
                if args.json:
                    rows = reg.list_groups()
                    rows = [r for r in rows if r.get("status") in ("pending", "disabled") or not r.get("listen_enabled")]
                    print_json({"ok": False, "message": "missing key", "hint": "python manage_targets.py on <群名或微信ID>", "groups": rows})
                else:
                    safe_print("缺少群名或微信ID。用法：")
                    safe_print("  python manage_targets.py on \"群名\"")
                    safe_print("  python manage_targets.py on \"100003@chatroom\"")
                    safe_print("\n可启用/重新启用的群：")
                    rows = reg.list_groups()
                    rows = [r for r in rows if r.get("status") in ("pending", "disabled") or not r.get("listen_enabled")]
                    if rows:
                        print_table(rows, ["status", "listen_enabled", "name", "username"], htransform={"status": "状态", "listen_enabled": "监听", "name": "名称", "username": "微信ID"})
                    else:
                        safe_print("(empty)")
                return 2
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

        # -- trigger-list / trigger-add / trigger-remove / trigger-replace / trigger-clear --
        if cmd in ("trigger-list", "tl"):
            out = reg.get_triggers(args.key)
            if args.json:
                print_json({"ok": True, "target": out})
            else:
                safe_print("per-target triggers: %s" % (", ".join(out.get("triggers") or []) or "(empty; uses default_triggers)"))
                safe_print("default_triggers: %s" % (", ".join(out.get("default_triggers") or []) or "(empty)"))
            return 0

        if cmd in ("trigger-add", "ta"):
            out = reg.set_triggers(args.key, list(args.words), replace=False)
            _audit_event("trigger_add", target=out.get("name"), payload={"words": list(args.words)})
            if args.json:
                print_json({"ok": True, "action": "added", "target": out})
            else:
                safe_print("已添加触发词: %s -> %s" % (out.get("name"), ", ".join(args.words)))
            return 0

        if cmd in ("trigger-remove", "tr"):
            out = reg.remove_triggers(args.key, list(args.words), clear=False)
            _audit_event("trigger_remove", target=out.get("name"), payload={"words": list(args.words)})
            if args.json:
                print_json({"ok": True, "action": "removed", "target": out})
            else:
                safe_print("已删除触发词: %s -> %s" % (out.get("name"), ", ".join(args.words)))
            return 0

        if cmd in ("trigger-replace", "tp"):
            out = reg.set_triggers(args.key, list(args.words), replace=True)
            _audit_event("trigger_replace", target=out.get("name"), payload={"words": list(args.words)})
            if args.json:
                print_json({"ok": True, "action": "replaced", "target": out})
            else:
                safe_print("已替换触发词: %s -> %s" % (out.get("name"), ", ".join(args.words)))
            return 0

        if cmd in ("trigger-clear", "tc"):
            out = reg.remove_triggers(args.key, [], clear=True)
            _audit_event("trigger_clear", target=out.get("name"), payload={})
            if args.json:
                print_json({"ok": True, "action": "cleared", "target": out})
            else:
                safe_print("已清空触发词: %s" % out.get("name"))
            return 0

        # -- trigger-default-* --
        if cmd == "trigger-default-list":
            words = reg.get_default_triggers()
            if args.json:
                print_json({"ok": True, "default_triggers": words})
            else:
                safe_print("default_triggers: %s" % (", ".join(words) or "(empty)"))
            return 0

        if cmd == "trigger-default-replace":
            words = reg.set_default_triggers(list(args.words))
            if args.json:
                print_json({"ok": True, "action": "replaced", "default_triggers": words})
            else:
                safe_print("已替换默认触发词: %s" % ", ".join(words))
            return 0

        if cmd == "trigger-default-clear":
            words = reg.set_default_triggers([])
            if args.json:
                print_json({"ok": True, "action": "cleared", "default_triggers": words})
            else:
                safe_print("已清空默认触发词")
            return 0


        # -- legacy `trigger key [words] [--set|--remove|--clear]` --
        if cmd in ("trigger", "triggers", "kw", "keyword"):
            if args.clear:
                out = reg.remove_triggers(args.key, [], clear=True)
                action = "已清空触发词"
            elif args.remove:
                out = reg.remove_triggers(args.key, args.triggers, clear=False)
                action = "已删除触发词"
            elif args.triggers:
                out = reg.set_triggers(args.key, args.triggers, replace=args.replace)
                action = "已替换触发词" if args.replace else "已添加触发词"
            else:
                out = reg.get_triggers(args.key)
                action = "当前触发词"
            if args.json:
                print_json({"ok": True, "target": out, "action": action})
            else:
                safe_print("%s: %s (%s)" % (action, out.get("name"), out.get("username")))
                safe_print("per-target triggers: %s" % (", ".join(out.get("triggers") or []) or "(empty; uses default_triggers)"))
                safe_print("default_triggers: %s" % (", ".join(out.get("default_triggers") or []) or "(empty)"))
            return 0

        # -- kb-list / kbs --
        if cmd in ("kb-list", "kbs", "wiki-list"):
            rows = reg.list_knowledge_bases()
            if args.json:
                print_json({"count": len(rows), "knowledge_bases": rows})
            else:
                htransform = {
                    "id": "别名",
                    "type": "类型",
                    "enabled": "启用",
                    "knowledge_base_id": "外部ID",
                    "path": "路径",
                    "description": "说明",
                }
                print_table(rows, ["id", "type", "enabled", "knowledge_base_id", "path", "description"], htransform=htransform)
                safe_print("\n绑定示例：python manage_targets.py kb \"群名\" <别名> --replace")
                safe_print("创建示例：python manage_targets.py kb-add canteen --kid <GetNote知识库ID> --description \"食堂菜品知识库\"")
            return 0

        # -- kb-add / wiki-add --
        if cmd in ("kb-add", "wiki-add"):
            out = reg.add_knowledge_base(
                args.id,
                kb_type=args.type,
                knowledge_base_id=args.knowledge_base_id,
                path=args.path,
                description=args.description,
                executable=args.executable,
                scope=args.scope,
                limit=args.limit,
                timeout=args.timeout,
                replace=args.replace,
            )
            if args.json:
                print_json({"ok": True, "knowledge_base": out})
            else:
                safe_print("已创建/更新知识库别名: %s" % out.get("id"))
                safe_print("下一步绑定到群：python manage_targets.py kb \"群名\" %s --replace" % out.get("id"))
            return 0

        # -- kb-local / wiki-local --
        if cmd in ("kb-local", "wiki-local"):
            out = reg.create_local_kb_dir(args.id, description=args.description, replace=args.replace)
            if args.json:
                print_json({"ok": True, "knowledge_base": out})
            else:
                safe_print("已创建本地知识库: %s" % out.get("id"))
                safe_print("目录: %s" % out.get("path"))
                safe_print("\n接下来你可以:")
                safe_print("  1. 把 .md markdown 文件放入该目录（本地知识库暂时只支持 markdown 格式内容）")
                safe_print("  2. python manage_targets.py kb-import %s <文件或目录>" % out.get("id"))
                safe_print("  3. python manage_targets.py kb \"群名\" %s --replace" % out.get("id"))
                safe_print("\n或打开目录手动管理:")
                safe_print("  python manage_targets.py kb-open %s" % out.get("id"))
            return 0

        # -- kb-import / wiki-import --
        if cmd in ("kb-import", "wiki-import"):
            copied = reg.import_kb_file(args.id, args.source)
            if args.json:
                print_json({"ok": True, "copied": copied})
            else:
                safe_print("已导入 %d 个文件到知识库 '%s':" % (len(copied), args.id))
                for c in copied:
                    safe_print("  %s" % c)
                safe_print("\n绑定到群:")
                safe_print("  python manage_targets.py kb \"群名\" %s --replace" % args.id)
            return 0

        # -- kb-open / wiki-open --
        if cmd in ("kb-open", "wiki-open"):
            path = reg.open_kb_dir(args.id)
            if args.json:
                print_json({"ok": True, "path": path})
            else:
                safe_print("已打开目录: %s" % path)
            return 0

        # -- kb-info / wiki-info --
        if cmd in ("kb-info", "wiki-info"):
            info = reg.get_kb_info(args.id)
            if not info:
                if args.json:
                    print_json({"ok": False, "message": "知识库不存在: %s" % args.id})
                else:
                    safe_print("知识库不存在: %s" % args.id)
                return 1
            if args.json:
                print_json({"ok": True, "knowledge_base": info})
            else:
                safe_print("[%s] %s" % (info.get("type"), info.get("id")))
                safe_print("说明: %s" % info.get("description", ""))
                if info.get("type") == "local":
                    safe_print("路径: %s" % info.get("path"))
                    safe_print("目录存在: %s" % ("是" if info.get("exists") else "否"))
                    safe_print("文档文件数: %d" % info.get("file_count", 0))
                    safe_print("总文件数: %d" % info.get("total_files", 0))
                elif info.get("type") == "getnote":
                    safe_print("外部ID: %s" % info.get("knowledge_base_id"))
                    safe_print("可执行文件: %s" % (info.get("executable") or "默认"))
                elif info.get("type") == "hook":
                    safe_print("外部ID: %s" % (info.get("knowledge_base_id") or "(未设置)"))
                    safe_print("适配器: %s" % (info.get("executable") or info.get("cli") or "(未设置)"))
                    safe_print("调用约定: 环境变量 KB_QUERY/KB_ID/KB_LIMIT，stdout 输出 JSON results")
            return 0

        if cmd in ("kb", "bind-wiki"):
            out = reg.bind_wiki(args.key, args.wiki, replace=args.replace)
            if args.json:
                print_json(out)
            else:
                safe_print("knowledge_bases: %s" % ",".join(out.get("knowledge_bases") or []))
            return 0

        # -- kb-reindex / wiki-reindex --
        if cmd in ("kb-reindex", "wiki-reindex"):
            info = reg.rebuild_kb_index(args.id)
            if args.json:
                print_json({"ok": True, "knowledge_base": info})
            else:
                safe_print("已重建知识库索引: %s" % info.get("id"))
                safe_print("索引文件: %s" % info.get("index_path"))
                safe_print("文档数: %d" % info.get("doc_count", 0))
            return 0

        # -- kb-diagnose / wiki-diagnose --
        if cmd in ("kb-diagnose", "wiki-diagnose"):
            q = getattr(args, "query", "") or ""
            info = reg.diagnose_local_kb(args.id, query=q)
            if args.json:
                print_json({"ok": True, **info})
            else:
                safe_print("诊断结果: %s" % info.get("kb_id"))
                safe_print("索引文件: %s" % info.get("index_path"))
                safe_print("索引存在: %s" % ("是" if info.get("index_exists") else "否"))
                safe_print("schema版本: %s" % info.get("schema_version"))
                safe_print("文档数: %d" % info.get("doc_count", 0))
                if info.get("sample_fts_query"):
                    safe_print("样例FTS查询: %s" % info.get("sample_fts_query"))
                    safe_print("样例命中数: %d" % len(info.get("sample_hits", [])))
            return 0

        # -- target-show / target-delete / target-field / target-mode / target-category --
        if cmd == "target-show":
            info = reg.inspect_target(args.key)
            if args.json:
                print_json({"ok": True, **info})
            else:
                safe_print("kind: %s" % info.get("kind"))
                if info.get("kind") == "target":
                    t = info.get("target") or {}
                    safe_print("name: %s" % t.get("name"))
                    safe_print("username: %s" % t.get("username"))
                    safe_print("enabled: %s" % t.get("listen_enabled"))
                    safe_print("category: %s" % t.get("category", "user"))
                    safe_print("mode: %s" % t.get("mode", "group_assistant"))
                    safe_print("knowledge_bases: %s" % ", ".join(str(k) for k in (t.get("knowledge_bases") or [])))
                    safe_print("admin_senders: %s" % ", ".join(str(s) for s in (t.get("admin_senders") or [])))
                    safe_print("dedicated_agent_instance_id: %s" % (t.get("dedicated_agent_instance_id") or ""))
                    safe_print("effective_triggers: %s" % ", ".join(info.get("effective_triggers") or []))
                elif info.get("kind") == "candidate":
                    c = info.get("candidate") or {}
                    safe_print("name: %s" % c.get("name"))
                    safe_print("username: %s" % c.get("username"))
                    safe_print("status: %s" % c.get("status"))
                    safe_print("effective_triggers: %s" % ", ".join(info.get("effective_triggers") or []))
                safe_print("default_triggers: %s" % ", ".join(info.get("default_triggers") or []))
            return 0

        if cmd == "target-delete":
            if not args.yes:
                if args.json:
                    print_json({"ok": False, "message": "add --yes to delete target"})
                else:
                    safe_print("add --yes to delete target")
                return 2
            out = reg.delete_target(args.key)
            _audit_event("target_delete", target=out.get("name"), payload={"username": out.get("username")})
            if args.json:
                print_json({"ok": True, "action": "deleted", "target": out})
            else:
                safe_print("已删除目标: %s (%s)" % (out.get("name"), out.get("username")))
            return 0

        if cmd == "target-field":
            raw = args.value
            stripped = raw.strip()
            if not stripped:
                value = raw
            elif stripped[0] in "{[\"" or stripped in ("true", "false", "null") or stripped[0].isdigit() or stripped[0] == "-":
                try:
                    value = json.loads(stripped)
                except Exception:
                    value = raw
            else:
                value = raw
            out = reg.set_target_field(args.key, args.field, value)
            _audit_event("target_field", target=out.get("name"), payload={"field": args.field, "value": value})
            if args.json:
                print_json({"ok": True, "action": "set", "field": args.field, "target": out})
            else:
                safe_print("已设置 %s.%s = %s" % (out.get("name"), args.field, value))
            return 0

        if cmd == "target-mode":
            out = reg.set_target_mode_bundle(args.key, args.mode)
            _audit_event("target_mode", target=out.get("name"), payload={"mode": args.mode})
            if args.json:
                print_json({"ok": True, "action": "set", "mode": args.mode, "target": out})
            else:
                safe_print("已设置 %s mode = %s" % (out.get("name"), args.mode))
            return 0

        if cmd == "target-category":
            out = reg.set_category(args.key, args.category)
            _audit_event("target_category", target=out.get("name"), payload={"category": args.category})
            if args.json:
                print_json({"ok": True, "action": "set", "category": args.category, "target": out})
            else:
                safe_print("已设置 %s category = %s" % (out.get("name"), args.category))
            return 0

        # -- kb-enable / kb-disable / kb-delete / kb-search --
        if cmd == "kb-enable":
            out = reg.set_knowledge_base_enabled(args.id, True)
            if args.json:
                print_json({"ok": True, "action": "enabled", "knowledge_base": out})
            else:
                safe_print("已启用知识库: %s" % out.get("id"))
            return 0

        if cmd == "kb-disable":
            out = reg.set_knowledge_base_enabled(args.id, False)
            if args.json:
                print_json({"ok": True, "action": "disabled", "knowledge_base": out})
            else:
                safe_print("已禁用知识库: %s" % out.get("id"))
            return 0

        if cmd == "kb-delete":
            if not args.yes:
                if args.json:
                    print_json({"ok": False, "message": "add --yes to delete knowledge base"})
                else:
                    safe_print("add --yes to delete knowledge base")
                return 2
            out = reg.delete_knowledge_base(args.id, remove_files=args.remove_files)
            _audit_event("kb_delete", payload={"id": args.id, "remove_files": args.remove_files})
            if args.json:
                print_json({"ok": True, "action": "deleted", "knowledge_base": out})
            else:
                safe_print("已删除知识库: %s" % out.get("id"))
            return 0

        if cmd == "kb-search":
            try:
                result = reg.search_local_kb(args.id, args.query, limit=args.limit)
            except ValueError as e:
                if args.json:
                    print_json({"ok": False, "message": str(e)})
                else:
                    safe_print("ERROR: %s" % e)
                return 1
            hits = result.get("hits") or [] if isinstance(result, dict) else []
            if args.json:
                print_json({"ok": True, "knowledge_base": args.id, "query": args.query, "results": result})
            else:
                safe_print("知识库 '%s' 查询 '%s' 返回 %d 条结果:" % (args.id, args.query, len(hits)))
                for r in hits:
                    safe_print("  - %s (score=%s) %s" % (r.get("rel_path", "?"), r.get("score", "?"), (r.get("snippet") or "")[:80]))
            return 0



        safe_print("Unknown command: %s" % cmd)
        return 1
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
