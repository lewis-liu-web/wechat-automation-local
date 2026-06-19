"""End-to-end test for reply_engine raw_agent enqueue with skill fields."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs as jobs
import reply_engine as re


def test_raw_agent_enqueue_carries_skill_and_knowledge_fields():
    """raw_agent 模式下，命中的知识库内容必须原样进入 agent job payload。"""
    import tempfile
    from pathlib import Path
    import agent_jobs as jobs
    import reply_engine as re

    with tempfile.TemporaryDirectory() as tmpdir:
        wiki_root = Path(tmpdir) / "wiki"
        kb_path = wiki_root / "desktop_pdf"
        kb_path.mkdir(parents=True)
        (kb_path / "product.md").write_text(
            "移动云盘是中国移动推出的云存储产品，支持文件备份、多端同步和在线预览。",
            encoding="utf-8",
        )
        db = Path(tmpdir) / "agent_jobs.sqlite"
        target = {
            "name": "bot群聊测试",
            "username": "47965620946@chatroom",
            "table": "Msg_abc",
            "triggers": ["@小助理"],
            "knowledge_bases": ["desktop_pdf"],
            "mode": "group_assistant",
        }
        config = {
            "reply_engine": {"raw_mode": True},
            "knowledge_bases": {
                "desktop_pdf": {"type": "local", "path": "desktop_pdf"},
            },
            "wiki_dir": str(wiki_root),
        }
        message = {
            "content": "@小助理 介绍一下移动云盘",
            "message_content": "@小助理 介绍一下移动云盘",
            "local_id": 12345,
            "local_type": 1,
            "context_messages": [],
            "is_aggregated": False,
            "aggregated_local_ids": [12345],
            "text_parts_count": 1,
        }
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is False
            assert decision.intent in ("deep_agent_queued", "agent_job_queued"), decision.intent
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) >= 1
            job = all_jobs[-1]
            payload = job.get("payload") or {}
            assert payload.get("reply_mode") == "raw_agent"
            assert payload.get("knowledge_bases") == ["desktop_pdf"]
            assert isinstance(payload.get("knowledge_hits"), list)
            assert len(payload.get("knowledge_hits")) >= 1
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
def test_legacy_personal_mode_normalizes_to_balanced_agent_payload():
    """legacy personal_assistant target mode must be normalized to group_assistant in agent payload."""
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
            assert payload.get("response_mode") == "group_assistant"
            assert "当前响应模式：平衡" in str(payload.get("mode_instruction") or "")
            assert "自由" not in str(payload.get("mode_instruction") or "")
        finally:
            jobs.DEFAULT_DB_PATH = orig_default


def test_balanced_mode_smalltalk_enqueues_agent_without_local_fallback():
    """平衡模式下普通闲聊应进入 agent 队列，不再使用本地 fallback。"""
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
            assert decision.should_reply is False
            assert decision.reason == "agent_provider_enqueued", decision.reason
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) == 1
        finally:
            jobs.DEFAULT_DB_PATH = orig_default

def test_customer_service_mode_returns_immediate_ack():
    """客服模式下命中知识库时返回即时确认并进入 agent 队列。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        wiki_root = Path(tmpdir) / "wiki"
        kb_path = wiki_root / "desktop_pdf"
        kb_path.mkdir(parents=True)
        (kb_path / "product.md").write_text(
            "移动云盘套餐包括个人版、家庭版和超级会员。",
            encoding="utf-8",
        )
        db = Path(tmpdir) / "agent_jobs.sqlite"
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
            "wiki_dir": str(wiki_root),
        }
        message = {
            "content": "@小助理 我想咨询移动云盘套餐",
            "message_content": "@小助理 我想咨询移动云盘套餐",
            "local_id": 12348,
            "local_type": 1,
            "context_messages": [],
            "is_aggregated": False,
            "aggregated_local_ids": [12348],
            "text_parts_count": 1,
            "target_policy": {"mode": "customer_service"},
        }
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


def test_raw_agent_enqueue_payload_knowledge_hits_not_empty():
    """raw_agent 模式下，命中的知识库内容必须原样进入 agent job payload。"""
    import tempfile
    from pathlib import Path
    import agent_jobs as jobs
    import reply_engine as re

    with tempfile.TemporaryDirectory() as tmpdir:
        wiki_root = Path(tmpdir) / "wiki"
        kb_path = wiki_root / "desktop_pdf"
        kb_path.mkdir(parents=True)
        (kb_path / "product.md").write_text(
            "移动云盘是中国移动推出的云存储产品，支持文件备份、多端同步和在线预览。",
            encoding="utf-8",
        )
        db = Path(tmpdir) / "agent_jobs.sqlite"
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
            "wiki_dir": str(wiki_root),
        }
        message = {
            "content": "@小助理 介绍一下移动云盘",
            "message_content": "@小助理 介绍一下移动云盘",
            "local_id": 12346,
            "local_type": 1,
            "context_messages": [],
            "is_aggregated": False,
            "aggregated_local_ids": [12346],
            "text_parts_count": 1,
        }
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is True
            assert decision.intent in ("deep_agent_queued", "agent_job_queued"), decision.intent
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) >= 1
            job = all_jobs[-1]
            payload = job.get("payload") or {}
            hits = payload.get("knowledge_hits") or []
            assert len(hits) >= 1, "expected non-empty knowledge_hits in payload"
            contents = "\n".join(str(h.get("content") or "") for h in hits)
            assert "移动云盘" in contents, f"expected 移动云盘 in hits, got {contents!r}"
        finally:
            jobs.DEFAULT_DB_PATH = orig_default


def test_customer_service_no_kb_hit_returns_clarification_without_job():
    """客服模式下如果没有命中 scene 知识库，应直接澄清并不入队。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        wiki_root = Path(tmpdir) / "wiki"
        kb_path = wiki_root / "desktop_pdf"
        kb_path.mkdir(parents=True)
        (kb_path / "product.md").write_text(
            "移动云盘是中国移动推出的云存储产品，支持文件备份、多端同步和在线预览。",
            encoding="utf-8",
        )
        db = Path(tmpdir) / "agent_jobs.sqlite"
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
            "wiki_dir": str(wiki_root),
        }
        message = {
            "content": "@小助理 火星基地旅游政策是什么",
            "message_content": "@小助理 火星基地旅游政策是什么",
            "local_id": 12350,
            "local_type": 1,
            "context_messages": [],
            "is_aggregated": False,
            "aggregated_local_ids": [12350],
            "text_parts_count": 1,
            "target_policy": {"mode": "customer_service"},
        }
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            decision = re.generate_reply(message, target, config)
            assert decision.should_reply is True
            assert decision.intent == "kb_clarification", decision.intent
            assert decision.reason == "customer_service_no_kb_hit", decision.reason
            assert "需要先查到相关资料" in decision.reply_text
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) == 0
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
