"""四个工具的单元测试。不需要 API Key。运行：python -m unittest -v"""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.tools import (ToolCancelled, ToolContext, ToolError, bash, edit_file,
                         read_file, write_file)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.ctx = ToolContext(workspace=self.ws, cancel_event=threading.Event())

    def tearDown(self):
        self._tmp.cleanup()

    # ---- write / read
    def test_write_then_read_with_line_numbers(self):
        msg = write_file(self.ctx, "src/hello.py", "a = 1\nb = 2\n")
        self.assertIn("创建", msg)
        self.assertTrue((self.ws / "src/hello.py").exists())   # 自动建了 src 目录
        out = read_file(self.ctx, "src/hello.py")
        self.assertIn("     1\ta = 1", out)
        self.assertIn("     2\tb = 2", out)

    def test_read_offset_limit(self):
        write_file(self.ctx, "n.txt", "\n".join(str(i) for i in range(1, 101)))
        out = read_file(self.ctx, "n.txt", offset=10, limit=3)
        self.assertIn("    10\t10", out)
        self.assertIn("    12\t12", out)
        self.assertNotIn("    13\t13", out)
        self.assertIn("共 100 行", out)

    def test_read_missing_and_directory(self):
        with self.assertRaisesRegex(ToolError, "不存在"):
            read_file(self.ctx, "nope.txt")
        (self.ws / "d").mkdir()
        with self.assertRaisesRegex(ToolError, "是目录"):
            read_file(self.ctx, "d")

    def test_read_binary(self):
        (self.ws / "x.bin").write_bytes(b"\xff\xfe\x00\x81")
        with self.assertRaisesRegex(ToolError, "UTF-8"):
            read_file(self.ctx, "x.bin")

    def test_path_outside_workspace_rejected(self):
        for bad in ["../evil.txt", "/etc/passwd", "a/../../evil.txt"]:
            with self.assertRaisesRegex(ToolError, "工作目录"):
                write_file(self.ctx, bad, "x")

    # ---- edit
    def test_edit_unique(self):
        write_file(self.ctx, "f.py", "x = 1\ny = 2\n")
        edit_file(self.ctx, "f.py", "y = 2", "y = 3")
        self.assertEqual((self.ws / "f.py").read_text(), "x = 1\ny = 3\n")

    def test_edit_not_found(self):
        write_file(self.ctx, "f.py", "x = 1\n")
        with self.assertRaisesRegex(ToolError, "没有找到"):
            edit_file(self.ctx, "f.py", "z = 9", "z = 10")

    def test_edit_ambiguous_then_replace_all(self):
        write_file(self.ctx, "f.py", "print(1)\nprint(1)\n")
        with self.assertRaisesRegex(ToolError, "出现了 2 次"):
            edit_file(self.ctx, "f.py", "print(1)", "print(2)")
        edit_file(self.ctx, "f.py", "print(1)", "print(2)", replace_all=True)
        self.assertEqual((self.ws / "f.py").read_text(), "print(2)\nprint(2)\n")

    # ---- bash
    def test_bash_ok_and_exit_code(self):
        out = bash(self.ctx, "echo hello && echo oops 1>&2 && exit 3")
        self.assertIn("exit code: 3", out)
        self.assertIn("hello", out)
        self.assertIn("[stderr]\noops", out)

    def test_bash_runs_in_workspace(self):
        write_file(self.ctx, "marker.txt", "hi")
        out = bash(self.ctx, "ls")
        self.assertIn("marker.txt", out)

    def test_bash_timeout(self):
        start = time.monotonic()
        with self.assertRaisesRegex(ToolError, "超过 1 秒"):
            bash(self.ctx, "sleep 5", timeout=1)
        self.assertLess(time.monotonic() - start, 3)

    def test_bash_cancel(self):
        threading.Timer(0.5, self.ctx.cancel_event.set).start()
        start = time.monotonic()
        with self.assertRaises(ToolCancelled):
            bash(self.ctx, "sleep 10")
        self.assertLess(time.monotonic() - start, 3)

    def test_bash_kills_child_processes(self):
        # 命令里再套一层子进程，取消时也要一起结束
        threading.Timer(0.5, self.ctx.cancel_event.set).start()
        with self.assertRaises(ToolCancelled):
            bash(self.ctx, "sh -c 'sleep 10; echo leaked > leaked.txt' & wait")
        time.sleep(0.3)
        self.assertFalse((self.ws / "leaked.txt").exists())

    def test_bash_truncates_huge_output(self):
        out = bash(self.ctx, "python3 -c \"print('x' * 50000)\"")
        self.assertIn("中间省略", out)
        self.assertLess(len(out), 13000)


if __name__ == "__main__":
    unittest.main()
