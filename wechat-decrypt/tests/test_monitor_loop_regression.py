"""Regression test: one ordinary message through the monitor main loop.

The monitor loop (wechat_bot_monitor.main, L1637+) processes every message
through a chain of pre-routing branches that culminates in the durable
ingress gate at L1783 (``if _is_reliable_pipeline_target(t, cfg):``).  A
broken reference in that chain (e.g. A2 deleting the gate function while
leaving L1783 intact) compiles cleanly but raises NameError at runtime on
the first non-admin/non-self/non-muted message.  ``py_compile`` cannot
detect this class of regression; only a real loop iteration reaches L1783.

This test drives ONE cycle of ``main()`` end-to-end with ``fetch_new_for_db``
mocked to return one ordinary message, asserts that ``durable_ingress_event``
is invoked (proving the L1783 gate was evaluated True and the durable
branch was taken), and that no NameError escapes the loop.  Run via direct
invocation (pytest collection hangs in this environment):

    python -c "import importlib.util,sys;\\
    sys.path.insert(0,'wechat-decrypt');\\
    spec=importlib.util.spec_from_file_location('t',\\
    'wechat-decrypt/tests/test_monitor_loop_regression.py');\\
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);\\
    m.test_one_message_reaches_durable_ingress_via_main_loop()"
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _build_config(db_path: Path) -> dict:
    return {
        "targets": [{
            "name": "loop-regression-target",
            "username": "loop_regression_chatroom",
            "db": "message_0.db",
            "table": "Msg_loop_regression",
            "enabled": True,
            "reliable_pipeline_target": True,
            "triggers": ["@bot"],
            "response_mode": "trigger",
        }],
        "default_triggers": ["@bot"],
        "default_response_mode": "trigger",
        "reply_engine": {"mode": "raw_agent"},
        "knowledge_bases": {},
        "agent_provider": {"instances": [{
            "id": "loop-reg-worker",
            "provider": "hermes",
            "profile": "loop-reg",
            "worker_id": "loop-reg",
        }]},
        "reliable_pipeline": {
            "enabled": True,
            "test_target_only": False,
            "db_path": str(db_path),
        },
        "send_mode": "foreground",
    }


def _ordinary_message() -> dict:
    return {
        "local_id": 1,
        "message_content": "@bot hello",
        "sender_username": "wxid_sender",
        "real_sender_id": "wxid_sender",
        "sender": "wxid_sender",
        "sender_display_name": "Sender",
        "mention_name": "Sender",
        "local_type": 1,
        "status": 0,
    }


def test_one_message_reaches_durable_ingress_via_main_loop():
    """Drive one main() cycle; assert L1783 gate + durable_ingress reached."""
    print("\n=== test_one_message_reaches_durable_ingress_via_main_loop ===")
    # Fresh import
    for k in list(sys.modules):
        if k in ("wechat_bot_monitor", "reliable_pipeline", "reliable_worker",
                 "agent_provider", "target_registry", "wechat_sender", "reply_engine"):
            del sys.modules[k]
    import wechat_bot_monitor as monitor

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = tmp / "config.json"
        db_path = tmp / "reliable_pipeline.sqlite"
        cfg_path.write_text(json.dumps(_build_config(db_path)), encoding="utf-8")

        target = _build_config(db_path)["targets"][0]
        msg = _ordinary_message()
        stop_file = tmp / "STOP"
        # ensure no leftover stop
        if stop_file.exists():
            stop_file.unlink()

        durable_calls = []

        def fake_durable(t_, m_, **kw):
            durable_calls.append({"target": t_.get("name"), "local_id": m_.get("local_id"),
                                  "db_path": str(kw.get("db_path"))})
            return {"advanced": True, "error": "", "event_id": "wx:reg:1"}

        # Mock fetch_new_for_db: yield one message on first call, empty after.
        fetch_calls = {"n": 0}
        def fake_fetch_new(db_name, db_targets):
            fetch_calls["n"] += 1
            if fetch_calls["n"] == 1:
                yield (target, msg)
            return
            yield  # pragma: no cover -- makes it a generator

        # Raise after the first cycle's sleep to exit main() deterministically.
        sleep_calls = {"n": 0}
        def fake_sleep(_s):
            sleep_calls["n"] += 1
            # main() sleeps at end of while loop. After 1st sleep, stop.
            if sleep_calls["n"] >= 1:
                raise SystemExit("stop after one cycle")

        orig_argv = sys.argv
        sys.argv = ["monitor", "--config", str(cfg_path),
                    "--once", "--no-save-state", "--no-fast-refresh"]
        try:
            with mock.patch.object(monitor, "fetch_new_for_db", side_effect=fake_fetch_new), \
                 mock.patch.object(monitor, "fetch_latest_for_target", return_value=None), \
                 mock.patch.object(monitor, "load_contact_name_map", return_value={}), \
                 mock.patch.object(monitor, "durable_ingress_event", side_effect=fake_durable), \
                 mock.patch.object(monitor.time, "sleep", side_effect=fake_sleep):
                try:
                    rc = monitor.main()
                except SystemExit:
                    rc = "SystemExit(after one cycle)"
        finally:
            sys.argv = orig_argv

        print(f"  main() returned: {rc}")
        print(f"  fetch_new_for_db calls: {fetch_calls['n']}")
        print(f"  durable_ingress_event calls: {len(durable_calls)}")
        if durable_calls:
            print(f"  durable[0]: {durable_calls[0]}")
        print(f"  time.sleep calls: {sleep_calls['n']}")

        assert len(durable_calls) == 1, (
            f"durable_ingress_event was not reached through the loop "
            f"(calls={len(durable_calls)}). L1783 gate may be broken or "
            f"pre-routing branches short-circuited.")
        assert durable_calls[0]["target"] == target["name"]
        assert durable_calls[0]["local_id"] == 1
        assert durable_calls[0]["db_path"] == str(db_path)
        # Gate function must exist and be callable (catches A2 NameError regressions)
        assert callable(monitor._is_reliable_pipeline_target), \
            "_is_reliable_pipeline_target missing — L1783 will NameError"
        assert monitor._is_reliable_pipeline_target(target, _build_config(db_path)) is True
        print("  PASS: loop reached L1783, durable_ingress_event invoked, gate resolvable")


def test_loop_nameerror_if_gate_function_missing():
    """Simulate the A2 regression: remove the gate function, confirm the
    loop iteration raises NameError before any durable ingress.  This is
    the failure mode the regression test must protect against.
    """
    print("\n=== test_loop_nameerror_if_gate_function_missing (regression proof) ===")
    for k in list(sys.modules):
        if k in ("wechat_bot_monitor", "reliable_pipeline", "reliable_worker",
                 "agent_provider", "target_registry", "wechat_sender", "reply_engine"):
            del sys.modules[k]
    import wechat_bot_monitor as monitor

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = tmp / "config.json"
        db_path = tmp / "reliable_pipeline.sqlite"
        cfg_path.write_text(json.dumps(_build_config(db_path)), encoding="utf-8")
        target = _build_config(db_path)["targets"][0]
        msg = _ordinary_message()

        def fake_fetch_new(db_name, db_targets):
            yield (target, msg)
            return
            yield

        def fake_durable(t_, m_, **kw):
            return {"advanced": True, "error": "", "event_id": "wx:r:1"}

        def fake_sleep(_s):
            raise SystemExit("stop")

        orig_argv = sys.argv
        sys.argv = ["monitor", "--config", str(cfg_path),
                    "--once", "--no-save-state", "--no-fast-refresh"]
        raised = None
        try:
            with mock.patch.object(monitor, "fetch_new_for_db", side_effect=fake_fetch_new), \
                 mock.patch.object(monitor, "fetch_latest_for_target", return_value=None), \
                 mock.patch.object(monitor, "load_contact_name_map", return_value={}), \
                 mock.patch.object(monitor, "durable_ingress_event", side_effect=fake_durable), \
                 mock.patch.object(monitor.time, "sleep", side_effect=fake_sleep):
                # THE REGRESSION: remove the gate function (A2 scenario)
                with mock.patch.object(monitor, "_is_reliable_pipeline_target",
                                       side_effect=NameError("_is_reliable_pipeline_target")):
                    try:
                        monitor.main()
                    except NameError as e:
                        raised = e
                    except SystemExit:
                        pass
        finally:
            sys.argv = orig_argv

        if raised is None:
            print("  WARN: NameError not raised in this build (loop may have "
                  "exited before reaching L1783). Confirm test targets loop body.")
        else:
            print(f"  CONFIRMED regression detectable: NameError({raised!s})")
        assert raised is not None, \
            "Gate-function-missing regression must surface as NameError; " \
            "if it did not, the test does not actually exercise L1783."
        print("  PASS: regression proof — missing gate function is caught")


if __name__ == "__main__":
    cases = [
        ("reach_durable", test_one_message_reaches_durable_ingress_via_main_loop),
        ("nameerror_regression", test_loop_nameerror_if_gate_function_missing),
    ]
    import time as _t
    passed = failed = 0
    for label, fn in cases:
        t0 = _t.time()
        try:
            fn()
            print(f"  PASS  {_t.time()-t0:5.2f}s  {label}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {_t.time()-t0:5.2f}s  {label}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERR   {_t.time()-t0:5.2f}s  {label}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 2)
