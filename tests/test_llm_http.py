"""端到端测试：在本机起一个"假的 DeepSeek 服务器"，让真正的 OpenAI SDK 去请求它。

验证两件事：
1. 整条链路（SDK 发请求 → 解析 tool_calls → 执行工具 → 再请求）是通的；
2. DeepSeek 思考模式的坑：带 tools 时，reasoning_content 必须原样传回，否则服务器返回 400。
   这个假服务器就模拟了这条规则。
"""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent.core import Agent
from agent.llm import ChatLLM, LLMError

# 一张 1×1 像素的 PNG 图片
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


def completion(message, finish="stop"):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def chunk(delta, finish=None):
    return {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def stream_chunks(message):
    """把一条完整的 message 拆成 SSE 片段，模拟真实服务商的流式返回。

    刻意把内容和 tool_calls 的参数都切碎，用来验证客户端会把碎片拼回去。
    """
    pieces = []
    reasoning = message.get("reasoning_content")
    if reasoning:
        mid = len(reasoning) // 2
        pieces.append(chunk({"role": "assistant", "reasoning_content": reasoning[:mid]}))
        pieces.append(chunk({"reasoning_content": reasoning[mid:]}))

    content = message.get("content") or ""
    if content:
        pieces.append(chunk({"role": "assistant", "content": content[0]}))
        if len(content) > 1:
            pieces.append(chunk({"content": content[1:]}))

    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = tc["function"]
        # 第一片只带 id 和 name，参数切成两半放在后面两片里（真实服务商就是这么发的）
        pieces.append(chunk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                            "function": {"name": fn["name"], "arguments": ""}}]}))
        args = fn["arguments"]
        mid = len(args) // 2
        pieces.append(chunk({"tool_calls": [{"index": i, "function": {"arguments": args[:mid]}}]}))
        pieces.append(chunk({"tool_calls": [{"index": i, "function": {"arguments": args[mid:]}}]}))

    return pieces


