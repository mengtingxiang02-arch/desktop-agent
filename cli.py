"""命令行版 Agent 的入口。

用法：
    python cli.py                # 以当前目录为工作目录
    python cli.py ./playground   # 指定工作目录（不存在会自动创建）

对话中：
    /reset   清空对话历史        /exit   退出
    Ctrl+C   打断正在输出的模型 / 正在执行的任务
             再按一次 Ctrl+C（或输入 /exit）直接退出程序

两点设计：
1. 模型的回答是流式打印的（打字机效果）：模型吐一个字，屏幕上一个字。
2. Agent 跑在后台线程里，主线程专职接收 Ctrl+C —— 信号只会送到主线程，
   所以"随时能停"必须靠这个结构。之后的桌面版是同样的分工：
   界面线程负责响应"停止"按钮，Agent 在后台线程里工作。
"""
from __future__ import annotations

import sys
import threading
from agent import Agent, ChatLLM, LLMError, load_config

# ---- 一点颜色，让输出好读（现代终端包括 Windows Terminal 都支持）
DIM, RED, YELLOW, CYAN, GREEN, BOLD, RESET = "\033[2m", "\033[31m", "\033[33m", "\033[36m", "\033[32m", "\033[1m", "\033[0m"

print_lock = threading.Lock()


def out(text: str = "") -> None:
    with print_lock:
        print(text, flush=True)


class StreamPrinter:
    """把模型一片片吐出来的文字连续打印出来，做出"打字机"效果。

    两个状态：
      open     —— 当前有一行流式输出还没收尾（收尾要补换行，否则会和下一行粘连）
      silenced —— 用户已经按了 Ctrl+C，模型再吐字就不再往屏幕上写
    """

    def __init__(self) -> None:
        self.open = False
        self.silenced = False

    def begin_turn(self) -> None:
        """每轮对话开始时调用：重新允许打印。"""
        with print_lock:
            self.silenced = False
            self.open = False

    def delta(self, text: str) -> None:
        with print_lock:
            if self.silenced or not text:
                return
            if not self.open:
                sys.stdout.write("\n")      # 和上面的提示分行
                self.open = True
            sys.stdout.write(f"{CYAN}{text}{RESET}")
            sys.stdout.flush()

    def finish(self) -> None:
        with print_lock:
            if self.open:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self.open = False

    def silence(self) -> None:
        """用户中断：把半截输出收个尾，之后到的片段直接丢掉。"""
        with print_lock:
            self.silenced = True
            if self.open:
                sys.stdout.write(f"{RESET}\n")
                sys.stdout.flush()
                self.open = False


printer = StreamPrinter()


def short(text: str, max_lines: int = 4, max_chars: int = 300) -> str:
    lines = text.strip().splitlines()
    clipped = "\n".join(lines[:max_lines])[:max_chars]
    if len(lines) > max_lines or len(clipped) < len(text.strip()):
        clipped += " …"
    return clipped


def describe_call(name: str, args: dict) -> str:
    if name == "bash":
        return f"$ {args.get('command', '')}"
    if name == "write_file":
        n = str(args.get("content", "")).count("\n") + 1
        return f"写入 {args.get('path')}（约 {n} 行）"
    if name == "edit_file":
        return f"修改 {args.get('path')}"
    if name == "read_file":
        return f"读取 {args.get('path')}"
    return f"{name} {args}"


def on_event(kind: str, data: dict) -> None:
    if kind == "assistant_delta":
        # 流式片段：不换行，接着上一次的位置往下写
        printer.delta(data["text"])
        return

    # 其它事件都是"整行"输出，先把还没收尾的流式那行结束掉
    printer.finish()

    if kind == "assistant_text":
        out(f"\n{CYAN}{data['text']}{RESET}")
    elif kind == "tool_call":
        out(f"{BOLD}  ▸ {describe_call(data['name'], data['args'])}{RESET}")
    elif kind == "tool_result":
        color = DIM if data["ok"] else RED
        body = short(data["result"]).replace("\n", "\n    ")
        out(f"{color}    {body}{RESET}")
    elif kind == "warning":
        out(f"{YELLOW}⚠ {data['text']}{RESET}")
    elif kind == "error":
        out(f"{RED}✗ {data['text']}{RESET}")
    elif kind == "cancelled":
        out(f"{YELLOW}■ 已停止{RESET}")


def make_confirm(agent_ref: list):
    always_allowed: set[str] = set()

    def confirm(name: str, args: dict) -> bool:
        agent = agent_ref[0]
        if agent.cancel_event.is_set():
            return False
        if name in always_allowed:
            return True
        with print_lock:
            print(f"{YELLOW}  允许执行吗？ {describe_call(name, args)}{RESET}")
            if name == "edit_file":
                print(f"{DIM}    - {short(str(args.get('old_string', '')), 6)}\n    + {short(str(args.get('new_string', '')), 6)}{RESET}")
        # 注意：input() 不能拿着 print_lock，否则用户按 Ctrl+C 时主线程会被这把锁卡住，
        # 连"正在停止…"都打印不出来，看起来就像程序卡死了。
        answer = input(f"  [y] 允许  [n] 拒绝  [a] 本次会话都允许 {name} > ").strip().lower()
        if agent.cancel_event.is_set():     # 等输入的期间用户按了 Ctrl+C
            return False
        if answer == "a":
            always_allowed.add(name)
            return True
        return answer in {"y", "yes", "是"}

    return confirm


def main() -> None:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    try:
        llm = ChatLLM(cfg.api_key, cfg.base_url, cfg.model, thinking=cfg.thinking)
    except LLMError as e:
        out(f"{RED}{e}{RESET}")
        out("提示：复制 .env.example 为 .env，然后把你的 API Key 填进去。")
        sys.exit(1)

    agent_ref: list = []
    agent = Agent(llm, cfg.workspace, max_steps=cfg.max_steps,
                  on_event=on_event, confirm=make_confirm(agent_ref), vision=cfg.vision)
    agent_ref.append(agent)

    out(f"{GREEN}{BOLD}Desktop Agent · 命令行版{RESET}")
    out(f"{DIM}模型：{cfg.model}    工作目录：{cfg.workspace}{RESET}")
    out(f"{DIM}输入 /exit 退出，/reset 清空对话；模型输出或任务执行中按 Ctrl+C 可随时打断。{RESET}")

    while True:
        try:
            user_text = input(f"\n{BOLD}你 > {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            out("\n再见")
            break
        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            break
        if user_text == "/reset":
            agent.reset()
            out(f"{DIM}（对话已清空）{RESET}")
            continue

        printer.begin_turn()
        worker = threading.Thread(target=agent.run, args=(user_text,), daemon=True)
        worker.start()
        cancel_sent = False
        while worker.is_alive():
            try:
                worker.join(timeout=0.2)        # 带 timeout 的 join 才能被 Ctrl+C 打断
            except KeyboardInterrupt:
                if cancel_sent:
                    # 已经请求过停止了还卡着：不客气了，直接退出
                    printer.finish()
                    out("\n强制退出")
                    sys.exit(130)
                agent.cancel()
                cancel_sent = True
                printer.silence()               # 模型正在吐字也不再往屏幕上写
                out(f"{YELLOW}（正在停止…如果卡在确认提示，按回车即可）{RESET}")
        printer.finish()                        # 收尾：保证下一行提示从新行开始


if __name__ == "__main__":
    main()
