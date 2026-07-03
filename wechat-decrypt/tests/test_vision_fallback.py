"""Tests for the multi-source vision fallback chain."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import image_handler as ih
import reply_engine as re


def test_is_vision_error_detects_error_markers():
    assert ih.is_vision_error("[VLM Error] mmx not found")
    assert ih.is_vision_error("[Vision Hook Error] exit=1")
    assert not ih.is_vision_error("这是一张图片")
    assert ih.is_vision_error("")


def test_recognize_image_with_fallback_uses_first_successful_source(monkeypatch, tmp_path):
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    calls = []

    def fake_mmx(path, prompt):
        calls.append("mmx")
        return "mmx description"

    monkeypatch.setattr(ih, "mmx_recognize_image", fake_mmx)
    result = ih.recognize_image_with_fallback(str(img), sources=["mmx", "ocr"])
    assert result == "mmx description"
    assert calls == ["mmx"]


def test_recognize_image_with_fallback_falls_back_to_hook(monkeypatch, tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    def fake_mmx(path, prompt):
        return "[VLM Error] mmx failed"

    def fake_hook(image_path, hook_cmd):
        return "hook description"

    monkeypatch.setattr(ih, "mmx_recognize_image", fake_mmx)
    monkeypatch.setattr(ih, "run_vision_hook", fake_hook)

    result = ih.recognize_image_with_fallback(
        str(img),
        sources=["mmx", "hook:custom"],
        hooks={"custom": ["python", "hook.py"]},
    )
    assert result == "hook description"


def test_recognize_image_with_fallback_falls_back_to_ocr(monkeypatch, tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    def fake_mmx(path, prompt):
        return "[VLM Error] mmx failed"

    def fake_ocr(path):
        return "[OCR 识别结果]\nhello world"

    monkeypatch.setattr(ih, "mmx_recognize_image", fake_mmx)
    monkeypatch.setattr(ih, "_try_ocr", fake_ocr)

    result = ih.recognize_image_with_fallback(str(img), sources=["mmx", "ocr"])
    assert result == "[OCR 识别结果]\nhello world"


def test_recognize_image_with_fallback_returns_metadata_when_all_fail(monkeypatch, tmp_path):
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    monkeypatch.setattr(ih, "mmx_recognize_image", lambda path, prompt: "[VLM Error] failed")
    monkeypatch.setattr(ih, "_try_ocr", lambda path: None)

    result = ih.recognize_image_with_fallback(str(img), sources=["mmx", "ocr"])
    assert "[图片元数据]" in result
    assert "JPEG" in result or "unknown" in result


def test_recognize_image_with_fallback_no_metadata_returns_error(monkeypatch, tmp_path):
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    monkeypatch.setattr(ih, "mmx_recognize_image", lambda path, prompt: "[VLM Error] failed")
    monkeypatch.setattr(ih, "_try_ocr", lambda path: None)

    result = ih.recognize_image_with_fallback(str(img), sources=["mmx", "ocr"], fallback_metadata=False)
    assert result.startswith("[VLM Error]")


def test_resolve_vision_sources_uses_explicit_sources():
    target = {"vision": {"sources": ["hook:a", "mmx"], "hooks": {"a": ["x"]}, "fallback_metadata": False}}
    sources, hooks, fallback = re._resolve_vision_sources(target, {})
    assert sources == ["hook:a", "mmx"]
    assert hooks == {"a": ["x"]}
    assert fallback is False


def test_resolve_vision_sources_derives_from_llm_vision_mode():
    target = {"vision": {"mode": "llm_vision"}}
    sources, hooks, fallback = re._resolve_vision_sources(target, {})
    assert sources == ["mmx", "ocr"]
    assert fallback is True


def test_resolve_vision_sources_derives_from_hook_mode():
    target = {"vision": {"mode": "hook", "hook_cmd": ["python", "hook.py"]}}
    sources, hooks, fallback = re._resolve_vision_sources(target, {})
    assert sources == ["hook:default", "mmx", "ocr"]
    assert hooks == {"default": ["python", "hook.py"]}
    assert fallback is True


def test_call_vision_recognizer_skips_error_results(monkeypatch, tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    calls = []

    def fake_mmx(path, prompt):
        calls.append("mmx")
        return "[VLM Error] network"

    def fake_ocr(path):
        calls.append("ocr")
        return "ocr text"

    monkeypatch.setattr(ih, "mmx_recognize_image", fake_mmx)
    monkeypatch.setattr(ih, "_try_ocr", fake_ocr)

    result = re._call_vision_recognizer(str(img), target={"vision": {"sources": ["mmx", "ocr"]}}, config={})
    assert result == "ocr text"
    assert calls == ["mmx", "ocr"]


def test_try_vision_hook_uses_fallback_chain(monkeypatch, tmp_path):
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    def fake_recognizer(image_path, user_prompt="", target=None, config=None):
        # Verify the hook is configured as the first source.
        sources, hooks, _ = re._resolve_vision_sources(target, config)
        assert sources[0] == "hook:default"
        assert hooks.get("default") == ["python", "hook.py"]
        return "fallback description"

    monkeypatch.setattr(re, "_call_vision_recognizer", fake_recognizer)
    result = re._try_vision_hook(str(img), ["python", "hook.py"], "raw text")
    assert "fallback description" in result
