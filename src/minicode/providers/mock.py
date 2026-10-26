"""Mock 模型：按 JSON 脚本回放响应（设计文档第 5 节）。

它不看输入，只按顺序回放，因此可以在没有 API Key、没有网络的情况下
完整演示和测试 Agent 循环。脚本项格式：

    {"text": "先看看项目结构。", "tool_calls": [{"name": "list_files", "arguments": {}}]}

压缩请求（tools 为空且系统提示词以压缩标记开头）不消耗脚本，
而是根据输入确定性地生成一段摘要。
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from ..context import COMPACT_MARKER
from ..messages import Message, ModelResponse, TextBlock, ToolCall, ToolSpec, Usage
from .base import TextCallback

SCRIPT_EXHAUSTED = "（Mock 脚本已结束）"


class ScriptedProvider:
    def __init__(self, script: list[dict[str, Any]], model: str = "mock") -> None:
        self.model = model
        self._script = list(script)
        self._cursor = 0
        self._session_tag = secrets.token_hex(2)  # 区分不同进程，--resume 后 id 也不会重复
        self._call_counter = 0

    @classmethod
    def from_file(cls, path: Path, model: str = "mock") -> ScriptedProvider:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), model=model)

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        on_text: TextCallback | None = None,
    ) -> ModelResponse:
        if not tools and system.startswith(COMPACT_MARKER):
            text = _deterministic_summary(messages)
            calls: list[ToolCall] = []
        elif self._cursor < len(self._script):
            item = self._script[self._cursor]
            self._cursor += 1
            text = item.get("text", "")
            calls = [self._make_call(c) for c in item.get("tool_calls", [])]
        else:
            text, calls = SCRIPT_EXHAUSTED, []

        if on_text:
            for word in re.findall(r"\S+\s*|\s+", text):
                on_text(word)

        blocks: list[TextBlock | ToolCall] = [TextBlock(text=text)] if text else []
        blocks.extend(calls)
        prompt_chars = len(system) + sum(len(m.model_dump_json()) for m in messages)
        prompt_chars += sum(len(t.model_dump_json()) for t in tools)
        output_chars = len(text) + sum(len(json.dumps(c.arguments, ensure_ascii=False)) for c in calls)
        return ModelResponse(
            blocks=blocks,
            stop_reason="tool_use" if calls else "end_turn",
            usage=Usage(input_tokens=prompt_chars // 4, output_tokens=output_chars // 4),
        )

    def _make_call(self, spec: dict[str, Any]) -> ToolCall:
        self._call_counter += 1
        return ToolCall(
            id=f"mock_{self._session_tag}_{self._call_counter}", name=spec["name"], arguments=spec.get("arguments", {})
        )


def _deterministic_summary(messages: list[Message]) -> str:
    transcript = "\n".join(m.text for m in messages)
    goal = next((line for line in transcript.splitlines() if line.startswith("用户：")), "用户：（未知）")
    files = sorted(set(re.findall(r"[\w./-]+\.(?:py|md|json|toml|txt)", transcript)))
    return (
        f"任务目标：{goal.removeprefix('用户：')[:200]}\n"
        f"已完成：已处理 {len(transcript)} 字符的对话（Mock 摘要）\n"
        f"修改过的文件：{', '.join(files[:10]) or '无'}\n"
        "关键决策与发现：无\n未解决问题：无\n下一步：继续当前任务"
    )
