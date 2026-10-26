"""Agent 循环：端到端修复 buggy-calc、审批拒绝、连续失败、步数与预算、重试、中断。"""

from __future__ import annotations

import subprocess
import sys

from conftest import call, load_fix_script, make_agent

from minicode.agent import CANCELLED_RESULT, RetryPolicy
from minicode.messages import ModelResponse, TextBlock, ToolResult, Usage
from minicode.permissions import ApprovalAnswer
from minicode.providers.base import ProviderError, RetryableError
from minicode.providers.mock import ScriptedProvider
from minicode.tools import builtin_registry
from minicode.tools.base import Tool, object_schema


def events_of(agent, type_):
    return [e for e in agent.log.events if e["type"] == type_]


class RecordingProvider(ScriptedProvider):
    """记录每次调用收到的消息，便于断言“模型看到了什么”。"""

    def __init__(self, script):
        super().__init__(script)
        self.seen = []

    def complete(self, **kwargs):
        self.seen.append(kwargs["messages"])
        return super().complete(**kwargs)


# ---- 端到端 ----


def test_fix_buggy_calc_end_to_end(buggy_calc):
    agent = make_agent(buggy_calc, load_fix_script(), mode="auto-edit")
    result = agent.run_turn("修复失败的测试")

    assert result.status == "done"
    assert "range(1, n + 1)" in result.text
    assert "range(1, n + 1)" in (buggy_calc / "calc.py").read_text()
    pytest_results = [e for e in events_of(agent, "tool_result") if "exit code" in e["content"]]
    assert [e["is_error"] for e in pytest_results] == [True, False]  # 先失败、修复后通过
    assert all(e["decision"] == "allow" and e["by"] == "policy" for e in events_of(agent, "approval"))
    assert all(t["status"] == "done" for t in agent.state.todos)

    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=buggy_calc, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


def test_default_mode_headless_denies_edit_and_model_sees_error(buggy_calc):
    original = (buggy_calc / "calc.py").read_text()
    script = [
        {
            "text": "我来修改。",
            "tool_calls": [call("edit_file", path="calc.py", old_str="range(1, n)", new_str="range(1, n + 1)")],
        },
        {"text": "修改被拒绝了，需要你授权。"},
    ]
    provider = RecordingProvider(script)
    agent = make_agent(buggy_calc, provider=provider, mode="default")
    result = agent.run_turn("修 bug")

    assert result.status == "done"
    assert (buggy_calc / "calc.py").read_text() == original
    approval = events_of(agent, "approval")[0]
    assert approval["decision"] == "deny" and approval["by"] == "headless"
    last_seen = provider.seen[-1][-1].tool_results[0]
    assert last_seen.is_error and "--allow" in last_seen.content


def test_user_denial_reason_is_passed_to_model(buggy_calc):
    class DenyingApprover:
        name = "user"

        def ask(self, call_, decision):
            return ApprovalAnswer(allow=False, reason="不要改 calc.py，先写测试")

    script = [
        {"text": "改一下。", "tool_calls": [call("edit_file", path="calc.py", old_str="range(1, n)", new_str="x")]},
        {"text": "好的。"},
    ]
    agent = make_agent(buggy_calc, script, approver=DenyingApprover())
    agent.run_turn("修 bug")
    tool_result = events_of(agent, "tool_result")[0]
    assert tool_result["is_error"] and "不要改 calc.py，先写测试" in tool_result["content"]
    assert events_of(agent, "approval")[0]["by"] == "user"


def test_always_allow_is_remembered_within_session(workspace):
    asked = []

    class AlwaysApprover:
        name = "user"

        def ask(self, call_, decision):
            asked.append(call_.name)
            return ApprovalAnswer(allow=True, remember=True)

    script = [
        {"text": "建文件。", "tool_calls": [call("create_file", path="a.txt", content="1")]},
        {"text": "再建一个。", "tool_calls": [call("create_file", path="b.txt", content="2")]},
        {"text": "完成。"},
    ]
    agent = make_agent(workspace, script, approver=AlwaysApprover())
    agent.run_turn("建两个文件")
    assert asked == ["create_file"]
    assert (workspace / "b.txt").read_text() == "2"


# ---- 连续失败、步数、预算 ----


def test_pauses_after_three_consecutive_failures(workspace):
    script = [{"text": f"读第 {i} 个。", "tool_calls": [call("read_file", path=f"missing{i}.py")]} for i in range(5)]
    agent = make_agent(workspace, script)
    result = agent.run_turn("读文件")
    assert result.status == "paused"
    assert events_of(agent, "paused")[0]["reason"] == "consecutive_failures"
    assert len(events_of(agent, "tool_result")) == 3


