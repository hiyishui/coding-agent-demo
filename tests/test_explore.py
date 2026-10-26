"""explore 子代理：只能用只读工具；主会话日志里只有最终摘要。"""

from __future__ import annotations

from conftest import call, make_agent

from minicode.agent import AgentConfig
from minicode.providers.mock import ScriptedProvider
from minicode.tools import builtin_registry
from minicode.tools.explore import EXPLORE_TOOLS, make_explore_tool

SECRET_LINE = "SEARCH_RESULT_MARKER = 42"
SUMMARY = "结论：factorial 定义在 calc.py:18，循环上界有差一错误。"


def build(buggy_calc, script):
    (buggy_calc / "marker.py").write_text(SECRET_LINE + "\n", encoding="utf-8")
    provider = ScriptedProvider(script)
    registry = builtin_registry()
    registry.register(make_explore_tool(provider, registry, AgentConfig()))
    return make_agent(buggy_calc, provider=provider, registry=registry, mode="yolo")


def test_explore_returns_only_final_summary(buggy_calc):
    script = [
        {"text": "交给子代理调查。", "tool_calls": [call("explore", task="factorial 在哪里定义，有什么问题？")]},
        # 以下三项由子代理消费
        {"text": "搜索一下。", "tool_calls": [call("search_code", pattern="SEARCH_RESULT_MARKER|def factorial")]},
        {"text": "读一下。", "tool_calls": [call("read_file", path="calc.py")]},
        {"text": SUMMARY},
        # 回到主 Agent
        {"text": "子代理找到了问题所在。"},
    ]
    agent = build(buggy_calc, script)
    result = agent.run_turn("调查 factorial")
    assert result.status == "done" and result.text == "子代理找到了问题所在。"

    tool_results = [e for e in agent.log.events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["content"] == SUMMARY
    assert tool_results[0]["usage"]["input_tokens"] > 0  # 子代理的用量计入主会话预算
    log_text = agent.log.path.read_text(encoding="utf-8")
    assert SECRET_LINE not in log_text  # 中间的搜索结果没有进入主会话
    assert "def factorial" not in log_text


def test_explore_subagent_cannot_edit(buggy_calc):
    original = (buggy_calc / "calc.py").read_text()
    script = [
        {"text": "调查一下。", "tool_calls": [call("explore", task="修一下 bug")]},
        {
            "text": "我顺手改了。",
            "tool_calls": [call("edit_file", path="calc.py", old_str="range(1, n)", new_str="range(1, n + 1)")],
        },
        {"text": "我跑个命令。", "tool_calls": [call("run_command", command="touch hacked.txt")]},
        {"text": "子代理：无法修改，只能报告。"},
        {"text": "好的。"},
    ]
    agent = build(buggy_calc, script)
    agent.run_turn("调查")
    assert (buggy_calc / "calc.py").read_text() == original
    assert not (buggy_calc / "hacked.txt").exists()
    assert [e["content"] for e in agent.log.events if e["type"] == "tool_result"] == ["子代理：无法修改，只能报告。"]


def test_explore_registry_is_read_only():
    registry = builtin_registry()
    sub = registry.subset(EXPLORE_TOOLS)
    assert sorted(sub.tools) == sorted(EXPLORE_TOOLS)
    assert {tool.kind for tool in sub.tools.values()} == {"read"}
