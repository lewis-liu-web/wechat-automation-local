"""Smoke tests for skill prompt injection and knowledge hit formatting."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_provider as ap
from skills import load_skill, list_skills, render_skill


def test_render_skill_wechat_task():
    text = render_skill(
        "wechat_task",
        mention_name="飞扬",
        user_request="介绍一下产品",
        knowledge_hits="",
        mode_instruction="当前响应模式：自由。",
    )
    assert "{{mention_name}}" not in text
    assert "{{user_request}}" not in text
    assert "{{knowledge_hits}}" not in text
    assert "{{mode_instruction}}" not in text
    assert "飞扬" in text
    assert "介绍一下产品" in text
    assert "当前响应模式：自由。" in text
    assert "WeChat Task Skill" in text
    assert "Hard Rules" in text
    assert "Output Format" in text
    assert "Output ONLY the final Chinese reply text" in text



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


def test_build_prompt_with_skill_name_and_knowledge_hits():
    job = {
        "payload": {
            "prompt": "介绍一下工作号真实号",
            "clean_text": "介绍一下工作号真实号",
            "skill_name": "wechat_task",
            "mention_name": "飞扬",
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
    assert "WeChat Task Skill" in prompt
    assert "Hard Rules" in prompt
    assert "Output Format" in prompt
    assert "[知识库片段]" in prompt
    assert "工作号真实号" in prompt
    assert "product_info.md" in prompt
    assert "飞扬" in prompt
    assert "介绍一下工作号真实号" in prompt
    # knowledge_hits must be pre-formatted; raw dict repr should never leak.
    assert "[{'source':" not in prompt


def test_build_prompt_with_legacy_skill_prompt():
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
def test_build_prompt_with_skill_passes_mode_instruction():
    job = {
        "payload": {
            "prompt": "聊聊天",
            "clean_text": "聊聊天",
            "skill_name": "wechat_task",
            "mention_name": "飞扬",
            "mode_instruction": "当前响应模式：自由。允许闲聊。",
            "knowledge_hits": [],
        },
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "当前响应模式：自由。允许闲聊。" in prompt
    assert "{{mode_instruction}}" not in prompt
    assert "Output ONLY the final Chinese reply text" in prompt


def test_build_prompt_keeps_long_knowledge_hit_content():
    """知识库片段内容超过 800 字符时不应被截断；总预算超限时再截尾。"""
    long_content = "移动云盘" + "，支持文件备份" * 200  # well over 800 chars
    job = {
        "payload": {
            "prompt": "介绍一下移动云盘",
            "sender": "user",
            "knowledge_hits": [
                {
                    "source": "local",
                    "kb_id": "desktop_pdf",
                    "rel_path": "product.md",
                    "content": long_content,
                }
            ],
        }
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert "[知识库片段]" in prompt
    # Content beyond the old 800-char cap must be preserved
    assert long_content[:900] in prompt
    assert "### 片段 1 (来源: local product.md)" in prompt


def test_build_prompt_total_kb_budget_stops_at_tail():
    """总字符预算 12000 超限时，应停止追加后续片段，而不是截断到片段中间。"""
    chunk = "0123456789" * 500  # 5000 chars
    job = {
        "payload": {
            "prompt": "查询资料",
            "sender": "user",
            "knowledge_hits": [
                {"source": "local", "kb_id": "a", "rel_path": "a.md", "content": chunk},
                {"source": "local", "kb_id": "b", "rel_path": "b.md", "content": chunk},
                {"source": "local", "kb_id": "c", "rel_path": "c.md", "content": chunk},
            ],
        }
    }
    prompt = ap._build_wechat_deep_prompt(job)
    assert prompt.count("### 片段") == 3  # 12000 budget fits three 4000-char hits
    assert "### 片段 4" not in prompt


def test_wechat_auto_skill_contract():
    """wechat_auto skill must state that monitor only provides paths and decode_image is not a vision fallback."""
    text = load_skill("wechat_auto")
    assert "WeChat 自动化 Agent" in text
    assert "decode_image" in text
    assert "decode_image(chat_name, local_id)" in text
    assert "decode_image(image_path)" not in text
    assert "不能替代视觉理解" in text
    assert "不要编造图片内容" in text
    assert "Monitor 仅提供图片在本地的解密路径" in text
    assert "最终回复不超过 600 字（含标点），超出会被系统强制截断" in text
    assert "明确提到" in text
    assert "不要编造" in text
    assert "直接询问用户具体品牌/型号" in text
    assert "如果命中结果里出现了相关品牌" in text
    assert "decode_image(image_path)" not in text
    assert "不要编造图片内容" in text
