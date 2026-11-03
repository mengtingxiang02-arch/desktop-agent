"""右栏：对话区。只负责"显示"和"收集输入"，不知道 Agent 的存在。

单独运行可以预览效果：python -m desktop.chat_panel
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QPlainTextEdit, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

COLORS = {"user": "#3b82f6", "tool": "#8b5cf6", "dim": "#8b949e",
          "error": "#e5534b", "warning": "#d29922", "ok": "#3fb950"}


class InputBox(QPlainTextEdit):
    """输入框：回车发送，Shift+回车换行。"""
    submitted = Signal()

    def keyPressEvent(self, event) -> None:
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    send_requested = Signal(str)   # 用户点了发送，参数是输入的文字
    stop_requested = Signal()      # 用户点了停止

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)

        self.input = InputBox()
        self.input.setPlaceholderText("输入任务，回车发送，Shift+回车换行")
        self.input.setFixedHeight(80)
        self.input.submitted.connect(self._submit)

        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self._submit)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        self.stop_btn.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.stop_btn)
        buttons.addWidget(self.send_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view, 1)    # 1 = 多出来的高度都给对话区
        layout.addWidget(self.input)
        layout.addLayout(buttons)

        self._streaming = False           # 是否正在一个字一个字地接收 Agent 的话

    # ------------------------------------------------ 输入

    def _submit(self) -> None:
        text = self.input.toPlainText().strip()
        if text and self.send_btn.isEnabled():
            self.input.clear()
            self.send_requested.emit(text)

    def set_busy(self, busy: bool) -> None:
        """Agent 工作时：不能再发送，可以停止。"""
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    # ------------------------------------------------ 输出

    def _write(self, text: str, color: str | None = None, bold: bool = False,
               mono: bool = False, new_block: bool = True) -> None:
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if new_block and not self.view.document().isEmpty():
            cursor.insertBlock()
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if mono:
            fmt.setFont(self.mono)
        cursor.insertText(text, fmt)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())      # 自动滚到最底下

    def _end_stream(self) -> None:
        self._streaming = False

    def add_user(self, text: str) -> None:
        self._end_stream()
        self._write("")
        self._write("你", COLORS["user"], bold=True)
        self._write(text)

    def append_delta(self, piece: str) -> None:
        """流式：收到 Agent 说的一小段话，接在后面，不换行。"""
        if not self._streaming:
            self._write("")
            self._write("Agent", bold=True)
            self._write("")
            self._streaming = True
        self._write(piece, new_block=False)

    def finish_assistant(self, text: str) -> None:
        """一段话说完了。已经流式显示过就不用再显示一遍。"""
        if not self._streaming and text:
            self.append_delta(text)
        self._end_stream()

    def add_tool_call(self, summary: str) -> None:
        self._end_stream()
        self._write(f"▸ {summary}", COLORS["tool"], bold=True)

    def add_tool_result(self, result: str, ok: bool) -> None:
        lines = result.strip().splitlines()
        shown = "\n".join(lines[:6]) + ("\n…" if len(lines) > 6 else "")
        self._write(shown, None if ok else COLORS["error"], mono=True)

    def add_notice(self, text: str, kind: str = "dim") -> None:
        self._end_stream()
        self._write(text, COLORS.get(kind, COLORS["dim"]))

    def clear(self) -> None:
        self.view.clear()
        self._end_stream()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    panel = ChatPanel()
    panel.resize(480, 600)
    panel.show()

    # 不接 Agent，用定时器假装模型在一个字一个字地回答
    def fake_reply(text: str) -> None:
        panel.add_user(text)
        panel.set_busy(True)
        pieces = list(f"你刚才说的是：{text}。这是一段模拟的流式回复，字会一个个出来。")

        def tick():
            if pieces:
                panel.append_delta(pieces.pop(0))
            else:
                timer.stop()
                panel.finish_assistant("")
                panel.add_tool_call("$ ls -la")
                panel.add_tool_result("exit code: 0\n[stdout]\nhello.py", ok=True)
                panel.set_busy(False)

        timer = QTimer(panel)
        timer.timeout.connect(tick)
        timer.start(40)
        panel._demo_timer = timer          # 存起来，防止被当成垃圾回收

    def stop():
        timer = getattr(panel, "_demo_timer", None)
        if timer:
            timer.stop()
        panel.add_notice("■ 已停止", "warning")
        panel.set_busy(False)

    panel.send_requested.connect(fake_reply)
    panel.stop_requested.connect(stop)
    sys.exit(app.exec())
