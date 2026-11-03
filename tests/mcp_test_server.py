"""测试用的小 MCP 服务器，不需要联网。test_mcp.py 会把它当子进程启动。"""
import anyio

from mcp.server import MCPServer

server = MCPServer("test-server", log_level="WARNING")


@server.tool()
def add(a: int, b: int) -> int:
    """两个整数相加"""
    return a + b


@server.tool()
def fail() -> str:
    """一定会失败的工具"""
    raise ValueError("故意失败")


@server.tool()
async def slow(seconds: float) -> str:
    """等一会儿再返回"""
    await anyio.sleep(seconds)
    return "睡醒了"


if __name__ == "__main__":
    server.run()