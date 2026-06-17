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
        "mode": "customer_service",
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
        "is_aggregated": False,
        "aggregated_local_ids": [12345],
        "text_parts_count": 1,
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
            assert not payload.get("skill_prompt")
            assert payload.get("agent_timeout") == 90.0
            assert payload.get("is_aggregated") is False
            assert payload.get("aggregated_local_ids") == [12345]
            assert payload.get("text_parts_count") == 1
            assert payload.get("reply_mode") == "raw_agent"
            assert payload.get("knowledge_bases") == ["desktop_pdf"]
            assert isinstance(payload.get("knowledge_hits"), list)
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
def test_free_mode_smalltalk_enqueues_agent_job():
    """自由模式(personal_assistant)下的闲聊应进入 agent 队列，而不是本地 fallback。"""
    target = {
        "name": "bot群聊测试",
        "username": "47965620946@chatroom",
        "table": "Msg_abc",
        "triggers": ["@小助理"],
        "mode": "personal_assistant",
    }
    config = {
        "reply_engine": {"raw_mode": True},
        "default_triggers": ["@小助理"],
    }
    message = {
        "content": "@小助理 聊聊天",
        "message_content": "@小助理 聊聊天",
        "local_id": 12346,
        "local_type": 1,
        "context_messages": [],
        "is_aggregated": False,
        "aggregated_local_ids": [12346],
        "text_parts_count": 1,
        "target_policy": {"mode": "personal_assistant"},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is False
            assert decision.reason == "agent_provider_enqueued", decision.reason
            assert decision.reply_text == ""
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) >= 1
            job = all_jobs[-1]
            payload = job.get("payload") or {}
            assert payload.get("response_mode") == "personal_assistant"
            assert "当前响应模式：自由" in str(payload.get("mode_instruction") or "")
        finally:
            jobs.DEFAULT_DB_PATH = orig_default


def test_balanced_mode_smalltalk_uses_local_fallback():
    """平衡模式(group_assistant)下的闲聊应保持保守本地 fallback，不进入 agent 队列。"""
    target = {
        "name": "bot群聊测试",
        "username": "47965620946@chatroom",
        "table": "Msg_abc",
        "triggers": ["@小助理"],
        "mode": "group_assistant",
    }
    config = {
        "reply_engine": {"raw_mode": True},
        "default_triggers": ["@小助理"],
    }
    message = {
        "content": "@小助理 聊聊天",
        "message_content": "@小助理 聊聊天",
        "local_id": 12347,
        "local_type": 1,
        "context_messages": [],
        "is_aggregated": False,
        "aggregated_local_ids": [12347],
        "text_parts_count": 1,
        "target_policy": {"mode": "group_assistant"},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is True
            assert decision.reason == "raw_agent_smalltalk_fallback", decision.reason
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) == 0
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
def test_customer_service_mode_returns_immediate_ack():
    """客服模式(customer_service)下 agent 入队应返回立即确认回复。"""
    target = {
        "name": "bot群聊测试",
        "username": "47965620946@chatroom",
        "table": "Msg_abc",
        "triggers": ["@小助理"],
        "mode": "customer_service",
    }
    config = {
        "reply_engine": {"raw_mode": True},
        "default_triggers": ["@小助理"],
    }
    message = {
        "content": "@小助理 我想咨询套餐",
        "message_content": "@小助理 我想咨询套餐",
        "local_id": 12348,
        "local_type": 1,
        "context_messages": [],
        "is_aggregated": False,
        "aggregated_local_ids": [12348],
        "text_parts_count": 1,
        "target_policy": {"mode": "customer_service"},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is True
            assert decision.reply_text == "正在处理中，请稍等。"
            assert decision.reason == "agent_provider_enqueued", decision.reason
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) >= 1
            job = all_jobs[-1]
            payload = job.get("payload") or {}
            assert payload.get("response_mode") == "customer_service"
        finally:
            jobs.DEFAULT_DB_PATH = orig_default


def test_balanced_mode_question_does_not_return_ack():
    """平衡模式(group_assistant)下 agent 入队不应返回立即确认回复。"""
    target = {
        "name": "bot群聊测试",
        "username": "47965620946@chatroom",
        "table": "Msg_abc",
        "triggers": ["@小助理"],
        "mode": "group_assistant",
    }
    config = {
        "reply_engine": {"raw_mode": True},
        "default_triggers": ["@小助理"],
    }
    message = {
        "content": "@小助理 请介绍一下移动云盘功能",
        "message_content": "@小助理 请介绍一下移动云盘功能",
        "local_id": 12349,
        "local_type": 1,
        "context_messages": [],
        "is_aggregated": False,
        "aggregated_local_ids": [12349],
        "text_parts_count": 1,
        "target_policy": {"mode": "group_assistant"},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is False
            assert decision.reply_text == ""
            assert decision.reason == "agent_provider_enqueued", decision.reason
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) >= 1
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
