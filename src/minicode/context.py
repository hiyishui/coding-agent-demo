"""上下文工程（设计文档第 10 节）：系统提示词、AGENTS.md、token 估算、自动压缩。

- 系统提示词在会话内保持不变（todo 等动态信息不放进去），利于提示词缓存；
- 项目约定写在仓库根目录的 AGENTS.md 里，原样附在系统提示词末尾；
- 上下文快满时，把较早的对话交给模型按固定模板总结，只保留最近的尾部。
"""

from __future__ import annotations

import json
import platform
from datetime import date
from pathlib import Path

from .events import Event, reduce
from .messages import Message, TextBlock, ToolCall, ToolResult

AGENTS_MD = "AGENTS.md"
AGENTS_MD_LIMIT = 8000
COMPACTION_THRESHOLD = 0.75
KEEP_USER_MESSAGES = 4
COMPACT_MARKER = "【压缩任务】"
TRANSCRIPT_RESULT_LIMIT = 2000

SYSTEM_PROMPT = """你是 minicode，一个在终端里工作的编程 Agent。
你通过工具读代码、搜代码、改代码、跑命令，帮助用户完成软件工程任务。

# 工作方式
1. 先探索再修改：用 search_code、list_files 定位相关代码，用 read_file 阅读需要的片段；不要凭猜测修改代码。
2. 修改后验证：运行项目的测试或相关命令确认修改有效；失败时先读报错，再有针对性地修正。
3. 多步任务先用 update_todos 列出步骤，并随进展更新状态。
4. 需要大范围调查（例如“某个功能在哪些地方实现”）时，交给 explore 子代理，它只返回结论。
5. 需求不明确或操作有风险时，先向用户提问，不要自作主张。
6. 工具调用被拒绝时，阅读拒绝原因，改用被允许的做法，或向用户说明需要什么。
7. 修改保持最小、聚焦，遵循项目已有的风格；完成后简要说明改了什么、如何验证的。

# 环境
- 工作区：{workspace}
- 操作系统：{os_name}
- 日期：{today}
- 初始运行模式：{mode}
  （plan 只读；default 修改与执行命令需审批；auto-edit 自动允许修改文件；yolo 自动允许大多数命令）。
  用户可能在会话中切换模式，以工具返回的权限提示为准。
"""

COMPACT_SYSTEM = (
    COMPACT_MARKER
    + """你在为一个编程 Agent 压缩对话历史。请阅读用户给出的对话记录，按下面的模板输出摘要，
保留继续工作所必需的全部信息（文件路径、函数名、命令、报错要点），省略寒暄和重复内容：

任务目标：
已完成：
修改过的文件：
关键决策与发现：
未解决问题：
下一步：
"""
)


def load_agents_md(workspace: Path) -> str | None:
    path = workspace / AGENTS_MD
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > AGENTS_MD_LIMIT:
        text = text[:AGENTS_MD_LIMIT] + f"\n…（{AGENTS_MD} 超过 {AGENTS_MD_LIMIT} 字符，其余部分已省略）"
    return text


def build_system_prompt(workspace: Path, mode: str, *, today: date | None = None) -> str:
    prompt = SYSTEM_PROMPT.format(
        workspace=workspace,
        os_name=f"{platform.system()} {platform.release()}",
        today=(today or date.today()).isoformat(),
        mode=mode,
    )
    agents_md = load_agents_md(workspace)
    if agents_md:
        prompt += f"\n# 项目指令（来自 {AGENTS_MD}）\n\n{agents_md}\n"
    return prompt


# ---- token 估算 ----


def estimate_context_tokens(events: list[Event]) -> int:
    """上一次响应的 input_tokens 是最准确的基线，之后新增的内容按“字符数 / 4”粗估。"""
    state = reduce(events)
    return state.last_input_tokens + state.chars_since_last_call // 4


def needs_compaction(events: list[Event], context_window: int) -> bool:
    return estimate_context_tokens(events) > context_window * COMPACTION_THRESHOLD


# ---- 自动压缩 ----

_MESSAGE_EVENTS = ("user_message", "assistant_message", "tool_result")


def find_compaction_cut(events: list[Event], keep_user_messages: int = KEEP_USER_MESSAGES) -> int | None:
    """选择保留尾部的起点（事件下标）；没有可压缩的内容时返回 None。

    尾部从倒数第 keep_user_messages 条 user 消息开始。切分点只能落在
    “真实用户输入”或 assistant 消息之前——若那条 user 消息是 tool_result，
    就把切分点前移到发出这些调用的 assistant 消息，保证 tool_use 与 tool_result 不被拆开。
    """
    compactions = [e for e in events if e["type"] == "compaction"]
    window_start = compactions[-1]["upto"] + 1 if compactions else 0

    user_starts: list[int] = []  # 每条 user 消息对应的第一个事件
    last_assistant: dict[int, int] = {}  # user 消息起点 → 它之前最近的 assistant_message
    previous_kind, assistant_index = None, -1
    for index in range(window_start, len(events)):
        kind = events[index]["type"]
        if kind not in _MESSAGE_EVENTS:
            continue
        if kind == "assistant_message":
            assistant_index = index
        elif kind == "user_message" or previous_kind == "assistant_message":
            user_starts.append(index)
            last_assistant[index] = assistant_index
        previous_kind = kind

    for keep in range(min(keep_user_messages, len(user_starts)), 0, -1):
        cut = user_starts[-keep]
        if events[cut]["type"] == "tool_result":
            cut = last_assistant[cut]
        if any(e["type"] in _MESSAGE_EVENTS for e in events[window_start:cut]):
            return cut
    return None


def render_transcript(messages: list[Message]) -> str:
    """把消息渲染成纯文本记录，交给模型总结（不带工具定义，避免协议约束）。"""
    lines = []
    for message in messages:
        for block in message.blocks:
            if isinstance(block, TextBlock):
                speaker = "用户" if message.role == "user" else "助手"
                lines.append(f"{speaker}：{block.text}")
            elif isinstance(block, ToolCall):
                lines.append(f"助手调用工具 {block.name}({json.dumps(block.arguments, ensure_ascii=False)})")
            elif isinstance(block, ToolResult):
                content = block.content[:TRANSCRIPT_RESULT_LIMIT]
                lines.append(f"工具结果{'（错误）' if block.is_error else ''}：{content}")
    return "\n".join(lines)


def messages_to_summarize(events: list[Event], cut: int) -> list[Message]:
    """切分点之前的对话。若已有压缩，摘要作为开头（reduce 只看最后一次压缩，与其位置无关）。"""
    head = events[:cut]
    compactions = [e for e in events if e["type"] == "compaction"]
    if compactions and compactions[-1] not in head:
        head = head + [compactions[-1]]
    return reduce(head).messages


def compaction_request(messages: list[Message]) -> tuple[str, list[Message]]:
    """返回（系统提示词, 消息列表），由调用方发给模型。"""
    transcript = render_transcript(messages)
    return COMPACT_SYSTEM, [Message.user("以下是需要总结的对话记录：\n\n" + transcript)]
