"""文件工具：read_file、list_files、edit_file、create_file（设计文档第 8 节）。

工具描述写清“何时用、何时不用、返回什么”，返回值里带上下一步的提示
（例如“继续读请用 offset=…”），让模型少走弯路。所有路径都经过路径守卫。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext, ToolOutput, object_schema

IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".minicode", ".pytest_cache"}
MAX_LIST_ENTRIES = 500
SNIPPET_CONTEXT_LINES = 3


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def numbered(lines: list[str], start: int) -> str:
    """带行号的文本：行号右对齐 6 位，后跟制表符。"""
    return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(lines, start=start))


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 转成正则：`**/` 匹配任意层目录，`*` 不跨目录，`?` 匹配单个字符。"""
    parts, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def glob_match(rel_path: str, pattern: str) -> bool:
    """不含 / 的模式（如 `*.py`）只匹配文件名，与 .gitignore 的习惯一致。"""
    target = rel_path if "/" in pattern else rel_path.rsplit("/", 1)[-1]
    return glob_to_regex(pattern).match(target) is not None


def iter_files(ctx: ToolContext, base: Path) -> list[Path]:
    """列出 base 下的文件。git 仓库内遵循 .gitignore，否则遍历并跳过常见的无关目录。"""
    root = ctx.workspace.resolve()
    files: list[Path] = []
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=base,
            capture_output=True,
            timeout=30,
        )
        in_git = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        in_git = False
    if in_git:
        files = [base / name for name in proc.stdout.decode("utf-8", "replace").split("\0") if name]
    else:
        files = [p for p in base.rglob("*") if p.is_file() or p.is_symlink()]
    result = []
    for path in files:
        rel_parts = path.relative_to(base).parts
        if any(part in IGNORED_DIRS for part in rel_parts):
            continue
        if path.is_file() and path.resolve().is_relative_to(root):  # 跳过已删除文件和指向外部的链接
            result.append(path)
    return sorted(result)


# ---- read_file ----


def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    path = ctx.resolve(args["path"])
    if not path.is_file():
        return ToolOutput(f"文件不存在：{args['path']}。可以先用 list_files 或 search_code 找到正确路径。", True)
    data = path.read_bytes()
    if is_binary(data):
        return ToolOutput(f"{args['path']} 是二进制文件，无法以文本读取。", True)
    lines = data.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    if total == 0:
        return ToolOutput(f"（{args['path']} 是空文件）")
    offset = max(1, int(args.get("offset", 1)))
    limit = max(1, int(args.get("limit", 400)))
    if offset > total:
        return ToolOutput(f"offset={offset} 超出文件范围（共 {total} 行）。", True)
    end = min(total, offset + limit - 1)
    footer = f"（显示第 {offset}–{end} 行，共 {total} 行"
    footer += f"；继续读请用 offset={end + 1}）" if end < total else "）"
    return ToolOutput(numbered(lines[offset - 1 : end], offset) + "\n" + footer)


READ_FILE = Tool(
    name="read_file",
    description=(
        "读取工作区内的文本文件，返回带行号的内容（每次默认最多 400 行）。"
        "不知道代码在哪个文件时，先用 search_code 定位，再读相关片段，不要逐个通读整个项目。"
        "大文件用 offset/limit 分页；返回末尾会提示下一页的 offset。"
    ),
    input_schema=object_schema(
        {
            "path": {"type": "string", "description": "相对工作区根目录的文件路径"},
            "offset": {"type": "integer", "description": "起始行号（从 1 开始），默认 1"},
            "limit": {"type": "integer", "description": "最多读取的行数，默认 400"},
        },
        ["path"],
    ),
    handler=read_file,
    kind="read",
)


# ---- list_files ----


