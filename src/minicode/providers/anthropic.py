"""Anthropic Messages API 适配器（设计文档第 5 节）。

请求构造与响应解析写成纯函数，可以不发网络请求做单元测试。
系统提示词和最后一个工具定义加 cache_control，让“系统提示词 + 工具列表”
这段稳定前缀命中提示词缓存。
"""

from __future__ import annotations

from typing import Any

from ..messages import Message, ModelResponse, StopReason, TextBlock, ToolCall, ToolResult, ToolSpec, Usage
from .base import ProviderError, TextCallback, classify_http_error

CACHE_CONTROL = {"type": "ephemeral"}

STOP_REASONS: dict[str, StopReason] = {
    "tool_use": "tool_use",
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "pause_turn": "end_turn",  # 只在使用服务端工具时出现，本项目不使用
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
}


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        content: list[dict[str, Any]] = []
        for block in message.blocks:
            if isinstance(block, TextBlock):
                if block.text:  # API 不接受空文本块
                    content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCall):
                content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.arguments})
            elif isinstance(block, ToolResult):
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.call_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
        if content:
            result.append({"role": message.role, "content": content})
    return result


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
    ]
    if result:
        result[-1]["cache_control"] = CACHE_CONTROL
    return result


def build_anthropic_request(
    *, model: str, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}],
        "messages": to_anthropic_messages(messages),
    }
    if tools:
        request["tools"] = to_anthropic_tools(tools)
    return request


def from_anthropic_message(data: dict[str, Any]) -> ModelResponse:
    """把 Messages API 的响应（dict 形式）转成中立的 ModelResponse。"""
    blocks: list[TextBlock | ToolCall] = []
    for block in data.get("content", []):
        if block["type"] == "text":
            blocks.append(TextBlock(text=block["text"]))
        elif block["type"] == "tool_use":
            blocks.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {}))
    usage = data.get("usage") or {}
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    return ModelResponse(
        blocks=blocks,
        stop_reason=STOP_REASONS.get(data.get("stop_reason") or "end_turn", "end_turn"),
        usage=Usage(
            input_tokens=(usage.get("input_tokens") or 0) + cache_read + cache_write,
            output_tokens=usage.get("output_tokens") or 0,
            cache_read_tokens=cache_read,
        ),
    )


class AnthropicProvider:
    def __init__(self, *, model: str, api_key: str) -> None:
        import anthropic

        if not api_key:
            raise ProviderError("缺少 ANTHROPIC_API_KEY")
        self.model = model
        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0)

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        on_text: TextCallback | None = None,
    ) -> ModelResponse:
        request = build_anthropic_request(
            model=self.model, system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
        try:
            with self._client.messages.stream(**request) as stream:
                for text in stream.text_stream:
                    if on_text:
                        on_text(text)
                final = stream.get_final_message()
        except self._sdk.APIStatusError as err:
            raise classify_http_error(err.status_code, err.response.headers, str(err)) from err
        except self._sdk.APIConnectionError as err:  # 包含超时
            raise classify_http_error(None, None, str(err)) from err
        return from_anthropic_message(final.model_dump())
