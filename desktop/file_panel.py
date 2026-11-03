"""左栏 + 中栏：文件树，以及查看文件内容 / 查看改动的区域。

单独运行可以预览效果：python -m desktop.file_panel playground
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (QApplication, QFileSystemModel, QPlainTextEdit, QSplitter,
                               QTabWidget, QTreeView)

MAX_PREVIEW_BYTES = 500_000   # 太大的文件不预览，免得界面卡住


class DiffHighlighter(QSyntaxHighlighter):
    """给改动着色：+ 开头的行绿色（新增），- 开头的行红色（删除）。"""

    def highlightBlock(self, text: str) -> None:
        fmt = QTextCharFormat()
        if text.startswith("+") and not text.startswith("+++"):
            fmt.setForeground(QColor("#1a7f37"))
            fmt.setBackground(QColor(46, 160, 67, 40))
        elif text.startswith("-") and not text.startswith("---"):
            fmt.setForeground(QColor("#cf222e"))
            fmt.setBackground(QColor(248, 81, 73, 40))
        elif text.startswith("@@"):
            fmt.setForeground(QColor("#8250df"))
        else:
            return
        self.setFormat(0, len(text), fmt)


def proposed_change(workspace: Path, tool: str, args: dict) -> tuple[str, str, str] | None:
    """算出 write_file / edit_file 执行后文件会变成什么样，返回 (文件名, 改前, 改后)。

    只是预览，不真的改文件。算不出来（比如路径不对）就返回 None。
    """
    try:
        path = (workspace / str(args.get("path", ""))).resolve()
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        if tool == "write_file":
            new = str(args.get("content", ""))
        elif tool == "edit_file":
            a, b = str(args.get("old_string", "")), str(args.get("new_string", ""))
            new = old.replace(a, b) if args.get("replace_all") else old.replace(a, b, 1)
        else:
            return None
        return str(args.get("path")), old, new
    except (OSError, UnicodeDecodeError, ValueError):
        return None


class FilePanel(QSplitter):
    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        # ---- 左栏：文件树。QFileSystemModel 会自动监视磁盘，Agent 新建的文件会自己冒出来
        self.model = QFileSystemModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        for column in (1, 2, 3):            # 只留"名称"一列，隐藏大小、类型、修改时间
            self.tree.hideColumn(column)
        self.tree.setHeaderHidden(True)
        self.tree.clicked.connect(self._on_clicked)

        # ---- 中栏：两个标签页，"文件"看内容，"改动"看 Agent 想改什么
        self.viewer = QPlainTextEdit(readOnly=True)
        self.viewer.setFont(mono)
        self.viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.viewer.setPlaceholderText("在左边点一个文件查看内容")

        self.diff_view = QPlainTextEdit(readOnly=True)
        self.diff_view.setFont(mono)
        self.diff_view.setPlaceholderText("Agent 要修改文件时，改动会显示在这里")
        self._highlighter = DiffHighlighter(self.diff_view.document())

        self.tabs = QTabWidget()
        self.tabs.addTab(self.viewer, "文件")
        self.tabs.addTab(self.diff_view, "改动")

        self.addWidget(self.tree)
        self.addWidget(self.tabs)
        self.setSizes([220, 520])
        self.current_path: Path | None = None

    def set_root(self, folder: Path) -> None:
        self.tree.setRootIndex(self.model.setRootPath(str(folder)))
        self.current_path = None
        self.viewer.clear()
        self.diff_view.clear()

    def _on_clicked(self, index) -> None:
        path = Path(self.model.filePath(index))
        if path.is_file():
            self.open_file(path)

    def open_file(self, path: Path) -> None:
        self.current_path = path
        self.tabs.setCurrentWidget(self.viewer)
        try:
            if path.stat().st_size > MAX_PREVIEW_BYTES:
                self.viewer.setPlainText(f"（{path.name} 太大，不预览）")
                return
            self.viewer.setPlainText(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            self.viewer.setPlainText(f"（{path.name} 不是文本文件，无法预览）")
        except OSError as e:
            self.viewer.setPlainText(f"（打不开 {path.name}：{e}）")

    def show_diff(self, name: str, old: str, new: str) -> None:
        lines = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     fromfile=f"{name}（改前）", tofile=f"{name}（改后）", lineterm="")
        self.diff_view.setPlainText("\n".join(lines) or "（内容没有变化）")
        self.tabs.setCurrentWidget(self.diff_view)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    panel = FilePanel()
    panel.set_root(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve())
    panel.show_diff("demo.py", "a = 1\nb = 2\n", "a = 1\nb = 3\nc = 4\n")
    panel.resize(900, 560)
    panel.show()
    sys.exit(app.exec())