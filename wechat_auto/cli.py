#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified CLI entry-point for wechat_auto package.

Mirrors the commands available in manage_targets.py but exposes them
through a pip-installable console script so external agents can run:
    wechat-auto list-targets
    wechat-auto add-target --name Alice --type user
"""
import argparse
import sys
from pathlib import Path

# Ensure vendored modules on sys.path when installed as package
_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "wechat-decrypt"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from manage_targets import main as _manage_main  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wechat-auto",
        description="WeChat automation CLI (target management, send, reply)",
    )
    sub = p.add_subparsers(dest="command", help="Available commands")

    # list-targets
    sub.add_parser("list-targets", help="List configured chat targets")

    # add-target
    add_p = sub.add_parser("add-target", help="Add a new chat target")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--type", required=True, choices=["user", "group"])
    add_p.add_argument("--alias", default=None)
    add_p.add_argument("--note", default=None)
    add_p.add_argument("--strategy", default="auto",
                       choices=["auto", "uia", "ocr", "physical"])

    # remove-target
    rm_p = sub.add_parser("remove-target", help="Remove a target by name")
    rm_p.add_argument("--name", required=True)

    # set-default
    def_p = sub.add_parser("set-default", help="Set default target")
    def_p.add_argument("--name", required=True)

    # test-send
    ts_p = sub.add_parser("test-send", help="Send a test message")
    ts_p.add_argument("--text", default="Hello from wechat-auto CLI")
    ts_p.add_argument("--target", default=None)
    ts_p.add_argument("--mode", default="auto",
                      choices=["auto", "uia", "ocr", "physical", "backend"])

    # version
    sub.add_parser("version", help="Show package version")

    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        from wechat_auto import __version__
        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    # Bridge to manage_targets CLI by rebuilding argv
    cmd_map = {
        "list-targets": ["--list-targets"],
        "add-target": ["--add-target", args.name, args.type,
                       args.alias or "", args.note or "", args.strategy],
        "remove-target": ["--remove-target", args.name],
        "set-default": ["--set-default", args.name],
        "test-send": ["--test-send", args.text, args.target or "", args.mode],
    }
    if args.command not in cmd_map:
        parser.print_help()
        return 1
    return _manage_main(cmd_map[args.command])


if __name__ == "__main__":
    sys.exit(main())
