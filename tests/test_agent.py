"""Agent 循环测试：用一个"假模型"按剧本回复，验证循环逻辑。不需要 API Key。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent.core import CANCELLED_RESULT, Agent
from agent.llm import LLMCancelled, LLMError


def tool_call(call_id, name, **args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def assistant(content=None, calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return msg


class FakeLLM:
    """按顺序返回预先写好的回复，并记录每次收到的消息条数。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, ("tool_calls" if item.get("tool_calls") else "stop")


class FakeStreamLLM:
    """支持流式的假模型：把回答拆成几片，一片一片交出去。"""

    def __init__(self, pieces, reply, finish="stop", cancel_after=None):
        self.pieces = pieces
        self.reply = reply
        self.finish = finish
        self.cancel_after = cancel_after      # 吐到第几片时"用户按了 Ctrl+C"
        self.seen_on_text = []

    def chat(self, messages, tools=None):        # 不该被调用
        raise AssertionError("有 chat_stream 就应该走流式")

    def chat_stream(self, messages, tools=None, on_text=None, cancel_event=None):
        for i, piece in enumerate(self.pieces):
            if self.cancel_after is not None and i == self.cancel_after:
                cancel_event.set()               # 模拟用户此刻按下 Ctrl+C
            if cancel_event is not None and cancel_event.is_set():
                raise LLMCancelled("".join(self.pieces[:i]))
            self.seen_on_text.append(piece)
            on_text(piece)
        return self.reply, self.finish


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.events = []

    def tearDown(self):
        self._tmp.cleanup()

    def make_agent(self, script, **kw):
        self.llm = FakeLLM(script)
        return Agent(self.llm, self.ws, on_event=lambda k, d: self.events.append((k, d)), **kw)

    def kinds(self):
        return [k for k, _ in self.events]

    def test_full_react_loop(self):
        """写文件 → 运行 → 读结果 → 最终回答，模拟一个完整任务。"""
        agent = self.make_agent([
            assistant("我先写一个脚本。", [tool_call("c1", "write_file", path="hi.py", content="print('hi')\n")]),
            assistant(None, [tool_call("c2", "bash", command="python3 hi.py")]),
            assistant("脚本运行成功，输出 hi。"),
        ])
        answer = agent.run("写个打印 hi 的脚本并运行")

        self.assertEqual(answer, "脚本运行成功，输出 hi。")
        self.assertTrue((self.ws / "hi.py").exists())
        # 第二次请求时，模型能看到第一次工具的结果（这就是"记忆"）
        second_request = self.llm.seen[1]
        self.assertEqual(second_request[-1]["role"], "tool")
        self.assertEqual(second_request[-1]["tool_call_id"], "c1")
        # 第三次请求里有 bash 的真实输出
        self.assertIn("hi", self.llm.seen[2][-1]["content"])
        self.assertEqual(self.kinds()[-1], "done")
        # 历史顺序：system, user, assistant, tool, assistant, tool, assistant
        self.assertEqual([m["role"] for m in agent.messages],
                         ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"])

    def test_bad_json_arguments_are_reported_back(self):
        bad = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{path: oops"}}
        agent = self.make_agent([assistant(None, [bad]), assistant("好的，我修正一下。")])
        agent.run("读文件")
        tool_msg = self.llm.seen[1][-1]
        self.assertIn("不是合法的 JSON", tool_msg["content"])

    def test_unknown_tool_and_wrong_param_name(self):
        agent = self.make_agent([
            assistant(None, [tool_call("c1", "delete_everything"),
                             tool_call("c2", "read_file", file="a.txt")]),   # 参数名写错
            assistant("知道了"),
        ])
        agent.run("x")
        results = [m["content"] for m in self.llm.seen[1] if m["role"] == "tool"]
        self.assertIn("没有名为 delete_everything 的工具", results[0])
        self.assertIn("参数不对", results[1])

    def test_tool_error_does_not_crash(self):
        agent = self.make_agent([
            assistant(None, [tool_call("c1", "read_file", path="missing.txt")]),
            assistant("文件不存在。"),
        ])
        self.assertEqual(agent.run("读 missing.txt"), "文件不存在。")
        self.assertIn("错误：文件不存在", self.llm.seen[1][-1]["content"])

    def test_user_denies_confirmation(self):
        agent = self.make_agent([
            assistant(None, [tool_call("c1", "bash", command="echo hi")]),
            assistant("好的，不执行。"),
        ], confirm=lambda name, args: False)
        agent.run("x")
        self.assertIn("用户拒绝", self.llm.seen[1][-1]["content"])

    def test_cancel_fills_every_tool_call(self):
        """中断时，没执行的 tool_call 也必须补上 tool 消息，否则下一次请求会 400。"""
        agent = self.make_agent([
            assistant(None, [tool_call("c1", "bash", command="sleep 10"),
                             tool_call("c2", "write_file", path="x.txt", content="x")]),
        ])
        threading.Timer(0.5, agent.cancel).start()
        agent.run("x")

        self.assertEqual(self.kinds()[-1], "cancelled")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["c1", "c2"])
        self.assertEqual(tool_msgs[1]["content"], CANCELLED_RESULT)
        self.assertFalse((self.ws / "x.txt").exists())

        # 中断后还能继续聊：历史是合法的
        agent.llm = FakeLLM([assistant("继续")])
        self.assertEqual(agent.run("继续吧"), "继续")

    def test_llm_error_removes_unanswered_user_message(self):
        agent = self.make_agent([LLMError("连不上模型服务")])
        agent.run("你好")
        self.assertEqual(self.kinds(), ["error"])
        self.assertEqual([m["role"] for m in agent.messages], ["system"])

    def test_max_steps(self):
        loop = [assistant(None, [tool_call(f"c{i}", "bash", command="echo 1")]) for i in range(5)]
        agent = self.make_agent(loop, max_steps=3)
        agent.run("x")
        self.assertEqual(self.kinds()[-1], "error")
        self.assertIn("3 步", self.events[-1][1]["text"])

    def test_streaming_events(self):
        """流式客户端：文字通过 assistant_delta 一片片发出去，最终回答仍然完整。"""
        llm = FakeStreamLLM(["你好", "，我是", "助手。"],
                            {"role": "assistant", "content": "你好，我是助手。"})
        agent = Agent(llm, self.ws, on_event=lambda k, d: self.events.append((k, d)))
        self.assertEqual(agent.run("hi"), "你好，我是助手。")
        self.assertEqual([d["text"] for k, d in self.events if k == "assistant_delta"],
                         ["你好", "，我是", "助手。"])
        self.assertNotIn("assistant_text", self.kinds())   # 不要重复整段打印
        self.assertEqual(self.kinds()[-1], "done")
        self.assertEqual(agent.messages[-1]["content"], "你好，我是助手。")

    def test_cancel_while_streaming(self):
        """流式输出中途被打断：历史里不留半截回答，这条提问也撤掉。"""
        llm = FakeStreamLLM(["一", "二", "三", "四"],
                            {"role": "assistant", "content": "一二三四"},
                            cancel_after=2)
        agent = Agent(llm, self.ws, on_event=lambda k, d: self.events.append((k, d)))
        self.assertEqual(agent.run("hi"), "")
        self.assertEqual(self.kinds()[-1], "cancelled")
        self.assertEqual([m["role"] for m in agent.messages], ["system"])
        # 只吐出了前两片，后面被叫停了
        self.assertEqual(llm.seen_on_text, ["一", "二"])

    def test_reset(self):
        agent = self.make_agent([assistant("hi")])
        agent.run("hello")
        agent.reset()
        self.assertEqual(len(agent.messages), 1)


if __name__ == "__main__":
    unittest.main()
