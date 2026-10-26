"""玩具 MCP 服务器（stdio，协议版本 2026-07-28），零依赖，用于演示和测试 minicode 的 MCP 客户端。

实现的子集与 minicode 客户端一致：
- server/discover：返回 supportedVersions、capabilities、serverInfo 与缓存提示；
- tools/list：按 cursor 分页（每页 1 个工具，便于演示分页），带 ttlMs / cacheScope；
- tools/call：word_count(text)、utc_now()。

每个请求都必须在 params._meta 中带协议版本与客户端能力（规范要求），
否则返回 -32602；版本不受支持时返回 UnsupportedProtocolVersionError（-32022）。

运行：python examples/toy_mcp_server.py（从 stdin 读、向 stdout 写，每行一条 JSON-RPC 消息）
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

PROTOCOL_VERSION = "2026-07-28"
SERVER_INFO = {"name": "toy-mcp-server", "version": "0.1.0"}
PAGE_SIZE = 1

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
UNSUPPORTED_PROTOCOL_VERSION = -32022

TOOLS = [
    {
        "name": "word_count",
        "title": "Word Count",
        "description": "统计一段文本的单词数、字符数和行数。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要统计的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "utc_now",
        "title": "UTC Now",
        "description": "返回当前 UTC 时间（ISO 8601）。",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def complete(result: dict) -> dict:
    """所有结果都带 resultType，并在 _meta 中标明服务器身份。"""
    return {"resultType": "complete", **result, "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}


def check_meta(params: dict) -> None:
    meta = params.get("_meta") or {}
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    if version is None or "io.modelcontextprotocol/clientCapabilities" not in meta:
        raise RpcError(INVALID_PARAMS, "Missing required _meta fields (protocolVersion, clientCapabilities)")
    if version != PROTOCOL_VERSION:
        raise RpcError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": [PROTOCOL_VERSION], "requested": version},
        )


def discover(params: dict) -> dict:
    return complete(
        {
            "supportedVersions": [PROTOCOL_VERSION],
            "capabilities": {"tools": {}},
            "instructions": "A toy server with word_count and utc_now tools.",
            "ttlMs": 3_600_000,
            "cacheScope": "public",
        }
    )


def list_tools(params: dict) -> dict:
    start = int(params.get("cursor") or 0)
    page = TOOLS[start : start + PAGE_SIZE]
    result = {"tools": page, "ttlMs": 300_000, "cacheScope": "public"}
    if start + PAGE_SIZE < len(TOOLS):
        result["nextCursor"] = str(start + PAGE_SIZE)
    return complete(result)


def call_tool(params: dict) -> dict:
    name, arguments = params.get("name"), params.get("arguments") or {}
    if name == "word_count":
        text = arguments.get("text")
        if not isinstance(text, str):
            # 工具执行错误：放在结果里（isError），让模型可以自行修正参数
            return complete({"content": [{"type": "text", "text": "text must be a string"}], "isError": True})
        summary = f"words={len(text.split())} chars={len(text)} lines={len(text.splitlines())}"
        return complete({"content": [{"type": "text", "text": summary}], "isError": False})
    if name == "utc_now":
        now = datetime.now(UTC).isoformat(timespec="seconds")
        return complete({"content": [{"type": "text", "text": now}], "isError": False})
    # 协议错误：未知工具用 JSON-RPC 错误返回
    raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")


METHODS = {"server/discover": discover, "tools/list": list_tools, "tools/call": call_tool}


def handle(message: dict) -> dict | None:
    if "id" not in message:
        return None  # 通知（如 notifications/cancelled）不需要响应
    request_id = message["id"]
    try:
        method = message.get("method")
        if method == "initialize":
            raise RpcError(METHOD_NOT_FOUND, f"initialize is not supported; this server only speaks {PROTOCOL_VERSION}")
        if method not in METHODS:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        params = message.get("params") or {}
        check_meta(params)
        return {"jsonrpc": "2.0", "id": request_id, "result": METHODS[method](params)}
    except RpcError as err:
        error = {"code": err.code, "message": err.message}
        if err.data is not None:
            error["data"] = err.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def main() -> None:
    for line in sys.stdin:  # stdin 关闭即退出（规范中的正常关闭方式）
        if not line.strip():
            continue
        try:
            response = handle(json.loads(line))
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    print("toy-mcp-server: stdin closed, bye", file=sys.stderr)


if __name__ == "__main__":
    main()
