"""和大模型通信的一层薄封装。

用的是"OpenAI 兼容"的 Chat Completions 接口。DeepSeek、通义千问（阿里云百炼）、
Kimi、智谱、OpenAI 本身都提供这种接口，所以只要改 .env 里的 base_url 和 model 就能换模型，
Agent 的其余代码一行都不用动。

这一层负责三件事：
  1. 把请求发出去（带上 tools）；
  2. 把各种网络/鉴权错误翻译成看得懂的 LLMError；
  3. 把返回的 message 转成普通 dict，方便追加进对话历史。

两种拿结果的方式：
  chat()        一次性返回（简单，但用户要等模型说完才能看到字）
  chat_stream() 边生成边往回吐（打字机效果），并且可以被 cancel_event 随时打断
"""
from __future__ import annotations

import openai
from openai import OpenAI


class LLMError(Exception):
    """调用模型失败。信息直接展示给用户。"""


class LLMCancelled(Exception):
    """用户在流式输出过程中打断了。

    这半截回答不能写进对话历史：它可能连 tool_calls 都没收完整，
    留着会让下一次请求因为"tool_calls 后面没有对应的 tool 消息"而报 400。
    partial 只是给界面/日志看的，别塞回 messages。
    """

    def __init__(self, partial: str = ""):
        super().__init__("模型输出被用户中断")
        self.partial = partial


def message_to_dict(msg) -> dict:
    """把 SDK 返回的 assistant message 转成可以再发回给 API 的 dict。

    DeepSeek V4 的坑：开启思考模式时，返回里多一个 reasoning_content 字段。
    只要请求里带了 tools，之后的每次请求都必须把它原样传回，否则接口直接报 400。
    OpenAI SDK 不认识这个字段，会把它放在 model_extra 里，所以这里要专门捞出来。
    """
    out: dict = {"role": "assistant", "content": msg.content}

    extra = getattr(msg, "model_extra", None) or {}
    reasoning = extra.get("reasoning_content")
    #if reasoning is not None:
        #out["reasoning_content"] = reasoning
        
    calls = []
    for tc in msg.tool_calls or []:
        fn = getattr(tc, "function", None)
        if fn is None:          # 只处理 function 类型的工具调用
            continue
        calls.append({
            "id": tc.id,
            "type": "function",
            "function": {"name": fn.name, "arguments": fn.arguments or "{}"},
        })
    if calls:
        out["tool_calls"] = calls
    return out


