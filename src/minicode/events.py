"""事件日志与归约器（设计文档第 6 节）。

会话 = 只追加的事件列表，每行一个 JSON，存在 `<workspace>/.minicode/sessions/<id>.jsonl`。
任何时刻的会话状态都由纯函数 `reduce(events)` 从事件重建：
- 进程被杀、断电后，从日志就能恢复（`--resume`）；
- 审批、工具结果、压缩都有记录，便于审计和复盘；
- 状态只有一个来源，不会出现“内存里的消息”和“日志”不一致。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .messages import Message, TextBlock, ToolResult, Usage

Event = dict[str, Any]

SUMMARY_PREFIX = "【此前对话的摘要】"
DANGLING_CALL_RESULT = "操作已中断，未执行"


def make_event(type_: str, **fields: Any) -> Event:
    return {"type": type_, "ts": datetime.now().isoformat(timespec="seconds"), **fields}


@dataclass
class SessionState:
    messages: list[Message] = field(default_factory=list)
    todos: list[dict[str, str]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    steps: int = 0  # assistant_message 的数量
    consecutive_failures: int = 0  # 连续出错的工具结果数（审批拒绝不计入）
    last_input_tokens: int = 0  # 最近一次模型调用的 input_tokens（压缩后清零）
    chars_since_last_call: int = 0  # 最近一次模型调用之后新增内容的字符数


class _MessageBuilder:
    """把事件逐条翻译成中立消息，并维护“每个工具调用都有结果”的不变式。"""

    def __init__(self, initial: list[Message]) -> None:
        self.messages = list(initial)
        self.pending: list[str] = []  # 已发出、尚无结果的工具调用 id

    def add_user(self, blocks: list[TextBlock | ToolResult]) -> int:
        # 相邻的 user 内容合并成一条消息（例如多个 tool_result、摘要后紧跟用户输入）
        if self.messages and self.messages[-1].role == "user":
            self.messages[-1].blocks.extend(blocks)
        else:
            self.messages.append(Message(role="user", blocks=list(blocks)))
        return sum(len(b.text if isinstance(b, TextBlock) else b.content) for b in blocks)

    def add_assistant(self, message: Message) -> None:
        self.close_dangling()
        self.messages.append(message)
        self.pending = [call.id for call in message.tool_calls]

    def add_result(self, result: ToolResult) -> int | None:
        if result.call_id not in self.pending:
            return None  # 找不到对应调用的结果，丢弃以保持配对
        self.pending.remove(result.call_id)
        return self.add_user([result])

    def close_dangling(self) -> None:
        if self.pending:
            self.add_user(
                [ToolResult(call_id=cid, content=DANGLING_CALL_RESULT, is_error=True) for cid in self.pending]
            )
            self.pending = []


def reduce(events: list[Event]) -> SessionState:
    """从事件列表重建会话状态（纯函数）。

    消息只从最后一次压缩的 upto 之后开始重建，开头放一条摘要；
    todos、用量、步数等统计则来自全部事件，不受压缩影响。
    """
    state = SessionState()
    compactions = [i for i, e in enumerate(events) if e["type"] == "compaction"]
    last_compaction = compactions[-1] if compactions else -1
    window_start = 0
    builder = _MessageBuilder([])
    if last_compaction >= 0:
        compaction = events[last_compaction]
        window_start = compaction["upto"] + 1
        builder = _MessageBuilder([Message.user(SUMMARY_PREFIX + compaction["summary"])])
    denied: set[str] = set()

    for index, event in enumerate(events):
        kind = event["type"]
        in_window = index >= window_start
        if kind == "user_message":
            state.consecutive_failures = 0
            if in_window:
                builder.close_dangling()
                state.chars_since_last_call += builder.add_user([TextBlock(text=event["text"])])
        elif kind == "assistant_message":
            message = Message(role="assistant", blocks=event["blocks"])
            usage = Usage(**event.get("usage", {}))
            state.usage = state.usage + usage
            state.steps += 1
            state.last_input_tokens = usage.input_tokens
            state.chars_since_last_call = len(message.model_dump_json())
            if in_window:
                builder.add_assistant(message)
        elif kind == "approval" and event["decision"] == "deny":
            denied.add(event["call_id"])
        elif kind == "tool_result":
            if not event["is_error"]:
                state.consecutive_failures = 0
            elif event["call_id"] not in denied:
                state.consecutive_failures += 1
            if in_window:
                result = ToolResult(call_id=event["call_id"], content=event["content"], is_error=event["is_error"])
                state.chars_since_last_call += builder.add_result(result) or 0
        elif kind == "todos_updated":
            state.todos = event["todos"]
        elif kind == "compaction" and index == last_compaction:
            # 压缩后旧的 usage 不再代表上下文大小，改为按字符数重新估算
            state.last_input_tokens = 0
            state.chars_since_last_call = sum(len(m.model_dump_json()) for m in builder.messages)

    builder.close_dangling()
    state.messages = builder.messages
    return state


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


class SessionLog:
    """只追加的事件日志。path 为 None 时只存在内存里（explore 子代理使用）。"""

    def __init__(self, session_id: str, events: list[Event] | None = None, path: Path | None = None) -> None:
        self.session_id = session_id
        self.events: list[Event] = events or []
        self.path = path

    @staticmethod
    def sessions_dir(workspace: Path) -> Path:
        return workspace / ".minicode" / "sessions"

    @classmethod
    def create(cls, workspace: Path, meta: dict[str, Any]) -> SessionLog:
        session_id = new_session_id()
        path = cls.sessions_dir(workspace) / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        log = cls(session_id, path=path)
        log.append(make_event("session_start", session_id=session_id, workspace=str(workspace), **meta))
        return log

    @classmethod
    def in_memory(cls) -> SessionLog:
        return cls("memory-" + secrets.token_hex(2))

    @classmethod
    def load(cls, workspace: Path, session_id: str) -> SessionLog:
        path = cls.sessions_dir(workspace) / f"{session_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"找不到会话 {session_id}（{path}）")
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    break  # 进程在写最后一行时被杀：丢弃残缺行及之后的内容
        return cls(session_id, events=events, path=path)

    def append(self, event: Event) -> Event:
        self.events.append(event)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
        return event
