"""Agent 和界面之间的"桥"。

为什么需要它？Qt 有一条铁律：界面只能在主线程里改。
但 Agent 必须在后台线程里跑（否则它工作时整个窗口会卡死，"停止"按钮也点不了）。
所以 Agent 在后台发出的每个事件，都要先变成 Qt 的信号（Signal），
Qt 会自动把它排队送回主线程，再由主线程去更新界面。

还有一个难点是"确认"：Agent 在后台线程里问"能不能执行？"，必须等用户在界面上点了按钮才能继续。
做法是：后台线程发出 confirm_needed 信号，然后停下来等一个 threading.Event；
主线程弹出对话框，用户点完后调用 answer()，把 Event 设上，后台线程就继续往下走。
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from agent import Agent, ChatLLM, load_config


class AgentBridge(QObject):
    event = Signal(str, object)           # Agent 的任何事件：(事件类型, 数据)
    confirm_needed = Signal(str, object)  # 需要用户确认：(工具名, 参数)
    finished = Signal()                   # 一次 run() 结束（不管是正常完成、出错还是被停止）

    def __init__(self, workspace: Path, llm=None):
        """llm 参数留给测试用：传一个假模型进来，就不用真的调用 API。"""
        super().__init__()
        cfg = load_config(workspace)
        if llm is None:
            llm = ChatLLM(cfg.api_key, cfg.base_url, cfg.model, thinking=cfg.thinking)  # Key 不对会在这里抛 LLMError
        self.model_name = cfg.model
        self.workspace = cfg.workspace
        # 这一行要和 cli.py 里创建 Agent 的写法一致
        self.agent = Agent(llm, cfg.workspace, max_steps=cfg.max_steps,
                           on_event=self._on_event, confirm=self._confirm)
        self.always_allowed: set[str] = set()
        self._answer = False
        self._answered = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------ 主线程调用

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def send(self, text: str) -> None:
        if self.busy:
            return
        self._thread = threading.Thread(target=self._run, args=(text,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.agent.cancel()
        self.answer(False)          # 万一后台正等着确认，也让它别等了

    def reset(self) -> None:
        if not self.busy:
            self.agent.reset()
            self.always_allowed.clear()

    def answer(self, allowed: bool, always: bool = False, tool: str = "") -> None:
        """用户在确认对话框里做了选择。"""
        if always and tool:
            self.always_allowed.add(tool)
        self._answer = allowed
        self._answered.set()

    # ------------------------------------------------ 后台线程里执行

    def _run(self, text: str) -> None:
        try:
            self.agent.run(text)
        except Exception as e:      # 兜底：任何意外都不能让后台线程悄悄死掉
            self.event.emit("error", {"text": f"程序内部错误：{type(e).__name__}: {e}"})
        finally:
            self.finished.emit()

    def _on_event(self, kind: str, data: dict) -> None:
        self.event.emit(kind, data)       # 只转发，不碰界面

    def _confirm(self, tool: str, args: dict) -> bool:
        if tool in self.always_allowed:
            return True
        self._answered.clear()
        self.confirm_needed.emit(tool, args)
        while not self._answered.wait(0.1):        # 每 0.1 秒看一眼有没有被停止
            if self.agent.cancel_event.is_set():
                return False
        return self._answer