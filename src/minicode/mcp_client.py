"""最小 stdio MCP 客户端（设计文档第 12 节），只实现 2026-07-28 版规范中与工具相关的子集。

规范要点（https://modelcontextprotocol.io/specification/2026-07-28/）：
- 无状态：没有 initialize 握手，每个请求都在 params._meta 中携带
  `io.modelcontextprotocol/protocolVersion`、`io.modelcontextprotocol/clientCapabilities`（必填）
  和 `io.modelcontextprotocol/clientInfo`（建议）；
- 启动后先调用 `server/discover`，确认 supportedVersions 包含 2026-07-28；
- `tools/list` 用 cursor / nextCursor 分页；`tools/call` 返回 content 与 isError；
- 所有结果都带 resultType："complete" 是普通结果，"input_required" 需要多轮往返（本客户端不支持）；
- stdio 传输：每行一条 JSON-RPC 消息；stderr 只是日志；关闭 stdin 是正常的关闭信号。
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .tools.base import Tool, ToolContext, ToolOutput

PROTOCOL_VERSION = "2026-07-28"
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
UNSUPPORTED_PROTOCOL_VERSION = -32022

REQUEST_TIMEOUT_SEC = 60.0
CLIENT_INFO = {"name": "minicode", "version": __version__}


class McpError(Exception):
    """MCP 调用失败：JSON-RPC 错误、超时、进程退出或不支持的结果类型。"""

    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class McpServer:
    """一个通过 stdio 连接的 MCP 服务器子进程。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        cwd: Path | None = None,
        timeout: float = REQUEST_TIMEOUT_SEC,
        on_stderr: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.server_info: dict[str, Any] = {}
        self._ids = itertools.count(1)
        self._inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._on_stderr = on_stderr or (lambda line: None)
        self._process = subprocess.Popen(
            [command, *(args or [])],
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # ---- 传输层 ----

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            if not line.strip():
                continue
            try:
                self._inbox.put(json.loads(line))
            except json.JSONDecodeError:
                self._on_stderr(f"[{self.name}] stdout 中出现非 JSON 内容：{line.strip()[:200]}")
        self._inbox.put(None)  # 进程的 stdout 关闭了

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._on_stderr(f"[{self.name}] {line.rstrip()}")

    def _send(self, message: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as err:
            raise McpError(f"MCP 服务器 {self.name} 已退出") from err

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送一个请求并等待对应 id 的响应，返回 result。"""
        request_id = next(self._ids)
        meta = {
            META_PROTOCOL_VERSION: PROTOCOL_VERSION,
            META_CLIENT_INFO: CLIENT_INFO,
            META_CLIENT_CAPABILITIES: {},  # 不支持 sampling / elicitation / roots
        }
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": {**(params or {}), "_meta": meta}})
        while True:
            try:
                message = self._inbox.get(timeout=self.timeout)
            except queue.Empty:
                # 规范要求：超时后用 notifications/cancelled 通知服务器放弃这个请求
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": request_id, "reason": "timeout"},
                    }
                )
                raise McpError(f"{method} 超时（{self.timeout:.0f} 秒）") from None
            if message is None:
                raise McpError(f"MCP 服务器 {self.name} 已退出")
            if message.get("id") != request_id:
                continue  # 通知或过期响应，忽略
            if "error" in message:
                error = message["error"]
                raise McpError(error.get("message", "未知错误"), error.get("code"), error.get("data"))
            result = message.get("result") or {}
            result_type = result.get("resultType", "complete")  # 缺省视为 complete（兼容旧服务器）
            if result_type != "complete":
                raise McpError(f"不支持的 resultType：{result_type}（minicode 不支持多轮往返请求）")
            return result

    # ---- 协议操作 ----

    def discover(self) -> dict[str, Any]:
        try:
            result = self.request("server/discover")
        except McpError as err:
            if err.code == UNSUPPORTED_PROTOCOL_VERSION:
                supported = (err.data or {}).get("supported", [])
                raise McpError(f"服务器不支持 {PROTOCOL_VERSION}（支持：{supported}）") from err
            raise
        versions = result.get("supportedVersions", [])
        if PROTOCOL_VERSION not in versions:
            raise McpError(f"服务器不支持 {PROTOCOL_VERSION}（支持：{versions}）")
        self.server_info = (result.get("_meta") or {}).get(META_SERVER_INFO, {})
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        try:
            result = self.request("tools/call", {"name": name, "arguments": arguments})
        except McpError as err:
            return ToolOutput(f"MCP 错误：{err}", is_error=True)
        texts = [item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"]
        content = "\n".join(texts)
        if not texts and "structuredContent" in result:
            content = json.dumps(result["structuredContent"], ensure_ascii=False)
        return ToolOutput(content or "（无文本输出）", is_error=bool(result.get("isError", False)))

    def close(self) -> None:
        """按规范关闭：先关 stdin，等待退出，超时再 terminate / kill。"""
        if self._process.poll() is not None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()


def tool_name(server: str, tool: str) -> str:
    """mcp__<server>__<tool>；把模型 API 不接受的字符替换为下划线。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool}")[:128]


def make_mcp_tool(server: McpServer, spec: dict[str, Any]) -> Tool:
    remote_name = spec["name"]

    def handler(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return server.call_tool(remote_name, args)

    return Tool(
        name=tool_name(server.name, remote_name),
        description=f"[MCP:{server.name}] {spec.get('description', '')}".strip(),
        input_schema=spec.get("inputSchema") or {"type": "object"},
        handler=handler,
        kind="mcp",
    )


def load_mcp_config(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("servers", {})


def connect_servers(
    config_path: Path, *, cwd: Path, on_notice: Callable[[str], None], on_stderr: Callable[[str], None] | None = None
) -> tuple[list[McpServer], list[Tool]]:
    """启动配置中的所有服务器并收集工具；某个服务器失败时报告并跳过，不影响其他服务器。"""
    servers: list[McpServer] = []
    tools: list[Tool] = []
    for name, entry in load_mcp_config(config_path).items():
        server = None
        try:
            server = McpServer(
                name, entry["command"], entry.get("args", []), entry.get("env"), cwd=cwd, on_stderr=on_stderr
            )
            server.discover()
            specs = server.list_tools()
        except (McpError, OSError, KeyError) as err:
            on_notice(f"MCP 服务器 {name} 不可用，已跳过：{err}")
            if server:
                server.close()
            continue
        servers.append(server)
        tools.extend(make_mcp_tool(server, spec) for spec in specs)
        on_notice(f"MCP 服务器 {name} 已连接，提供 {len(specs)} 个工具。")
    return servers, tools
