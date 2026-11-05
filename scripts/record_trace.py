"""录一次真实运行，写成 docs/trace.md。

用法（在项目文件夹里）：
    .venv/bin/python scripts/record_trace.py

它会做三件事：
1. 在 playground/trace-demo 里准备一个有 bug 的小项目（每次都重新准备，所以可以重复录）；
2. 用 .env 里配置的真实模型，让 Agent 去把测试修好。
   需要确认的操作一律自动允许——只在这个准备好的小项目里用，不要拿它跑别的任务；
3. 把全过程（模型每一步说了什么、调用了什么工具、结果是成功还是失败）写进 docs/trace.md。

录制中按 Ctrl+C 可以停止，已经发生的部分照样会写进 trace.md。
"""
from __future__ import annotations

import difflib
import platform
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))      # 让下面的 import agent 能找到项目里的代码

from agent import Agent, ChatLLM, LLMError, load_config  # noqa: E402

WORKSPACE = PROJECT / "playground" / "trace-demo"
OUTPUT = PROJECT / "docs" / "trace.md"

TASK = ("这个项目的测试没有通过。请先运行 test_stats.py 看看哪里失败，"
        "然后修改 stats.py 把问题修好（不要改测试文件），最后再运行一次测试，确认全部通过。")

# 有 bug 的代码：mean 和 mean_abs 遇到空列表会除以 0，median 遇到偶数个数时算错。
# 两个函数的最后一行一模一样，用 edit_file 改的时候，只给这一行会因为"出现了 2 次"而失败。
STATS_PY = '''"""一个小小的统计工具包。"""


def mean(nums):
    """平均值。空列表返回 0。"""
    total = 0
    for n in nums:
        total += n
    return total / len(nums)


def mean_abs(nums):
    """绝对值的平均值。空列表返回 0。"""
    total = 0
    for n in nums:
        total += abs(n)
    return total / len(nums)


def median(nums):
    """中位数。空列表返回 0；偶数个数时取中间两个数的平均值。"""
    if not nums:
        return 0
    s = sorted(nums)
    mid = len(s) // 2
    return s[mid]
'''

TEST_STATS_PY = '''import unittest

from stats import mean, mean_abs, median


class StatsTests(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)
        self.assertEqual(mean([]), 0)

    def test_mean_abs(self):
        self.assertEqual(mean_abs([-2, 2]), 2)
        self.assertEqual(mean_abs([]), 0)

    def test_median(self):
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)
        self.assertEqual(median([]), 0)


if __name__ == "__main__":
    unittest.main()
'''

FENCE = "````"          # 用四个反引号包代码，工具输出里就算有 ``` 也不会把格式弄乱


def prepare_workspace() -> None:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)
    (WORKSPACE / "stats.py").write_text(STATS_PY, encoding="utf-8")
    (WORKSPACE / "test_stats.py").write_text(TEST_STATS_PY, encoding="utf-8")


def clean(text: str) -> str:
    """去掉本机路径（里面有用户名），公开的仓库里只留相对位置。"""
    return text.replace(str(WORKSPACE), "<工作目录>").replace(str(Path.home()), "~")


def clip(text: str, max_lines: int = 40) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + f"\n…（后面还有 {len(lines) - max_lines} 行，省略）"


class Recorder:
    """把 Agent 的一举一动按时间顺序记下来，同时打印到终端。"""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.entries: list[dict] = []
        self.steps = 0

    def now(self) -> float:
        return time.monotonic() - self.start

    def add(self, kind: str, **data) -> None:
        self.entries.append({"kind": kind, "t": self.now(), **data})
        self.echo(kind, data)

    def echo(self, kind: str, data: dict) -> None:
        if kind == "step":
            print(f"\n===== 第 {data['n']} 步（{self.now():.1f} 秒）", flush=True)
        elif kind == "text":
            print(f"模型：{data['text']}", flush=True)
        elif kind == "tool_call":
            print(f"  ▸ {data['name']} {clean(str(data['args']))[:200]}", flush=True)
        elif kind == "tool_result":
            print(f"    [{status(data)}] {clip(clean(data['result']), 6)}", flush=True)
        elif kind in ("warning", "error"):
            print(f"  ⚠ {data['text']}", flush=True)

    # ---- 给 Agent 用的回调
    def on_event(self, kind: str, data: dict) -> None:
        if kind == "tool_call":
            self.add("tool_call", name=data["name"], args=data["args"])
        elif kind == "tool_result":
            self.add("tool_result", name=data["name"], result=data["result"], ok=data["ok"])
        elif kind in ("warning", "error"):
            self.add(kind, text=data["text"])
        elif kind == "cancelled":
            self.add("warning", text="录制被中断")


