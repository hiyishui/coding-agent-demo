"""Agent 循环（设计文档第 7 节）。

    记录用户输入
    循环：
        从事件日志归约出当前状态
        检查预算（步数 / token / 费用）→ 超出则暂停
        上下文快满 → 压缩后继续
        带重试地调用模型，记录响应
        没有工具调用 → 本轮结束
        逐个工具调用：权限决策 →（需要时）人工审批 → 执行 → 记录结果
        连续失败过多 → 暂停，请用户介入

循环本身不关心界面：所有输出通过 AgentEvents 回调通知 UI，
审批通过 Approver 接口完成，因此同一套循环既能跑交互式 REPL，也能跑无头模式和测试。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import estimate_cost
from .context import compaction_request, find_compaction_cut, messages_to_summarize, needs_compaction
from .events import SessionLog, SessionState, make_event, reduce
from .messages import ModelResponse, ToolCall
from .permissions import Approver, Decision, HeadlessApprover, PermissionPolicy
from .providers.base import Provider, ProviderError, RetryableError
from .tools.base import DEFAULT_MAX_OUTPUT_CHARS, ToolContext, ToolOutput, ToolRegistry

MAX_CONSECUTIVE_FAILURES = 3
CANCELLED_RESULT = "用户已取消，未执行"


class AgentEvents:
    """Agent → UI 的通知接口。默认什么都不做，UI 按需覆盖。"""

    def on_text(self, text: str) -> None: ...

    def on_tool_call(self, call: ToolCall) -> None: ...

    def on_tool_result(self, call: ToolCall, output: ToolOutput) -> None: ...

    def on_todos(self, todos: list[dict[str, str]]) -> None: ...

    def on_notice(self, message: str) -> None: ...


@dataclass
class AgentConfig:
    model: str = "mock"
    context_window: int = 200_000
    max_steps: int = 50  # 每轮（一次 run_turn）最多调用模型的次数
    max_tokens_total: int | None = None  # 整个会话累计 token 上限
    max_cost_usd: float | None = None  # 整个会话累计费用上限（按 config 中的单价估算）
    max_output_tokens: int = 8192
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS


@dataclass
class RetryPolicy:
    """有界指数退避：delay = min(上限, base × 2^attempt + 抖动)，优先服从服务端的 retry_after。"""

    base_delay: float = 1.0
    max_retries: int = 4
    max_delay: float = 30.0
    rng: random.Random = field(default_factory=random.Random)

    def delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(self.max_delay, retry_after)
        return min(self.max_delay, self.base_delay * 2**attempt + self.rng.uniform(0, self.base_delay))


@dataclass
class TurnResult:
    status: Literal["done", "paused", "interrupted", "error"]
    text: str = ""
    reason: str = ""


class Agent:
    def __init__(
        self,
        *,
        provider: Provider,
        registry: ToolRegistry,
        log: SessionLog,
        system_prompt: str,
        workspace: Path,
        policy: PermissionPolicy | None = None,
        approver: Approver | None = None,
        events: AgentEvents | None = None,
        config: AgentConfig | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.log = log
        self.system_prompt = system_prompt
        self.workspace = workspace
        self.policy = policy or PermissionPolicy()
        self.approver = approver or HeadlessApprover()
        self.events = events or AgentEvents()
        self.config = config or AgentConfig()
        self.retry = retry or RetryPolicy()
        self.sleep = sleep

    @property
    def state(self) -> SessionState:
        return reduce(self.log.events)

    # ---- 主循环 ----

    def run_turn(self, user_text: str) -> TurnResult:
        self._record("user_message", text=user_text)
        steps_at_start = self.state.steps
        just_compacted = False
        while True:
            state = self.state
            paused = self._check_budget(state, steps_at_start)
            if paused:
                return paused
            if not just_compacted and needs_compaction(self.log.events, self.config.context_window):
                just_compacted = True  # 每次调用模型前最多压缩一次，避免压缩无效时死循环
                if self.compact():
                    continue
            just_compacted = False

            try:
                response = self._call_model(state)
            except KeyboardInterrupt:
                return self._interrupted("用户在模型输出时中断")
            except ProviderError as err:
                self.events.on_notice(f"模型调用失败：{err}")
                return TurnResult("error", reason=str(err))
            self._record(
                "assistant_message",
                blocks=[block.model_dump() for block in response.blocks],
                stop_reason=response.stop_reason,
                usage=response.usage.model_dump(),
            )

            if response.stop_reason != "tool_use" or not response.tool_calls:
                if response.stop_reason == "max_tokens":
                    self.events.on_notice("模型输出达到 max_tokens 上限，回答可能不完整。")
                return TurnResult("done", text=response.text)

            pending = list(response.tool_calls)
            try:
                while pending:
                    self._execute(pending[0])
                    pending.pop(0)
            except KeyboardInterrupt:
                for call in pending:  # 补齐结果，保持每个 tool_use 都有 tool_result
                    self._record("tool_result", call_id=call.id, content=CANCELLED_RESULT, is_error=True)
                return self._interrupted("用户在工具执行时中断")

            if self.state.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                return self._pause(
                    "consecutive_failures",
                    f"工具已连续失败 {MAX_CONSECUTIVE_FAILURES} 次，已暂停。请检查情况并给出进一步指示。",
                )

    def _check_budget(self, state: SessionState, steps_at_start: int) -> TurnResult | None:
        steps = state.steps - steps_at_start
        if steps >= self.config.max_steps:
            return self._pause("max_steps", f"本轮已调用模型 {steps} 次，达到上限 max_steps={self.config.max_steps}。")
        limit = self.config.max_tokens_total
        if limit is not None and state.usage.total_tokens >= limit:
            return self._pause("budget", f"会话累计 {state.usage.total_tokens} tokens，达到上限 {limit}。")
        cost = estimate_cost(self.config.model, state.usage)
        if self.config.max_cost_usd is not None and cost is not None and cost >= self.config.max_cost_usd:
            return self._pause("budget", f"会话估算费用 ${cost:.4f}，达到上限 ${self.config.max_cost_usd}。")
        return None

    # ---- 调用模型（带重试）----

    def _call_model(self, state: SessionState) -> ModelResponse:
        attempt = 0
        while True:
            try:
                return self.provider.complete(
                    system=self.system_prompt,
                    messages=state.messages,
                    tools=self.registry.specs(),  # 工具定义始终全部发送，模式只在执行时生效
                    max_tokens=self.config.max_output_tokens,
                    on_text=self.events.on_text,
                )
            except RetryableError as err:
                if attempt >= self.retry.max_retries:
                    raise
                delay = self.retry.delay(attempt, err.retry_after)
                attempt += 1
                self.events.on_notice(f"模型调用失败（{err}），{delay:.1f} 秒后第 {attempt} 次重试…")
                self.sleep(delay)

    # ---- 执行工具：决策 → 审批 → 执行 → 记录 ----

    def _execute(self, call: ToolCall) -> None:
        self.events.on_tool_call(call)
        tool = self.registry.get(call.name)
        if tool is None:  # 未知工具不需要审批，直接由注册表返回错误说明
            output = self.registry.run(call, self._tool_context())
        else:
            decision, by = self._approve(tool.kind, call)
            self._record(
                "approval", call_id=call.id, tool=call.name, decision=decision.action, by=by, reason=decision.reason
            )
            if decision.action == "allow":
                output = self.registry.run(call, self._tool_context())
            else:
                output = ToolOutput(_denial_message(decision, by), is_error=True)
        self._record("tool_result", call_id=call.id, content=output.content, is_error=output.is_error)
        self.events.on_tool_result(call, output)

    def _approve(self, kind: str, call: ToolCall) -> tuple[Decision, str]:
        decision = self.policy.decide(kind, call)
        if decision.action != "ask":
            return decision, "policy"
        answer = self.approver.ask(call, decision)
        if answer.allow and answer.remember and not self.policy.remember(decision, call):
            self.events.on_notice("联网命令不能设为“总是允许”，仅本次允许。")
        action = "allow" if answer.allow else "deny"
        return Decision(action, answer.reason, decision.column), self.approver.name

    def _tool_context(self) -> ToolContext:
        def on_todos(todos: list[dict[str, str]]) -> None:
            self._record("todos_updated", todos=todos)
            self.events.on_todos(todos)

        return ToolContext(workspace=self.workspace, max_output_chars=self.config.max_output_chars, on_todos=on_todos)

    # ---- 压缩 ----

    def compact(self) -> bool:
        """把较早的对话替换为摘要。没有可压缩的内容时返回 False。"""
        cut = find_compaction_cut(self.log.events)
        if cut is None:
            self.events.on_notice("没有可以压缩的历史。")
            return False
        system, messages = compaction_request(messages_to_summarize(self.log.events, cut))
        try:
            response = self.provider.complete(
                system=system, messages=messages, tools=[], max_tokens=self.config.max_output_tokens
            )
        except ProviderError as err:
            self.events.on_notice(f"压缩失败：{err}")
            return False
        self._record("compaction", summary=response.text, upto=cut - 1)
        self.events.on_notice(f"上下文已压缩：第 {cut} 个事件之前的对话已替换为摘要。")
        return True

    # ---- 记录 ----

    def _record(self, type_: str, **fields: Any) -> None:
        self.log.append(make_event(type_, **fields))

    def _pause(self, reason: str, message: str) -> TurnResult:
        self._record("paused", reason=reason)
        self.events.on_notice(message)
        return TurnResult("paused", reason=message)

    def _interrupted(self, reason: str) -> TurnResult:
        self._record("interrupted", reason=reason)
        self.events.on_notice(f"已中断：{reason}。")
        return TurnResult("interrupted", reason=reason)


def _denial_message(decision: Decision, by: str) -> str:
    if by == "user":
        message = "用户拒绝了这个操作"
        message += f"，理由：{decision.reason}" if decision.reason else ""
        return message + "。请根据理由调整方案，或询问用户希望怎么做。"
    return f"操作未被允许：{decision.reason}"
