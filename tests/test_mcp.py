"""MCP 客户端测试。用 tests/mcp_test_server.py 这个本地小服务器，不需要联网，也不需要 API Key。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.mcp_client import MCPManager, load_mcp_config, tool_name
from agent.tools import ToolCancelled, ToolContext, ToolError

SERVER = str(Path(__file__).with_name("mcp_test_server.py"))
GOOD = {"command": sys.executable, "args": [SERVER]}


class ConfigTests(unittest.TestCase):
    def write(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        return Path(tmp.name)

    def test_missing_file_means_no_servers(self):
        self.assertEqual(load_mcp_config(Path("/nonexistent/mcp.json")), {})

    def test_disabled_server_is_skipped(self):
        path = self.write(json.dumps({"mcpServers": {"a": GOOD, "b": {**GOOD, "disabled": True}}}))
        self.assertEqual(list(load_mcp_config(path)), ["a"])

    def test_bad_json(self):
        with self.assertRaisesRegex(ValueError, "不是合法的 JSON"):
            load_mcp_config(self.write('{"mcpServers": {'))

    def test_tool_name(self):
        self.assertEqual(tool_name("fetch", "fetch"), "mcp__fetch__fetch")
        self.assertRegex(tool_name("my server", "读.文件"), r"^[a-zA-Z0-9_-]+$")   # 空格、汉字、点都被换成 _
        self.assertLessEqual(len(tool_name("x" * 50, "y" * 50)), 64)


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.devnull = open(os.devnull, "w")
        cls.mcp = MCPManager(errlog=cls.devnull)   # 不让测试服务器的日志刷屏
        cls.tools = {t.name: t for t in cls.mcp.start({
            "test": GOOD,
            "broken": {"command": "this-command-does-not-exist-123"},
        })}

    @classmethod
    def tearDownClass(cls):
        cls.mcp.close()
        cls.devnull.close()

    def ctx(self):
        return ToolContext(workspace=Path(".").resolve(), cancel_event=threading.Event())

    def test_tools_are_listed_and_wrapped(self):
        self.assertEqual(sorted(self.tools), ["mcp__test__add", "mcp__test__fail", "mcp__test__slow"])
        add = self.tools["mcp__test__add"]
        self.assertTrue(add.needs_confirm)
        self.assertEqual(add.schema()["function"]["parameters"]["type"], "object")
        self.assertIn("a", add.schema()["function"]["parameters"]["properties"])

    def test_call(self):
        self.assertEqual(self.tools["mcp__test__add"].func(self.ctx(), a=2, b=3), "5")

    def test_server_error_becomes_tool_error(self):
        with self.assertRaisesRegex(ToolError, "fail"):
            self.tools["mcp__test__fail"].func(self.ctx())

    def test_broken_server_does_not_break_others(self):
        self.assertIn("broken", self.mcp.errors)
        self.assertIn("找不到", self.mcp.errors["broken"])
        self.assertEqual(self.mcp.connected, {"test": 3})
        self.assertIn("test（3 个工具）", self.mcp.summary())

    def test_cancel_slow_call_then_still_usable(self):
        ctx = self.ctx()
        threading.Timer(0.3, ctx.cancel_event.set).start()
        start = time.monotonic()
        with self.assertRaises(ToolCancelled):
            self.tools["mcp__test__slow"].func(ctx, seconds=10)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.tools["mcp__test__add"].func(self.ctx(), a=1, b=1), "2")


if __name__ == "__main__":
    unittest.main()