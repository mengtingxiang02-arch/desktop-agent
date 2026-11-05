"""读取配置：API Key、接口地址、模型名、工作目录。

优先级：环境变量 > 项目根目录下的 .env 文件。
.env 里放密钥，千万不要提交到 Git（.gitignore 已经忽略它）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    api_key: str
    base_url: str
    model: str
    workspace: Path          # Agent 只能在这个目录里读写文件、跑命令
    max_steps: int = 30      # 一次提问里最多循环多少步，防止模型陷入死循环
    thinking: bool | None = None  # DeepSeek 思考模式：True 开 / False 关 / None 用服务商默认
    vision: bool = False     # 模型能不能看图：开了以后，read_file 读到的图片会发给模型


def _parse_bool(value: str | None) -> bool | None:
    if value is None or value.strip() == "":
        return None
    return value.strip().lower() in {"1", "true", "on", "yes"}


def load_config(workspace: str | os.PathLike | None = None) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    ws = Path(workspace or os.getenv("AGENT_WORKSPACE") or Path.cwd()).expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)

    return Config(
        api_key=os.getenv("LLM_API_KEY", "").strip(),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip(),
        model=os.getenv("LLM_MODEL", "deepseek-flash").strip(),
        workspace=ws,
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "30")),
        thinking=_parse_bool(os.getenv("LLM_THINKING")),
        vision=bool(_parse_bool(os.getenv("LLM_VISION"))),
    )
