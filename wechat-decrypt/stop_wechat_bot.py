#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gracefully stop wechat_bot_monitor.py by creating the configured stop file."""
from pathlib import Path
import json
import time
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "wechat_bot_targets.json"
try:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
except Exception:
    cfg = {}
stop_value = cfg.get("stop_file") or "wechat_bot_monitor.stop"
stop_path = Path(stop_value)
if not stop_path.is_absolute():
    stop_path = ROOT / stop_path
stop_path.write_text("stop requested at %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
print("stop signal written:", stop_path)
print("monitor will exit after current cycle/subagent reply finishes (normally within poll_interval, except LLM call may take longer).")
