"""OpenAI 兼容 Chat Completions 适配器（设计文档第 5 节）。

兼容 OpenAI、DeepSeek、Qwen、Ollama、vLLM 等服务，改 OPENAI_BASE_URL 即可切换。
与 Anthropic 协议的主要差异：
- 工具调用参数是 JSON **字符串**，可能解析失败；
- 工具结果是独立的 role="tool" 消息，而不是 user 消息里的块；
- 流式响应里工具调用按 index 分片到达，需要自己拼接。
"""

from __future__ import annotations

import json
from typing import Any

from ..messages import Message, ModelResponse, StopReason, TextBlock, ToolCall, ToolSpec, Usage
from .base import ProviderError, TextCallback, classify_http_error

FINISH_REASONS: dict[str, StopReason] = {
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "assistant":
            # 只有带 tool_calls 时 content 才能为 null；空回复发 null 会被接口拒绝
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": message.text or (None if message.tool_calls else ""),
            }
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": _dump_arguments(call.arguments)},
                    }
                    for call in message.tool_calls
                ]
            result.append(entry)
            continue
        # user 消息：先输出 tool 消息（必须紧跟在带 tool_calls 的 assistant 消息之后），再输出文本
        for block in message.tool_results:
            content = f"ERROR: {block.content}" if block.is_error else block.content
            result.append({"role": "tool", "tool_call_id": block.call_id, "content": content})
        if message.text:
            result.append({"role": "user", "content": message.text})
    return result


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
        for t in tools
    ]


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    """解析参数字符串；失败时保留原文放进 {"_raw": ...}，由工具层报参数错误给模型。"""
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": raw}


def _dump_arguments(arguments: dict[str, Any]) -> str:
    if set(arguments) == {"_raw"}:  # 原样回传模型当初生成的字符串
        return str(arguments["_raw"])
    return json.dumps(arguments, ensure_ascii=False)


def from_openai_message(
    message: dict[str, Any], finish_reason: str | None, usage: dict[str, Any] | None = None
) -> ModelResponse:
    """把 choices[0].message（dict 形式）转成中立的 ModelResponse。"""
    blocks: list[TextBlock | ToolCall] = []
    if message.get("content"):
        blocks.append(TextBlock(text=message["content"]))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        blocks.append(
            ToolCall(
                id=call["id"], name=function.get("name", ""), arguments=parse_tool_arguments(function.get("arguments"))
            )
        )
    stop_reason = FINISH_REASONS.get(finish_reason or "", "end_turn")
    usage = usage or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return ModelResponse(
        blocks=blocks,
        stop_reason=stop_reason,
        usage=Usage(
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            cache_read_tokens=cached,
        ),
    )


class StreamAccumulator:
    """把流式分片（chunk 的 dict 形式）拼成一条完整消息。"""

    def __init__(self) -> None:
        self.content = ""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None

    def feed(self, chunk: dict[str, Any]) -> str:
        """吸收一个分片，返回其中新增的文本（用于流式显示）。"""
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        new_text = ""
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                new_text += delta["content"]
            for part in delta.get("tool_calls") or []:
                slot = self.tool_calls.setdefault(part.get("index", 0), {"id": "", "name": "", "arguments": ""})
                function = part.get("function") or {}
                slot["id"] = part.get("id") or slot["id"]
                slot["name"] += function.get("name") or ""
                slot["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        self.content += new_text
        return new_text

    def result(self) -> ModelResponse:
        message = {
            "content": self.content,
            "tool_calls": [
                {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
                for _, c in sorted(self.tool_calls.items())
            ],
        }
        return from_openai_message(message, self.finish_reason, self.usage)


class OpenAICompatProvider:
    def __init__(self, *, model: str, api_key: str, base_url: str | None = None) -> None:
        import openai

        if not api_key:
            raise ProviderError("缺少 OPENAI_API_KEY（本地 Ollama 等服务可随便填一个值）")
        self.model = model
        self._sdk = openai
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        # OpenAI 官方新模型只接受 max_completion_tokens，多数兼容服务只认 max_tokens
        is_official = base_url is None or "api.openai.com" in base_url
        self._max_tokens_param = "max_completion_tokens" if is_official else "max_tokens"

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        on_text: TextCallback | None = None,
    ) -> ModelResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(system, messages),
            self._max_tokens_param: max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = to_openai_tools(tools)
        accumulator = StreamAccumulator()
        try:
            for chunk in self._client.chat.completions.create(**request):
                text = accumulator.feed(chunk.model_dump())
                if text and on_text:
                    on_text(text)
        except self._sdk.APIStatusError as err:
            raise classify_http_error(err.status_code, err.response.headers, str(err)) from err
        except self._sdk.APIConnectionError as err:
            raise classify_http_error(None, None, str(err)) from err
        return accumulator.result()
