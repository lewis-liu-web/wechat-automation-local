"""Tests for the KB file upload staging endpoint."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_api


def _route(method, path, params=None, body=None):
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, params or {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _write_cfg(tmp_path, knowledge_bases):
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(
        json.dumps(
            {
                "wiki_dir": str(tmp_path),
                "targets": [],
                "knowledge_bases": knowledge_bases,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return cfg_path


def _patch_cfg(monkeypatch, cfg_path):
    monkeypatch.setattr(
        control_api.reg,
        "load_config",
        lambda _path=None: json.loads(cfg_path.read_text()),
    )


def test_upload_two_files_to_local_kb(monkeypatch, tmp_path):
    cfg_path = _write_cfg(tmp_path, {})
    _patch_cfg(monkeypatch, cfg_path)
    monkeypatch.setattr(control_api, "KB_UPLOAD_STAGING", tmp_path / "staging")

    # create local KB
    control_api.reg.create_local_kb_dir("local_kb", config_path=cfg_path)

    body = {
        "files": [
            {
                "filename": "hello.txt",
                "content_b64": base64.b64encode(b"hello world").decode("ascii"),
            },
            {
                "filename": "utf8.md",
                "content_b64": base64.b64encode("你好，世界".encode("utf-8")).decode("ascii"),
            },
        ]
    }
    status, payload = _route("POST", "/kbs/local_kb/upload", body=body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["staged"] == 2

    staging_dir = Path(payload["staging_dir"])
    assert staging_dir.exists()
    assert (staging_dir / "hello.txt").read_bytes() == b"hello world"
    assert (staging_dir / "utf8.md").read_text(encoding="utf-8") == "你好，世界"
    assert not (staging_dir / "sub").exists()


def test_upload_strips_path_traversal(monkeypatch, tmp_path):
    cfg_path = _write_cfg(tmp_path, {})
    _patch_cfg(monkeypatch, cfg_path)
    monkeypatch.setattr(control_api, "KB_UPLOAD_STAGING", tmp_path / "staging")

    control_api.reg.create_local_kb_dir("local_kb", config_path=cfg_path)

    body = {
        "files": [
            {
                "filename": "sub/evil.txt",
                "content_b64": base64.b64encode(b"payload").decode("ascii"),
            }
        ]
    }
    status, payload = _route("POST", "/kbs/local_kb/upload", body=body)
    assert status == 200
    assert payload["staged"] == 1

    staging_dir = Path(payload["staging_dir"])
    assert (staging_dir / "evil.txt").read_bytes() == b"payload"
    assert not (staging_dir / "sub").exists()


def test_upload_kb_not_found(monkeypatch, tmp_path):
    cfg_path = _write_cfg(tmp_path, {})
    _patch_cfg(monkeypatch, cfg_path)
    monkeypatch.setattr(control_api, "KB_UPLOAD_STAGING", tmp_path / "staging")

    status, payload = _route(
        "POST", "/kbs/missing_kb/upload", body={"files": []}
    )
    assert status == 404
    assert payload["ok"] is False
    assert "missing_kb" in payload["error"]


def test_upload_non_local_kb_returns_400(monkeypatch, tmp_path):
    cfg_path = _write_cfg(
        tmp_path,
        {
            "online_kb": {
                "type": "getnote",
                "knowledge_base_id": "kid123",
                "enabled": True,
            }
        },
    )
    _patch_cfg(monkeypatch, cfg_path)
    monkeypatch.setattr(control_api, "KB_UPLOAD_STAGING", tmp_path / "staging")

    status, payload = _route(
        "POST",
        "/kbs/online_kb/upload",
        body={
            "files": [
                {"filename": "x.txt", "content_b64": base64.b64encode(b"x").decode("ascii")}
            ]
        },
    )
    assert status == 400
    assert payload["ok"] is False
    assert "not local" in payload["error"]


def test_upload_too_large_returns_413(monkeypatch, tmp_path):
    cfg_path = _write_cfg(tmp_path, {})
    _patch_cfg(monkeypatch, cfg_path)
    monkeypatch.setattr(control_api, "KB_UPLOAD_STAGING", tmp_path / "staging")
    control_api.reg.create_local_kb_dir("local_kb", config_path=cfg_path)

    def _huge_decode(data, validate=False):
        # Decoding a tiny b64 payload as if it were >20 MiB.
        return b"x" * (20 * 1024 * 1024 + 1)

    monkeypatch.setattr(control_api.base64, "b64decode", _huge_decode)

    body = {
        "files": [
            {"filename": "big.bin", "content_b64": base64.b64encode(b"small").decode("ascii")}
        ]
    }
    status, payload = _route("POST", "/kbs/local_kb/upload", body=body)
    assert status == 413
    assert payload["ok"] is False
    assert "too large" in payload["error"].lower()


def test_upload_invalid_base64_returns_400(monkeypatch, tmp_path):
    cfg_path = _write_cfg(tmp_path, {})
    _patch_cfg(monkeypatch, cfg_path)
    monkeypatch.setattr(control_api, "KB_UPLOAD_STAGING", tmp_path / "staging")
    control_api.reg.create_local_kb_dir("local_kb", config_path=cfg_path)

    status, payload = _route(
        "POST",
        "/kbs/local_kb/upload",
        body={"files": [{"filename": "bad.txt", "content_b64": "not-base64!!!"}]},
    )
    assert status == 400
    assert payload["ok"] is False
    assert "invalid base64" in payload["error"].lower()


def test_upload_invalid_filename_returns_400(monkeypatch, tmp_path):
    cfg_path = _write_cfg(tmp_path, {})
    _patch_cfg(monkeypatch, cfg_path)
    monkeypatch.setattr(control_api, "KB_UPLOAD_STAGING", tmp_path / "staging")
    control_api.reg.create_local_kb_dir("local_kb", config_path=cfg_path)

    status, payload = _route(
        "POST",
        "/kbs/local_kb/upload",
        body={"files": [{"filename": ".", "content_b64": ""}]},
    )
    assert status == 400
    assert payload["ok"] is False
    assert "invalid filename" in payload["error"].lower()