def test_denials_do_not_count_as_failures(workspace):
    script = [{"text": "改。", "tool_calls": [call("create_file", path=f"f{i}", content="")]} for i in range(4)]
    script.append({"text": "需要授权。"})
    agent = make_agent(workspace, script, mode="plan")
    result = agent.run_turn("建文件")
    assert result.status == "done"
    assert not events_of(agent, "paused")


def test_pauses_at_max_steps(workspace):
    script = [{"text": "再看看。", "tool_calls": [call("list_files")]} for _ in range(10)]
    agent = make_agent(workspace, script, max_steps=3)
    result = agent.run_turn("看看")
    assert result.status == "paused"
    assert events_of(agent, "paused")[0]["reason"] == "max_steps"
    assert agent.state.steps == 3


def test_pauses_when_token_or_cost_budget_exceeded(workspace):
    script = [{"text": "看看。", "tool_calls": [call("list_files")]} for _ in range(5)]
    agent = make_agent(workspace, script, max_tokens_total=10)
    assert agent.run_turn("看看").status == "paused"
    assert events_of(agent, "paused")[0]["reason"] == "budget"

    priced = make_agent(workspace, script, model="claude-sonnet-5-5", max_cost_usd=0.000001)
    assert priced.run_turn("看看").status == "paused"


# ---- 重试 ----


class FlakyProvider:
    model = "flaky"

    def __init__(self, failures, retry_after=None):
        self.failures = failures
        self.retry_after = retry_after
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise RetryableError("429 Too Many Requests", retry_after=self.retry_after)
        return ModelResponse(blocks=[TextBlock(text="终于成功")], stop_reason="end_turn", usage=Usage())


def test_retryable_errors_are_retried_with_backoff(workspace):
    provider = FlakyProvider(failures=3)
    agent = make_agent(workspace, provider=provider)
    delays = []
    agent.sleep = delays.append
    result = agent.run_turn("你好")
    assert result.status == "done" and result.text == "终于成功"
    assert provider.calls == 4
    assert len(delays) == 3
    assert 1 <= delays[0] < 2 and 2 <= delays[1] < 3 and 4 <= delays[2] < 5  # base × 2^n + 抖动


def test_retry_after_is_preferred_and_capped(workspace):
    agent = make_agent(workspace, provider=FlakyProvider(failures=1, retry_after=7))
    delays = []
    agent.sleep = delays.append
    agent.run_turn("你好")
    assert delays == [7]
    assert RetryPolicy().delay(0, retry_after=120) == 30


def test_gives_up_after_max_retries(workspace):
    provider = FlakyProvider(failures=10)
    agent = make_agent(workspace, provider=provider)
    result = agent.run_turn("你好")
    assert result.status == "error"
    assert provider.calls == 5  # 首次 + 4 次重试


def test_non_retryable_error_is_not_retried(workspace):
    class BrokenProvider:
        model = "broken"
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            raise ProviderError("401 invalid api key")

    provider = BrokenProvider()
    result = make_agent(workspace, provider=provider).run_turn("你好")
    assert result.status == "error" and provider.calls == 1


# ---- 中断 ----


def interrupting_tool() -> Tool:
    def handler(args, ctx):
        raise KeyboardInterrupt

    return Tool("boom", "模拟用户按下 Ctrl-C", object_schema({}, []), handler, "read")


def test_interrupt_during_tools_keeps_calls_paired(workspace):
    registry = builtin_registry()
    registry.register(interrupting_tool())
    script = [
        {"text": "三件事。", "tool_calls": [call("list_files"), call("boom"), call("list_files")]},
        {"text": "好的，继续。"},
    ]
    agent = make_agent(workspace, script, registry=registry)
    result = agent.run_turn("开始")
    assert result.status == "interrupted"
    assert events_of(agent, "interrupted")

    messages = agent.state.messages
    calls = messages[1].tool_calls
    results = {r.call_id: r for r in messages[2].tool_results}
    assert set(results) == {c.id for c in calls}
    assert not results[calls[0].id].is_error
    assert results[calls[1].id].content == CANCELLED_RESULT
    assert results[calls[2].id].content == CANCELLED_RESULT

    assert agent.run_turn("继续").status == "done"  # 中断后可以继续对话
    last_user = agent.state.messages[-2]
    assert isinstance(last_user.blocks[0], ToolResult) and last_user.text == "继续"


def test_interrupt_during_model_call(workspace):
    class InterruptedProvider:
        model = "x"

        def complete(self, **kwargs):
            raise KeyboardInterrupt

    agent = make_agent(workspace, provider=InterruptedProvider())
    assert agent.run_turn("你好").status == "interrupted"
    assert not events_of(agent, "assistant_message")
