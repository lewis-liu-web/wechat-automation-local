"""Smoke tests for skill prompt injection and knowledge hit formatting."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_provider as ap
from skills import load_skill, list_skills


def test_skill_file_exists():
    skills = list_skills()
    assert "wechat_task" in skills
    text = load_skill("wechat_task")
    assert "WeChat Task Skill" in text
    assert "Available Tools" in text
    assert "Hard Rules" in text
    assert "Output Format" in text


def test_build_prompt_without_skill():
    job = {
        "payload": {
            "prompt": "介绍一下产品",
            "clean_text": "介绍一下产品",
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "介绍一下产品" in prompt
    assert "wechat-deep-reply" in prompt
    assert "[知识库片段]" not in prompt


def test_build_prompt_with_skill_and_knowledge_hits():
    skill_text = load_skill("wechat_task")
    job = {
        "payload": {
            "prompt": "介绍一下工作号真实号",
            "clean_text": "介绍一下工作号真实号",
            "skill_name": "wechat_task",
            "skill_prompt": skill_text,
            "knowledge_hits": [
                {
                    "source": "local",
                    "kb_id": "desktop_pdf",
                    "rel_path": "product_info.md",
                    "content": "工作号真实号是一款号码认证产品，用于企业客服号码真实性认证。",
                },
            ],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert skill_text[:60] in prompt
    assert "[知识库片段]" in prompt
    assert "工作号真实号" in prompt
    assert "product_info.md" in prompt


def test_build_prompt_with_legacy_tuple_hits():
    job = {
        "payload": {
            "prompt": "查询信息",
            "knowledge_hits": [
                ("product_info.md", "工作号真实号是一款号码认证产品。"),
            ],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "[知识库片段]" in prompt


def test_build_prompt_with_getnote_hit_no_rel_path():
    job = {
        "payload": {
            "prompt": "查询政策",
            "knowledge_hits": [
                {"source": "getnote", "kb_id": "getnote_QYAxqbPn", "content": "政策内容摘要。"},
            ],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "[知识库片段]" in prompt
    assert "来源: getnote)" in prompt
    assert "来源: getnote )" not in prompt


def test_build_prompt_ignores_empty_skill():
    job = {
        "payload": {
            "prompt": "你好",
            "skill_prompt": "",
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "WeChat Task Skill" not in prompt
    assert "wechat-deep-reply" in prompt
