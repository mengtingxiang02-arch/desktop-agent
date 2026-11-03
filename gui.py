"""桌面版入口（第 3 阶段做界面，第 4 阶段接入 MCP）。

用法：python gui.py [工作目录]      不写工作目录就用项目里的 playground
MCP 服务器在项目根目录的 mcp.json 里配置。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox, QSplitter

from agent import LLMError
from agent.mcp_client import MCPManager, load_mcp_config
from desktop.agent_bridge import AgentBridge
from desktop.chat_panel import ChatPanel
from desktop.file_panel import FilePanel, proposed_change

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = PROJECT_ROOT / "playground"
MCP_CONFIG = PROJECT_ROOT / "mcp.json"


def describe_call(name: str, args: dict) -> str:
    if name == "bash":
        return f"$ {args.get('command', '')}"
    if name == "write_file":
        return f"写入 {args.get('path')}"
    if name == "edit_file":
        return f"修改 {args.get('path')}"
    if name == "read_file":
        return f"读取 {args.get('path')}"
    if name.startswith("mcp__"):                      # mcp__服务器__工具
        _, server, tool = name.split("__", 2)
        return f"MCP {server}.{tool} {json.dumps(args, ensure_ascii=False)[:200]}"
    return f"{name} {args}"


class MainWindow(QMainWindow):
    def __init__(self, workspace: Path, llm=None, mcp: MCPManager | None = None):
        super().__init__()
        self.setWindowTitle("Desktop Agent")
        self.resize(1280, 800)
        self._llm = llm                        # 测试时传入假模型；正常运行是 None
        self.mcp = mcp                         # 已经启动好的 MCP 服务器（没有就是 None）
        self.bridge: AgentBridge | None = None
        self._paths: dict[str, str] = {}       # tool_call 的 id → 它要写的文件，用来在写完后打开文件

        # ---- 三栏：FilePanel 自己包含左栏和中栏，ChatPanel 是右栏
        self.files = FilePanel()
        self.chat = ChatPanel()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.files)
        splitter.addWidget(self.chat)
        splitter.setSizes([760, 520])
        self.setCentralWidget(splitter)

        self.chat.send_requested.connect(self.on_send)
        self.chat.stop_requested.connect(self.on_stop)

        # ---- 菜单
        menu = self.menuBar().addMenu("文件")
        open_action = QAction("打开文件夹…", self, shortcut=QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.choose_folder)
        new_action = QAction("新对话", self, shortcut=QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_chat)
        menu.addAction(open_action)
        menu.addAction(new_action)

        self.set_workspace(workspace)

    # ------------------------------------------------ 工作目录

    def set_workspace(self, folder: Path) -> bool:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            bridge = AgentBridge(folder, llm=self._llm, extra_tools=self.mcp.tools if self.mcp else None)
        except LLMError as e:
            QMessageBox.critical(self, "无法启动 Agent", str(e))
            return False
        if self.bridge is not None:
            self.bridge.deleteLater()
        self.bridge = bridge
        bridge.event.connect(self.on_agent_event)
        bridge.confirm_needed.connect(self.on_confirm)
        bridge.finished.connect(self.on_finished)

        self.files.set_root(bridge.workspace)
        self.chat.clear()
        self.chat.set_busy(False)
        mcp_info = self.mcp.summary() if self.mcp else "MCP：未启用"
        self.statusBar().showMessage(f"模型：{bridge.model_name}    工作目录：{bridge.workspace}    {mcp_info}")
        if self.mcp:
            for name, reason in self.mcp.errors.items():
                self.chat.add_notice(f"⚠ MCP 服务器 {name} 启动失败：{reason}", "warning")
        return True

    def choose_folder(self) -> None:
        if self.bridge and self.bridge.busy:
            QMessageBox.information(self, "请稍等", "Agent 正在工作，先停止再换文件夹")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择工作目录")
        if folder:
            self.set_workspace(Path(folder))

    def new_chat(self) -> None:
        if self.bridge and not self.bridge.busy:
            self.bridge.reset()
            self.chat.clear()

    # ------------------------------------------------ 用户操作

    def on_send(self, text: str) -> None:
        if self.bridge is None:
            return
        self.chat.add_user(text)
        self.chat.set_busy(True)
        self.bridge.send(text)

    def on_stop(self) -> None:
        if self.bridge:
            self.bridge.stop()

    def on_finished(self) -> None:
        self.chat.set_busy(False)

    # ------------------------------------------------ Agent 事件（已经回到主线程）

    def on_agent_event(self, kind: str, data: dict) -> None:
        if kind == "assistant_delta":
            self.chat.append_delta(data["text"])
        elif kind == "assistant_text":
            self.chat.finish_assistant(data["text"])
        elif kind == "tool_call":
            self.chat.add_tool_call(describe_call(data["name"], data["args"]))
            if data["name"] in ("write_file", "edit_file"):
                self._paths[data["id"]] = str(data["args"].get("path", ""))
        elif kind == "tool_result":
            self.chat.add_tool_result(data["result"], data["ok"])
            path = self._paths.pop(data["id"], None)
            if data["ok"] and path:
                self.files.open_file(self.bridge.workspace / path)   # 写完马上在中栏显示新内容
        elif kind == "warning":
            self.chat.add_notice(f"⚠ {data['text']}", "warning")
        elif kind == "error":
            self.chat.add_notice(f"✗ {data['text']}", "error")
        elif kind == "cancelled":
            self.chat.add_notice("■ 已停止", "warning")
        # 其它事件（比如 done）界面上不需要显示

    def on_confirm(self, tool: str, args: dict) -> None:
        if tool in ("write_file", "edit_file"):
            change = proposed_change(self.bridge.workspace, tool, args)
            if change:
                self.files.show_diff(*change)        # 中栏切到"改动"，让用户看清要改什么

        box = QMessageBox(self)
        box.setWindowTitle("需要确认")
        box.setText("Agent 想要执行：")
        box.setInformativeText(describe_call(tool, args))
        allow = box.addButton("允许", QMessageBox.ButtonRole.AcceptRole)
        always = box.addButton(f"本次都允许 {tool}", QMessageBox.ButtonRole.YesRole)
        box.addButton("拒绝", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        self.bridge.answer(clicked in (allow, always), always=clicked is always, tool=tool)

    def closeEvent(self, event) -> None:
        if self.bridge and self.bridge.busy:
            self.bridge.stop()
        if self.mcp:
            self.mcp.close()                   # 结束所有 MCP 服务器进程
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Desktop Agent")
    workspace = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else DEFAULT_WORKSPACE

    # ---- 先启动 MCP 服务器。配置文件写错不影响启动，只是不用 MCP
    mcp = MCPManager()
    try:
        servers = load_mcp_config(MCP_CONFIG)
    except ValueError as e:
        QMessageBox.warning(None, "mcp.json 有问题", f"{e}\n\n这次先不启用 MCP。")
        servers = {}
    if servers:
        print(f"正在启动 {len(servers)} 个 MCP 服务器：{'、'.join(servers)} …", flush=True)
    mcp.start(servers)
    print(mcp.summary(), flush=True)

    window = MainWindow(workspace, mcp=mcp)
    if window.bridge is None:     # Key 没配好，上面已经弹过错误提示了
        mcp.close()
        sys.exit(1)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()