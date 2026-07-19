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

def test_aggregated_prompt_prefix_and_summary():
    """聚合消息在 raw_agent 模式下必须在 prompt 中携带连续对话前缀和 aggregator_summary。"""
    import agent_jobs as jobs
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
            "content": "@小助理 看看",
            "message_content": "@小助理 看看",
            "local_id": 2,
            "local_type": 1,
            "sender_display_name": "测试用户",
            "context_messages": [
                {
                    "local_id": 1,
                    "sender_id": "u1",
                    "message_content": "帮我查一下这个订单",
                    "create_time": 1700000000.0,
                    "image_path": None,
                },
                {
                    "local_id": 2,
                    "sender_id": "u1",
                    "message_content": "@小助理 看看",
                    "create_time": 1700000001.0,
                    "image_path": None,
                },
            ],
            "is_aggregated": True,
            "text_parts_count": 2,
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
            assert payload.get("aggregator_summary") is not None
            assert "以下是你需要回复的连续对话" in payload.get("prompt", "")
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


def test_customer_service_no_kb_hit_is_enqueued_as_standard_trigger():
    """客服模式下如果没有命中 scene 知识库，thin-monitor 应把消息入队交给 agent 决定。"""
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
            assert decision.intent in ("deep_agent_queued", "agent_job_queued"), decision.intent
            all_jobs = jobs.list_jobs(db_path=db)
            assert len(all_jobs) >= 1
        finally:
            jobs.DEFAULT_DB_PATH = orig_default


# --- Scoped MCP authorization snapshot (legacy enqueue gap fix) ---------------
#
# Stage 2 forces every enqueued agent job onto the strict AgentResult contract,
# whose prompt instructs Hermes to call mcp__wechat_kb_search__search_knowledge.
# The fast (agent_provider_enqueued) branch previously lacked ``_config_path`` /
# ``_allowed_kb_ids`` / trace fields, so the MCP server saw an empty allowlist
# and fail-closed ``invalid`` for every legacy job: legacy replies never had KB
# grounding and the calls were unaudited.  Both enqueue branches now share
# ``reply_engine._scoped_mcp_payload_fields``.

import json as _json
from unittest import mock as _mock

import agent_provider as _ap
import task_router as _tr


def _scoped_setup(tmpdir, *, kbs=("scene.a", "scene.b")):
    """Minimal raw_agent target/config plus a REAL config file on disk."""
    wiki_root = Path(tmpdir) / "wiki"
    for kb in kbs:
        (wiki_root / "scenes" / kb.split(".")[-1]).mkdir(parents=True, exist_ok=True)
    config_path = Path(tmpdir) / "wechat_bot_targets.json"
    config_path.write_text(_json.dumps({"targets": [], "knowledge_bases": {}}), encoding="utf-8")
    target = {
        "name": "parity-replay",
        "username": "wxid_parity_replay",
        "table": "Msg_parity",
        "triggers": [],
        "knowledge_bases": list(kbs),
        "mode": "customer_service",
        "thin_monitor": True,
    }
    config = {
        "reply_engine": {"raw_mode": True},
        "knowledge_bases": {kb: {"type": "local", "path": "scenes/%s" % kb.split(".")[-1]} for kb in kbs},
        "wiki_dir": str(wiki_root),
    }
    message = {
        "content": "苹果是什么",
        "message_content": "苹果是什么",
        "local_id": 1,
        "local_type": 1,
        "context_messages": [],
        "is_aggregated": False,
        "text_parts_count": 1,
    }
    return target, config, config_path, message


def _enqueue(tmpdir, target, config, config_path, message):
    db = Path(tmpdir) / "agent_jobs.sqlite"
    orig_default = jobs.DEFAULT_DB_PATH
    try:
        jobs.DEFAULT_DB_PATH = db
        decision = re.generate_reply(message, target, config, config_path=str(config_path))
    finally:
        jobs.DEFAULT_DB_PATH = orig_default
    return decision, db


def _assert_scoped_payload(payload, config_path, kbs):
    """Shared assertions for both enqueue branches."""
    assert payload.get("_config_path") == str(config_path), payload.get("_config_path")
    assert payload.get("_allowed_kb_ids") == list(kbs), payload.get("_allowed_kb_ids")
    assert payload.get("_knowledge_trace_id"), "trace id must be snapshotted at enqueue"
    assert str(payload.get("_knowledge_trace_dir") or "").endswith("knowledge_traces")
    # Pre-staged source specs must never escape the snapshotted scope.
    for spec in payload.get("knowledge_base_specs") or []:
        spec_id = spec.get("id") or spec.get("kb_id")
        if spec_id is None:
            continue
        assert spec_id in payload["_allowed_kb_ids"], "spec %r escapes _allowed_kb_ids" % (spec_id,)


