"""MCP 客户端（第 4 阶段）：按 mcp.json 启动 MCP 服务器，把它们的工具包装成 Agent 能用的 Tool。

难点：Agent 是同步代码（普通函数 + 线程），MCP 的 SDK 却全是异步的（async/await）。
做法：开一个专门的后台线程，在里面运行一个 asyncio 事件循环，所有 MCP 连接都"住"在这个线程里。
Agent 要调用 MCP 工具时，用 asyncio.run_coroutine_threadsafe() 把任务投递进这个线程，然后同步地等结果。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .tools import Tool, ToolCancelled, ToolContext, ToolError, truncate

START_TIMEOUT = 90    # 启动一个服务器最多等几秒（第一次用 uvx 要先下载，会慢一些）
CALL_TIMEOUT = 120    # 调用一次 MCP 工具最多等几秒

# 从 Finder 双击打开的程序拿不到终端里的 PATH，常用命令可能找不到，所以额外在这些地方找一找
EXTRA_BIN_DIRS = [Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]


def load_mcp_config(path: Path) -> dict[str, dict]:
    """读取 mcp.json。格式和 Claude Desktop 的配置文件一样：{"mcpServers": {名字: {command, args, env}}}"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name} 不是合法的 JSON：第 {e.lineno} 行附近，{e.msg}")
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError(f"{path.name} 里必须有一个叫 mcpServers 的对象")
    return {name: cfg for name, cfg in servers.items() if isinstance(cfg, dict) and not cfg.get("disabled")}


