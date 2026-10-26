"""命令行入口（设计文档第 13 节）：参数解析、组装各模块、交互式 REPL 与无头模式。

    minicode [-C WORKSPACE] [--provider mock|anthropic|openai] [--model MODEL]
             [--mode plan|default|auto-edit|yolo] [--task TEXT] [--resume SESSION_ID]
             [--allow edit|exec|mcp ...] [--max-steps N] [--max-cost-usd X]
             [--mock-script PATH] [--mcp-config PATH]

无头模式（--task）的退出码：0 = 正常结束，2 = 暂停（预算 / 连续失败 / 步数），1 = 错误或中断。
最终回答写到 stdout，过程写到 stderr，便于接入 CI 与评测脚本。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from rich.console import Console
from rich.markup import escape

from .agent import Agent, AgentConfig, TurnResult
from .config import ALLOW_KINDS, MODES, PROVIDERS, Settings, estimate_cost, load_settings
from .context import build_system_prompt
from .events import SessionLog
from .mcp_client import McpServer, connect_servers
from .permissions import HeadlessApprover, PermissionPolicy
from .providers import ProviderError, make_provider
from .tools import ToolRegistry, builtin_registry
from .tools.explore import make_explore_tool
from .ui import ConsoleUI, InteractiveApprover

EXIT_CODES = {"done": 0, "paused": 2}

HELP_TEXT = """斜杠命令：
  /mode <plan|default|auto-edit|yolo>  切换运行模式
  /todos                               查看待办清单
  /compact                             立即压缩上下文
  /cost                                查看 token 用量与估算费用
  /exit                                退出（也可以按 Ctrl-D）"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minicode", description="一个运行在终端里的编程 Agent")
    parser.add_argument("-C", dest="workspace", metavar="WORKSPACE", help="工作区目录，默认当前目录")
    parser.add_argument("--provider", choices=PROVIDERS, help="模型提供方，默认 mock（或 MINICODE_PROVIDER）")
    parser.add_argument("--model", help="模型名（或 MINICODE_MODEL）")
    parser.add_argument("--mode", choices=MODES, help="运行模式，默认 default")
    parser.add_argument("--task", help="无头模式：执行这个任务后退出")
    parser.add_argument("--resume", metavar="SESSION_ID", help="从已有会话继续")
    parser.add_argument("--allow", nargs="+", choices=ALLOW_KINDS, help="预先批准的操作类别（联网命令除外）")
    parser.add_argument("--max-steps", type=int, help="每轮最多调用模型的次数，默认 50")
    parser.add_argument("--max-tokens-total", type=int, help="会话累计 token 上限")
    parser.add_argument("--max-cost-usd", type=float, help="会话累计费用上限（美元，按估算单价）")
    parser.add_argument("--mock-script", help="Mock 模型的回放脚本（JSON）")
    parser.add_argument("--mcp-config", help="MCP 配置文件，默认 <workspace>/.minicode/mcp.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    headless = args.task is not None
    console = Console(stderr=True) if headless else Console()
    ui = ConsoleUI(console)
    try:
        settings = load_settings(vars(args))
        if not settings.workspace.is_dir():
            raise ValueError(f"工作区不存在：{settings.workspace}")
        provider = make_provider(settings)
    except (ValueError, ProviderError, OSError, json.JSONDecodeError) as err:
        console.print(f"[red]启动失败：{escape(str(err))}[/]")
        return 1

    config = AgentConfig(
        model=settings.model,
        context_window=settings.context_window,
        max_steps=settings.max_steps,
        max_tokens_total=settings.max_tokens_total,
        max_cost_usd=settings.max_cost_usd,
        max_output_tokens=settings.max_output_tokens,
        max_output_chars=settings.max_output_chars,
    )
    registry = builtin_registry()
    registry.register(make_explore_tool(provider, registry, config, ui))
    servers = _connect_mcp(settings, registry, ui)

    log: SessionLog | None = None
    try:
        if args.resume:
            log = SessionLog.load(settings.workspace, args.resume)
        else:
            meta = {"provider": settings.provider, "model": settings.model, "mode": settings.mode}
            log = SessionLog.create(settings.workspace, meta)
        agent = Agent(
            provider=provider,
            registry=registry,
            log=log,
            system_prompt=build_system_prompt(settings.workspace, settings.mode),
            workspace=settings.workspace,
            policy=PermissionPolicy(mode=settings.mode, preapproved=settings.allow),
            approver=HeadlessApprover() if headless else InteractiveApprover(ui),
            events=ui,
            config=config,
        )
        verb = "继续会话" if args.resume else "会话"
        console.print(
            f"[bold]minicode[/] {verb} [cyan]{log.session_id}[/] · {settings.provider}/{escape(settings.model)}"
            f" · 模式 {settings.mode} · 工作区 {escape(str(settings.workspace))}",
            highlight=False,
        )
        if headless:
            return run_headless(agent, args.task, ui)
        return run_repl(agent, ui)
    except FileNotFoundError as err:
        console.print(f"[red]{escape(str(err))}[/]")
        return 1
    finally:
        for server in servers:
            server.close()
        if log is not None:
            console.print(
                f"[dim]恢复本会话：minicode -C {escape(str(settings.workspace))} --resume {log.session_id}[/]"
            )


def _connect_mcp(settings: Settings, registry: ToolRegistry, ui: ConsoleUI) -> list[McpServer]:
    path = settings.mcp_config or settings.workspace / ".minicode" / "mcp.json"
    if not path.is_file():
        if settings.mcp_config:
            ui.on_notice(f"找不到 MCP 配置：{path}")
        return []
    stderr_log = settings.workspace / ".minicode" / "mcp-stderr.log"
    stderr_log.parent.mkdir(parents=True, exist_ok=True)

    def write_stderr(line: str) -> None:
        with stderr_log.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")

    try:
        servers, tools = connect_servers(path, cwd=settings.workspace, on_notice=ui.on_notice, on_stderr=write_stderr)
    except (OSError, json.JSONDecodeError) as err:
        ui.on_notice(f"MCP 配置无法读取：{err}")
        return []
    for tool in tools:
        registry.register(tool)
    return servers


def run_headless(agent: Agent, task: str, ui: ConsoleUI) -> int:
    result = agent.run_turn(task)
    ui.finish_turn()
    if result.text:
        print(result.text)
    if result.status != "done":
        ui.on_notice(f"任务未完成（{result.status}）：{result.reason}")
    return EXIT_CODES.get(result.status, 1)


def run_repl(agent: Agent, ui: ConsoleUI) -> int:
    console = ui.console
    console.print("[dim]输入任务开始工作；/help 查看命令，/exit 退出。[/]")
    while True:
        try:
            line = input("› ").strip()
        except EOFError:
            console.print()
            return 0
        except KeyboardInterrupt:
            console.print("\n[dim]（输入 /exit 或按 Ctrl-D 退出）[/]")
            continue
        if not line:
            continue
        if line.startswith("/"):
            if not handle_command(line, agent, ui):
                return 0
            continue
        try:
            result: TurnResult = agent.run_turn(line)
        except KeyboardInterrupt:
            result = TurnResult("interrupted", reason="用户中断")
        ui.finish_turn()
        if result.status == "paused":
            console.print("[dim]Agent 已暂停。输入新的指示继续，或调整预算后重试。[/]")


def handle_command(line: str, agent: Agent, ui: ConsoleUI) -> bool:
    """执行斜杠命令；返回 False 表示退出 REPL。"""
    command, _, argument = line.partition(" ")
    argument = argument.strip()
    console = ui.console
    if command == "/exit":
        return False
    if command == "/mode":
        if argument not in MODES:
            console.print(f"当前模式：{agent.policy.mode}。可选：{' / '.join(MODES)}")
        else:
            agent.policy.mode = argument
            console.print(f"已切换到 {argument} 模式。")
    elif command == "/todos":
        ui.print_todos(agent.state.todos)
    elif command == "/compact":
        agent.compact()
    elif command == "/cost":
        usage = agent.state.usage
        cost = estimate_cost(agent.config.model, usage)
        cost_text = f"${cost:.4f}" if cost is not None else "未知（该模型没有单价）"
        console.print(
            f"输入 {usage.input_tokens} tokens（缓存命中 {usage.cache_read_tokens}），"
            f"输出 {usage.output_tokens} tokens，估算费用 {cost_text}"
        )
    else:
        console.print(HELP_TEXT, highlight=False)
    return True


if __name__ == "__main__":
    sys.exit(main())
