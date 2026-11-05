"""四个核心工具：read_file / write_file / edit_file / bash。

一个"工具"由两部分组成：
  1. JSON Schema —— 写给模型看的"说明书"：工具叫什么、干什么、要哪些参数。
  2. Python 函数  —— 真正干活的代码。

模型永远不会直接执行任何东西。它只是输出一段"我想调用 X，参数是 Y"的 JSON，
由我们的程序去执行，再把结果文字交回给它。这就是课上 ReAct 里的 Action → Observation。

工具出错时抛 ToolError。Agent 循环会把错误信息当成观察结果交回给模型，
让模型自己改正（比如换一个路径重试），而不是让整个程序崩溃。
"""
from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_OUTPUT_CHARS = 12_000   # 工具返回给模型的文字上限，太长会浪费 token 甚至撑爆上下文
DEFAULT_READ_LIMIT = 2000   # read_file 默认最多读多少行
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 图片最大 5 MB：图片会留在对话历史里，每一轮都要重新发给模型


class ToolError(Exception):
    """工具执行失败。错误信息会原样交给模型。"""


class ToolCancelled(Exception):
    """用户点了"停止"，工具被中断。"""


@dataclass
class ToolContext:
    workspace: Path
    cancel_event: threading.Event


@dataclass
class ImageContent:
    """read_file 读到图片时的返回值，也就是一个"多模态内容块"：文字说明 + 图片本身。

    模型接口规定 role="tool" 的消息里只能放文字，图片只能放在 role="user" 的消息里。
    所以工具只负责把图片读出来、编成 base64；怎么交给模型，由 Agent 循环决定（见 core.py）。
    """
    text: str           # 给模型和界面看的说明，比如"已读取图片 a.png（PNG，12 KB）"
    media_type: str     # image/png、image/jpeg、image/gif、image/webp
    data: str           # base64 编码后的图片内容

    def data_url(self) -> str:
        """接口要的格式：data:image/png;base64,iVBORw0..."""
        return f"data:{self.media_type};base64,{self.data}"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                 # JSON Schema
    func: Callable[..., str | ImageContent]   # func(ctx, **args)，一般返回文字，read_file 读到图片时返回 ImageContent
    needs_confirm: bool = False      # 执行前要不要问用户（会改文件或跑命令的都要问）

    def schema(self) -> dict:
        """转成 OpenAI 兼容接口要求的 tools 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------- 辅助函数

def resolve_path(ctx: ToolContext, path: str) -> Path:
    """把模型给的路径变成绝对路径，并确保它在工作目录里面。

    这是最基本的安全措施：不让 Agent 去改工作目录之外的文件（比如 ~/.ssh）。
    """
    if not path or not path.strip():
        raise ToolError("path 不能为空")
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = ctx.workspace / p
    p = p.resolve()
    if not p.is_relative_to(ctx.workspace):
        raise ToolError(f"路径 {path} 在工作目录 {ctx.workspace} 之外，不允许访问")
    return p


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """保留开头和结尾，中间截掉。报错信息通常在结尾，所以结尾也要留。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n\n...（中间省略 {omitted} 个字符）...\n\n{text[-half:]}"


def _rel(ctx: ToolContext, p: Path) -> str:
    return str(p.relative_to(ctx.workspace)) or "."


# ---------------------------------------------------------------- 1. read_file

