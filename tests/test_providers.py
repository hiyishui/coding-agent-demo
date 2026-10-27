"""模型适配器：请求构造与响应解析（手写样例，不发网络请求）。"""

from __future__ import annotations

import json

import pytest

from minicode.messages import Message, TextBlock, ToolCall, ToolResult, ToolSpec
from minicode.providers.anthropic import build_anthropic_request, from_anthropic_message, to_anthropic_messages
from minicode.providers.base import ProviderError, RetryableError, classify_http_error
from minicode.providers.mock import SCRIPT_EXHAUSTED, ScriptedProvider
from minicode.providers.openai_compat import (
    StreamAccumulator,
    from_openai_message,
    parse_tool_arguments,
    to_openai_messages,
    to_openai_tools,
)

TOOLS = [
    ToolSpec(name="read_file", description="读文件", input_schema={"type": "object", "properties": {}}),
    ToolSpec(name="list_files", description="列文件", input_schema={"type": "object", "properties": {}}),
]

CONVERSATION = [
    Message.user("修 bug"),
    Message(
        role="assistant",
        blocks=[
            TextBlock(text="先读文件。"),
            ToolCall(id="call_1", name="read_file", arguments={"path": "calc.py"}),
            ToolCall(id="call_2", name="read_file", arguments={"path": "missing.py"}),
        ],
    ),
    Message(
        role="user",
        blocks=[
            ToolResult(call_id="call_1", content="def f(): ..."),
            ToolResult(call_id="call_2", content="文件不存在", is_error=True),
            TextBlock(text="顺便看看测试"),
        ],
    ),
]


# ---- Anthropic ----


def test_anthropic_request_has_cache_control_and_tool_blocks():
    request = build_anthropic_request(
        model="claude-x", system="SYS", messages=CONVERSATION, tools=TOOLS, max_tokens=1000
    )
    assert request["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert "cache_control" not in request["tools"][0]
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert request["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    assistant = request["messages"][1]
    assert assistant["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "read_file",
        "input": {"path": "calc.py"},
    }
    user = request["messages"][2]
    assert [b["type"] for b in user["content"]] == ["tool_result", "tool_result", "text"]
    assert user["content"][0]["is_error"] is False
    assert user["content"][1] == {
        "type": "tool_result",
        "tool_use_id": "call_2",
        "content": "文件不存在",
        "is_error": True,
    }


def test_anthropic_skips_empty_text_blocks():
    messages = [Message(role="assistant", blocks=[TextBlock(text=""), ToolCall(id="a", name="x", arguments={})])]
    assert to_anthropic_messages(messages)[0]["content"] == [{"type": "tool_use", "id": "a", "name": "x", "input": {}}]


def test_anthropic_response_parsing():
    response = from_anthropic_message(
        {
            "content": [
                {"type": "text", "text": "我来读文件"},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900},
        }
    )
    assert response.text == "我来读文件"
    assert response.tool_calls == [ToolCall(id="toolu_1", name="read_file", arguments={"path": "a.py"})]
    assert response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 1000 and response.usage.cache_read_tokens == 900


def test_anthropic_stop_reason_mapping():
    def stop(reason):
        return from_anthropic_message({"content": [], "stop_reason": reason}).stop_reason

    assert stop("end_turn") == "end_turn"
    assert stop("stop_sequence") == "end_turn"
    assert stop("pause_turn") == "end_turn"
    assert stop("max_tokens") == "max_tokens"
    assert stop("refusal") == "refusal"


# ---- OpenAI 兼容 ----


def test_openai_messages_tool_role_order():
    messages = to_openai_messages("SYS", CONVERSATION)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool", "user"]
    assistant = messages[2]
    assert assistant["content"] == "先读文件。"
    assert assistant["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "calc.py"}'}
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "def f(): ..."}
    assert messages[4]["content"] == "ERROR: 文件不存在"
    assert messages[5] == {"role": "user", "content": "顺便看看测试"}


def test_openai_tools_format():
    assert to_openai_tools(TOOLS[:1]) == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读文件",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_openai_arguments_parse_failure_keeps_raw():
    assert parse_tool_arguments('{"path": "a.py"}') == {"path": "a.py"}
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments('{"path": "a.py"') == {"_raw": '{"path": "a.py"'}
    assert parse_tool_arguments("[1, 2]") == {"_raw": "[1, 2]"}
    # 原文会原样回传给模型，而不是被改写
    message = Message(role="assistant", blocks=[ToolCall(id="c", name="x", arguments={"_raw": "{bad"})])
    assert to_openai_messages("S", [message])[1]["tool_calls"][0]["function"]["arguments"] == "{bad"


def test_openai_response_parsing():
    response = from_openai_message(
        {
            "content": None,
            "tool_calls": [{"id": "call_9", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}],
        },
        "tool_calls",
        {"prompt_tokens": 50, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 32}},
    )
    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].name == "list_files" and response.tool_calls[0].arguments == {}
    assert response.usage.input_tokens == 50 and response.usage.cache_read_tokens == 32
    assert from_openai_message({"content": "x"}, "length").stop_reason == "max_tokens"
    assert from_openai_message({"content": "x"}, "content_filter").stop_reason == "refusal"
    assert from_openai_message({"content": "x"}, "stop").stop_reason == "end_turn"


