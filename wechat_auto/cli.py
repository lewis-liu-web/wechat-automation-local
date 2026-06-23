#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified CLI entry-point for wechat_auto package.

Bridges the pip-installed console script `wechat-auto` (sub-command style)
to the manage_targets.py sub-command API, and provides a direct agent
control sub-command group that talks to the local control_api HTTP surface.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Ensure wechat-decrypt vendored modules are on sys.path when installed as package
_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "wechat-decrypt"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import control_client  # noqa: E402


def _manage_main(cmd):
    from manage_targets import main as _mt_main
    return _mt_main(cmd)


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------
def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _print_rows(rows: Iterable[Dict[str, Any]], fields: List[str]) -> None:
    rows = list(rows)
    if not rows:
        print("(empty)")
        return
    widths = [max(len(str(r.get(f, ""))) for r in rows + [{f: f}]) for f in fields]
    print("  ".join(f.ljust(widths[i]) for i, f in enumerate(fields)))
    for r in rows:
        print("  ".join(str(r.get(f, "")).ljust(widths[i]) for i, f in enumerate(fields)))


# ------------------------------------------------------------------
# Parser helpers (keep alias parsers identical to primary commands)
# ------------------------------------------------------------------
def _add_enable_parser(sub, name: str, help_text: str):
    p = sub.add_parser(name, help=help_text)
    p.add_argument("key", nargs="?", help="group name or username; omit to show pending")
    p.add_argument("--wiki", action="append", default=[], help="knowledge base id; can repeat")
    p.add_argument("--json", action="store_true")
    return p


def _add_disable_parser(sub, name: str, help_text: str):
    p = sub.add_parser(name, help=help_text)
    p.add_argument("key", help="group name or username")
    p.add_argument("--json", action="store_true")
    return p


def _add_reenable_parser(sub, name: str, help_text: str):
    p = sub.add_parser(name, help=help_text)
    p.add_argument("key", help="group name or username")
    p.add_argument("--json", action="store_true")
    return p