def sniff_image(head: bytes) -> str | None:
    """看文件开头的几个字节（"魔数"）判断是不是图片，返回 media_type，不是图片返回 None。

    比看扩展名可靠：扩展名可以随便改，文件开头的字节改不了。
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _human_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def _read_image(ctx: ToolContext, p: Path, media_type: str) -> ImageContent:
    size = p.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ToolError(
            f"图片 {_rel(ctx, p)} 有 {_human_size(size)}，超过 {_human_size(MAX_IMAGE_BYTES)} 的上限。"
            "可以先用 bash 把图片缩小（macOS 上可以用 sips -Z 1024 图片路径 --out 新路径）再读"
        )
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    kind = media_type.split("/")[1].upper()
    return ImageContent(text=f"已读取图片 {_rel(ctx, p)}（{kind}，{_human_size(size)}）",
                        media_type=media_type, data=data)


def read_file(ctx: ToolContext, path: str, offset: int = 1,
              limit: int = DEFAULT_READ_LIMIT) -> str | ImageContent:
    p = resolve_path(ctx, path)
    if not p.exists():
        raise ToolError(f"文件不存在：{path}")
    if p.is_dir():
        entries = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir())
        raise ToolError(f"{path} 是目录，不是文件。目录内容：{', '.join(entries[:100]) or '（空）'}")

    with p.open("rb") as f:
        media_type = sniff_image(f.read(12))
    if media_type:                                   # 图片：不按行读，整张交给模型看
        return _read_image(ctx, p, media_type)

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path} 不是 UTF-8 文本文件，也不是能识别的图片（PNG/JPEG/GIF/WebP），无法读取")

    lines = text.splitlines()
    offset = max(1, int(offset))
    limit = max(1, int(limit))
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return f"（{path} 共 {len(lines)} 行，第 {offset} 行之后没有内容）"

    # 带行号返回，方便模型之后精确地引用和修改
    body = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, start=offset))
    end = offset + len(chunk) - 1
    note = f"\n（共 {len(lines)} 行，本次显示 {offset}-{end} 行）" if end < len(lines) or offset > 1 else ""
    return truncate(body + note)


# ---------------------------------------------------------------- 2. write_file

def _count_lines(text: str) -> int:
    """数行数："a\nb\n" 和 "a\nb" 都算 2 行，空文件算 0 行。"""
    return len(text.splitlines())


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    """原子写：先写到同一个文件夹里的临时文件，写完再一步改名，替换掉原文件。

    如果直接往原文件里写，写到一半程序崩溃或被停止，原文件就只剩半截。
    os.replace 改名是一步完成的：任何时候看到的，要么是完整的旧文件，要么是完整的新文件。
    """
    p = resolve_path(ctx, path)
    if p.is_dir():
        raise ToolError(f"{path} 是一个目录，不能当文件写")

    existed = p.is_file()
    old_lines = None
    if existed:
        try:
            old_lines = _count_lines(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            pass  # 原文件不是文本（比如图片），就不报原来的行数

    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}.tmp")   # 和目标在同一个文件夹，改名才是一步完成的
    try:
        tmp.write_text(content, encoding="utf-8")
        if existed:
            shutil.copymode(p, tmp)   # 保留原文件的权限，比如"可执行"
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)   # 失败了也不留下临时文件
        raise

    new_lines = _count_lines(content)
    if not existed:
        return f"已创建文件 {_rel(ctx, p)}（{new_lines} 行）"
    before = f"{old_lines} 行" if old_lines is not None else "非文本内容"
    return f"已覆盖文件 {_rel(ctx, p)}：原来 {before}，现在 {new_lines} 行"


# ---------------------------------------------------------------- 3. edit_file

def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    """精确的字符串替换。

    为什么不让模型直接用 write_file 重写整个文件？
    - 大文件全部重写既慢又费 token；
    - 模型重写时容易"顺手"改坏别的地方。
    要求 old_string 在文件里恰好出现一次，可以保证改的就是模型想改的那一处。
    """
    p = resolve_path(ctx, path)
    if not p.is_file():
        raise ToolError(f"文件不存在：{path}。新建文件请用 write_file")
    if old_string == "":
        raise ToolError("old_string 不能为空。新建文件请用 write_file")
    if old_string == new_string:
        raise ToolError("old_string 和 new_string 完全一样，没有需要修改的内容")

    text = p.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        raise ToolError(
            f"在 {path} 中没有找到 old_string。请先用 read_file 查看文件的当前内容，"
            "注意空格、缩进和换行必须完全一致（不要包含行号）"
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string 在 {path} 中出现了 {count} 次，无法确定改哪一处。"
            "请在 old_string 里多带几行上下文让它唯一，或者设置 replace_all=true 全部替换"
        )

    new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"已修改 {_rel(ctx, p)}：替换了 {count if replace_all else 1} 处"


# ---------------------------------------------------------------- 4. bash

def _kill_process_tree(proc: subprocess.Popen) -> None:
    """命令可能又启动了子进程（比如 npm 启动 node），要连根一起结束。"""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    except (ProcessLookupError, OSError):
        pass


def bash(ctx: ToolContext, command: str, timeout: int = 60) -> str:
    """在工作目录里执行一条 shell 命令。

    macOS / Linux 用 /bin/sh，Windows 用 cmd.exe（shell=True 时 Python 自动选择）。
    不用 subprocess.run 而是 Popen + 轮询，是为了能随时响应"停止"按钮。
    """
    if not command or not command.strip():
        raise ToolError("command 不能为空")
    timeout = max(1, min(int(timeout), 600))

    kwargs: dict = dict(
        shell=True, cwd=ctx.workspace,
        stdin=subprocess.DEVNULL,            # 不给 stdin：需要交互输入的命令会直接结束，而不是卡死
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    if os.name == "posix":
        kwargs["start_new_session"] = True   # 单独的进程组，方便整组结束
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # 添加安全检查
    dangerous_patterns = ["rm -rf /", "sudo", "mkfs", "shutdown", "reboot", "> /dev/sda"]
    for pattern in dangerous_patterns:
        if pattern in command:
            # 抛出错误，这个错误会被 Agent 捕获，当成 Observation 发回给模型
            raise ToolError(f"安全拦截：检测到危险命令 '{pattern}'，已拒绝执行。")

    proc = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if ctx.cancel_event.is_set():
                _kill_process_tree(proc)
                proc.communicate()
                raise ToolCancelled("命令已被用户中断")
            if time.monotonic() > deadline:
                _kill_process_tree(proc)
                proc.communicate()
                raise ToolError(f"命令运行超过 {timeout} 秒，已强制结束。可能是程序陷入了死循环，或者在等待输入；"
                                "长时间运行的程序（如服务器）不要在这里前台启动")

    parts = [f"exit code: {proc.returncode}"]
    if out.strip():
        parts.append(f"[stdout]\n{out.rstrip()}")
    if err.strip():
        parts.append(f"[stderr]\n{err.rstrip()}")
    if len(parts) == 1:
        parts.append("（没有输出）")
    return truncate("\n".join(parts))


# ---------------------------------------------------------------- 注册表

BUILTIN_TOOLS: list[Tool] = [
    Tool(
        name="read_file",
        description=(
            "读取工作目录中的文件。文本文件返回带行号的内容（格式：行号<TAB>内容），"
            "修改文件之前必须先读，大文件可以用 offset/limit 分段读。"
            "图片（PNG/JPEG/GIF/WebP）会把图片本身交给你看。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对于工作目录"},
                "offset": {"type": "integer", "description": "从第几行开始读（从 1 开始），默认 1"},
                "limit": {"type": "integer", "description": f"最多读多少行，默认 {DEFAULT_READ_LIMIT}"},
            },
            "required": ["path"],
        },
        func=read_file,
    ),
    Tool(
        name="write_file",
        description="创建新文件，或用新内容完整覆盖已有文件。会自动创建不存在的上级目录。修改已有文件的局部请用 edit_file。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对于工作目录"},
                "content": {"type": "string", "description": "要写入的完整文件内容"},
            },
            "required": ["path", "content"],
        },
        func=write_file,
        needs_confirm=True,
    ),
    Tool(
        name="edit_file",
        description=(
            "把文件中的 old_string 精确替换为 new_string。old_string 必须与文件内容逐字一致"
            "（包括缩进和换行，不含行号），并且在文件中只出现一次；否则请多带上下文，或设置 replace_all。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对于工作目录"},
                "old_string": {"type": "string", "description": "要被替换的原文"},
                "new_string": {"type": "string", "description": "替换后的新内容"},
                "replace_all": {"type": "boolean", "description": "是否替换所有出现的位置，默认 false"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        func=edit_file,
        needs_confirm=True,
    ),
    Tool(
        name="bash",
        description=(
            "在工作目录中执行一条 shell 命令，返回退出码、stdout 和 stderr。"
            "适合运行程序、跑测试、安装依赖、查看目录（ls）、搜索（grep）。"
            "不要运行需要交互输入或永不退出的命令。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"},
            },
            "required": ["command"],
        },
        func=bash,
        needs_confirm=True,
    ),
]
