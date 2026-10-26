"""search_code：按正则搜索代码（设计文档第 8 节）。

优先用 ripgrep（快、遵循 .gitignore）；没有安装 rg 时退回 Python 实现，
两条分支的输出格式一致：`path:line:text`。这是“按需检索”的核心工具——
让模型先定位、再精读，而不是把整个仓库塞进上下文。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext, ToolOutput, object_schema
from .files import glob_match, is_binary, iter_files

MAX_MATCHES = 100
MAX_LINE_CHARS = 300


def find_rg() -> str | None:
    """单独成函数，便于测试时强制走 Python 回退分支。"""
    return shutil.which("rg")


def search_code(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    pattern = args["pattern"]
    base = ctx.resolve(args.get("path", "."))
    glob = args.get("glob")
    ignore_case = bool(args.get("case_insensitive", False))
    try:
        re.compile(pattern)
    except re.error as err:
        return ToolOutput(f"pattern 不是合法的正则表达式：{err}", True)

    rg = find_rg()
    if rg:
        matches = _search_with_rg(rg, pattern, base, glob, ignore_case, ctx)
    else:
        matches = _search_with_python(pattern, base, glob, ignore_case, ctx)

    if not matches:
        return ToolOutput(f"没有找到匹配 {pattern!r} 的内容。")
    text = "\n".join(matches[:MAX_MATCHES])
    if len(matches) > MAX_MATCHES:
        text += (
            f"\n（结果超过 {MAX_MATCHES} 条，只显示前 {MAX_MATCHES} 条；请用更精确的 pattern、path 或 glob 缩小范围）"
        )
    return ToolOutput(text)


def _format(rel_path: str, line_no: int, line: str) -> str:
    return f"{rel_path}:{line_no}:{line.strip()[:MAX_LINE_CHARS]}"


def _search_with_rg(
    rg: str, pattern: str, base: Path, glob: str | None, ignore_case: bool, ctx: ToolContext
) -> list[str]:
    cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--with-filename"]
    if ignore_case:
        cmd.append("--ignore-case")
    if glob:
        cmd += ["--glob", glob]
    cmd += ["-e", pattern, ctx.relative(base)]
    proc = subprocess.run(cmd, cwd=ctx.workspace, capture_output=True, text=True, timeout=60)
    matches = []
    for line in proc.stdout.splitlines():
        path, _, rest = line.partition(":")
        line_no, _, text = rest.partition(":")
        if line_no.isdigit():
            matches.append(_format(path.removeprefix("./"), int(line_no), text))
    return matches[: MAX_MATCHES + 1]


def _search_with_python(pattern: str, base: Path, glob: str | None, ignore_case: bool, ctx: ToolContext) -> list[str]:
    regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    matches: list[str] = []
    root, paths = (base.parent, [base]) if base.is_file() else (base, iter_files(ctx, base))
    for path in paths:
        if glob and not glob_match(path.relative_to(root).as_posix(), glob):
            continue
        data = path.read_bytes()
        if is_binary(data):
            continue
        for line_no, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            if regex.search(line):
                matches.append(_format(ctx.relative(path), line_no, line))
                if len(matches) > MAX_MATCHES:
                    return matches
    return matches


SEARCH_CODE = Tool(
    name="search_code",
    description=(
        "在工作区内按正则表达式搜索文件内容，返回 `路径:行号:该行文本`（最多 100 条）。"
        "用于定位函数定义、调用点、报错信息等；找到位置后再用 read_file 读取上下文。"
        "可用 glob 限定文件类型（如 `*.py`），用 path 限定目录。"
    ),
    input_schema=object_schema(
        {
            "pattern": {"type": "string", "description": "正则表达式（Python / ripgrep 语法）"},
            "path": {"type": "string", "description": "搜索的起始目录或文件，默认工作区根目录"},
            "glob": {"type": "string", "description": "文件名 glob 过滤，如 *.py"},
            "case_insensitive": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
        },
        ["pattern"],
    ),
    handler=search_code,
    kind="read",
)
