"""Agent 核心：工具调用循环（课上 ReAct 循环的工程实现）。

    用户提问
       │
       ▼
  ┌─► 把完整对话历史 + 工具说明书发给模型 ───────────────┐
  │        │                                              │
  │        ▼                                              │
  │   模型回复里有 tool_calls 吗？ ── 没有 ──► 这就是最终回答，结束
  │        │ 有（= Action）
  │        ▼
  │   逐个执行工具，把结果作为 role="tool" 的消息追加进历史（= Observation）
  └────────┘

模型本身没有记忆：每一轮都要把"迄今为止的全部对话"重新发给它，
它才知道自己刚才读过什么文件、跑过什么命令。所以 self.messages 就是 Agent 的记忆。

这个类不关心界面。它只通过 on_event 回调往外"广播"发生了什么：
命令行版把事件打印出来，之后的桌面版把事件画到界面上，核心代码不用改。
"""
from __future__ import annotations

import json
import platform
import threading
from pathlib import Path
from typing import Callable, Protocol

from .llm import LLMCancelled
from .tools import BUILTIN_TOOLS, Tool, ToolCancelled, ToolContext, ToolError

# 事件回调：on_event(事件类型, 数据)
#   assistant_delta 流式输出的一个小片段 {"text"}   （边生成边来，界面靠它做打字机效果）
#   assistant_text  模型说的话（整段）   {"text"}   （只在模型不支持流式时出现）
#   tool_call       模型要调用工具       {"id", "name", "args"}
#   tool_result     工具执行结果         {"id", "name", "result", "ok"}
#   warning         提示（不致命）       {"text"}
#   error           出错，本轮结束       {"text"}
#   done            本轮正常结束         {"text"}
#   cancelled       用户中断             {}
EventHandler = Callable[[str, dict], None]
# 确认回调：confirm(工具名, 参数) -> 是否允许执行
ConfirmHandler = Callable[[str, dict], bool]


