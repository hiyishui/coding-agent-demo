"""explore 子代理（设计文档第 11 节）。

大范围调查（“这个功能在哪些地方实现？”）会产生大量搜索结果，全部塞进主上下文既贵又干扰。
做法是开一个**独立的**只读 Agent 去调查：它有自己的上下文，只能用 read_file、list_files、
search_code，事件只写在内存里；主 Agent 只收到它最后的结论。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..agent import Agent, AgentConfig, AgentEvents
from ..events import SessionLog
from ..permissions import HeadlessApprover, PermissionPolicy
from ..providers.base import Provider
from .base import Tool, ToolContext, ToolOutput, ToolRegistry, object_schema

EXPLORE_TOOLS = ["read_file", "list_files", "search_code"]
EXPLORE_MAX_STEPS = 15

EXPLORE_SYSTEM_PROMPT = """你是一个只读的代码调查助手，为另一个编程 Agent 调查问题。
- 只调查，不修改：你只能使用 read_file、list_files、search_code。
- 先用 search_code / list_files 定位，再用 read_file 阅读关键片段，不要通读整个项目。
- 最后用不超过 300 字总结结论，并列出关键文件位置（路径:行号）。

工作区：{workspace}
"""


def make_explore_tool(
    provider: Provider, registry: ToolRegistry, config: AgentConfig, events: AgentEvents | None = None
) -> Tool:
    """创建 explore 工具。registry 是主 Agent 的注册表，子代理只取其中的只读工具。"""
    parent_events = events or AgentEvents()

    def explore(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        sub_agent = Agent(
            provider=provider,
            registry=registry.subset(EXPLORE_TOOLS),
            log=SessionLog.in_memory(),
            system_prompt=EXPLORE_SYSTEM_PROMPT.format(workspace=ctx.workspace),
            workspace=ctx.workspace,
            policy=PermissionPolicy(mode="plan"),
            approver=HeadlessApprover(),
            config=replace(config, max_steps=EXPLORE_MAX_STEPS),
        )
        parent_events.on_notice(f"explore 子代理开始调查：{args['task']}")
        result = sub_agent.run_turn(args["task"])
        if result.status == "interrupted":
            raise KeyboardInterrupt
        usage = sub_agent.state.usage
        if result.status != "done":
            return ToolOutput(f"子代理未能完成调查（{result.reason}）。", is_error=True, usage=usage)
        return ToolOutput(result.text or "（子代理没有给出结论）", usage=usage)

    return Tool(
        name="explore",
        description=(
            "启动一个只读的子代理调查问题，只返回它的结论（不超过 300 字，含关键文件位置）。"
            "适合需要大量搜索和阅读的开放式问题，例如“某功能在哪里实现、调用链是怎样的”；"
            "已知具体文件或只需一次搜索时，直接用 read_file / search_code 更快。"
        ),
        input_schema=object_schema(
            {"task": {"type": "string", "description": "要调查的问题，写清楚要回答什么"}}, ["task"]
        ),
        handler=explore,
        kind="meta",
    )
