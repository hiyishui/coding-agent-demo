"""内置工具（设计文档第 8 节）。explore 子代理依赖 Agent，由 cli 单独注册。"""

from __future__ import annotations

from .base import Tool, ToolContext, ToolOutput, ToolRegistry
from .files import CREATE_FILE, EDIT_FILE, LIST_FILES, READ_FILE
from .search import SEARCH_CODE
from .shell import RUN_COMMAND
from .todos import UPDATE_TODOS

BUILTIN_TOOLS = [READ_FILE, LIST_FILES, SEARCH_CODE, EDIT_FILE, CREATE_FILE, RUN_COMMAND, UPDATE_TODOS]

__all__ = ["BUILTIN_TOOLS", "Tool", "ToolContext", "ToolOutput", "ToolRegistry", "builtin_registry"]


def builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    return registry
