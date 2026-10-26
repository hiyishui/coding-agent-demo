"""事件日志与归约器：JSONL 往返、重建消息、合并 tool_result、压缩替换、悬空调用、resume。"""

from __future__ import annotations

from conftest import call, make_agent

from minicode.events import DANGLING_CALL_RESULT, SUMMARY_PREFIX, SessionLog, make_event, reduce
from minicode.messages import TextBlock, ToolCall, ToolResult


def assistant(*blocks, input_tokens=10, output_tokens=5):
    return make_event(
        "assistant_message",
        blocks=[b.model_dump() for b in blocks],
        stop_reason="tool_use" if any(isinstance(b, ToolCall) for b in blocks) else "end_turn",
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
    )


def tool_use(call_id, name="read_file", **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def result(call_id, content="ok", is_error=False):
    return make_event("tool_result", call_id=call_id, content=content, is_error=is_error)


def test_jsonl_round_trip(workspace):
    log = SessionLog.create(workspace, {"provider": "mock", "model": "mock", "mode": "default"})
    log.append(make_event("user_message", text="你好 😀"))
    log.append(assistant(TextBlock(text="hi"), tool_use("c1", path="a.py")))
    log.append(result("c1", "内容\n第二行"))
    loaded = SessionLog.load(workspace, log.session_id)
    assert loaded.events == log.events
    assert loaded.events[0]["type"] == "session_start" and loaded.events[0]["session_id"] == log.session_id
    path = workspace / ".minicode" / "sessions" / f"{log.session_id}.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4


def test_load_ignores_truncated_last_line(workspace):
    log = SessionLog.create(workspace, {})
    log.append(make_event("user_message", text="a"))
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"type": "user_mess')  # 写到一半进程被杀
    assert len(SessionLog.load(workspace, log.session_id).events) == 2


def test_reduce_rebuilds_messages_and_merges_tool_results():
    events = [
        make_event("user_message", text="修 bug"),
        assistant(TextBlock(text="看两个文件"), tool_use("c1", path="a"), tool_use("c2", path="b")),
        make_event("approval", call_id="c1", tool="read_file", decision="allow", by="policy", reason=""),
        result("c1", "A"),
        make_event("approval", call_id="c2", tool="read_file", decision="allow", by="policy", reason=""),
        result("c2", "B"),
        assistant(TextBlock(text="完成"), input_tokens=30, output_tokens=7),
    ]
    state = reduce(events)
    assert [m.role for m in state.messages] == ["user", "assistant", "user", "assistant"]
    assert [r.call_id for r in state.messages[2].tool_results] == ["c1", "c2"]
    assert state.steps == 2
    assert state.usage.input_tokens == 40 and state.usage.output_tokens == 12
    assert state.last_input_tokens == 30


def test_reduce_tracks_todos_and_consecutive_failures():
    events = [
        make_event("user_message", text="go"),
        assistant(tool_use("c1"), tool_use("c2"), tool_use("c3")),
        result("c1", is_error=True),
        make_event("approval", call_id="c2", tool="edit_file", decision="deny", by="user", reason=""),
        result("c2", "拒绝", is_error=True),
        result("c3", is_error=True),
        make_event("todos_updated", todos=[{"content": "a", "status": "pending"}]),
    ]
    state = reduce(events)
    assert state.consecutive_failures == 2  # 审批拒绝不计入
    assert state.todos == [{"content": "a", "status": "pending"}]
    events.append(make_event("user_message", text="继续"))
    assert reduce(events).consecutive_failures == 0


def test_reduce_closes_dangling_tool_calls():
    events = [
        make_event("user_message", text="go"),
        assistant(tool_use("c1"), tool_use("c2")),
        result("c1"),
        # 进程在执行 c2 时被杀
    ]
    state = reduce(events)
    results = state.messages[-1].tool_results
    assert [(r.call_id, r.is_error) for r in results] == [("c1", False), ("c2", True)]
    assert results[1].content == DANGLING_CALL_RESULT

    events.append(make_event("user_message", text="刚才断了，继续"))
    state = reduce(events)
    assert state.messages[-1].role == "user"
    blocks = state.messages[-1].blocks
    assert isinstance(blocks[0], ToolResult) and isinstance(blocks[-1], TextBlock)


def test_reduce_applies_compaction():
    events = [
        make_event("session_start", session_id="s"),
        make_event("user_message", text="第一轮"),  # 1
        assistant(tool_use("c1")),  # 2
        result("c1"),  # 3
        assistant(TextBlock(text="第一轮完成")),  # 4
        make_event("user_message", text="第二轮"),  # 5
        assistant(TextBlock(text="第二轮完成")),  # 6
        make_event("compaction", summary="做过第一轮", upto=4),
        make_event("user_message", text="第三轮"),
    ]
    state = reduce(events)
    texts = [m.text for m in state.messages]
    assert texts[0].startswith(SUMMARY_PREFIX + "做过第一轮")
    assert "第二轮" in texts[0]  # 摘要后紧跟的用户输入合并进同一条 user 消息
    assert texts[1:] == ["第二轮完成", "第三轮"]
    assert state.steps == 3  # 统计不受压缩影响
    assert state.last_input_tokens == 0


def test_resume_continues_conversation(workspace):
    first = make_agent(workspace, [{"text": "你好，我记住了：项目叫 demo。"}])
    first.run_turn("记住项目名 demo")
    session_id = first.log.session_id

    resumed_log = SessionLog.load(workspace, session_id)
    second = make_agent(
        workspace,
        [{"text": "先看看文件。", "tool_calls": [call("list_files")]}, {"text": "项目叫 demo。"}],
        log=resumed_log,
    )
    result_ = second.run_turn("项目叫什么？")
    assert result_.text == "项目叫 demo。"
    messages = reduce(SessionLog.load(workspace, session_id).events).messages
    assert messages[0].text == "记住项目名 demo"
    assert messages[1].text.startswith("你好")
    assert messages[2].text == "项目叫什么？"
    assert len(messages) == 6
