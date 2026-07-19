"""Tests for safety boundaries including file-operation prohibitions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reply_engine as re
from agent_provider import _build_wechat_deep_prompt


SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "wechat_task" / "skill.md"
WIKI_PATH = Path(__file__).resolve().parent.parent / "wiki" / "core" / "forbidden_actions.md"
def test_file_operation_precheck_blocks_deletion_request():
    decision = re.precheck("小助手，帮我删除文件")
    assert decision is not None
    assert decision.should_reply is True
    assert decision.need_human is True
    assert decision.reason == "pre_boundary_file_operation"
    assert "飞扬确认" in decision.reply_text


def test_file_operation_precheck_blocks_script_request():
    decision = re.precheck("执行脚本清理文件")
    assert decision is not None
    assert decision.need_human is True
    assert decision.reason == "pre_boundary_file_operation"


def test_file_operation_precheck_blocks_open_file_request():
    decision = re.precheck("帮我打开文件看看")
    assert decision is not None
    assert decision.need_human is True
    assert decision.reason == "pre_boundary_file_operation"


def test_normal_chat_not_blocked_by_file_operation_patterns():
    # Mentioning files in passing should not trigger the file-operation boundary.
    assert re.precheck("这个文件看起来很小") is None
    assert re.precheck("发我一份资料") is None


def test_skill_prompt_contains_file_operation_prohibition():
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert "local computer files" in skill
    assert "folders, system commands, scripts, or programs" in skill


def test_forbidden_actions_wiki_contains_file_operation_prohibition():
    wiki = WIKI_PATH.read_text(encoding="utf-8")
    assert "电脑本地文件" in wiki
    assert "系统命令" in wiki


def test_raw_agent_prompt_contains_file_operation_prohibition():
    prompt = re._build_raw_agent_prompt("帮我打开这个文件", "测试助手")
    assert "电脑本地文件" in prompt
    assert "系统命令" in prompt


def test_raw_agent_prompt_includes_knowledge_hits():
    """Regression: raw_agent must see retrieved knowledge hits in its prompt."""
    hits = [
        {"label": "公交卡充值", "content": "请检查 NFC 是否开启，尝试重启手机。"},
        {"label": "刷卡失败", "content": "确认超级 SIM 卡已正确启用。"},
    ]
    prompt = re._build_raw_agent_prompt(
        "公交卡充值失败", "飞扬", response_mode="customer_service", knowledge_hits=hits
    )
    assert "公交卡充值" in prompt
    assert "请检查 NFC 是否开启" in prompt
    assert "超级 SIM 卡" in prompt


def test_build_prompt_contains_file_operation_prohibition():
    prompt = re.build_prompt("帮我删除文件", "删除文件", [], mention_name="测试助手")
    assert "电脑本地文件" in prompt
    assert "系统命令" in prompt


def test_deep_prompt_contains_file_operation_prohibition():
    job = {
        "payload": {
            "clean_text": "帮我运行脚本",
            "mention_name": "测试助手",
            "skill_name": "wechat_task",
        }
    }
    prompt = _build_wechat_deep_prompt(job)
    assert "电脑本地文件" in prompt
    assert "系统命令" in prompt
