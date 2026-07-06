import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcp_leann


class McpLeannToolTests(unittest.TestCase):
    def test_leann_search_returns_hits_summary(self):
        stdout = json.dumps({
            "ok": True,
            "hits": [
                {
                    "id": "doc1",
                    "score": 0.9,
                    "text": "这是第一条结果的内容。",
                    "metadata": {"source": "kb/faq.md", "file_name": "faq.md"},
                },
                {
                    "id": "doc2",
                    "score": 0.8,
                    "text": "第二条结果内容。",
                    "metadata": {},
                },
            ],
        })
        with mock.patch("mcp_leann.subprocess.run", return_value=mock.Mock(
            returncode=0, stdout=stdout, stderr=""
        )):
            result = mcp_leann.leann_search("work", "测试", top_k=2)
        self.assertIn("索引 'work' 搜索结果", result)
        self.assertIn("[1]", result)
        self.assertIn("这是第一条结果的内容", result)
        self.assertIn("[2]", result)

    def test_leann_search_returns_error_when_cli_fails(self):
        with mock.patch("mcp_leann.subprocess.run", return_value=mock.Mock(
            returncode=1, stdout="", stderr="index not found"
        )):
            result = mcp_leann.leann_search("missing", "q")
        self.assertIn("错误", result)
        self.assertIn("rc=1", result)

    def test_leann_search_returns_error_payload_message(self):
        stdout = json.dumps({"ok": False, "hits": [], "error": "index 'x' not found"})
        with mock.patch("mcp_leann.subprocess.run", return_value=mock.Mock(
            returncode=0, stdout=stdout, stderr=""
        )):
            result = mcp_leann.leann_search("x", "q")
        self.assertIn("index 'x' not found", result)

    def test_leann_search_no_hits(self):
        stdout = json.dumps({"ok": True, "hits": []})
        with mock.patch("mcp_leann.subprocess.run", return_value=mock.Mock(
            returncode=0, stdout=stdout, stderr=""
        )):
            result = mcp_leann.leann_search("work", "unlikely_xyz")
        self.assertIn("未找到相关结果", result)

    def test_leann_search_subprocess_timeout_returns_readable_error(self):
        with mock.patch("mcp_leann.subprocess.run", side_effect=mcp_leann.subprocess.TimeoutExpired("leann", 120)):
            result = mcp_leann.leann_search("work", "q")
        self.assertIn("超时", result)

    def test_leann_search_missing_args(self):
        self.assertIn("index_name", mcp_leann.leann_search("", "q"))
        self.assertIn("query", mcp_leann.leann_search("work", ""))


if __name__ == "__main__":
    unittest.main()
