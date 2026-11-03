# Desktop Agent：用 Python 做一个桌面端 AI 编程助手

两周作业：做一个类似 Cursor / Claude Desktop 的桌面编程助手。
要求：真正的桌面应用、三栏界面、read / write / edit / bash 四个工具、接入 MCP、错误处理、中断、打包成 .exe / .dmg。

## 两周路线

| 阶段 | 时间 | 做什么 | 对应作业要求 |
|---|---|---|---|
| **1 ✅** | 第 1–2 天 | Agent 核心循环 + 四个工具，先在命令行里跑通 | 四个工具、错误处理、中断（逻辑层） |
| **2 ✅** | 第 3 天 | 流式输出（字一个个出来），让"停止"在模型说话中途也能生效 | 中断 |
| 3 | 第 4–7 天 | PySide6 三栏界面：左文件树、中间文件查看/修改对比、右边对话；Agent 跑在后台线程 | 桌面应用、三栏界面 |
| 4 | 第 8–9 天 | 接入 MCP：读 `mcp.json` 启动 MCP 服务器，把它们的工具并进工具列表 | MCP |
| 5 | 第 10–11 天 | PyInstaller 打包 → macOS .app → .dmg；Windows .exe | 打包 |
| 6 | 第 12–14 天 | 测试、打磨、写说明、录演示视频、提交 | — |

**为什么选 PySide6（Qt）做界面？** 作业要求"不能只跑在 localhost"，所以 Streamlit、Gradio、Flask 这类在浏览器里打开的网页都不算。Qt 是原生桌面界面库，Windows 和 macOS 都能用，而且 PyInstaller 对它支持很成熟。

## 第 1 阶段：在 Mac 上跑起来

```bash
# 1. 解压后进入项目（放在哪里都行）
cd ~/Documents/GitHub/desktop-agent

# 2. 建一个 Python 3.13 的虚拟环境并装依赖（如果提示 uv: command not found，先执行 export PATH="$HOME/.local/bin:$PATH"）
uv venv --python 3.13
source .venv/bin/activate
uv pip install -r requirements.txt

# 3. 跑测试：不需要 API Key，32 个测试应该全部 OK
python -m unittest

# 4. 配置 Key：复制模板，用文本编辑器打开，把 LLM_API_KEY 换成你的
cp .env.example .env
open -e .env

# 5. 启动！playground 是给 Agent 折腾的工作目录，会自动创建
python cli.py playground
```

DeepSeek 的 Key 在 https://platform.deepseek.com 的「API keys」里创建，需要先充一点钱（按 token 计费，做作业用量很小）。
用别家模型（通义千问、Kimi、智谱……）也可以，只要它提供 OpenAI 兼容接口：改 `.env` 里的 `LLM_BASE_URL` 和 `LLM_MODEL` 即可。

启动后可以试试：

> 写一个猜数字游戏 guess.py：程序随机选 1 到 100 的数，用户猜，提示大了还是小了。再写一个 test_guess.py 用 unittest 测试判断大小的函数，并运行测试。

你会看到它依次 写文件 → 跑测试 → 如果失败就读报错、改代码 → 再跑，直到通过。每次写文件、改文件、跑命令前都会问你 `[y/n/a]`。任务进行中按 `Ctrl+C` 可以打断。

## 代码结构（建议按这个顺序读）

```
desktop-agent/
├── agent/
│   ├── tools.py    ① 四个工具：每个 = JSON Schema（给模型看的说明书）+ Python 函数（真正干活）
│   ├── core.py     ② Agent 循环：问模型 → 执行工具 → 把结果交回去 → 再问，直到模型给出最终回答
│   ├── llm.py      ③ 和模型通信：OpenAI 兼容接口、错误翻译、DeepSeek 的 reasoning_content 处理
│   └── config.py   ④ 读 .env 配置
├── cli.py          ⑤ 命令行界面（第 3 阶段会换成桌面界面，①–④ 不用改）
└── tests/          ⑥ 32 个测试，用"假模型"和"假服务器"验证所有逻辑
```

### 和课上 ReAct 的对应

| 课件里的 ReAct | 代码里 |
|---|---|
| Thought（想下一步） | `core.py` 里 `self.llm.chat(self.messages, tool_schemas)`：把全部历史发给模型 |
| Action（调用工具） | 模型返回的 `tool_calls`，例如 `{"name": "bash", "arguments": "{\"command\": \"ls\"}"}` |
| Observation（观察结果） | `_execute()` 执行后，追加一条 `{"role": "tool", "tool_call_id": ..., "content": 结果}` |
| 结束 | 模型某一轮不再返回 `tool_calls`，它说的话就是最终回答 |

ReAct 论文原版的做法是让模型在文本里写 `Action: xxx`，再由程序从文字里解析出来。现在的模型 API 有原生的 tool calling，模型直接返回结构化的 JSON，不用自己解析，更可靠，循环的原理完全一样。

## 几个值得弄懂的设计

1. **模型没有记忆，`self.messages` 就是它的记忆。** 每一轮都把完整历史重新发过去，它才知道刚才读过什么。
2. **工具出错不抛给用户，而是作为 Observation 交回给模型。** 比如 `edit_file` 找不到原文，模型看到错误会先 `read_file` 再重试。这就是 Agent 能"自己修 bug"的原因。
3. **中断时要给每个没执行的 `tool_call` 补一条 tool 消息。** API 规定 assistant 的每个 tool_call 后面必须跟对应的 tool 结果，否则下一次请求直接 400，对话就坏了。见 `test_cancel_fills_every_tool_call`。
4. **`edit_file` 要求 `old_string` 只出现一次。** 保证改的就是模型想改的那一处，不会误伤。
5. **路径限制在工作目录里。** `resolve_path()` 拒绝 `../`、绝对路径等越界访问。
6. **DeepSeek V4 的坑：** 思考模式默认开启，返回里多一个 `reasoning_content`。只要请求带了 tools，之后每次都必须原样传回，否则 400。`llm.py` 的 `message_to_dict()` 专门处理它，`test_llm_http.py` 用假服务器模拟了这条规则。

## 动手练习（建议做，答辩时会被问到）

1. **画一遍 messages。** 读 `tests/test_agent.py` 的 `test_full_react_loop`，在纸上写出每次请求时 `messages` 列表的样子。
2. **加一道安全闸。** 在 `bash()` 开头拒绝明显危险的命令（如包含 `rm -rf /`、`sudo`、`mkfs`），抛 `ToolError`。再在 `tests/test_tools.py` 里加一个测试证明它生效。
3. **亲眼看看那个 400。** 在 `llm.py` 里临时注释掉 `out["reasoning_content"] = reasoning` 这一行，`.env` 里设 `LLM_THINKING=on`，让 Agent 做一个要用工具的任务，看报什么错；然后改回来。

## 常见报错

| 现象 | 原因 |
|---|---|
| `没有配置 API Key` | 没有 `.env` 文件，或里面的 `LLM_API_KEY` 是空的 |
| `API Key 无效（401）` | Key 复制错了，或多了空格 |
| `账户余额不足（402）` | 需要到 DeepSeek 平台充值 |
| `接口地址或模型名不对（404）` | 检查 `LLM_BASE_URL` 和 `LLM_MODEL` |
| `连不上模型服务` | 网络或代理问题 |
| `ModuleNotFoundError: openai` | 忘了 `source .venv/bin/activate` |
