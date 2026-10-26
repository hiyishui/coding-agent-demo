"""中立消息模型（设计文档第 4 节）。

Agent 循环、事件日志、工具层只使用这里定义的类型，不接触任何厂商的协议字段。
厂商之间的差异（工具调用参数是对象还是 JSON 字符串、工具结果放在哪个角色里……）
全部由 providers/ 下的适配器负责转换。这样换模型只需要换适配器。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str
    is_error: bool = False


Block = Annotated[TextBlock | ToolCall | ToolResult, Field(discriminator="type")]


class Message(BaseModel):
    """一条对话消息。工具结果放在 user 消息里（与 Anthropic 一致）。"""

    role: Literal["user", "assistant"]
    blocks: list[Block]

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", blocks=[TextBlock(text=text)])

    @property
    def text(self) -> str:
        """所有文本块，块之间空一行（合并后的 user 消息可能有多个文本块）。"""
        return "\n\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.blocks if isinstance(b, ToolCall)]

    @property
    def tool_results(self) -> list[ToolResult]:
        return [b for b in self.blocks if isinstance(b, ToolResult)]


class Usage(BaseModel):
    """一次模型调用的 token 用量。input_tokens 包含命中缓存的部分。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]


class ModelResponse(BaseModel):
    blocks: list[Block]
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.blocks if isinstance(b, ToolCall)]

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))


class ToolSpec(BaseModel):
    """发给模型的工具定义：名称、描述和参数的 JSON Schema。"""

    name: str
    description: str
    input_schema: dict[str, Any]