class RecordingLLM:
    """包在真正的模型外面：每问一次模型算一"步"，并记下模型的思考和回答。"""

    def __init__(self, llm, recorder: Recorder):
        self.llm = llm
        self.rec = recorder

    def _record(self, reply: dict) -> None:
        if reply.get("reasoning_content"):
            self.rec.add("thinking", text=reply["reasoning_content"])
        if reply.get("content"):
            self.rec.add("text", text=reply["content"])

    def chat(self, messages, tools=None):
        self.rec.steps += 1
        self.rec.add("step", n=self.rec.steps)
        reply, finish = self.llm.chat(messages, tools)
        self._record(reply)
        return reply, finish

    def chat_stream(self, messages, tools=None, on_text=None, cancel_event=None):
        self.rec.steps += 1
        self.rec.add("step", n=self.rec.steps)
        reply, finish = self.llm.chat_stream(messages, tools, on_text=None, cancel_event=cancel_event)
        self._record(reply)
        return reply, finish


def describe_call(name: str, args: dict) -> str:
    """一次工具调用写成 Markdown：标题一行，代码另起一段（代码块必须单独成行）。"""
    title = f"**调用 `{name}`**"
    if name == "bash":
        return f"{title}\n\n{FENCE}sh\n{args.get('command', '')}\n{FENCE}"
    if name == "read_file":
        return f"{title}：`{args.get('path')}`"
    if name == "write_file":
        return f"{title}：`{args.get('path')}`\n\n{FENCE}python\n{clip(str(args.get('content', '')))}\n{FENCE}"
    if name == "edit_file":
        old = "\n".join("- " + line for line in str(args.get("old_string", "")).splitlines())
        new = "\n".join("+ " + line for line in str(args.get("new_string", "")).splitlines())
        extra = "（replace_all=true）" if args.get("replace_all") else ""
        return f"{title}：`{args.get('path')}`{extra}\n\n{FENCE}diff\n{old}\n{new}\n{FENCE}"
    return f"{title}\n\n{FENCE}json\n{args}\n{FENCE}"


def exit_code(result: str) -> int | None:
    """bash 的结果第一行是 "exit code: N"。"""
    first = result.split("\n", 1)[0]
    if first.startswith("exit code: "):
        try:
            return int(first.removeprefix("exit code: "))
        except ValueError:
            return None
    return None


def status(entry: dict) -> str:
    """工具本身出错是一种失败；命令跑完了但退出码不是 0（比如测试没过）是另一种。"""
    if not entry["ok"]:
        return "❌ 工具报错"
    code = exit_code(entry["result"]) if entry["name"] == "bash" else None
    if code:
        return f"⚠️ 命令执行完了，但退出码是 {code}（命令本身失败）"
    return "✅ 成功"


