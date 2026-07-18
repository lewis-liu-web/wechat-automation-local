"""Tests for wechat_auto.cli argv forwarding to manage_targets.main()."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import wechat_auto.cli as bridge


def _run(capsys, argv):
    with patch.object(bridge, "_manage_main", return_value=0) as mocked:
        code = bridge.main(argv)
        captured = capsys.readouterr()
        return code, mocked, captured


def test_decrypt_db_forwarding(capsys):
    code, mocked, _ = _run(capsys, ["decrypt", "--db", "message/message_0.db"])
    assert code == 0
    assert mocked.call_args[0][0] == ["decrypt", "--db", "message/message_0.db"]


def test_enable_alias_with_key_and_wiki(capsys):
    code, mocked, _ = _run(capsys, ["enable", "wxid_a", "--wiki", "kb1", "--wiki", "kb2"])
    assert code == 0
    assert mocked.call_args[0][0] == ["enable", "wxid_a", "--wiki", "kb1", "--wiki", "kb2"]


def test_disable_alias_passes_key(capsys):
    code, mocked, _ = _run(capsys, ["disable", "wxid_a"])
    assert code == 0
    assert mocked.call_args[0][0] == ["disable", "wxid_a"]


def test_reenable_alias_passes_key(capsys):
    code, mocked, _ = _run(capsys, ["reenable", "wxid_a"])
    assert code == 0
    assert mocked.call_args[0][0] == ["reenable", "wxid_a"]


def test_wiki_add_alias_forwarding(capsys):
    code, mocked, _ = _run(capsys, ["wiki-add", "kb", "--kid", "kid", "--replace"])
    assert code == 0
    assert mocked.call_args[0][0] == ["wiki-add", "kb", "--kid", "kid", "--replace"]


def test_trigger_add_forwarding(capsys):
    code, mocked, _ = _run(capsys, ["trigger-add", "wxid_a", "#ask", "--json"])
    assert code == 0
    assert mocked.call_args[0][0] == ["trigger-add", "wxid_a", "#ask", "--json"]


def test_kb_diagnose_forwarding(capsys):
    code, mocked, _ = _run(capsys, ["kb-diagnose", "scene.a", "-q", "押金", "--json"])
    assert code == 0
    assert mocked.call_args[0][0] == ["kb-diagnose", "scene.a", "--query", "押金", "--json"]


def test_target_delete_forwards_yes(capsys):
    code, mocked, _ = _run(capsys, ["target-delete", "wxid_a", "--yes", "--json"])
    assert code == 0
    assert mocked.call_args[0][0] == ["target-delete", "wxid_a", "--yes", "--json"]


def test_kb_delete_forwards_yes_and_remove_files(capsys):
    code, mocked, _ = _run(capsys, ["kb-delete", "kb", "--remove-files", "--yes", "--json"])
    assert code == 0
    assert mocked.call_args[0][0] == ["kb-delete", "kb", "--remove-files", "--yes", "--json"]


def test_json_passthrough_for_existing_commands(capsys):
    for argv, expected in [
        (["on", "wxid_a", "--json"], ["on", "wxid_a", "--json"]),
        (["off", "wxid_a", "--json"], ["off", "wxid_a", "--json"]),
        (["re", "wxid_a", "--json"], ["re", "wxid_a", "--json"]),
        (["scan", "--include-contacts", "--json"], ["scan", "--include-contacts", "--json"]),
        (["ls", "--kind", "enabled", "--json"], ["ls", "--kind", "enabled", "--json"]),
        (["kb-list", "--json"], ["kb-list", "--json"]),
        (["kb-local", "kb", "--replace", "--json"], ["kb-local", "kb", "--replace", "--json"]),
        (["kb-import", "kb", "/src", "--json"], ["kb-import", "kb", "/src", "--json"]),
        (["kb-open", "kb", "--json"], ["kb-open", "kb", "--json"]),
        (["kb-info", "kb", "--json"], ["kb-info", "kb", "--json"]),
        (["kb", "群", "kb1", "--replace", "--json"], ["kb", "群", "kb1", "--replace", "--json"]),
        (["init", "--json"], ["init", "--json"]),
        (["key", "--json"], ["key", "--json"]),
        (["dec", "--db", "m.db", "--json"], ["dec", "--db", "m.db", "--json"]),
        (["setup", "--admin", "--json"], ["setup", "--admin", "--json"]),
        (["refresh", "--force", "--json"], ["refresh", "--force", "--json"]),
        (["start", "--json"], ["start", "--json"]),
        (["stop", "--json"], ["stop", "--json"]),
        (["restart", "--json"], ["restart", "--json"]),
        (["status", "--json"], ["status", "--json"]),
    ]:
        code, mocked, _ = _run(capsys, argv)
        assert code == 0, argv
        assert mocked.call_args[0][0] == expected, (argv, mocked.call_args[0][0])


def test_agent_profiles_calls_control_client(capsys):
    with patch.object(bridge.control_client, "discover_hermes_profiles", return_value=[]) as mocked:
        code = bridge.main(["agent", "profiles", "--base-url", "http://test:18590", "--json"])
        assert code == 0
        assert mocked.call_args[1]["base_url"] == "http://test:18590"


def test_agent_on_calls_control_client(capsys):
    with patch.object(bridge.control_client, "agent_instance_on_duty", return_value={"ok": True}) as mocked:
        code = bridge.main(["agent", "on", "hermes-a", "--json"])
        assert code == 0
        assert mocked.call_args[0][0] == "hermes-a"


def test_agent_off_calls_control_client(capsys):
    with patch.object(bridge.control_client, "agent_instance_off_duty", return_value={"ok": True}) as mocked:
        code = bridge.main(["agent", "off", "hermes-a", "--json"])
        assert code == 0
        assert mocked.call_args[0][0] == "hermes-a"