def tool_name(server: str, tool: str) -> str:
    """给模型看的工具名：mcp__服务器名__工具名。模型接口只允许字母、数字、_ 和 -，最长 64 个字符。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def result_to_text(result) -> str:
    """MCP 返回的内容是一个列表，里面可能有文字、图片等。我们只取文字。"""
    parts = []
    for item in result.content:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else f"[{item.type} 类型的内容，无法显示]")
    return "\n".join(parts) or "（没有返回内容）"


def describe_error(e: BaseException) -> str:
    """把异常变成一句看得懂的话。SDK 有时会把真正的错误包在 ExceptionGroup 里，要拆开看。"""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    if isinstance(e, FileNotFoundError):
        return f"找不到要运行的命令（{e.filename or e}）"
    if "Connection closed" in str(e):
        return "服务器进程一启动就退出了，具体原因看终端里它打印的报错"
    return f"{type(e).__name__}: {e}"


def resolve_command(command: str) -> str:
    if shutil.which(command):
        return command
    for folder in EXTRA_BIN_DIRS:
        candidate = folder / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return command   # 实在找不到就原样返回，启动时会报"找不到命令"


class MCPManager:
    def __init__(self, errlog=None):
        """errlog：服务器打印的日志写到哪里。默认是终端（stderr），测试时传一个空文件让输出干净些。"""
        self.errlog = errlog or sys.stderr or open(os.devnull, "w")
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.sessions: dict[str, ClientSession] = {}
        self.connected: dict[str, int] = {}    # 服务器名 → 提供了几个工具
        self.errors: dict[str, str] = {}       # 服务器名 → 启动失败的原因
        self.tools: list[Tool] = []
        self._tasks: list[concurrent.futures.Future] = []
        self._stop: asyncio.Event | None = None

    # ------------------------------------------------ 在普通线程里调用

    def start(self, servers: dict[str, dict]) -> list[Tool]:
        """启动所有服务器，返回它们的工具。某个服务器启动失败，不影响其它服务器和内置工具。"""
        self._stop = self._submit(self._new_event()).result()
        waiting = []
        for name, cfg in servers.items():
            ready: concurrent.futures.Future = concurrent.futures.Future()
            self._tasks.append(self._submit(self._serve(name, cfg, ready)))
            waiting.append((name, ready))

        for (name, ready), task in zip(waiting, self._tasks):
            try:
                mcp_tools = ready.result(timeout=START_TIMEOUT)
            except concurrent.futures.TimeoutError:
                task.cancel()
                self.errors[name] = f"超过 {START_TIMEOUT} 秒还没启动好"
                continue
            except Exception as e:
                self.errors[name] = describe_error(e)
                continue
            self.connected[name] = len(mcp_tools)
            self.tools += [self._wrap(name, t) for t in mcp_tools]
        return self.tools

    def call(self, server: str, tool: str, args: dict, cancel_event: threading.Event) -> str:
        session = self.sessions.get(server)
        if session is None:
            raise ToolError(f"MCP 服务器 {server} 没有连接（可能已经退出了）")
        future = self._submit(session.call_tool(tool, args))
        deadline = time.monotonic() + CALL_TIMEOUT
        while True:
            try:
                result = future.result(timeout=0.2)
                break
            except concurrent.futures.TimeoutError:
                if cancel_event.is_set():           # 用户点了停止
                    future.cancel()
                    raise ToolCancelled("MCP 工具调用已被用户中断")
                if time.monotonic() > deadline:
                    future.cancel()
                    raise ToolError(f"MCP 工具 {tool} 超过 {CALL_TIMEOUT} 秒没有返回")
            except Exception as e:
                raise ToolError(f"调用 MCP 工具 {tool} 出错：{describe_error(e)}")

        text = truncate(result_to_text(result))
        if result.is_error:                         # 服务器说"执行失败"，和内置工具出错一样，交回给模型
            raise ToolError(text)
        return text

    def summary(self) -> str:
        if not self.connected and not self.errors:
            return "MCP：未配置"
        ok = "、".join(f"{name}（{n} 个工具）" for name, n in self.connected.items()) or "无"
        bad = f"    启动失败：{'、'.join(self.errors)}" if self.errors else ""
        return f"MCP：{ok}{bad}"

    def close(self) -> None:
        """断开所有服务器（SDK 会结束服务器进程），然后停掉事件循环。"""
        if self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        for task in self._tasks:
            try:
                task.result(timeout=5)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self.loop.close()

    # ------------------------------------------------ 内部

    def _submit(self, coro) -> concurrent.futures.Future:
        """把一个异步任务投递到 MCP 线程的事件循环里，返回一个可以在普通线程里等待的 Future。"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _new_event(self) -> asyncio.Event:
        return asyncio.Event()

    async def _serve(self, name: str, cfg: dict, ready: concurrent.futures.Future) -> None:
        """一个服务器的一生：启动 → 握手 → 列工具 → 一直保持连接 → close() 时断开。"""
        try:
            params = StdioServerParameters(
                command=resolve_command(str(cfg["command"])),
                args=[str(a) for a in cfg.get("args", [])],
                env={str(k): str(v) for k, v in cfg.get("env", {}).items()} or None,
            )
            async with stdio_client(params, errlog=self.errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.sessions[name] = session
                    ready.set_result(listed.tools)
                    await self._stop.wait()          # 停在这里保持连接，直到 close()
        except KeyError:
            if not ready.done():
                ready.set_exception(ValueError("配置里缺少 command"))
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(describe_error(e)))
            if not isinstance(e, Exception):
                raise                                # 取消之类的信号要继续往外传
        finally:
            self.sessions.pop(name, None)

    def _wrap(self, server: str, mcp_tool) -> Tool:
        """把一个 MCP 工具包装成 Tool：说明书照抄，执行函数改成"转发给服务器"。"""
        schema = dict(mcp_tool.input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})

        def func(ctx: ToolContext, **args) -> str:
            return self.call(server, mcp_tool.name, args, ctx.cancel_event)

        return Tool(
            name=tool_name(server, mcp_tool.name),
            description=f"[MCP 服务器 {server} 提供] {mcp_tool.description or ''}".strip(),
            parameters=schema,
            func=func,
            needs_confirm=True,   # 不知道外部工具会做什么，一律先问用户
        )