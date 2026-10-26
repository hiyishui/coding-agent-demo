"""上下文工程：系统提示词与 AGENTS.md、token 估算、自动压缩的触发与切分点、压缩后 todos 仍在。"""

from __future__ import annotations

from datetime import date

from conftest import call, make_agent

from minicode.context import (
    AGENTS_MD_LIMIT,
    COMPACT_MARKER,
    build_system_prompt,
    estimate_context_tokens,
    find_compaction_cut,
)
from minicode.events import SUMMARY_PREFIX, make_event, reduce
from minicode.messages import ModelResponse, TextBlock, ToolCall


def test_system_prompt_includes_agents_md_and_environment(buggy_calc):
    prompt = build_system_prompt(buggy_calc, "auto-edit", today=date(2026, 7, 28))
    assert "python -m pytest -q" in prompt  # 来自 buggy-calc/AGENTS.md
    assert str(buggy_calc) in prompt
    assert "2026-07-28" in prompt and "auto-edit" in prompt
    assert "update_todos" in prompt and "explore" in prompt


def test_agents_md_is_capped(workspace):
    (workspace / "AGENTS.md").write_text("规" * (AGENTS_MD_LIMIT + 500), encoding="utf-8")
    prompt = build_system_prompt(workspace, "default")
    assert prompt.count("规") == AGENTS_MD_LIMIT
    assert "其余部分已省略" in prompt


def test_system_prompt_without_agents_md(workspace):
    assert "项目指令" not in build_system_prompt(workspace, "default")


def assistant(*blocks, input_tokens=0):
    return make_event(
        "assistant_message",
        blocks=[b.model_dump() for b in blocks],
        stop_reason="end_turn",
        usage={"input_tokens": input_tokens, "output_tokens": 0},
    )


def test_estimate_uses_last_usage_plus_new_chars():
    events = [make_event("user_message", text="hi"), assistant(TextBlock(text="ok"), input_tokens=1000)]
    base = estimate_context_tokens(events)
    assert base >= 1000
    events.append(make_event("user_message", text="x" * 4000))
    assert estimate_context_tokens(events) == base + 1000


def tool_round(call_id):
    return [
        assistant(ToolCall(id=call_id, name="list_files", arguments={})),
        make_event("tool_result", call_id=call_id, content="ok", is_error=False),
    ]


def test_cut_moves_before_assistant_when_landing_on_tool_result():
    events = [make_event("session_start"), make_event("user_message", text="任务")]
    for i in range(6):
        events += tool_round(f"c{i}")
    cut = find_compaction_cut(events, keep_user_messages=4)
    assert events[cut]["type"] == "assistant_message"
    assert events[cut + 1]["type"] == "tool_result"
    kept_rounds = sum(1 for e in events[cut:] if e["type"] == "assistant_message")
    assert kept_rounds == 4


def test_cut_lands_on_real_user_message():
    events = [make_event("session_start")]
    for i in range(6):
        events += [make_event("user_message", text=f"问题 {i}"), assistant(TextBlock(text=f"回答 {i}"))]
    cut = find_compaction_cut(events, keep_user_messages=4)
    assert events[cut] == events[5]  # 倒数第 4 个问题
    assert events[cut]["text"] == "问题 2"


def test_no_cut_when_nothing_to_compact():
    events = [make_event("session_start"), make_event("user_message", text="只有一句")]
    assert find_compaction_cut(events) is None


def big_reading_script(n):
    return [{"text": f"读第 {i} 遍。", "tool_calls": [call("read_file", path="big.py")]} for i in range(n)]


def test_auto_compaction_triggers_with_valid_cut(workspace):
    (workspace / "big.py").write_text("\n".join(f"value_{i} = {i}" for i in range(300)), encoding="utf-8")
    script = big_reading_script(6) + [{"text": "读完了。"}]
    agent = make_agent(workspace, script, context_window=8000)
    result = agent.run_turn("把 big.py 读几遍")
    assert result.status == "done"

    compactions = [(i, e) for i, e in enumerate(agent.log.events) if e["type"] == "compaction"]
    assert compactions, "上下文超过阈值时应自动压缩"
    for _, event in compactions:
        first_kept = agent.log.events[event["upto"] + 1]
        assert first_kept["type"] in ("user_message", "assistant_message")
        assert "任务目标" in event["summary"]

    messages = agent.state.messages
    assert messages[0].text.startswith(SUMMARY_PREFIX)
    # 每个 tool_use 都紧跟着对应的 tool_result
    for i, message in enumerate(messages):
        if message.tool_calls:
            assert {c.id for c in message.tool_calls} == {r.call_id for r in messages[i + 1].tool_results}
        if message.tool_results:
            assert messages[i - 1].role == "assistant"
            assert {r.call_id for r in message.tool_results} <= {c.id for c in messages[i - 1].tool_calls}


def test_compaction_request_uses_marker_and_no_tools(workspace):
    seen = []

    class SpyProvider:
        model = "mock"

        def complete(self, *, system, messages, tools, max_tokens, on_text=None):
            seen.append((system, tools))
            return ModelResponse(blocks=[TextBlock(text="摘要")], stop_reason="end_turn")

    agent = make_agent(workspace, provider=SpyProvider())
    for i in range(3):
        agent.run_turn(f"问题 {i}")
    agent.compact()
    system, tools = seen[-1]
    assert system.startswith(COMPACT_MARKER) and tools == []
    first = reduce(agent.log.events).messages[0]
    assert first.text.startswith(SUMMARY_PREFIX + "摘要")
    assert "问题 0" not in first.text and "问题 2" in reduce(agent.log.events).messages[-2].text


def test_todos_survive_compaction(workspace):
    (workspace / "big.py").write_text("\n".join(f"value_{i} = {i}" for i in range(300)), encoding="utf-8")
    todos = [{"content": "读 big.py", "status": "in_progress"}, {"content": "总结", "status": "pending"}]
    script = [{"text": "列计划。", "tool_calls": [call("update_todos", todos=todos)]}]
    script += big_reading_script(6) + [{"text": "完成。"}]
    agent = make_agent(workspace, script, context_window=8000)
    agent.run_turn("读 big.py")

    events = agent.log.events
    todo_index = next(i for i, e in enumerate(events) if e["type"] == "todos_updated")
    last_compaction = [e for e in events if e["type"] == "compaction"][-1]
    assert last_compaction["upto"] > todo_index  # 记录 todos 的那段对话已被摘要替换
    assert agent.state.todos == todos
