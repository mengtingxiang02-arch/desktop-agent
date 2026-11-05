"""防死循环（RepeatGuard）的测试。不需要 API Key。"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.core import Agent
from agent.guard import RepeatGuard


class RepeatGuardTests(unittest.TestCase):
    def test_blocks_after_limit(self):
        g = RepeatGuard(limit=3)
        for _ in range(3):
            self.assertIsNone(g.check("bash", {"command": "ls"}))
        self.assertIn("连续 4 次", g.check("bash", {"command": "ls"}))

    def test_different_call_in_between_resets(self):
        """改代码 → 跑测试 → 再改 → 再跑测试，不算重复。"""
        g = RepeatGuard(limit=1)
        self.assertIsNone(g.check("bash", {"command": "python -m unittest"}))
        self.assertIsNone(g.check("edit_file", {"path": "a.py"}))
        self.assertIsNone(g.check("bash", {"command": "python -m unittest"}))

    def test_argument_order_does_not_matter(self):
        g = RepeatGuard(limit=1)
        g.check("read_file", {"path": "a.py", "offset": 1})
        self.assertIsNotNone(g.check("read_file", {"offset": 1, "path": "a.py"}))

    def test_reset(self):
        g = RepeatGuard(limit=1)
        g.check("bash", {"command": "ls"})
        g.reset()
        self.assertIsNone(g.check("bash", {"command": "ls"}))


class FakeLLM:
    """按顺序返回写好的回复。**kwargs 用来接住流式回调等额外参数。"""

    def __init__(self, script):
        self.script = list(script)

    def chat(self, messages, tools=None, **kwargs):
        reply = self.script.pop(0)
        return reply, ("tool_calls" if reply.get("tool_calls") else "stop")


class AgentRepeatTests(unittest.TestCase):
    def test_agent_blocks_fourth_identical_call(self):
        def same_call(i):
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})}}]}

        llm = FakeLLM([same_call(i) for i in range(4)] + [{"role": "assistant", "content": "我换个办法。"}])
        events = []
        with tempfile.TemporaryDirectory() as d:
            agent = Agent(llm, Path(d), on_event=lambda kind, data: events.append(kind))
            agent.run("跑一下")

        results = [m["content"] for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(len(results), 4)
        self.assertIn("hi", results[2])             # 前 3 次正常执行
        self.assertIn("连续 4 次", results[3])       # 第 4 次被拦下，没有执行
        self.assertIn("warning", events)


if __name__ == "__main__":
    unittest.main()