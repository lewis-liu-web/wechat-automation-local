"""Tests for enable_candidate response_mode defaults."""

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import target_registry as reg


class TestEnableCandidateResponseMode(TestCase):
    def _tmp_paths(self):
        td = tempfile.TemporaryDirectory()
        cfg_path = Path(td.name) / "wechat_bot_targets.json"
        cand_path = Path(td.name) / "wechat_bot_candidates.json"
        cfg_path.write_text(json.dumps({"version": 1, "targets": []}), encoding="utf-8")
        cand_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "updated_at": "",
                    "candidates": [
                        {
                            "username": "wxid_private",
                            "name": "Private Friend",
                            "db": "message_0.db",
                            "table": "Msg_abc",
                            "last_local_id": 0,
                            "status": "pending",
                        },
                        {
                            "username": "room@chatroom",
                            "name": "Group Chat",
                            "db": "message_0.db",
                            "table": "Msg_def",
                            "last_local_id": 0,
                            "status": "pending",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return td, cfg_path, cand_path

    def test_private_contact_gets_response_mode_free(self):
        _, cfg_path, cand_path = self._tmp_paths()
        target = reg.enable_candidate("wxid_private", config_path=cfg_path, candidates_path=cand_path)
        assert target.get("response_mode") == "free"

    def test_existing_private_contact_not_changed_on_re_enable(self):
        td, cfg_path, cand_path = self._tmp_paths()
        cfg_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": [
                        {
                            "name": "Private Friend",
                            "username": "wxid_private",
                            "db": "message_0.db",
                            "table": "Msg_abc",
                            "last_local_id": 0,
                            "enabled": False,
                            "triggers": [],
                            "reply_template": "",
                            "knowledge_bases": [],
                            "category": "user",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        target = reg.enable_candidate("wxid_private", config_path=cfg_path, candidates_path=cand_path)
        assert "response_mode" not in target

    def test_chatroom_does_not_set_response_mode(self):
        _, cfg_path, cand_path = self._tmp_paths()
        target = reg.enable_candidate("room@chatroom", config_path=cfg_path, candidates_path=cand_path)
        assert "response_mode" not in target
