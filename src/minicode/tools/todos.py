"""update_todos：任务清单（设计文档第 8、10 节）。

多步任务里，模型容易“做着做着忘了还剩什么”。让它显式维护一个清单：
每次整体替换，最多一个 in_progress。清单通过 todos_updated 事件写进会话日志，
由归约器独立保存——压缩上下文时不会丢。
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolOutput

STATUSES = ("pending", "in_progress", "done")
MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}


def render_todos(todos: list[dict[str, str]]) -> str:
    if not todos:
        return "（清单为空）"
    return "\n".join(f"{MARKS.get(t['status'], '[?]')} {t['content']}" for t in todos)


def update_todos(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    todos = args["todos"]
    cleaned: list[dict[str, str]] = []
    for index, item in enumerate(todos, start=1):
        if not isinstance(item, dict) or not str(item.get("content", "")).strip():
            return ToolOutput(f"第 {index} 项缺少 content。", True)
        if item.get("status") not in STATUSES:
            return ToolOutput(f"第 {index} 项的 status 必须是 {' / '.join(STATUSES)} 之一。", True)
        cleaned.append({"content": str(item["content"]).strip(), "status": item["status"]})
    if sum(t["status"] == "in_progress" for t in cleaned) > 1:
        return ToolOutput("最多只能有一项处于 in_progress，请一次专注一件事。", True)
    ctx.on_todos(cleaned)
    return ToolOutput("清单已更新：\n" + render_todos(cleaned))


UPDATE_TODOS = Tool(
    name="update_todos",
    description=(
        "维护当前任务的待办清单（整体替换）。多步任务开始时先列出步骤，"
        "推进过程中及时更新状态：pending / in_progress / done，同一时间最多一项 in_progress。"
        "简单的一步任务不需要使用。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "完整的清单",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": list(STATUSES)},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
    handler=update_todos,
    kind="meta",
)