def run_tests() -> tuple[bool, str]:
    """录完以后，脚本自己再跑一次测试，不只听模型说"已经通过了"。"""
    proc = subprocess.run([sys.executable, "-m", "unittest", "test_stats"], cwd=WORKSPACE,
                          capture_output=True, text=True, timeout=60)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def write_markdown(rec: Recorder, info: dict, answer: str) -> str:
    calls = [e for e in rec.entries if e["kind"] == "tool_call"]
    results = [e for e in rec.entries if e["kind"] == "tool_result"]
    tool_errors = sum(1 for e in results if not e["ok"])
    bad_exits = sum(1 for e in results if e["ok"] and e["name"] == "bash" and exit_code(e["result"]))
    passed, test_output = run_tests()
    final = (WORKSPACE / "stats.py").read_text(encoding="utf-8")
    diff = "\n".join(difflib.unified_diff(STATS_PY.splitlines(), final.splitlines(),
                                          "stats.py（修改前）", "stats.py（修改后）", lineterm=""))

    out = [
        "# 一次真实运行记录（trace）",
        "",
        "由 `scripts/record_trace.py` 自动生成：脚本准备一个有 bug 的小项目，"
        "让 Agent 用真实模型把测试修好，并记下每一步。需要确认的操作由脚本自动允许。",
        "",
        f"- 时间：{info['time']}",
        f"- 模型：`{info['model']}`（思考模式：{info['thinking']}）",
        f"- 系统：{info['system']}，Python {platform.python_version()}",
        f"- 过程：问了模型 {rec.steps} 次，调用工具 {len(calls)} 次；其中工具报错 {tool_errors} 次，"
        f"命令退出码非 0 {bad_exits} 次；用时 {rec.now():.0f} 秒",
        f"- 脚本复查：{'测试全部通过' if passed else '测试没有全部通过'}",
        "",
        "## 任务",
        "",
        f"> {TASK}",
        "",
        "<details><summary>准备好的代码（有 bug）</summary>",
        "",
        f"{FENCE}python\n# stats.py\n{STATS_PY}{FENCE}",
        "",
        f"{FENCE}python\n# test_stats.py\n{TEST_STATS_PY}{FENCE}",
        "",
        "</details>",
        "",
        "## 运行过程",
    ]

    for e in rec.entries:
        kind = e["kind"]
        if kind == "step":
            out += ["", f"### 第 {e['n']} 步　<sub>{e['t']:.1f} 秒</sub>", ""]
        elif kind == "thinking":
            thought = clean(e["text"]).strip()
            short = thought if len(thought) <= 1500 else thought[:1500] + "…（省略）"
            out += ["<details><summary>模型的思考</summary>", "", short, "", "</details>", ""]
        elif kind == "text":
            out += [f"**模型**：{clean(e['text']).strip()}", ""]
        elif kind == "tool_call":
            out += [clean(describe_call(e["name"], e["args"])), ""]
        elif kind == "tool_result":
            out += [status(e), "", f"{FENCE}\n{clip(clean(e['result']))}\n{FENCE}", ""]
        elif kind == "warning":
            out += [f"⚠️ {clean(e['text'])}", ""]
        elif kind == "error":
            out += [f"🛑 出错：{clean(e['text'])}", ""]

    out += ["## 最终回答", "", clean(answer).strip() or "（没有最终回答）", "",
            "## 最终改动", "", f"{FENCE}diff\n{diff or '（没有改动）'}\n{FENCE}", "",
            "## 脚本复查：重新运行测试", "", f"{FENCE}\n{clip(clean(test_output))}\n{FENCE}", ""]
    return "\n".join(out)


def record(llm, model: str, thinking, max_steps: int = 30) -> Path:
    prepare_workspace()
    rec = Recorder()
    agent = Agent(RecordingLLM(llm, rec), WORKSPACE, max_steps=max_steps,
                  on_event=rec.on_event, confirm=lambda name, args: True)
    info = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": model,
        "thinking": {True: "开", False: "关", None: "服务商默认"}[thinking],
        "system": f"macOS {platform.mac_ver()[0]}" if sys.platform == "darwin" else platform.platform(),
    }
    print(f"任务：{TASK}\n工作目录：{WORKSPACE}", flush=True)

    result: dict = {}
    worker = threading.Thread(target=lambda: result.setdefault("answer", agent.run(TASK)), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(timeout=0.2)
    except KeyboardInterrupt:
        agent.cancel()
        worker.join(timeout=10)

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(write_markdown(rec, info, result.get("answer", "")), encoding="utf-8")
    return OUTPUT


def main() -> None:
    cfg = load_config(WORKSPACE)
    try:
        llm = ChatLLM(cfg.api_key, cfg.base_url, cfg.model, thinking=cfg.thinking)
    except LLMError as e:
        sys.exit(str(e))
    path = record(llm, cfg.model, cfg.thinking, cfg.max_steps)
    print(f"\n已写入 {path.relative_to(PROJECT)}", flush=True)


if __name__ == "__main__":
    main()