def _add_kb_add_parser(sub, name: str, help_text: str):
    p = sub.add_parser(name, help=help_text)
    p.add_argument("id", help="alias used by 'kb', e.g. canteen or workdocs")
    p.add_argument("--type", choices=["getnote", "local", "hook"], default="getnote")
    p.add_argument("--kid", "--knowledge-base-id", dest="knowledge_base_id",
                   help="external provider knowledge base id")
    p.add_argument("--path", help="local wiki path for --type local")
    p.add_argument("--description", "--desc", dest="description", default="")
    p.add_argument("--executable", help="adapter executable path for --type hook/getnote")
    p.add_argument("--scope", default="scene")
    p.add_argument("--limit", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--replace", action="store_true", help="update alias if it already exists")
    p.add_argument("--json", action="store_true")
    return p


def _add_decrypt_parser(sub, name: str, help_text: str):
    p = sub.add_parser(name, help=help_text)
    p.add_argument("--db", help="only decrypt one relative DB, e.g. message/message_0.db")
    p.add_argument("--out-dir", help="output directory; default from config.json")
    p.add_argument("--keys-file", help="keys json path; default from config.json")
    p.add_argument("--db-dir", help="raw db_storage path; default from config.json")
    p.add_argument("--json", action="store_true")
    return p


# ------------------------------------------------------------------
# Agent subcommand parser
# ------------------------------------------------------------------
def _build_agent_parser(sub) -> argparse.ArgumentParser:
    agent = sub.add_parser("agent", help="Control the Hermes/agent pool via control_api")
    agent_sub = agent.add_subparsers(dest="agent_command", help="Agent commands")

    def _agent_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = agent_sub.add_parser(name, help=help_text)
        p.add_argument("--base-url", default=control_client.DEFAULT_BASE_URL,
                       help="control_api base URL (default: %s)" % control_client.DEFAULT_BASE_URL)
        p.add_argument("--json", action="store_true")
        return p

    _agent_cmd("profiles", "List discovered Hermes profiles")
    _agent_cmd("instances", "List registered agent instances")

    p = _agent_cmd("register", "Register a new agent instance")
    p.add_argument("--id", required=True, help="instance id")
    p.add_argument("--provider", required=True, choices=["hermes", "echo"])
    p.add_argument("--profile", help="Hermes profile name (required for provider=hermes)")
    p.add_argument("--label")
    p.add_argument("--model")
    p.add_argument("--toolsets")
    p.add_argument("--hermes-home", help="Hermes installation directory")
    p.add_argument("--cli-path", help="Agent CLI executable path")

    _agent_cmd("pool", "Show agent pool status")

    p = _agent_cmd("on", "Put an agent instance on duty")
    p.add_argument("instance_id", help="instance id")

    p = _agent_cmd("off", "Take an agent instance off duty")
    p.add_argument("instance_id", help="instance id")

    p = _agent_cmd("loop", "Control the async processing loop")
    p.add_argument("action", choices=["status", "start", "stop"])
    p.add_argument("--instance-id", help="instance id(s) to start loop with, comma-separated")

    p = _agent_cmd("health", "Check agent provider health")
    p.add_argument("--provider", choices=["echo", "hermes"], default="hermes")
    p.add_argument("--instance-id")

    p = _agent_cmd("jobs", "List agent jobs")
    p.add_argument("--status")
    p.add_argument("--group-key")
    p.add_argument("--limit", type=int, default=50)

    p = _agent_cmd("job", "Show a single agent job")
    p.add_argument("job_id", type=int)

    p = _agent_cmd("job-test", "Create a test agent job")
    p.add_argument("--prompt", required=True)
    p.add_argument("--provider", choices=["echo", "hermes"], default="echo")
    p.add_argument("--target")
    p.add_argument("--sender")
    p.add_argument("--priority", type=int)
    p.add_argument("--task-type")

    p = _agent_cmd("job-dismiss", "Dismiss a failed/timed-out job")
    p.add_argument("job_id", type=int)

    p = _agent_cmd("job-retry-send", "Retry sending a completed job's result")
    p.add_argument("job_id", type=int)

    p = _agent_cmd("job-recover", "Recover a late agent result")
    p.add_argument("job_id", type=int)
    p.add_argument("--send", action="store_true", help="also send the recovered result to WeChat")

    p = _agent_cmd("step", "Run one dispatcher/reconciler/sender step")
    p.add_argument("step", choices=["dispatcher", "reconciler", "sender"])
    p.add_argument("--instance-id")
    p.add_argument("--provider")
    p.add_argument("--limit", type=int, default=10)

    return agent


# ------------------------------------------------------------------
# Main parser
# ------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wechat-auto",
        description="WeChat automation CLI – target management, monitor control, and agent pool",
    )
    sub = p.add_subparsers(dest="command", help="Available commands")

    # --- Monitor / process control ---
    for name in ("start", "stop", "restart", "status"):
        sp = sub.add_parser(name, help=f"{name} the WeChat monitor background process")
        sp.add_argument("--json", action="store_true")

    # scan = discover
    scan_p = sub.add_parser("scan", help="Scan new chats (alias: discover)")
    scan_p.add_argument("--include-contacts", action="store_true")
    scan_p.add_argument("--dry-run", action="store_true")
    scan_p.add_argument("--json", action="store_true")
    discover_p = sub.add_parser("discover", help="Alias for scan")
    discover_p.add_argument("--include-contacts", action="store_true")
    discover_p.add_argument("--dry-run", action="store_true")
    discover_p.add_argument("--json", action="store_true")

    # ls = list
    ls_p = sub.add_parser("ls", help="List groups with status (alias: list)")
    ls_p.add_argument("--kind", choices=["all", "pending", "enabled", "disabled"], default="all")
    ls_p.add_argument("--json", action="store_true")
    list_p = sub.add_parser("list", help="Alias for ls")
    list_p.add_argument("--kind", choices=["all", "pending", "enabled", "disabled"], default="all")
    list_p.add_argument("--json", action="store_true")

    # --- Enable / disable / reenable ---
    on_p = sub.add_parser("on", help="Enable a target (alias: enable)")
    on_p.add_argument("key", nargs="?", help="group name or username; omit to show pending")
    on_p.add_argument("--wiki", action="append", default=[], help="knowledge base id; can repeat")
    on_p.add_argument("--json", action="store_true")
    _add_enable_parser(sub, "enable", "Alias for on")

    off_p = sub.add_parser("off", help="Disable a target (alias: disable)")
    off_p.add_argument("key")
    off_p.add_argument("--json", action="store_true")
    _add_disable_parser(sub, "disable", "Alias for off")

    re_p = sub.add_parser("re", help="Re-enable a target (alias: reenable)")
    re_p.add_argument("key")
    re_p.add_argument("--json", action="store_true")
    _add_reenable_parser(sub, "reenable", "Alias for re")

    # --- Triggers ---
    tp = sub.add_parser("trigger-list", aliases=["tl"], help="List trigger keywords for a target")
    tp.add_argument("key")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("trigger-add", aliases=["ta"], help="Add trigger keywords to a target")
    tp.add_argument("key")
    tp.add_argument("words", nargs="+")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("trigger-remove", aliases=["tr"], help="Remove trigger keywords from a target")
    tp.add_argument("key")
    tp.add_argument("words", nargs="+")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("trigger-replace", aliases=["tp"], help="Replace all per-target trigger keywords")
    tp.add_argument("key")
    tp.add_argument("words", nargs="+")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("trigger-clear", aliases=["tc"], help="Clear per-target trigger keywords")
    tp.add_argument("key")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("trigger", aliases=["triggers", "kw", "keyword"],
                        help="Legacy trigger alias; prefer trigger-list/add/remove/replace/clear")
    tp.add_argument("key")
    tp.add_argument("triggers", nargs="*")
    tp.add_argument("--set", dest="replace", action="store_true")
    tp.add_argument("--remove", "--rm", action="store_true")
    tp.add_argument("--clear", action="store_true")
    tp.add_argument("--json", action="store_true")

    # --- Default triggers ---
    tp = sub.add_parser("trigger-default-list", help="List global default trigger keywords")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("trigger-default-replace", help="Replace global default trigger keywords")
    tp.add_argument("words", nargs="+")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("trigger-default-clear", help="Clear global default trigger keywords")
    tp.add_argument("--json", action="store_true")

    # --- Knowledge bases ---
    kb_list_p = sub.add_parser("kb-list", aliases=["kbs", "wiki-list"], help="List knowledge base aliases")
    kb_list_p.add_argument("--json", action="store_true")

    _add_kb_add_parser(sub, "kb-add", "Add/update KB alias (alias: wiki-add)")
    _add_kb_add_parser(sub, "wiki-add", "Alias for kb-add")

    kb_local = sub.add_parser("kb-local", aliases=["wiki-local"], help="Create local KB")
    kb_local.add_argument("id")
    kb_local.add_argument("--description", "--desc", dest="description", default="")
    kb_local.add_argument("--replace", action="store_true")
    kb_local.add_argument("--json", action="store_true")

    kb_imp = sub.add_parser("kb-import", aliases=["wiki-import"], help="Import files into a local KB")
    kb_imp.add_argument("id")
    kb_imp.add_argument("source")
    kb_imp.add_argument("--json", action="store_true")

    kb_open = sub.add_parser("kb-open", aliases=["wiki-open"], help="Open a local KB directory")
    kb_open.add_argument("id")
    kb_open.add_argument("--json", action="store_true")

    kb_info = sub.add_parser("kb-info", aliases=["wiki-info"], help="Show KB details")
    kb_info.add_argument("id")
    kb_info.add_argument("--json", action="store_true")

    kb = sub.add_parser("kb", aliases=["bind-wiki"],
                        help="Bind same-source knowledge base aliases to a target")
    kb.add_argument("key")
    kb.add_argument("wiki", nargs="+")
    kb.add_argument("--replace", action="store_true")
    kb.add_argument("--json", action="store_true")

    kb_reindex = sub.add_parser("kb-reindex", aliases=["wiki-reindex"], help="Rebuild local KB FTS index")
    kb_reindex.add_argument("id")
    kb_reindex.add_argument("--json", action="store_true")

    kb_diagnose = sub.add_parser("kb-diagnose", aliases=["wiki-diagnose"], help="Diagnose local KB FTS index")
    kb_diagnose.add_argument("id")
    kb_diagnose.add_argument("--query", "-q", default="")
    kb_diagnose.add_argument("--json", action="store_true")

    kb_enable = sub.add_parser("kb-enable", help="Enable a knowledge base alias")
    kb_enable.add_argument("id")
    kb_enable.add_argument("--json", action="store_true")

    kb_disable = sub.add_parser("kb-disable", help="Disable a knowledge base alias")
    kb_disable.add_argument("id")
    kb_disable.add_argument("--json", action="store_true")

    kb_delete = sub.add_parser("kb-delete", help="Delete a knowledge base alias")
    kb_delete.add_argument("id")
    kb_delete.add_argument("--remove-files", action="store_true")
    kb_delete.add_argument("--yes", action="store_true", required=True,
                           help="required confirmation; deletion commands require --yes")
    kb_delete.add_argument("--json", action="store_true")

    kb_search = sub.add_parser("kb-search", help="Search a local knowledge base")
    kb_search.add_argument("id")
    kb_search.add_argument("query")
    kb_search.add_argument("--limit", type=int, default=5)
    kb_search.add_argument("--json", action="store_true")

    # --- Targets ---
    tp = sub.add_parser("target-show", help="Show target or pending candidate details")
    tp.add_argument("key")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("target-delete", help="Delete a configured target")
    tp.add_argument("key")
    tp.add_argument("--yes", action="store_true", required=True,
                    help="required confirmation; deletion commands require --yes")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("target-field", help="Set an arbitrary target field")
    tp.add_argument("key")
    tp.add_argument("field")
    tp.add_argument("value")
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("target-mode", help="Set target reply mode")
    tp.add_argument("key")
    tp.add_argument("mode", choices=["group_assistant", "customer_service"])
    tp.add_argument("--json", action="store_true")

    tp = sub.add_parser("target-category", help="Set target category")
    tp.add_argument("key")
    tp.add_argument("category", choices=["user", "admin"])
    tp.add_argument("--json", action="store_true")

    # --- DB lifecycle ---
    init_p = sub.add_parser("init", aliases=["cfg"], help="Init config.json")
    init_p.add_argument("--json", action="store_true")

    key_p = sub.add_parser("key", aliases=["keys"], help="Extract DB keys")
    key_p.add_argument("--json", action="store_true")

    _add_decrypt_parser(sub, "dec", "Decrypt DBs (alias: decrypt)")
    _add_decrypt_parser(sub, "decrypt", "Alias for dec")

    setup_p = sub.add_parser("setup", aliases=["bootstrap"], help="init+keys+decrypt")
    setup_p.add_argument("--admin", action="store_true")
    setup_p.add_argument("--json", action="store_true")

    refresh_p = sub.add_parser("refresh", aliases=["rf"], help="Refresh enabled targets")
    refresh_p.add_argument("--force", action="store_true")
    refresh_p.add_argument("--json", action="store_true")

    # --- Decrypt status ---
    ds_p = sub.add_parser("decrypt-status", help="Show decrypt/key status (read-only, no keys printed)")
    ds_p.add_argument("--json", action="store_true")

    # --- Agent ---
    _build_agent_parser(sub)

    # --- Info ---
    sub.add_parser("version", help="Show package version")

    return p