class LLMClient(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> tuple[dict, str]: ...
    # 可选：如果客户端还实现了 chat_stream(messages, tools, on_text, cancel_event)，
    # Agent 会优先用它，从而做到流式输出（打字机效果）+ 随时打断。


CANCELLED_RESULT = "（用户中断了操作，这个工具没有执行）"


def build_system_prompt(workspace: Path) -> str:
    os_name = {"Darwin": "macOS", "Windows": "Windows"}.get(platform.system(), platform.system())
    shell = "cmd.exe" if os_name == "Windows" else "/bin/sh"
    return f"""你是一个运行在用户电脑上的 AI 编程助手，可以通过工具读写文件、执行命令来完成编程任务。

## 环境
- 工作目录：{workspace}（所有相对路径都相对于它；不能访问它之外的文件）
- 操作系统：{os_name}，bash 工具实际使用的 shell 是 {shell}

## 工作方式
1. 先弄清楚现状：不确定项目结构时，用 bash 列目录（如 ls、find）或用 read_file 查看文件。
2. 修改已有文件前，必须先用 read_file 读取它；局部修改用 edit_file，不要整份重写。
3. 写完代码后，尽量用 bash 运行或测试一下，确认真的能用；出错就根据报错修复。
4. 工具返回"错误"时，阅读错误信息，调整做法后重试，不要重复完全相同的调用。
5. 任务完成后，用简洁的中文告诉用户你做了什么、结果如何。

## 注意
- 不要执行危险命令（如 rm -rf /、格式化磁盘、修改系统配置）。
- 不要启动永不退出的程序（如 web 服务器）或需要键盘输入的程序。
"""


class Agent:
    def __init__(self, llm: LLMClient, workspace: Path, *,
                 tools: list[Tool] | None = None,
                 max_steps: int = 30,
                 on_event: EventHandler | None = None,
                 confirm: ConfirmHandler | None = None,
                 system_prompt: str | None = None):
        self.llm = llm
        self.workspace = Path(workspace).resolve()
        self.tools: dict[str, Tool] = {t.name: t for t in (tools if tools is not None else BUILTIN_TOOLS)}
        self.max_steps = max_steps
        self.on_event = on_event or (lambda kind, data: None)
        self.confirm = confirm or (lambda name, args: True)
        self.cancel_event = threading.Event()
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt or build_system_prompt(self.workspace)}
        ]

    # ------------------------------------------------------------ 对外接口

    def cancel(self) -> None:
        """请求中断。可以从任何线程调用（比如界面上的"停止"按钮）。"""
        self.cancel_event.set()

    def reset(self) -> None:
        """清空对话，只保留 system 提示词。"""
        self.messages = self.messages[:1]

    def run(self, user_text: str) -> str:
        """处理一次用户输入，直到模型给出最终回答、出错、被中断或达到步数上限。"""
        self.cancel_event.clear()
        self.messages.append({"role": "user", "content": user_text})
        tool_schemas = [t.schema() for t in self.tools.values()]

        for _step in range(self.max_steps):
            if self.cancel_event.is_set():
                return self._cancelled()

            # ---- Thought：让模型思考下一步
            try:
                reply, finish_reason, streamed = self._ask(tool_schemas)
            except LLMCancelled:
                # 流式说到一半被用户打断：这半截回答绝不能写进历史（tool_calls 可能没拼完），
                # 这条提问也一起撤掉，下次提问时历史才干净。
                self._drop_unanswered_user_message()
                return self._cancelled()
            except Exception as e:  # LLMError 或其它意外
                self.on_event("error", {"text": str(e)})
                # 调用失败，这条 user 消息没有得到回答。把它移除，用户重试时历史才干净
                self._drop_unanswered_user_message()
                return ""

            self.messages.append(reply)
            text = reply.get("content") or ""
            calls = reply.get("tool_calls") or []

            if text and not streamed:
                # 流式时文字已经通过 assistant_delta 一片片发出去了，这里就不再整段重发
                self.on_event("assistant_text", {"text": text})
            if finish_reason == "length":
                self.on_event("warning", {"text": "模型输出达到长度上限被截断了"})

            # ---- 没有工具调用 = 最终回答
            if not calls:
                self.on_event("done", {"text": text})
                return text

            # ---- Action → Observation
            for i, call in enumerate(calls):
                if self.cancel_event.is_set():
                    # 关键细节：API 规定每个 tool_call 后面都必须有对应的 tool 消息，
                    # 否则下一次请求会报 400。所以剩下没执行的也要补一条"已中断"。
                    for rest in calls[i:]:
                        self._append_tool_result(rest["id"], CANCELLED_RESULT)
                    return self._cancelled()
                result = self._execute(call)
                self._append_tool_result(call["id"], result)

        self.on_event("error", {"text": f"已经连续执行了 {self.max_steps} 步还没完成，为防止死循环先停下。可以让我继续。"})
        return ""

    # ------------------------------------------------------------ 内部

    def _ask(self, tool_schemas: list[dict]) -> tuple[dict, str, bool]:
        """问模型要下一步。返回 (消息, finish_reason, 是否走了流式)。

        模型客户端支持流式（有 chat_stream 方法）就走流式：文字一片片通过
        assistant_delta 事件发出去，界面就能做出打字机效果；
        不支持就退回一次性返回（测试里的假模型就是这样）。
        """
        stream = getattr(self.llm, "chat_stream", None)
        if stream is None:
            reply, finish_reason = self.llm.chat(self.messages, tool_schemas)
            return reply, finish_reason, False

        reply, finish_reason = stream(
            self.messages, tool_schemas,
            on_text=lambda piece: self.on_event("assistant_delta", {"text": piece}),
            cancel_event=self.cancel_event,
        )
        return reply, finish_reason, True

    def _drop_unanswered_user_message(self) -> None:
        """这条 user 消息没有得到回答，从历史里撤掉，免得下一轮上下文错乱。"""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()

    def _append_tool_result(self, call_id: str, content: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    def _cancelled(self) -> str:
        self.on_event("cancelled", {})
        return ""

    def _execute(self, call: dict) -> str:
        """执行一个工具调用，无论发生什么都返回一段文字（绝不抛异常）。"""
        call_id = call["id"]
        name = call["function"]["name"]
        raw_args = call["function"].get("arguments") or "{}"

        # 1. 解析参数。模型偶尔会生成不合法的 JSON，把错误告诉它让它重来
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as e:
            result = f"错误：工具参数不是合法的 JSON 对象（{e}）。原始参数：{raw_args[:500]}"
            self.on_event("tool_call", {"id": call_id, "name": name, "args": {"_raw": raw_args}})
            self.on_event("tool_result", {"id": call_id, "name": name, "result": result, "ok": False})
            return result

        self.on_event("tool_call", {"id": call_id, "name": name, "args": args})

        # 2. 找工具
        tool = self.tools.get(name)
        if tool is None:
            result = f"错误：没有名为 {name} 的工具。可用工具：{', '.join(self.tools)}"
            self.on_event("tool_result", {"id": call_id, "name": name, "result": result, "ok": False})
            return result

        # 3. 危险操作先征得用户同意
        if tool.needs_confirm and not self.confirm(name, args):
            result = "用户拒绝执行这个操作。请换一种做法，或者询问用户的意见。"
            self.on_event("tool_result", {"id": call_id, "name": name, "result": result, "ok": False})
            return result

        # 4. 执行
        ctx = ToolContext(workspace=self.workspace, cancel_event=self.cancel_event)
        ok = False
        try:
            result = tool.func(ctx, **args)
            ok = True
        except ToolCancelled as e:
            result = f"（{e}）"
        except ToolError as e:
            result = f"错误：{e}"
        except TypeError as e:
            # 多半是参数名写错或缺参数，例如模型传了 file 而不是 path
            result = f"错误：参数不对（{e}）。请对照工具说明重新调用"
        except Exception as e:  # 兜底：任何意外都不能让整个 Agent 崩掉
            result = f"错误：工具内部异常 {type(e).__name__}: {e}"

        self.on_event("tool_result", {"id": call_id, "name": name, "result": result, "ok": ok})
        return result