def test_openai_stream_accumulates_tool_call_deltas_by_index():
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "我来"}}]},
        {"choices": [{"delta": {"content": "看看"}}]},
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "id": "c0", "function": {"name": "read_", "arguments": ""}}]}}
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 1, "id": "c1", "function": {"name": "list_files", "arguments": "{}"}}]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
    ]
    accumulator = StreamAccumulator()
    streamed = "".join(accumulator.feed(chunk) for chunk in chunks)
    response = accumulator.result()
    assert streamed == "我来看看" and response.text == "我来看看"
    assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [
        ("c0", "read_file", {"path": "a.py"}),
        ("c1", "list_files", {}),
    ]
    assert response.stop_reason == "tool_use" and response.usage.output_tokens == 3


# ---- 错误分类与 Mock ----


def test_classify_http_error():
    assert isinstance(classify_http_error(429, {"retry-after": "3"}, "x"), RetryableError)
    assert classify_http_error(429, {"retry-after": "3"}, "x").retry_after == 3.0
    assert isinstance(classify_http_error(503, None, "x"), RetryableError)
    assert isinstance(classify_http_error(None, None, "timeout"), RetryableError)
    error = classify_http_error(400, None, "bad request")
    assert isinstance(error, ProviderError) and not isinstance(error, RetryableError)


def test_scripted_provider_replays_and_streams():
    provider = ScriptedProvider([{"text": "先看看 文件", "tool_calls": [{"name": "list_files", "arguments": {}}]}])
    chunks = []
    first = provider.complete(
        system="S", messages=[Message.user("hi")], tools=TOOLS, max_tokens=10, on_text=chunks.append
    )
    assert first.stop_reason == "tool_use" and first.tool_calls[0].name == "list_files"
    assert len(chunks) > 1 and "".join(chunks) == "先看看 文件"
    assert first.usage.input_tokens > 0
    second = provider.complete(system="S", messages=[], tools=TOOLS, max_tokens=10)
    assert second.text == SCRIPT_EXHAUSTED and second.stop_reason == "end_turn"


def test_scripted_provider_compaction_does_not_consume_script():
    provider = ScriptedProvider([{"text": "脚本第一项"}])
    summary = provider.complete(
        system="【压缩任务】总结", messages=[Message.user("用户：修复 calc.py 的 bug")], tools=[], max_tokens=10
    )
    assert "修复 calc.py 的 bug" in summary.text and "calc.py" in summary.text
    assert provider.complete(system="S", messages=[], tools=TOOLS, max_tokens=10).text == "脚本第一项"


def test_fix_script_is_valid_json_with_text_before_tool_calls():
    from conftest import FIX_SCRIPT

    script = json.loads(FIX_SCRIPT.read_text(encoding="utf-8"))
    names = [c["name"] for item in script for c in item.get("tool_calls", [])]
    assert names == [
        "update_todos",
        "list_files",
        "read_file",
        "run_command",
        "edit_file",
        "run_command",
        "update_todos",
    ]
    assert all(item["text"] for item in script)


