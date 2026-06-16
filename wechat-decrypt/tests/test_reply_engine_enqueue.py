"""End-to-end test for reply_engine raw_agent enqueue with skill fields."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs as jobs
import reply_engine as re


def test_raw_agent_enqueue_carries_skill_and_knowledge_fields():
    target = {
        "name": "bot群聊测试",
        "username": "47965620946@chatroom",
        "table": "Msg_abc",
        "triggers": ["@小助理"],
        "knowledge_bases": ["desktop_pdf"],
    }
    config = {
        "reply_engine": {"raw_mode": True},
        "knowledge_bases": {
            "desktop_pdf": {"type": "local", "path": "desktop_pdf"},
        },
        "wiki_dir": str(Path(__file__).resolve().parent.parent / "wiki"),
    }
    message = {
        "content": "@小助理 自由处理 分析一下我们群聊助手的功能",
        "message_content": "@小助理 自由处理 分析一下我们群聊助手的功能",
        "local_id": 12345,
        "local_type": 1,
        "context_messages": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        # Point agent_jobs at the temp db via monkeypatching the default path is hard;
        # instead call generate_reply which uses the default path. Use a temp dir override
        # by setting the module-level default path attribute.
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is True
            assert decision.intent in ("deep_agent_queued", "agent_job_queued"), decision.intent
            # Find the job
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) >= 1
            job = all_jobs[-1]
            payload = job.get("payload") or {}
            assert payload.get("skill_name") == "wechat_task"
            assert isinstance(payload.get("skill_prompt"), str)
            assert len(payload.get("skill_prompt", "")) > 100
            assert payload.get("reply_mode") == "raw_agent"
            assert payload.get("knowledge_bases") == ["desktop_pdf"]
            assert isinstance(payload.get("knowledge_hits"), list)
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
