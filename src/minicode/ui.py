"""终端界面（设计文档第 13 节）：用 rich 渲染 Agent 的过程，并实现交互式审批。

界面只通过 AgentEvents 回调和 Approver 接口与 Agent 交互；
无头模式复用同一个渲染器，只是把输出写到 stderr。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from rich.console import Console
from rich.markup import escape

from .agent import AgentEvents
from .messages import ToolCall
from .permissions import ApprovalAnswer, Decision
from .tools.base import ToolOutput
from .tools.todos import render_todos

KEY_ARGUMENTS = ("path", "command", "pattern", "task")


def summarize_call(call: ToolCall) -> str:
    """工具调用的一行摘要：工具名 + 关键参数。"""
    args = call.arguments
    if call.name == "update_todos" and isinstance(args.get("todos"), list):
        detail = f"{len(args['todos'])} 项"
    else:
        key = next((k for k in KEY_ARGUMENTS if k in args), None)
        detail = str(args[key]) if key else json.dumps(args, ensure_ascii=False)
    detail = detail.replace("\n", " ")
    return f"{call.name}({detail[:100]}{'…' if len(detail) > 100 else ''})"


class ConsoleUI(AgentEvents):
    def __init__(self, console: Console) -> None:
        self.console = console
        self._streaming = False

    def _end_stream(self) -> None:
        if self._streaming:
            self.console.print()
            self._streaming = False

    def on_text(self, text: str) -> None:
        self.console.print(text, end="", markup=False, highlight=False, soft_wrap=True)
        self._streaming = True

    def on_tool_call(self, call: ToolCall) -> None:
        self._end_stream()
        self.console.print(f"[cyan]●[/] {escape(summarize_call(call))}", highlight=False)

    def on_tool_result(self, call: ToolCall, output: ToolOutput) -> None:
        lines = output.content.strip().splitlines() or ["（空）"]
        more = f" [dim]…（共 {len(lines)} 行）[/]" if len(lines) > 1 else ""
        style = "red" if output.is_error else "dim"
        self.console.print(f"  [{style}]⎿ {escape(lines[0][:160])}[/]{more}", highlight=False)

    def on_todos(self, todos: list[dict[str, str]]) -> None:
        self._end_stream()
        self.print_todos(todos)

    def on_notice(self, message: str) -> None:
        self._end_stream()
        self.console.print(f"[yellow]{escape(message)}[/]", highlight=False)

    def print_todos(self, todos: list[dict[str, str]]) -> None:
        self.console.print("[bold]待办清单[/]")
        for line in render_todos(todos).splitlines():
            self.console.print(f"  {escape(line)}", highlight=False)

    def finish_turn(self) -> None:
        self._end_stream()


class InteractiveApprover:
    """在终端里询问用户：y 允许一次 / n 拒绝（可附理由）/ a 本会话总是允许。"""

    name = "user"

    def __init__(self, ui: ConsoleUI, read_line: Callable[[str], str] = input) -> None:
        self.ui = ui
        self.read_line = read_line

    def ask(self, call: ToolCall, decision: Decision) -> ApprovalAnswer:
        console = self.ui.console
        console.print(f"[bold magenta]需要审批[/]（{escape(decision.reason)}）：", highlight=False)
        if call.name == "run_command":
            console.print(f"  $ {escape(str(call.arguments.get('command', '')))}", highlight=False)
        elif call.name == "edit_file":
            console.print(f"  {escape(str(call.arguments.get('path', '')))}", highlight=False)
            for line in str(call.arguments.get("old_str", "")).splitlines():
                console.print(f"[red]  - {escape(line)}[/]", highlight=False)
            for line in str(call.arguments.get("new_str", "")).splitlines():
                console.print(f"[green]  + {escape(line)}[/]", highlight=False)
        else:
            console.print(f"  {escape(summarize_call(call))}", highlight=False)
        can_remember = decision.column != "network"
        options = "[y] 允许一次  [n] 拒绝" + ("  [a] 本会话总是允许" if can_remember else "")
        while True:
            try:
                choice = self.read_line(f"{options} > ").strip().lower()
            except EOFError:  # 输入已关闭（例如管道输入耗尽）：按拒绝处理
                return ApprovalAnswer(allow=False, reason="用户没有作出选择")
            if choice in ("y", "yes"):
                return ApprovalAnswer(allow=True)
            if choice == "a" and can_remember:
                return ApprovalAnswer(allow=True, remember=True)
            if choice in ("n", "no"):
                try:
                    reason = self.read_line("拒绝理由（可留空，会转告模型）> ").strip()
                except EOFError:
                    reason = ""
                return ApprovalAnswer(allow=False, reason=reason)