def test_fast_enqueue_payload_carries_scoped_mcp_fields():
    """Plain text routes default_fast; the fast branch must snapshot the scope."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target, config, config_path, message = _scoped_setup(tmpdir)
        fast_route = _tr.RouteDecision(route=_tr.ROUTE_FAST, reason="default_fast", detail="")
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            with _mock.patch.object(re._task_router, "route_message", return_value=fast_route):
                decision = re.generate_reply(message, target, config, config_path=str(config_path))
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
        assert decision.intent == "agent_job_queued", decision.intent
        payload = (jobs.list_jobs(db_path=db)[-1].get("payload") or {})
        assert payload.get("route") == "fast_reply"
        _assert_scoped_payload(payload, config_path, ("scene.a", "scene.b"))


def test_deep_enqueue_payload_carries_same_scoped_fields():
    """The deep branch already had two keys; the shared helper must not regress it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target, config, config_path, message = _scoped_setup(tmpdir)
        decision_route = _tr.RouteDecision(route=_tr.ROUTE_DEEP, reason="test_deep", detail="")
        db = Path(tmpdir) / "agent_jobs.sqlite"
        orig_default = jobs.DEFAULT_DB_PATH
        try:
            jobs.DEFAULT_DB_PATH = db
            with _mock.patch.object(re._task_router, "route_message", return_value=decision_route):
                decision = re.generate_reply(message, target, config, config_path=str(config_path))
        finally:
            jobs.DEFAULT_DB_PATH = orig_default
        assert decision.intent == "deep_agent_queued", decision.intent
        payload = (jobs.list_jobs(db_path=db)[-1].get("payload") or {})
        _assert_scoped_payload(payload, config_path, ("scene.a", "scene.b"))


def test_prepare_model_job_injects_scoped_env_from_enqueue_payload():
    """End-to-end: the enqueued payload must yield the trusted MCP env."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target, config, config_path, message = _scoped_setup(tmpdir)
        _enqueue(tmpdir, target, config, config_path, message)
        db = Path(tmpdir) / "agent_jobs.sqlite"
        job = jobs.list_jobs(db_path=db)[-1]
        provider = _ap.HermesProvider(cli_path="hermes-test")
        model_job, env = provider._prepare_model_job(job)
        assert env["WECHAT_MCP_CONFIG"] == str(config_path)
        assert _json.loads(env["WECHAT_MCP_ALLOWED_KB_IDS"]) == ["scene.a", "scene.b"]
        assert env["WECHAT_MCP_TRACE_ID"] == job["payload"]["_knowledge_trace_id"]
        assert env["WECHAT_MCP_TRACE_DIR"] == job["payload"]["_knowledge_trace_dir"]
        leaked = [k for k in (model_job.get("payload") or {}) if str(k).startswith("_")]
        assert leaked == [], leaked


def test_scoped_snapshot_immutable_after_enqueue():
    """Later config edits must never retro-scope an already queued job."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target, config, config_path, message = _scoped_setup(tmpdir)
        _enqueue(tmpdir, target, config, config_path, message)
        config["knowledge_bases"] = {"other.kb": {"type": "local", "path": "scenes/other"}}
        target["knowledge_bases"] = ["other.kb"]
        db = Path(tmpdir) / "agent_jobs.sqlite"
        payload = (jobs.list_jobs(db_path=db)[-1].get("payload") or {})
        assert payload.get("_allowed_kb_ids") == ["scene.a", "scene.b"]


def test_monitor_generate_reply_call_sites_removed_by_stage4():
    """Stage 4 retires the legacy aggregation in wechat_bot_monitor — no
    generate_reply call sites should remain. reply_engine itself still uses
    generate_reply; this assertion is scoped to the monitor source.
    """
    src = (Path(__file__).resolve().parent.parent / "wechat_bot_monitor.py").read_text(encoding="utf-8")
    assert src.count("generate_reply(") == 0, (
        "wechat_bot_monitor still has generate_reply call sites; Stage 4 "
        "should have retired the legacy aggregation path."
    )