class ChatLLM:
    def __init__(self, api_key: str, base_url: str, model: str,
                 thinking: bool | None = None, timeout: float = 120, max_retries: int = 2):
        if not api_key:
            raise LLMError("没有配置 API Key：请在项目根目录的 .env 文件里填写 LLM_API_KEY")
        self.model = model
        self.thinking = thinking
        # max_retries：遇到网络抖动、429 限流、5xx 时 SDK 会自动重试（带退避）
        self.client = OpenAI(api_key=api_key, base_url=base_url,
                             timeout=timeout, max_retries=max_retries)

    # ------------------------------------------------------------ 对内

    def _request_kwargs(self, tools: list[dict] | None) -> dict:
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = tools
        if self.thinking is not None:
            # DeepSeek 专有参数；OpenAI SDK 不认识，所以放在 extra_body 里原样透传
            kwargs["extra_body"] = {"thinking": {"type": "enabled" if self.thinking else "disabled"}}
        return kwargs

    def _translate(self, e: Exception) -> LLMError | None:
        """把 SDK 抛出的异常翻译成给人看的中文提示；不认识的返回 None。"""
        if isinstance(e, openai.AuthenticationError):
            return LLMError("API Key 无效或已过期（401），请检查 .env 里的 LLM_API_KEY")
        if isinstance(e, openai.PermissionDeniedError):
            return LLMError(f"没有权限（403）：{e.message}")
        if isinstance(e, openai.NotFoundError):
            return LLMError(f"接口地址或模型名不对（404）：base_url / model = {self.client.base_url} / {self.model}")
        if isinstance(e, openai.RateLimitError):
            return LLMError("请求太频繁（429），SDK 已自动重试仍失败，请稍后再试")
        if isinstance(e, openai.APITimeoutError):
            return LLMError("请求超时，模型太久没有响应")
        if isinstance(e, openai.APIConnectionError):
            return LLMError("连不上模型服务：请检查网络、代理设置和 LLM_BASE_URL")
        if isinstance(e, openai.APIStatusError):
            if e.status_code == 402:
                return LLMError("账户余额不足（402），请到模型服务商的控制台充值")
            return LLMError(f"模型接口返回错误 {e.status_code}：{e.message}")
        return None

    @staticmethod
    def _finish_reply(content: str, reasoning: str, calls: list[dict],
                      finish_reason: str) -> tuple[dict, str]:
        """拼出和 message_to_dict() 结构一致的 assistant 消息。"""
        reply: dict = {"role": "assistant", "content": content or None}
        if reasoning:
          # 和 message_to_dict 一样：思考内容要留着，下一轮请求原样传回
            reply["reasoning_content"] = reasoning
        if calls:
            reply["tool_calls"] = calls
            finish_reason = finish_reason or "tool_calls"
        return reply, finish_reason or "stop"

    @staticmethod
    def _close_quietly(stream) -> None:
        """扔掉流式连接。打断时不做这一步，连接会一直挂到超时。"""
        if stream is None:
            return
        close = getattr(stream, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            pass

    # ------------------------------------------------------------ 对外

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> tuple[dict, str]:
        """一次性发一轮请求，返回 (assistant 消息 dict, finish_reason)。"""
        kwargs = self._request_kwargs(tools)

        try:
            resp = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
        except Exception as e:
            translated = self._translate(e)
            if translated is not None:
                raise translated from e
            raise

        if not resp.choices:
            raise LLMError("模型返回了空结果")
        choice = resp.choices[0]
        return message_to_dict(choice.message), choice.finish_reason or ""

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                    on_text=None, cancel_event=None) -> tuple[dict, str]:
        """流式发一轮请求，边收边把文字片段交给 on_text，最后返回完整结果。

        参数：
            on_text(text)     每收到一小段文字就调用一次（打字机效果靠它）
            cancel_event      一个 threading.Event；一旦被 set，立刻停止收流并抛 LLMCancelled

        返回和 chat() 完全一样的 (assistant 消息 dict, finish_reason)，
        所以上层不用关心到底走的是不是流式。
        """
        kwargs = self._request_kwargs(tools)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # 流式返回的 tool_calls 是被切成碎片一段段来的，要按 index 拼回去：
        #   {0: {"id": "call_1", "name": "read_f", "arguments": "{\"pa"}}
        call_slots: dict[int, dict] = {}
        finish_reason = ""
        stream = None

        def partial() -> str:
            return "".join(content_parts)

        try:
            if cancel_event is not None and cancel_event.is_set():
                raise LLMCancelled()

            stream = self.client.chat.completions.create(
                model=self.model, messages=messages, stream=True, **kwargs)

            for chunk in stream:
                # 关键：每收到一片就检查一次"用户是不是按了 Ctrl+C"，
                # 这样最多再等一片数据的网络延迟就能停下来，而不是等整段说完。
                if cancel_event is not None and cancel_event.is_set():
                    raise LLMCancelled(partial())

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue          # 某些服务商最后会补一个只有 usage 的空 chunk
                choice = choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                piece = getattr(delta, "content", None)
                if piece:
                    content_parts.append(piece)
                    if on_text is not None:
                        on_text(piece)

                extra = getattr(delta, "model_extra", None) or {}
                thought = extra.get("reasoning_content")
                if thought:
                    reasoning_parts.append(thought)

                for tc in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tc, "index", None)
                    slot = call_slots.setdefault(0 if index is None else index,
                                                 {"id": "", "name": "", "arguments": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is None:
                        continue
                    if getattr(fn, "name", None):
                        slot["name"] += fn.name          # 名字一般只来一次，拼起来也没错
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments  # 参数是碎片，必须拼

        except LLMCancelled:
            raise
        except Exception as e:
            translated = self._translate(e)
            if translated is not None:
                raise translated from e
            raise LLMError(f"流式请求失败：{type(e).__name__}: {e}") from e
        finally:
            self._close_quietly(stream)

        calls = []
        for index in sorted(call_slots):
            slot = call_slots[index]
            if not slot["name"]:
                continue
            calls.append({
                "id": slot["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
            })

        return self._finish_reply(partial(), "".join(reasoning_parts), calls, finish_reason)