# ---- 与真实 SDK 的流式接口对接（MockTransport 返回手写的 SSE，不访问网络）----

try:
    import httpx2 as httpx  # anthropic / openai SDK 使用的 HTTP 客户端
except ImportError:  # SDK 换了底层 HTTP 库时跳过这几项，不影响其他测试
    httpx = None

needs_httpx = pytest.mark.skipif(httpx is None, reason="SDK 的 HTTP 客户端不是 httpx2")


def mock_http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def sse_response(lines: list[str]):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join(lines))


ANTHROPIC_STREAM = [
    {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "m",
     "content": [], "stop_reason": None, "stop_sequence": None,
     "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 5}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "读一下"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.py"}'}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 20}},
    {"type": "message_stop"},
]  # fmt: skip


@needs_httpx
def test_anthropic_provider_streams_with_sdk():
    import anthropic

    from minicode.providers.anthropic import AnthropicProvider

    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return sse_response([f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in ANTHROPIC_STREAM])

    provider = AnthropicProvider(model="claude-x", api_key="test")
    provider._client = anthropic.Anthropic(api_key="test", max_retries=0, http_client=mock_http(handler))
    chunks = []
    response = provider.complete(
        system="S", messages=[Message.user("hi")], tools=TOOLS, max_tokens=100, on_text=chunks.append
    )
    assert chunks == ["读一下"]
    assert response.tool_calls[0].arguments == {"path": "a.py"} and response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 15 and response.usage.output_tokens == 20
    assert sent["stream"] is True and sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}


@needs_httpx
def test_sdk_errors_are_classified():
    import anthropic

    from minicode.providers.anthropic import AnthropicProvider

    def rate_limited(request):
        error = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
        return httpx.Response(429, headers={"retry-after": "2"}, json=error)

    def unauthorized(request):
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "no"}})

    provider = AnthropicProvider(model="claude-x", api_key="test")
    for handler, expected in [(rate_limited, RetryableError), (unauthorized, ProviderError)]:
        provider._client = anthropic.Anthropic(api_key="test", max_retries=0, http_client=mock_http(handler))
        with pytest.raises(ProviderError) as info:
            provider.complete(system="S", messages=[Message.user("hi")], tools=[], max_tokens=10)
        assert type(info.value) is expected
    assert info.value.__cause__ is not None


@needs_httpx
def test_openai_provider_streams_with_sdk():
    import openai

    from minicode.providers.openai_compat import OpenAICompatProvider

    def chunk(delta, finish=None, usage=None):
        choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
        return {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "m",
            "choices": choices,
            "usage": usage,
        }

    chunks = [
        chunk({"role": "assistant", "content": "好的"}),
        chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                               "function": {"name": "read_file", "arguments": '{"pa'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}),
        chunk({}, finish="tool_calls"),
        chunk(None, usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}),
    ]  # fmt: skip
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return sse_response([f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"])

    provider = OpenAICompatProvider(model="gpt-x", api_key="test")
    provider._client = openai.OpenAI(api_key="test", max_retries=0, http_client=mock_http(handler))
    streamed = []
    response = provider.complete(
        system="S", messages=[Message.user("hi")], tools=TOOLS, max_tokens=100, on_text=streamed.append
    )
    assert streamed == ["好的"]
    assert response.tool_calls[0].arguments == {"path": "a.py"} and response.usage.input_tokens == 11
    assert sent["stream_options"] == {"include_usage": True} and sent["max_completion_tokens"] == 100


def test_openai_empty_assistant_reply_uses_empty_string_not_null():
    empty = Message(role="assistant", blocks=[])
    with_call = Message(role="assistant", blocks=[ToolCall(id="c", name="x", arguments={})])
    assert to_openai_messages("S", [empty])[1]["content"] == ""
    assert to_openai_messages("S", [with_call])[1]["content"] is None
