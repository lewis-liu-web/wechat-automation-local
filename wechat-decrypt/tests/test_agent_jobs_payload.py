"""Tests that agent_jobs preserves extended payload fields."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs as jobs
import tempfile


def test_enqueue_preserves_knowledge_and_skill_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        payload = {
            "prompt": "介绍一下产品",
            "clean_text": "介绍一下产品",
            "raw_text": "小助理 介绍一下产品",
            "skill_name": "wechat_task",
            "skill_prompt": "You are a WeChat assistant.",
            "knowledge_hits": [
                {
                    "source": "local",
                    "kb_id": "desktop_pdf",
                    "rel_path": "product_info.md",
                    "content": "工作号真实号是一款号码认证产品。",
                }
            ],
            "knowledge_bases": ["desktop_pdf", "getnote_QYAxqbPn"],
            "reply_mode": "balanced",
            "retrieval_debug": {"raw_scene_count": 1, "strong_scene_count": 1},
        }
        job = jobs.enqueue_job(
            job_key="test_payload_001",
            group_key="test_group",
            task_type="wechat_reply",
            payload=payload,
            target_name="bot群聊测试",
            sender="alice",
            message_local_id=42,
            db_path=db,
        )
        assert job is not None
        assert job["id"] is not None

        fetched = jobs.get_job(job_id=job["id"], db_path=db)
        assert fetched is not None
        fetched_payload = fetched.get("payload") or {}
        assert fetched_payload.get("skill_name") == "wechat_task"
        assert fetched_payload.get("skill_prompt") == "You are a WeChat assistant."
        assert fetched_payload.get("reply_mode") == "balanced"
        assert fetched_payload.get("knowledge_bases") == ["desktop_pdf", "getnote_QYAxqbPn"]
        assert fetched_payload.get("retrieval_debug") == {"raw_scene_count": 1, "strong_scene_count": 1}
        hits = fetched_payload.get("knowledge_hits") or []
        assert len(hits) == 1
        assert hits[0]["kb_id"] == "desktop_pdf"
        assert hits[0]["content"].startswith("工作号真实号")


def test_enqueue_merges_aggregation_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        payload = {
            "prompt": "介绍一下产品",
            "clean_text": "介绍一下产品",
            "raw_text": "小助理 介绍一下产品",
            # Pre-existing values must not be overridden by kwargs.
            "reply_mode": "creative",
            "skill_name": "existing_skill",
        }
        job = jobs.enqueue_job(
            job_key="test_agg_001",
            group_key="test_group",
            task_type="wechat_reply",
            payload=payload,
            target_name="bot群聊测试",
            sender="alice",
            message_local_id=42,
            is_aggregated=True,
            aggregated_local_ids=[526, 527],
            session_image_paths=["img1.jpg", "img2.jpg"],
            text_parts_count=2,
            agent_timeout=240.0,
            knowledge_hits=[
                {
                    "source": "local",
                    "kb_id": "desktop_pdf",
                    "rel_path": "product_info.md",
                    "content": "工作号真实号是一款号码认证产品。",
                }
            ],
            knowledge_bases=["desktop_pdf", "getnote_QYAxqbPn"],
            reply_mode="balanced",
            retrieval_debug={"raw_scene_count": 1, "strong_scene_count": 1},
            skill_name="wechat_task",
            db_path=db,
        )
        assert job is not None
        assert job["id"] is not None

        fetched = jobs.get_job(job_id=job["id"], db_path=db)
        assert fetched is not None
        p = fetched.get("payload") or {}

        assert p.get("is_aggregated") is True
        assert p.get("aggregated_local_ids") == [526, 527]
        assert p.get("session_image_paths") == ["img1.jpg", "img2.jpg"]
        assert p.get("text_parts_count") == 2
        assert p.get("agent_timeout") == 240.0
        assert p.get("knowledge_bases") == ["desktop_pdf", "getnote_QYAxqbPn"]
        assert p.get("reply_mode") == "creative"
        assert p.get("skill_name") == "existing_skill"

        hits = p.get("knowledge_hits") or []
        assert len(hits) == 1
        assert hits[0]["kb_id"] == "desktop_pdf"
        assert hits[0]["content"].startswith("工作号真实号")

        debug = p.get("retrieval_debug") or {}
        assert debug.get("raw_scene_count") == 1
        assert debug.get("strong_scene_count") == 1


def test_enqueue_backward_compatibility_with_skill_prompt():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        payload = {
            "prompt": "介绍一下产品",
            "skill_prompt": "You are the legacy WeChat assistant.",
        }
        job = jobs.enqueue_job(
            job_key="test_legacy_001",
            group_key="test_group",
            task_type="wechat_reply",
            payload=payload,
            db_path=db,
        )
        fetched = jobs.get_job(job_id=job["id"], db_path=db)
        p = fetched.get("payload") or {}
        assert p.get("skill_prompt") == "You are the legacy WeChat assistant."
        assert "skill_name" not in p