def list_files(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    base = ctx.resolve(args.get("path", "."))
    if not base.is_dir():
        return ToolOutput(f"目录不存在：{args.get('path', '.')}", True)
    pattern = args.get("pattern", "**/*")
    matches = [ctx.relative(p) for p in iter_files(ctx, base) if glob_match(p.relative_to(base).as_posix(), pattern)]
    if not matches:
        return ToolOutput(f"没有匹配 {pattern} 的文件。")
    shown = matches[:MAX_LIST_ENTRIES]
    text = "\n".join(shown)
    if len(matches) > MAX_LIST_ENTRIES:
        text += f"\n（共 {len(matches)} 个文件，只显示前 {MAX_LIST_ENTRIES} 个；请用更窄的 pattern 或 path）"
    return ToolOutput(text)


LIST_FILES = Tool(
    name="list_files",
    description=(
        "列出工作区内的文件路径（遵循 .gitignore，跳过 .git、.venv、node_modules 等）。"
        "用于了解项目结构或按文件名查找文件；要按内容查找请用 search_code。"
        "pattern 是 glob：`**/*.py` 匹配所有 Python 文件，不含 / 的模式（如 `test_*.py`）只匹配文件名。"
    ),
    input_schema=object_schema(
        {
            "pattern": {"type": "string", "description": "glob 模式，默认 **/*"},
            "path": {"type": "string", "description": "起始目录，默认工作区根目录"},
        },
        [],
    ),
    handler=list_files,
    kind="read",
)


# ---- edit_file ----


def edit_file(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    path = ctx.resolve(args["path"])
    old, new = args["old_str"], args["new_str"]
    replace_all = bool(args.get("replace_all", False))
    if not path.is_file():
        return ToolOutput(f"文件不存在：{args['path']}。新建文件请用 create_file。", True)
    if old == new:
        return ToolOutput("old_str 与 new_str 相同，没有需要修改的内容。", True)
    if old == "":
        return ToolOutput("old_str 不能为空。", True)
    text = path.read_bytes().decode("utf-8")
    count = text.count(old)
    if count == 0:
        return ToolOutput("没有找到 old_str。请先用 read_file 确认原文（注意缩进、空白和换行）。", True)
    if count > 1 and not replace_all:
        lines = sorted({text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old), text)})
        return ToolOutput(
            f"old_str 出现了 {count} 次（起始行：{', '.join(map(str, lines))}）。"
            "请在 old_str 中包含更多上下文使其唯一，或设置 replace_all=true。",
            True,
        )
    first = text.index(old)
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_bytes(new_text.encode("utf-8"))
    return ToolOutput(
        f"已修改 {args['path']}（替换 {count if replace_all else 1} 处）。\n" + _snippet(new_text, first, new)
    )


def _snippet(text: str, start: int, inserted: str) -> str:
    """修改处前后各若干行的带行号片段，让模型确认改动是否符合预期。"""
    lines = text.splitlines()
    first_line = text.count("\n", 0, start)  # 从 0 开始的行下标
    last_line = first_line + inserted.rstrip("\n").count("\n")
    lo = max(0, first_line - SNIPPET_CONTEXT_LINES)
    hi = min(len(lines), last_line + SNIPPET_CONTEXT_LINES + 1)
    return numbered(lines[lo:hi], lo + 1)


EDIT_FILE = Tool(
    name="edit_file",
    description=(
        "通过精确的字符串替换修改已有文件：把 old_str 替换为 new_str。"
        "修改前先用 read_file 确认上下文；old_str 必须与原文逐字一致（含缩进），"
        "并包含足够的上下文以唯一匹配，否则会报错并列出所有匹配的行号。"
        "确实要替换所有出现时设 replace_all=true。成功后返回修改处前后的带行号片段。"
    ),
    input_schema=object_schema(
        {
            "path": {"type": "string", "description": "相对工作区根目录的文件路径"},
            "old_str": {"type": "string", "description": "要被替换的原文"},
            "new_str": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有出现，默认 false"},
        },
        ["path", "old_str", "new_str"],
    ),
    handler=edit_file,
    kind="edit",
)


# ---- create_file ----


def create_file(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    path = ctx.resolve(args["path"])
    if path.exists() and not args.get("overwrite", False):
        return ToolOutput(f"{args['path']} 已存在。修改已有文件请用 edit_file；确需整体覆盖时设 overwrite=true。", True)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = args["content"]
    path.write_text(content, encoding="utf-8")
    return ToolOutput(f"已写入 {args['path']}（{len(content.splitlines())} 行）。")


CREATE_FILE = Tool(
    name="create_file",
    description=(
        "创建新文件并写入完整内容，自动创建父目录。只用于新文件；"
        "修改已有文件请用 edit_file（文件已存在时会报错，除非设 overwrite=true）。"
    ),
    input_schema=object_schema(
        {
            "path": {"type": "string", "description": "相对工作区根目录的文件路径"},
            "content": {"type": "string", "description": "文件的完整内容"},
            "overwrite": {"type": "boolean", "description": "文件已存在时是否覆盖，默认 false"},
        },
        ["path", "content"],
    ),
    handler=create_file,
    kind="edit",
)
