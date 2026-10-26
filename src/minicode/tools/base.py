"""工具抽象、注册表与统一截断（设计文档第 8 节）。

注册表是模型与真实世界之间的唯一通道：未知工具、参数错误、执行异常
都在这里变成“给模型看的错误说明”，而不是让 Agent 崩溃；
所有输出最后统一截断，防止一次大输出挤爆上下文。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..messages import ToolCall, ToolSpec
from ..permissions import resolve_in_workspace

ToolKind = Literal["read", "edit", "exec", "meta", "mcp"]

DEFAULT_MAX_OUTPUT_CHARS = 20_000


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False


@dataclass
class ToolContext:
    """工具执行时可用的环境：工作区、路径守卫、会话状态回调。"""

    workspace: Path
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    on_todos: Callable[[list[dict[str, str]]], None] = lambda todos: None

    def resolve(self, path: str) -> Path:
        return resolve_in_workspace(self.workspace, path)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.workspace.resolve()).as_posix() or "."


Handler = Callable[[dict[str, Any], ToolContext], ToolOutput]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    kind: ToolKind

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, input_schema=self.input_schema)


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def truncate(text: str, max_chars: int) -> str:
    """超长时保留头 70%、尾 30%，中间写明省略了多少以及如何缩小范围。"""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    omitted = len(text) - head - tail
    note = f"\n…已省略 {omitted} 个字符；可用 offset/limit 或更精确的 pattern 缩小范围…\n"
    return text[:head] + note + text[-tail:]


_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """轻量校验：必填项与顶层属性的类型。返回错误说明，通过时返回 None。"""
    if "_raw" in arguments:
        return f"参数不是合法的 JSON 对象：{str(arguments['_raw'])[:200]}"
    missing = [name for name in schema.get("required", []) if name not in arguments]
    if missing:
        return f"缺少必填参数：{', '.join(missing)}"
    for name, value in arguments.items():
        expected = schema.get("properties", {}).get(name, {}).get("type")
        check = _JSON_TYPES.get(expected) if isinstance(expected, str) else None
        if check and not check(value):
            return f"参数 {name} 应为 {expected} 类型，实际是 {type(value).__name__}"
    return None


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools.values()]

    def subset(self, names: list[str]) -> ToolRegistry:
        return ToolRegistry({name: self.tools[name] for name in names if name in self.tools})

    def run(self, call: ToolCall, ctx: ToolContext) -> ToolOutput:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolOutput(f"未知工具：{call.name}。可用工具：{', '.join(self.tools)}", is_error=True)
        error = validate_arguments(tool.input_schema, call.arguments)
        if error:
            return ToolOutput(f"参数错误：{error}", is_error=True)
        try:
            output = tool.handler(call.arguments, ctx)
        except PermissionError as err:
            output = ToolOutput(str(err), is_error=True)
        except Exception as err:  # 工具异常不能打断 Agent 循环，转成错误结果交给模型
            output = ToolOutput(f"工具执行出错：{type(err).__name__}: {err}", is_error=True)
        return ToolOutput(truncate(output.content, ctx.max_output_chars), output.is_error)
