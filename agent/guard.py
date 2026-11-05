"""防死循环：发现模型在同一轮提问里，反复用完全相同的参数调用同一个工具。

模型卡住时常见的表现是：一遍又一遍地跑同一条命令、读同一个文件，以为结果会变。
max_steps（一轮最多 30 步）能兜底，但要白白浪费很多步和 token。
这里更早发现：名字和参数完全相同的调用"连续"超过上限，就不再执行，
而是把一句提醒交回给模型，让它换做法。

只看"连续"：中间夹了别的调用就重新计数。所以"改代码 → 跑测试 → 再改 → 再跑测试"
这种正常的反复不会被误拦，因为两次跑测试之间隔着一次改代码。
"""
from __future__ import annotations

import json


class RepeatGuard:
    def __init__(self, limit: int = 3):
        self.limit = limit          # 允许连续相同的次数，超过就拦下
        self.reset()

    def reset(self) -> None:
        """每次用户新提问时调用，重新计数。"""
        self._last: tuple[str, str] | None = None
        self._count = 0

    def check(self, name: str, args: dict) -> str | None:
        """记录一次调用。连续相同的次数超过上限时，返回给模型的提醒；否则返回 None。"""
        # sort_keys：参数顺序不同但内容一样，也算同一个调用
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        self._count = self._count + 1 if key == self._last else 1
        self._last = key
        if self._count > self.limit:
            return (f"错误：你已经连续 {self._count} 次用完全相同的参数调用 {name}，结果不会变。"
                    "请换一种做法；如果确实做不下去，就停下来告诉用户卡在哪里。")
        return None