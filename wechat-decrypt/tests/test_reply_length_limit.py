"""Tests for the configurable reply length cap in reply_engine and agent_provider.

Covers:
- ``reply_engine.max_reply_chars`` config knob (default 600, custom values,
  invalid values, missing keys, nested fallback).
- ``postcheck`` enforcing the cap, including the legacy ``postcheck(text)`` path
  used by ``wechat_bot_monitor.py`` (no config -> default 600).
- ``_truncate_to_max`` soft-cut strategy: sentence boundary preferred, hard-cut
  with ellipsis fallback.
- ``_build_wechat_deep_prompt`` injecting the configured value into the
  wechat-deep-reply hard rule and warning about forced truncation.
- ``build_prompt`` propagating the cap into the standard-mode prompt.
- ``agent_worker`` sanitizing async raw-agent/deep results before complete/send.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_provider as ap
import reply_engine as re


# ---------------------------------------------------------------------------
# reply_engine._max_reply_chars / config plumbing
# ---------------------------------------------------------------------------


def test_default_max_reply_chars_is_600():
    assert re.DEFAULT_MAX_REPLY_CHARS == 600
    assert re.MAX_REPLY_CHARS == re.DEFAULT_MAX_REPLY_CHARS


def test_max_reply_chars_resolve_default_when_missing():
    assert re._max_reply_chars(None) == 600
    assert re._max_reply_chars({}) == 600
    assert re._max_reply_chars({"reply_engine": {}}) == 600


def test_max_reply_chars_resolve_from_config():
    cfg = {"reply_engine": {"max_reply_chars": 200}}
    assert re._max_reply_chars(cfg) == 200


def test_max_reply_chars_invalid_value_falls_back_to_default():
    # Strings that can't be coerced
    assert re._max_reply_chars({"reply_engine": {"max_reply_chars": "abc"}}) == 600
    # Non-positive values are ignored
    assert re._max_reply_chars({"reply_engine": {"max_reply_chars": 0}}) == 600
    assert re._max_reply_chars({"reply_engine": {"max_reply_chars": -1}}) == 600
    assert re._max_reply_chars({"reply_engine": {"max_reply_chars": None}}) == 600
    # Wrong type containers
    assert re._max_reply_chars({"reply_engine": {"max_reply_chars": [200]}}) == 600
    assert re._max_reply_chars({"reply_engine": "not_a_dict"}) == 600


# ---------------------------------------------------------------------------
# postcheck
# ---------------------------------------------------------------------------


def _make_long_text(n: int) -> str:
    """Build a long single-line Chinese text of length n (no boundary chars)."""
    base = "\u4e2d" * 50  # 50 Chinese chars, no sentence boundaries
    times, rem = divmod(n, len(base))
    return (base * times) + base[:rem]


def test_postcheck_default_cap_keeps_short_text_intact():
    text = "你好世界。"
    assert re.postcheck(text) == text
    assert re.postcheck("") == ""


def test_postcheck_default_cap_uses_600():
    """With no config, postcheck should use the new default of 600 chars."""
    text = _make_long_text(900)
    out = re.postcheck(text)
    # 600 char cap: hard-cut to 599 chars + ellipsis => 600 chars
    assert len(out) == 600
    assert out.endswith("\u2026")  # ellipsis


def test_postcheck_honors_custom_cap():
    text = _make_long_text(500)
    out = re.postcheck(text, {"reply_engine": {"max_reply_chars": 200}})
    assert len(out) == 200
    assert out.endswith("\u2026")


def test_postcheck_keeps_text_within_cap_unchanged():
    text = "这是一段很短的回复。"
    cfg = {"reply_engine": {"max_reply_chars": 200}}
    assert re.postcheck(text, cfg) == text


def test_postcheck_legacy_signature_unaffected():
    """wechat_bot_monitor.py calls postcheck(text) with no config; that path
    must still return a string and clamp to the module default (600)."""
    text = _make_long_text(800)
    out = re.postcheck(text)
    assert isinstance(out, str)
    assert len(out) == 600


def test_postcheck_prefers_sentence_boundary_in_chinese():
    """If a sentence boundary is reachable within the cap window, cut right
    after the last full sentence instead of mid-word."""
    text = "第一句回答。\n第二句更长的内容。\n最后一句短。"
    # 10 char cap: the window is the first 9 chars "第一句回答。\n第二句"
    # the last sentence boundary in that window is "。" after "回答" -> 4 chars.
    out = re.postcheck(text, {"reply_engine": {"max_reply_chars": 10}})
    assert out == "第一句回答。"
    assert "\u2026" not in out


def test_postcheck_prefers_sentence_boundary_with_full_width_punctuation():
    text = "你好！世界？这是测试；最后。"
    # 7-char window: candidate "你好！世界？这" -> last boundary "？" at idx 4
    out = re.postcheck(text, {"reply_engine": {"max_reply_chars": 7}})
    assert out == "你好！世界？"


def test_postcheck_falls_back_to_hard_cut_with_ellipsis_when_no_boundary():
    text = "abcdefghijklmnopqrstuvwxyz"
    out = re.postcheck(text, {"reply_engine": {"max_reply_chars": 10}})
    assert out == "abcdefghi\u2026"
    assert len(out) == 10


def test_postcheck_triggers_blocked_safety_response():
    blocked = "这里有 keys.json 的路径信息。"
    out = re.postcheck(blocked, {"reply_engine": {"max_reply_chars": 200}})
    assert "需要本人确认" in out
    assert "keys.json" not in out


def test_postcheck_exact_fit_text_not_modified():
    text = "a" * 600
    assert re.postcheck(text) == text
    text200 = "a" * 200
    assert re.postcheck(text200, {"reply_engine": {"max_reply_chars": 200}}) == text200


# ---------------------------------------------------------------------------
# _truncate_to_max
# ---------------------------------------------------------------------------


def test_truncate_to_max_returns_short_text_unchanged():
    assert re._truncate_to_max("hello", 100) == "hello"
    assert re._truncate_to_max("", 100) == ""


def test_truncate_to_max_uses_sentence_boundary_in_chinese():
    out = re._truncate_to_max("你好。世界。", 5)
    assert out == "你好。"
    assert "\u2026" not in out


def test_truncate_to_max_falls_back_to_ellipsis():
    out = re._truncate_to_max("abcdefghij", 5)
    assert out == "abcd\u2026"
    assert len(out) == 5


def test_truncate_to_max_ignores_zero_or_negative_max():
    # Should not crash; returns the input as-is when max is non-positive
    assert re._truncate_to_max("hello", 0) == "hello"
    assert re._truncate_to_max("hello", -3) == "hello"


def test_truncate_to_max_handles_newline_boundary():
    # The trailing newline is stripped to avoid emitting a blank new line in
    # the chat; the cut itself is still on a sentence boundary.
    out = re._truncate_to_max("first line\nsecond line\nthird", 12)
    assert out == "first line"
    assert "\u2026" not in out


# ---------------------------------------------------------------------------
# build_prompt standard mode
# ---------------------------------------------------------------------------


def test_build_prompt_uses_default_cap_when_no_max_chars_given():
    prompt = re.build_prompt("群消息", "清洗后问题", [])
    assert f"最多{re.DEFAULT_MAX_REPLY_CHARS}字" in prompt
    assert "超出部分会被系统强制截断" in prompt


def test_build_prompt_propagates_custom_max_chars():
    prompt = re.build_prompt("群消息", "清洗后问题", [], max_chars=120)
    assert "最多120字" in prompt
    assert "超出部分会被系统强制截断" in prompt
    # Should NOT contain the old default
    assert f"最多{re.DEFAULT_MAX_REPLY_CHARS}字" not in prompt


def test_build_prompt_invalid_max_chars_falls_back_to_default():
    prompt = re.build_prompt("群消息", "清洗后问题", [], max_chars=0)
    assert f"最多{re.DEFAULT_MAX_REPLY_CHARS}字" in prompt


# ---------------------------------------------------------------------------
# agent_provider._build_wechat_deep_prompt hard rule
# ---------------------------------------------------------------------------


def test_deep_prompt_default_hard_rule_uses_600_and_warns():
    prompt = ap._build_wechat_deep_prompt({"payload": {}})
    assert "6. 最多 600 字。超出部分会被系统强制截断。" in prompt
    # Legacy hard-coded text must be gone
    assert "6. 最多 600 字。\n" not in prompt


def test_deep_prompt_hard_rule_uses_payload_max_reply_chars():
    job = {"payload": {"max_reply_chars": 280}}
    prompt = ap._build_wechat_deep_prompt(job)
    assert "6. 最多 280 字。超出部分会被系统强制截断。" in prompt


def test_deep_prompt_hard_rule_uses_config_when_payload_missing():
    job = {"payload": {}, "config": {"reply_engine": {"max_reply_chars": 800}}}
    # _build_wechat_deep_prompt only looks at payload; verify the helper
    # behaviorally (so the provider has a path when payload omits the key).
    assert ap._resolve_max_reply_chars(job["payload"], job["config"]) == 800


def test_resolve_max_reply_chars_priority_order():
    # Payload wins over config
    assert ap._resolve_max_reply_chars({"max_reply_chars": 100}, {"reply_engine": {"max_reply_chars": 999}}) == 100
    # Falls back to config
    assert ap._resolve_max_reply_chars({}, {"reply_engine": {"max_reply_chars": 333}}) == 333
    # Falls back to default
    assert ap._resolve_max_reply_chars(None, None) == 600
    assert ap._resolve_max_reply_chars({}, {}) == 600
    # Invalid payload value skips to next candidate
    assert ap._resolve_max_reply_chars({"max_reply_chars": "oops"}, {"reply_engine": {"max_reply_chars": 222}}) == 222
    # Non-positive payload value skips to next candidate
    assert ap._resolve_max_reply_chars({"max_reply_chars": 0}, {"reply_engine": {"max_reply_chars": 222}}) == 222


# ---------------------------------------------------------------------------
# end-to-end: enqueue payload carries the cap so the deep-agent prompt picks
# it up automatically (no separate plumbing needed in agent_worker).
# ---------------------------------------------------------------------------


def test_enqueue_payload_carries_max_reply_chars_smoke(monkeypatch):
    """Smoke-test that generate_reply's deep_agent path attaches
    ``max_reply_chars`` into the enqueued payload.  We can't call generate_reply
    end-to-end without a wiki + provider, so we mock the router and the
    enqueue function and assert the payload content."""
    import types
    import reply_engine as re2

    captured = {}

    class _FakeJob:
        id = 1

    def _fake_enqueue(**kwargs):
        captured["payload"] = kwargs.get("payload")
        return {"id": 1, "payload": kwargs.get("payload")}

    monkeypatch.setattr(re2, "_agent_jobs", types.SimpleNamespace(enqueue_job=_fake_enqueue), raising=False)
    monkeypatch.setattr(re2, "_HAS_TASK_ROUTER", True)
    monkeypatch.setattr(
        re2._task_router,
        "ROUTE_DEEP",
        "deep",
        raising=False,
    )
    monkeypatch.setattr(re2, "_looks_like_smalltalk", lambda *_: False)

    class _Route:
        route = "deep"
        reason = "test"

    monkeypatch.setattr(
        re2._task_router,
        "route_message",
        lambda *_, **__: _Route(),
        raising=False,
    )

    cfg = {
        "reply_engine": {
            "raw_mode": True,
            "agent_mode": "standard",
            "max_reply_chars": 333,
        },
        "targets": [],
    }
    target = {"username": "u1", "name": "u1", "id": "t1"}
    message = {
        "content": "小助手 帮我分析一下",
        "local_id": 1,
        "local_type": 1,
        "chat_id": "c1",
        "talker_id": "c1",
        "sender_id": "s1",
    }
    # Stub wiki/knowledge lookups to keep the path simple.
    monkeypatch.setattr(re2, "retrieve_knowledge_layers", lambda *_, **__: {"core": [], "scene": []})

    re2.generate_reply(message, target, cfg)
    payload = captured.get("payload") or {}
    assert payload.get("max_reply_chars") == 333


def test_sanitize_reply_text_uses_payload_max_then_config_then_default():
    long_text = "中" * 700
    # payload wins
    assert len(re.sanitize_reply_text(long_text, {"max_reply_chars": 50}, {})) == 50
    # config fallback
    assert len(re.sanitize_reply_text(long_text, None, {"reply_engine": {"max_reply_chars": 80}})) == 80
    # default fallback
    assert len(re.sanitize_reply_text(long_text, None, None)) == 600


def test_agent_worker_process_once_sanitizes_reply_before_complete_and_send(monkeypatch, tmp_path):
    """Raw-agent/deep job result must be length-capped before complete_job and send."""
    import agent_worker as aw

    db_path = tmp_path / "jobs.db"
    import agent_jobs as jobs

    long_reply = "中" * 800
    class _Result:
        ok = True
        reply_text = long_reply
        provider = "echo"
        worker_id = "w1"
        status = "done"
        error = None
        latency = 0.1
        raw = {}
    class _Provider:
        def run(self, job, timeout=None):
            return _Result()

    # Track what gets completed and sent.
    completed_texts = []
    original_complete = jobs.complete_job
    def _capture_complete(job_id, result_text, *, db_path=None):
        completed_texts.append(result_text)
        return original_complete(job_id, result_text, db_path=db_path)
    monkeypatch.setattr(jobs, "complete_job", _capture_complete)

    sent_texts = []
    def _fake_send_result_back(job, result_text, config_path):
        sent_texts.append(result_text)
        return {"sent": True, "reason": "ok"}
    monkeypatch.setattr(aw, "_send_result_back", _fake_send_result_back)

    monkeypatch.setattr(aw, "_HAS_SENDER", True)

    # Enqueue a job with max_reply_chars=120 in payload.
    payload = {
        "target": {"username": "u1"},
        "agent_mode": "raw_agent",
        "max_reply_chars": 120,
    }
    jobs.enqueue_job(
        payload=payload,
        job_key="jk1",
        group_key="g1",
        task_type="reply",
        message_local_id=42,
        db_path=db_path,
    )

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"targets": [{"username": "u1", "name": "u1"}]}), encoding="utf-8")

    result = aw.process_once(
        provider=_Provider(),
        worker_id="w1",
        db_path=db_path,
        send_enabled=True,
        config_path=cfg_path,
    )
    assert result["action"] == "completed"
    assert len(completed_texts) == 1
    assert len(completed_texts[0]) == 120
    assert len(sent_texts) == 1
    assert len(sent_texts[0]) == 120
    assert len(result["reply_preview"]) == 120

    # Persisted result_text must also be the sanitized (truncated) version.
    job_after = jobs.get_job(int(result["job_id"]), db_path=db_path)
    assert job_after and len(job_after.get("result_text") or "") == 120


def test_agent_worker_process_once_preserves_full_text_when_cap_is_high(monkeypatch, tmp_path):
    """If payload max_reply_chars is larger than the reply, do not truncate."""
    import agent_worker as aw

    db_path = tmp_path / "jobs.db"
    import agent_jobs as jobs

    long_reply = "中" * 1200
    class _Result:
        ok = True
        reply_text = long_reply
        provider = "echo"
        worker_id = "w1"
        status = "done"
        error = None
        latency = 0.1
        raw = {}
    class _Provider:
        def run(self, job, timeout=None):
            return _Result()

    sent_texts = []
    def _fake_send_result_back(job, result_text, config_path):
        sent_texts.append(result_text)
        return {"sent": True, "reason": "ok"}
    monkeypatch.setattr(aw, "_send_result_back", _fake_send_result_back)
    monkeypatch.setattr(aw, "_HAS_SENDER", True)

    jobs.enqueue_job(
        payload={"target": {"username": "u1"}, "agent_mode": "raw_agent", "max_reply_chars": 2000},
        job_key="jk2",
        group_key="g2",
        task_type="reply",
        message_local_id=43,
        db_path=db_path,
    )

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"targets": [{"username": "u1", "name": "u1"}]}), encoding="utf-8")

    result = aw.process_once(
        provider=_Provider(),
        worker_id="w2",
        db_path=db_path,
        send_enabled=True,
        config_path=cfg_path,
    )
    assert result["action"] == "completed"
    assert len(sent_texts) == 1
    assert len(sent_texts[0]) == 1200
    job_after = jobs.get_job(int(result["job_id"]), db_path=db_path)
    assert job_after and len(job_after.get("result_text") or "") == 1200


def test_sanitize_reply_text_honors_high_payload_cap():
    """Payload cap above default must allow longer replies."""
    text = "中" * 1200
    # payload cap wins
    assert len(re.sanitize_reply_text(text, {"max_reply_chars": 2000}, {})) == 1200
    # config cap above default
    assert len(re.sanitize_reply_text(text, {}, {"reply_engine": {"max_reply_chars": 2000}})) == 1200
    # default still truncates to 600
    assert len(re.sanitize_reply_text(text, {}, {})) == 600
