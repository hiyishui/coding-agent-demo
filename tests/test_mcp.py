"""MCP：启动玩具服务器子进程，完成 discover / list / call，并注册为 mcp__toy__* 工具。"""

from __future__ import annotations

import json
import sys

import pytest
from conftest import EXAMPLES, call, make_agent

from minicode.mcp_client import (
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    McpError,
    McpServer,
    connect_servers,
    tool_name,
)
from minicode.messages import ToolCall
from minicode.tools import builtin_registry
from minicode.tools.base import ToolContext

TOY_SERVER = str(EXAMPLES / "toy_mcp_server.py")


@pytest.fixture
def server():
    srv = McpServer("toy", sys.executable, [TOY_SERVER], timeout=10)
    yield srv
    srv.close()


def test_discover_list_and_call(server):
    result = server.discover()
    assert PROTOCOL_VERSION in result["supportedVersions"]
    assert result["resultType"] == "complete" and "tools" in result["capabilities"]
    assert server.server_info["name"] == "toy-mcp-server"

    tools = server.list_tools()  # 玩具服务器每页 1 个工具，覆盖分页
    assert [t["name"] for t in tools] == ["word_count", "utc_now"]

    output = server.call_tool("word_count", {"text": "hello mcp world"})
    assert not output.is_error and "words=3" in output.content
    assert "T" in server.call_tool("utc_now", {}).content


def test_errors_map_to_is_error(server):
    unknown = server.call_tool("no_such_tool", {})  # JSON-RPC 错误
    assert unknown.is_error and "Unknown tool" in unknown.content
    bad_args = server.call_tool("word_count", {"text": 123})  # 工具执行错误 isError=true
    assert bad_args.is_error and "must be a string" in bad_args.content


def test_unsupported_version_is_reported(server, monkeypatch):
    import minicode.mcp_client as mcp

    monkeypatch.setattr(mcp, "PROTOCOL_VERSION", "1900-01-01")
    with pytest.raises(McpError) as info:
        server.request("tools/list")
    assert info.value.code == -32022 and info.value.data["supported"] == [PROTOCOL_VERSION]
    with pytest.raises(McpError, match="不支持 1900-01-01"):
        server.discover()


def test_toy_server_requires_meta():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("toy_mcp_server", TOY_SERVER)
    toy = module_from_spec(spec)
    spec.loader.exec_module(toy)
    missing = toy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert missing["error"]["code"] == -32602
    ok = toy.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "server/discover",
            "params": {"_meta": {META_PROTOCOL_VERSION: PROTOCOL_VERSION, META_CLIENT_CAPABILITIES: {}}},
        }
    )
    assert ok["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "toy-mcp-server"
    assert ok["result"]["ttlMs"] >= 0 and ok["result"]["cacheScope"] == "public"
    assert toy.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None


def test_tools_registered_and_callable_through_agent(workspace):
    config = workspace / "mcp.json"
    config.write_text(json.dumps({"servers": {"toy": {"command": sys.executable, "args": [TOY_SERVER]}}}))
    notices = []
    servers, tools = connect_servers(config, cwd=workspace, on_notice=notices.append)
    try:
        assert [t.name for t in tools] == ["mcp__toy__word_count", "mcp__toy__utc_now"]
        assert all(t.kind == "mcp" and t.description.startswith("[MCP:toy]") for t in tools)

        registry = builtin_registry()
        for tool in tools:
            registry.register(tool)
        direct = registry.run(
            ToolCall(id="1", name="mcp__toy__word_count", arguments={"text": "a b"}), ToolContext(workspace)
        )
        assert direct.content.startswith("words=2")

        script = [
            {"text": "数一下。", "tool_calls": [call("mcp__toy__word_count", text="one two three")]},
            {"text": "3 个词。"},
        ]
        agent = make_agent(workspace, script, registry=registry, mode="yolo")
        agent.run_turn("数词")
        result = next(e for e in agent.log.events if e["type"] == "tool_result")
        assert not result["is_error"] and "words=3" in result["content"]

        denied = make_agent(workspace, script, registry=registry, mode="default")  # 无头 + default → 需要审批 → 拒绝
        denied.run_turn("数词")
        assert next(e for e in denied.log.events if e["type"] == "tool_result")["is_error"]
    finally:
        for srv in servers:
            srv.close()


def test_broken_server_is_skipped(workspace):
    config = workspace / "mcp.json"
    config.write_text(json.dumps({"servers": {"bad": {"command": sys.executable, "args": ["-c", "pass"]}}}))
    notices = []
    servers, tools = connect_servers(config, cwd=workspace, on_notice=notices.append)
    assert servers == [] and tools == []
    assert "bad" in notices[0] and "跳过" in notices[0]


def test_tool_name_sanitizing():
    assert tool_name("toy", "word_count") == "mcp__toy__word_count"
    assert tool_name("my server", "admin.tools.list") == "mcp__my_server__admin_tools_list"
