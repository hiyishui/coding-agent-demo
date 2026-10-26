"""run_command：在工作区执行 shell 命令（设计文档第 8 节）。

执行前已经过权限策略（permissions.classify_command + 决策矩阵）。这里负责：
超时后杀掉整个进程组（避免留下孤儿进程）、分别截断 stdout 与 stderr、
用退出码告诉模型命令是否成功。
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

from .base import Tool, ToolContext, ToolOutput, object_schema, truncate

DEFAULT_TIMEOUT_SEC = 120
MAX_TIMEOUT_SEC = 600


def run_command(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    command = args["command"]
    timeout = min(max(1, int(args.get("timeout_sec", DEFAULT_TIMEOUT_SEC))), MAX_TIMEOUT_SEC)
    proc = subprocess.Popen(
        ["bash", "-c", command],
        cwd=ctx.workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,  # 新进程组，超时或中断时可以整组杀掉
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        return ToolOutput(
            f"命令超时（{timeout} 秒），已终止整个进程组。\n" + _format(None, stdout, stderr, ctx), is_error=True
        )
    except KeyboardInterrupt:
        _kill_group(proc)
        proc.communicate()
        raise
    return ToolOutput(_format(proc.returncode, stdout, stderr, ctx), is_error=proc.returncode != 0)


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _format(exit_code: int | None, stdout: str, stderr: str, ctx: ToolContext) -> str:
    half = ctx.max_output_chars // 2
    parts = [f"exit code: {exit_code if exit_code is not None else '（超时）'}"]
    parts.append("--- stdout ---\n" + (truncate(stdout, half).rstrip() or "（空）"))
    parts.append("--- stderr ---\n" + (truncate(stderr, half).rstrip() or "（空）"))
    return "\n".join(parts)


RUN_COMMAND = Tool(
    name="run_command",
    description=(
        "在工作区根目录用 bash 执行一条 shell 命令，返回退出码、stdout 与 stderr。"
        "用于运行测试、构建、git 只读命令等；修改代码后应运行项目的测试来验证。"
        "读文件、搜代码请优先用 read_file / search_code。联网、安装依赖等命令需要用户审批。"
        "默认超时 120 秒，最长 600 秒，超时会终止整个进程组。"
    ),
    input_schema=object_schema(
        {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout_sec": {"type": "integer", "description": "超时秒数，默认 120，最大 600"},
        },
        ["command"],
    ),
    handler=run_command,
    kind="exec",
)