class FakeDeepSeek(BaseHTTPRequestHandler):
    requests: list = []
    mode = "normal"
    delay = 0.0        # 每片 SSE 之间暂停多久，用来模拟"模型还在慢吞吞地吐字"

    def log_message(self, *a):  # 不要把请求日志打到测试输出里
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, message, finish):
        """按 SSE 协议一小片一小片地返回（text/event-stream）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in stream_chunks(message):
            self.wfile.write(f"data: {json.dumps(piece, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            if FakeDeepSeek.delay:
                time.sleep(FakeDeepSeek.delay)
        self.wfile.write(f"data: {json.dumps(chunk({}, finish))}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _reply(self, body, message, finish):
        if body.get("stream"):
            return self._send_stream(message, finish)
        return self._send(200, completion(message, finish))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeDeepSeek.requests.append(body)

        if FakeDeepSeek.mode == "unauthorized":
            return self._send(401, {"error": {"message": "Authentication Fails", "type": "authentication_error"}})

        # 模拟纯文字模型收到图片时的报错（DeepSeek 的纯文字模型就是这样回的）
        has_image = any(isinstance(m.get("content"), list) and
                        any(part.get("type") == "image_url" for part in m["content"])
                        for m in body["messages"])
        if has_image and FakeDeepSeek.mode == "text_only":
            return self._send(400, {"error": {
                "message": "Failed to deserialize the JSON body: unknown variant `image_url`, expected `text`",
                "type": "invalid_request_error"}})

        # 模拟 DeepSeek 的规则：带 tools 时，历史里每条 assistant 消息都要有 reasoning_content
        if body.get("tools"):
            for m in body["messages"]:
                if m["role"] == "assistant" and "reasoning_content" not in m:
                    return self._send(400, {"error": {
                        "message": "The reasoning_content in the thinking mode must be passed back to the API.",
                        "type": "invalid_request_error"}})

        n_assistant = sum(1 for m in body["messages"] if m["role"] == "assistant")
        try:
            if FakeDeepSeek.mode in ("vision", "text_only"):
                if n_assistant == 0:
                    return self._reply(body, {
                        "role": "assistant", "content": "", "reasoning_content": "先读图片。",
                        "tool_calls": [{"id": "call_1", "type": "function", "function": {
                            "name": "read_file", "arguments": json.dumps({"path": "dot.png"})}}],
                    }, "tool_calls")
                return self._reply(body, {"role": "assistant", "content": "图里是一个点。",
                                          "reasoning_content": "看到了。"}, "stop")
            if n_assistant == 0:
                return self._reply(body, {
                    "role": "assistant", "content": "",
                    "reasoning_content": "用户要一个文件，我用 write_file。",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "note.txt", "content": "你好"}, ensure_ascii=False)}}],
                }, "tool_calls")
            return self._reply(body, {
                "role": "assistant", "content": "已经写好 note.txt。",
                "reasoning_content": "工具成功了，可以收尾。"}, "stop")
        except (BrokenPipeError, ConnectionResetError):
            # 客户端中途断开（用户按了 Ctrl+C），服务器这边很正常，别刷屏
            pass


class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"   # 本机请求不要走代理
        cls.server = HTTPServer(("127.0.0.1", 0), FakeDeepSeek)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeDeepSeek.requests = []
        FakeDeepSeek.mode = "normal"
        FakeDeepSeek.delay = 0.0
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_tool_loop_and_reasoning_content_round_trip(self):
        llm = ChatLLM("sk-test", self.base_url, "deepseek-flash", max_retries=0)
        events = []
        agent = Agent(llm, self.ws, on_event=lambda k, d: events.append(k))
        answer = agent.run("帮我建一个 note.txt，内容是你好")

        self.assertEqual(answer, "已经写好 note.txt。")
        self.assertEqual((self.ws / "note.txt").read_text(encoding="utf-8"), "你好")
        self.assertEqual(len(FakeDeepSeek.requests), 2)
        sent_back = [m for m in FakeDeepSeek.requests[1]["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(sent_back["reasoning_content"], "用户要一个文件，我用 write_file。")
        self.assertEqual(FakeDeepSeek.requests[0]["tools"][0]["function"]["name"], "read_file")
        self.assertNotIn("error", events)

    def test_stream_emits_deltas_and_assembles_tool_calls(self):
        """流式：文字一小片一小片地来，tool_calls 的碎片也要被正确拼回去。"""
        llm = ChatLLM("sk-test", self.base_url, "deepseek-flash", max_retries=0)
        events = []
        agent = Agent(llm, self.ws, on_event=lambda k, d: events.append((k, d)))
        answer = agent.run("帮我建一个 note.txt")

        self.assertEqual(answer, "已经写好 note.txt。")
        # 打字机效果靠的就是这些片段事件
        deltas = [d["text"] for k, d in events if k == "assistant_delta"]
        self.assertGreaterEqual(len(deltas), 2)
        self.assertEqual("".join(deltas), "已经写好 note.txt。")
        # 已经流式输出过了，就不该再整段重发一次
        self.assertNotIn("assistant_text", [k for k, _ in events])
        # 被切成三片的 write_file 参数拼回来仍然是合法 JSON，工具真的执行了
        self.assertEqual((self.ws / "note.txt").read_text(encoding="utf-8"), "你好")
        self.assertEqual(events[-1][0], "done")

    def test_cancel_mid_stream(self):
        """流式输出按 Ctrl+C：立刻停，半截回答不写进历史，之后还能继续聊。"""
        FakeDeepSeek.delay = 0.05          # 让模型"慢慢吐字"，好让我们中途打断
        llm = ChatLLM("sk-test", self.base_url, "deepseek-flash", max_retries=0)
        events = []
        agent = Agent(llm, self.ws, on_event=lambda k, d: events.append((k, d)))

        threading.Timer(0.2, agent.cancel).start()
        answer = agent.run("你好")

        self.assertEqual(answer, "")
        self.assertEqual(events[-1][0], "cancelled")
        # 被打断的半截回答没有进历史，历史干净得像没问过
        self.assertEqual([m["role"] for m in agent.messages], ["system"])

        FakeDeepSeek.delay = 0.0
        self.assertEqual(agent.run("再来一次"), "已经写好 note.txt。")

    def test_thinking_switch_is_sent_in_body(self):
        llm = ChatLLM("sk-test", self.base_url, "deepseek-flash", thinking=False, max_retries=0)
        llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(FakeDeepSeek.requests[0]["thinking"], {"type": "disabled"})

    def test_auth_error_is_translated(self):
        FakeDeepSeek.mode = "unauthorized"
        llm = ChatLLM("sk-wrong", self.base_url, "deepseek-flash", max_retries=0)
        with self.assertRaisesRegex(LLMError, "API Key 无效"):
            llm.chat([{"role": "user", "content": "hi"}])

    def test_connection_error_is_translated(self):
        llm = ChatLLM("sk-test", "http://127.0.0.1:1/v1", "deepseek-flash", max_retries=0, timeout=3)
        with self.assertRaisesRegex(LLMError, "连不上"):
            llm.chat([{"role": "user", "content": "hi"}])

    def test_missing_key(self):
        with self.assertRaisesRegex(LLMError, "没有配置 API Key"):
            ChatLLM("", self.base_url, "deepseek-flash")

    def test_non_ascii_key_is_caught_early(self):
        """Key 还是中文占位符时，创建客户端就报中文提示，不等到发请求才报 ascii 编码错误。"""
        with self.assertRaisesRegex(LLMError, "中文或全角"):
            ChatLLM("在这里填你的Key", self.base_url, "deepseek-flash")
        with self.assertRaisesRegex(LLMError, "中文或全角"):
            ChatLLM("sk-abc１２３", self.base_url, "deepseek-flash")       # 全角数字

    # ---- 读图片
    def test_image_reaches_server_as_image_url_block(self):
        """图片经过真正的 OpenAI SDK 发出去，服务器收到的是标准的 image_url 内容块。"""
        FakeDeepSeek.mode = "vision"
        (self.ws / "dot.png").write_bytes(PNG)
        llm = ChatLLM("sk-test", self.base_url, "deepseek-flash", max_retries=0)
        agent = Agent(llm, self.ws, vision=True)
        self.assertEqual(agent.run("看看 dot.png"), "图里是一个点。")

        last = FakeDeepSeek.requests[-1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][1]["type"], "image_url")
        url = last["content"][1]["image_url"]["url"]
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), PNG)

    def test_text_only_model_rejects_image_with_hint(self):
        """开了 LLM_VISION，模型却不支持图片：给出中文提示，历史仍然合法，还能接着聊。"""
        FakeDeepSeek.mode = "text_only"
        (self.ws / "dot.png").write_bytes(PNG)
        llm = ChatLLM("sk-test", self.base_url, "deepseek-flash", max_retries=0)
        events = []
        agent = Agent(llm, self.ws, vision=True, on_event=lambda k, d: events.append((k, d)))
        self.assertEqual(agent.run("看看 dot.png"), "")

        errors = [d["text"] for k, d in events if k == "error"]
        self.assertIn("LLM_VISION", errors[0])
        self.assertEqual(agent.messages[-1]["role"], "tool")      # 发不出去的图片消息被撤掉了

        agent.vision = False                                     # 用户按提示关掉看图，再问一次
        self.assertEqual(agent.run("那换个说法"), "图里是一个点。")


if __name__ == "__main__":
    unittest.main()