# ------------------------------------------------------------------
# Agent command dispatch
# ------------------------------------------------------------------
def _agent_main(args: argparse.Namespace) -> int:
    try:
        cmd = args.agent_command
        base_url = args.base_url

        if cmd == "profiles":
            data = control_client.discover_hermes_profiles(base_url=base_url)
            if args.json:
                _print_json({"profiles": data})
            else:
                _print_rows(data, ["id", "provider", "label", "profile", "model"])
            return 0

        if cmd == "instances":
            data = control_client.list_agent_instances(base_url=base_url)
            if args.json:
                _print_json({"instances": data})
            else:
                _print_rows(data, ["id", "provider", "label", "on_duty", "status"])
            return 0

        if cmd == "register":
            if args.provider == "hermes" and not args.profile:
                print("ERROR: --profile is required when provider=hermes", file=sys.stderr)
                return 2
            instance: Dict[str, Any] = {"id": args.id, "provider": args.provider}
            for opt in ("profile", "label", "model", "toolsets", "hermes_home", "cli_path"):
                val = getattr(args, opt)
                if val is not None:
                    instance[opt] = val
            data = control_client.register_agent_instance(instance, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("registered instance: %s" % data.get("id", args.id))
            return 0

        if cmd == "pool":
            data = control_client.agent_pool_status(base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("instances: %s" % data.get("instances", []))
                print("running: %s" % data.get("running", False))
            return 0

        if cmd == "on":
            data = control_client.agent_instance_on_duty(args.instance_id, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("on-duty: %s" % args.instance_id)
            return 0

        if cmd == "off":
            data = control_client.agent_instance_off_duty(args.instance_id, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("off-duty: %s" % args.instance_id)
            return 0

        if cmd == "loop":
            if args.action == "status":
                data = control_client.async_loop_status(base_url=base_url)
            elif args.action == "start":
                data = control_client.start_async_loop(instance_id=args.instance_id, base_url=base_url)
            else:  # stop
                data = control_client.stop_async_loop(base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("loop %s: %s" % (args.action, data))
            return 0

        if cmd == "health":
            data = control_client.agent_provider_health(
                provider=args.provider, instance_id=args.instance_id, base_url=base_url
            )
            if args.json:
                _print_json(data)
            else:
                print("health: %s" % data)
            return 0

        if cmd == "jobs":
            data = control_client.agent_jobs(
                status=args.status, group_key=args.group_key, limit=args.limit, base_url=base_url
            )
            if args.json:
                _print_json({"jobs": data})
            else:
                _print_rows(data, ["id", "status", "task_type", "priority", "group_key"])
            return 0

        if cmd == "job":
            data = control_client.get_agent_job(args.job_id, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("job %s: %s" % (args.job_id, data))
            return 0

        if cmd == "job-test":
            data = control_client.create_agent_test_job(
                prompt=args.prompt,
                provider=args.provider,
                target=args.target,
                sender=args.sender,
                priority=args.priority,
                task_type=args.task_type,
                base_url=base_url,
            )
            if args.json:
                _print_json(data)
            else:
                print("created test job: %s" % data)
            return 0

        if cmd == "job-dismiss":
            data = control_client.dismiss_job(args.job_id, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("dismissed job %s" % args.job_id)
            return 0

        if cmd == "job-retry-send":
            data = control_client.retry_job_send(args.job_id, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("retry-send job %s" % args.job_id)
            return 0

        if cmd == "job-recover":
            data = control_client.recover_job_result(args.job_id, send=args.send, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("recovered job %s" % args.job_id)
            return 0

        if cmd == "step":
            if args.step == "dispatcher":
                data = control_client.run_dispatcher_once(
                    instance_id=args.instance_id, provider=args.provider, base_url=base_url
                )
            elif args.step == "reconciler":
                data = control_client.run_reconciler_once(limit=args.limit, base_url=base_url)
            else:  # sender
                data = control_client.run_sender_once(limit=args.limit, base_url=base_url)
            if args.json:
                _print_json(data)
            else:
                print("step %s: %s" % (args.step, data))
            return 0

        print("ERROR: unknown agent subcommand: %s" % cmd, file=sys.stderr)
        return 2
    except control_client.ControlAPIError as e:
        msg = str(e)
        if "Connection refused" in msg or "No connection could be made" in msg:
            print("ERROR: control_api 未连接，请先运行 python wechat-decrypt/control_api.py 或 start_console.ps1", file=sys.stderr)
        else:
            print("ERROR: %s" % msg, file=sys.stderr)
        return 1
    except Exception as e:  # pragma: no cover
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


# ------------------------------------------------------------------
# Bridge dispatch
# ------------------------------------------------------------------
def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd_name = getattr(args, "command", None)

    if cmd_name == "version":
        from wechat_auto import __version__
        print(__version__)
        return 0
    if cmd_name is None:
        parser.print_help()
        return 0

    if cmd_name == "agent":
        return _agent_main(args)

    cmd: List[str] = [cmd_name]

    if cmd_name in ("on", "enable"):
        key = getattr(args, "key", None)
        if key:
            cmd.append(key)
        for w in getattr(args, "wiki", []):
            cmd += ["--wiki", w]

    elif cmd_name in ("off", "disable", "re", "reenable"):
        cmd.append(getattr(args, "key", ""))

    elif cmd_name in ("kb-add", "wiki-add"):
        cmd.append(args.id)
        if args.type != "getnote":
            cmd += ["--type", args.type]
        if args.knowledge_base_id:
            cmd += ["--kid", args.knowledge_base_id]
        if args.path:
            cmd += ["--path", args.path]
        if args.description:
            cmd += ["--description", args.description]
        if args.executable:
            cmd += ["--executable", args.executable]
        if args.scope != "scene":
            cmd += ["--scope", args.scope]
        if args.limit is not None:
            cmd += ["--limit", str(args.limit)]
        if args.timeout is not None:
            cmd += ["--timeout", str(args.timeout)]
        if args.replace:
            cmd.append("--replace")

    elif cmd_name in ("kb-local", "wiki-local"):
        cmd.append(args.id)
        if args.description:
            cmd += ["--description", args.description]
        if args.replace:
            cmd.append("--replace")

    elif cmd_name in ("kb-import", "wiki-import"):
        cmd += [args.id, args.source]

    elif cmd_name in ("kb-open", "wiki-open", "kb-info", "wiki-info",
                      "kb-enable", "kb-disable", "kb-reindex", "wiki-reindex"):
        cmd.append(args.id)

    elif cmd_name in ("kb-delete",):
        cmd.append(args.id)
        if getattr(args, "remove_files", False):
            cmd.append("--remove-files")
        cmd.append("--yes")
    elif cmd_name in ("kb-search",):
        cmd += [args.id, args.query]
        if args.limit != 5:
            cmd += ["--limit", str(args.limit)]

    elif cmd_name in ("kb", "bind-wiki"):
        cmd.append(args.key)
        for w in args.wiki:
            cmd.append(w)
        if args.replace:
            cmd.append("--replace")

    elif cmd_name in ("kb-diagnose", "wiki-diagnose"):
        cmd.append(args.id)
        if args.query:
            cmd += ["--query", args.query]

    elif cmd_name in ("scan", "discover"):
        if getattr(args, "include_contacts", False):
            cmd.append("--include-contacts")
        if getattr(args, "dry_run", False):
            cmd.append("--dry-run")

    elif cmd_name in ("ls", "list"):
        kind = getattr(args, "kind", "all")
        if kind != "all":
            cmd += ["--kind", kind]

    elif cmd_name in ("trigger-list", "tl"):
        cmd.append(args.key)

    elif cmd_name in ("trigger-add", "ta", "trigger-remove", "tr",
                      "trigger-replace", "tp", "trigger-default-replace"):
        cmd.append(args.key if hasattr(args, "key") else "")
        if cmd[-1] == "":
            cmd.pop()
        for w in args.words:
            cmd.append(w)

    elif cmd_name in ("trigger-clear", "tc"):
        cmd.append(args.key)

    elif cmd_name in ("trigger", "triggers", "kw", "keyword"):
        cmd.append(args.key)
        for w in args.triggers:
            cmd.append(w)
        if args.replace:
            cmd.append("--set")
        if args.remove:
            cmd.append("--remove")
        if args.clear:
            cmd.append("--clear")

    elif cmd_name in ("dec", "decrypt"):
        if getattr(args, "db", None):
            cmd += ["--db", args.db]
        if getattr(args, "out_dir", None):
            cmd += ["--out-dir", args.out_dir]
        if getattr(args, "keys_file", None):
            cmd += ["--keys-file", args.keys_file]
        if getattr(args, "db_dir", None):
            cmd += ["--db-dir", args.db_dir]

    elif cmd_name in ("setup", "bootstrap"):
        if getattr(args, "admin", False):
            cmd.append("--admin")

    elif cmd_name in ("refresh", "rf"):
        if getattr(args, "force", False):
            cmd.append("--force")

    elif cmd_name == "target-show":
        cmd.append(args.key)

    elif cmd_name == "target-delete":
        cmd.append(args.key)
        cmd.append("--yes")

    elif cmd_name == "target-field":
        cmd += [args.key, args.field, args.value]

    elif cmd_name == "target-mode":
        cmd += [args.key, args.mode]

    elif cmd_name == "target-category":
        cmd += [args.key, args.category]

    # decrypt-status and simple commands need no extra positional args

    if getattr(args, "json", False):
        cmd.append("--json")

    return _manage_main(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
