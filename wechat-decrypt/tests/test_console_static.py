"""Tests for control_api static file resolution."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_api


def test_resolve_console_static_root_and_index():
    for path in ("/console", "/console/"):
        candidate, mime = control_api._resolve_console_static(path)
        assert candidate is not None
        assert candidate.name == "index.html"
        assert mime == "text/html; charset=utf-8"


def test_resolve_console_static_real_js_file():
    candidate, mime = control_api._resolve_console_static("/console/js/api.js")
    assert candidate is not None
    assert candidate.name == "api.js"
    assert mime == "text/javascript; charset=utf-8"


def test_resolve_console_static_rejects_traversal():
    for path in ("/console/../x", "/console/%2E%2E/x"):
        candidate, mime = control_api._resolve_console_static(path)
        assert candidate is None, path
        assert mime == "", path


def test_resolve_console_static_rejects_unknown_extension():
    candidate, mime = control_api._resolve_console_static("/console/missing.exe")
    assert candidate is None
    assert mime == ""


def test_resolve_console_static_rejects_missing_file():
    candidate, mime = control_api._resolve_console_static("/console/no/such/file.html")
    assert candidate is None
    assert mime == ""
