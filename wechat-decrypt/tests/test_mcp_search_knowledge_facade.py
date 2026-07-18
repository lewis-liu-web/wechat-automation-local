"""Focused tests for the target-authorized knowledge search MCP server.

These tests exercise the new knowledge_retrieval facade wiring, the structured
tool response, provenance/trace writing, and the absence of any reply_engine
import path.  They do not require a real MCP runtime, real knowledge bases, or
Hermes to be installed.
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class FakeKnowledgeSearchResult:
    """Minimal stand-in for knowledge_retrieval.KnowledgeSearchResult."""
    def __init__(self, status, hits, provenance, error=None):
        self.status = status
        self.hits = hits
        self.provenance = provenance
        self.error = error


class FakeKnowledgeRetrieval:
    """Recordable fake facade used by the MCP server tests."""
    KnowledgeSearchResult = FakeKnowledgeSearchResult

    def __init__(self):
        self.calls = []
        self.return_value = FakeKnowledgeSearchResult(
            "no_hit", [], [], None
        )
        self._handler = None

    def search_knowledge(self, query, config, allowed_kb_ids, limit=5,
                         core_limit=0, scene_limit=None, config_path=None):
        self.calls.append({
            "query": query,
            "config": config,
            "allowed_kb_ids": allowed_kb_ids,
            "limit": limit,
            "core_limit": core_limit,
            "scene_limit": scene_limit,
            "config_path": config_path,
        })
        if self._handler is not None:
            return self._handler(query, config, allowed_kb_ids, limit=limit,
                                 core_limit=core_limit, scene_limit=scene_limit,
                                 config_path=config_path)
        return self.return_value


class FakeFastMCP:
    """Minimal FastMCP stand-in that captures registered tools."""
    def __init__(self, name, dependencies=None):
        self.name = name
        self.tools = {}

    def tool(self, fn=None):
        def decorator(f):
            self.tools[f.__name__] = f
            return f
        if callable(fn):
            return decorator(fn)
        return decorator


def _load_server(fake_kr):
    """Load the MCP server module with injected fake dependencies."""
    kr_mod = types.ModuleType("knowledge_retrieval")
    kr_mod.search_knowledge = fake_kr.search_knowledge
    kr_mod.KnowledgeSearchResult = fake_kr.KnowledgeSearchResult
    sys.modules["knowledge_retrieval"] = kr_mod

    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = FakeFastMCP
    sys.modules["mcp.server.fastmcp"] = fastmcp_mod

    spec = importlib.util.spec_from_file_location(
        "search_knowledge_mcp_server",
        ROOT / "tools" / "search_knowledge_mcp_server.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSearchKnowledgeMCP(unittest.TestCase):
    def setUp(self):
        self.fake_kr = FakeKnowledgeRetrieval()
        self._orig_kr = sys.modules.get("knowledge_retrieval")
        self.server = _load_server(self.fake_kr)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore_modules)
        self.addCleanup(self.tmp.cleanup)
        self._clear_env()

    def _restore_modules(self):
        if self._orig_kr is None:
            sys.modules.pop("knowledge_retrieval", None)
        else:
            sys.modules["knowledge_retrieval"] = self._orig_kr
        # Remove the dynamically loaded MCP server so it can be reloaded cleanly
        # by other tests; the fake fastmcp module is harmless but can be dropped.
        sys.modules.pop("search_knowledge_mcp_server", None)
        sys.modules.pop("mcp.server.fastmcp", None)

    def tearDown(self):
        self._clear_env()

    def _clear_env(self):
        for key in (
            "WECHAT_MCP_CONFIG",
            "WECHAT_MCP_ALLOWED_KB_IDS",
            "WECHAT_MCP_TARGET_ID",
            "WECHAT_MCP_WIKI_DIR",
            "WECHAT_MCP_TRACE_ID",
            "WECHAT_MCP_TRACE_DIR",
        ):
            os.environ.pop(key, None)

    def _write_config(self, data):
        config_dir = Path(self.tmp.name) / "config"
        config_dir.mkdir(exist_ok=True)
        path = config_dir / "targets.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _trace_path(self, trace_id, trace_dir=None):
        trace_dir = trace_dir or os.environ.get("WECHAT_MCP_TRACE_DIR")
        return Path(trace_dir) / f"{trace_id}.json"

    def test_no_reply_engine_import_path(self):
        """The MCP server must never import from reply_engine."""
        source = (ROOT / "tools" / "search_knowledge_mcp_server.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("from reply_engine", source)
        self.assertNotIn("import reply_engine", source)

    def test_docstring_documents_structured_response(self):
        """The tool docstring must document the stable structured response."""
        doc = self.server.search_knowledge.__doc__ or ""
        for token in ("status", "hits", "provenance", "error", "no_source_reason"):
            self.assertIn(token, doc)

    def test_empty_allowlist_returns_invalid(self):
        """With no authorized KBs, the tool returns a structured invalid response."""
        os.environ["WECHAT_MCP_TRACE_ID"] = "t-1"
        os.environ["WECHAT_MCP_TRACE_DIR"] = self.tmp.name
        result = self.server.search_knowledge("query")
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["provenance"], [])
        self.assertEqual(result["no_source_reason"], "no authorized knowledge bases")
        self.assertFalse(self.fake_kr.calls)
        # Trace is written as invalid per the worker trace protocol.
        trace = json.loads(self._trace_path("t-1").read_text(encoding="utf-8"))
        self.assertEqual(trace["trace_id"], "t-1")
        self.assertEqual(trace["status"], "invalid")
        self.assertEqual(trace["no_source_reason"], "no authorized knowledge bases")

    def test_allowed_kb_ids_not_widened_by_agent_input(self):
        """The agent can only pass query and limit; allowed_kb_ids comes from env."""
        cfg_path = self._write_config({"wiki_dir": self.tmp.name})
        os.environ["WECHAT_MCP_CONFIG"] = cfg_path
        os.environ["WECHAT_MCP_ALLOWED_KB_IDS"] = json.dumps(["scene.a"])
        self.fake_kr.return_value = FakeKnowledgeSearchResult(
            "ok",
            [{"content": "hit"}],
            [{"kb_id": "scene.a", "rel_path": "faq.md"}],
        )
        self.server.search_knowledge("query", limit=3)
        self.assertEqual(len(self.fake_kr.calls), 1)
        call = self.fake_kr.calls[0]
        self.assertEqual(call["allowed_kb_ids"], ["scene.a"])
        self.assertEqual(call["limit"], 3)
        self.assertEqual(call["core_limit"], 0)
        self.assertEqual(call["scene_limit"], 3)
        self.assertEqual(call["config_path"], cfg_path)

    def test_facade_ok_response_carries_hits_and_provenance(self):
        """A successful search returns structured hits and provenance."""
        cfg_path = self._write_config({"wiki_dir": self.tmp.name})
        os.environ["WECHAT_MCP_CONFIG"] = cfg_path
        os.environ["WECHAT_MCP_ALLOWED_KB_IDS"] = json.dumps(["scene.a"])
        os.environ["WECHAT_MCP_TRACE_ID"] = "t-ok"
        os.environ["WECHAT_MCP_TRACE_DIR"] = self.tmp.name
        self.fake_kr.return_value = FakeKnowledgeSearchResult(
            "ok",
            [{"content": "hit"}],
            [{"kb_id": "scene.a", "rel_path": "faq.md"}],
        )
        result = self.server.search_knowledge("query")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hits"], [{"content": "hit"}])
        self.assertEqual(result["provenance"], [{"kb_id": "scene.a", "rel_path": "faq.md"}])
        self.assertEqual(result["no_source_reason"], "")
        trace = json.loads(self._trace_path("t-ok").read_text(encoding="utf-8"))
        self.assertEqual(trace["status"], "ok")
        self.assertEqual(trace["provenance"], [{"kb_id": "scene.a", "rel_path": "faq.md"}])
        self.assertEqual(trace["no_source_reason"], "")

    def test_facade_no_hit_response(self):
        """A clean no-hit response is distinct from provider failure."""
        cfg_path = self._write_config({"wiki_dir": self.tmp.name})
        os.environ["WECHAT_MCP_CONFIG"] = cfg_path
        os.environ["WECHAT_MCP_ALLOWED_KB_IDS"] = json.dumps(["scene.a"])
        os.environ["WECHAT_MCP_TRACE_ID"] = "t-no"
        os.environ["WECHAT_MCP_TRACE_DIR"] = self.tmp.name
        self.fake_kr.return_value = FakeKnowledgeSearchResult(
            "no_hit", [], [], None
        )
        result = self.server.search_knowledge("query")
        self.assertEqual(result["status"], "no_hit")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["no_source_reason"], "query returned no hits")
        trace = json.loads(self._trace_path("t-no").read_text(encoding="utf-8"))
        self.assertEqual(trace["status"], "no_hit")
        self.assertEqual(trace["no_source_reason"], "query returned no hits")

    def test_facade_provider_failure(self):
        """A facade exception is surfaced as a structured provider failure."""
        cfg_path = self._write_config({"wiki_dir": self.tmp.name})
        os.environ["WECHAT_MCP_CONFIG"] = cfg_path
        os.environ["WECHAT_MCP_ALLOWED_KB_IDS"] = json.dumps(["scene.a"])
        os.environ["WECHAT_MCP_TRACE_ID"] = "t-fail"
        os.environ["WECHAT_MCP_TRACE_DIR"] = self.tmp.name

        def boom(*args, **kwargs):
            raise RuntimeError("kb unreachable")
        self.fake_kr._handler = boom

        result = self.server.search_knowledge("query")
        self.assertEqual(result["status"], "provider_failure")
        self.assertIn("kb unreachable", result["error"])
        self.assertEqual(result["no_source_reason"], "kb unreachable")
        trace = json.loads(self._trace_path("t-fail").read_text(encoding="utf-8"))
        self.assertEqual(trace["status"], "provider_failure")
        self.assertEqual(trace["no_source_reason"], "kb unreachable")

    def test_trace_not_written_when_env_missing(self):
        """If the worker does not set trace env, the MCP server must not invent a path."""
        cfg_path = self._write_config({"wiki_dir": self.tmp.name})
        os.environ["WECHAT_MCP_CONFIG"] = cfg_path
        os.environ["WECHAT_MCP_ALLOWED_KB_IDS"] = json.dumps(["scene.a"])
        self.fake_kr.return_value = FakeKnowledgeSearchResult(
            "ok", [{"content": "hit"}], [{"kb_id": "scene.a"}], None
        )
        self.server.search_knowledge("query")
        # No trace files should be created under the temp dir.
        self.assertEqual(list(Path(self.tmp.name).glob("*.json")), [])

    def test_trace_file_is_atomic_and_fixed_name(self):
        """The trace is written atomically to {trace_dir}/{trace_id}.json."""
        cfg_path = self._write_config({"wiki_dir": self.tmp.name})
        os.environ["WECHAT_MCP_CONFIG"] = cfg_path
        os.environ["WECHAT_MCP_ALLOWED_KB_IDS"] = json.dumps(["scene.a"])
        os.environ["WECHAT_MCP_TRACE_ID"] = "t-atomic"
        os.environ["WECHAT_MCP_TRACE_DIR"] = self.tmp.name
        self.fake_kr.return_value = FakeKnowledgeSearchResult(
            "ok", [], [], None
        )
        self.server.search_knowledge("query")
        trace_path = self._trace_path("t-atomic")
        self.assertTrue(trace_path.exists())
        # No partial tmp files should remain.
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])
        data = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"trace_id", "status", "provenance", "no_source_reason"})


if __name__ == "__main__":
    unittest.main()
