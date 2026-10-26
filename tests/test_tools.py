"""工具：read_file、list_files、search_code、edit_file、create_file、run_command、注册表与截断。"""

from __future__ import annotations

import subprocess
import time

import pytest

from minicode.messages import ToolCall
from minicode.tools import builtin_registry, search
from minicode.tools.base import ToolContext, truncate, validate_arguments
from minicode.tools.files import create_file, edit_file, list_files, read_file
from minicode.tools.search import search_code
from minicode.tools.shell import run_command
from minicode.tools.todos import update_todos


def write(ws, rel, text):
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- read_file ----


def test_read_file_numbers_lines_and_paginates(workspace, ctx):
    write(workspace, "big.txt", "\n".join(f"line {i}" for i in range(1, 11)))
    out = read_file({"path": "big.txt", "offset": 3, "limit": 4}, ctx)
    assert not out.is_error
    assert "     3\tline 3" in out.content
    assert "     6\tline 6" in out.content and "line 7" not in out.content
    assert "显示第 3–6 行，共 10 行；继续读请用 offset=7" in out.content


def test_read_file_last_page_has_no_continue_hint(workspace, ctx):
    write(workspace, "a.txt", "one\ntwo\n")
    out = read_file({"path": "a.txt"}, ctx)
    assert "显示第 1–2 行，共 2 行" in out.content and "offset=" not in out.content


def test_read_file_rejects_binary_missing_and_outside(workspace, ctx):
    (workspace / "bin.dat").write_bytes(b"\x00\x01\x02")
    assert read_file({"path": "bin.dat"}, ctx).is_error
    assert read_file({"path": "nope.txt"}, ctx).is_error
    write(workspace, "a.txt", "x")
    assert read_file({"path": "a.txt", "offset": 5}, ctx).is_error
    with pytest.raises(PermissionError):
        read_file({"path": "../x"}, ctx)


# ---- list_files ----


def test_list_files_skips_ignored_dirs(workspace, ctx):
    for rel in ["src/app.py", "src/util.py", "README.md", "node_modules/x/index.js", ".venv/lib/a.py"]:
        write(workspace, rel, "x")
    write(workspace, "src/__pycache__/app.cpython.pyc", "x")
    write(workspace, ".minicode/sessions/s.jsonl", "{}")
    out = list_files({}, ctx).content.splitlines()
    assert out == ["README.md", "src/app.py", "src/util.py"]


def test_list_files_pattern_and_path(workspace, ctx):
    for rel in ["src/app.py", "src/sub/deep.py", "docs/a.md", "test_x.py"]:
        write(workspace, rel, "x")
    assert list_files({"pattern": "**/*.py"}, ctx).content.splitlines() == [
        "src/app.py",
        "src/sub/deep.py",
        "test_x.py",
    ]
    assert list_files({"pattern": "test_*.py"}, ctx).content == "test_x.py"
    assert list_files({"pattern": "*.py", "path": "src"}, ctx).content.splitlines() == ["src/app.py", "src/sub/deep.py"]
    assert "没有匹配" in list_files({"pattern": "*.rs"}, ctx).content


def test_list_files_respects_gitignore_in_git_repo(workspace, ctx):
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    write(workspace, ".gitignore", "build/\n")
    write(workspace, "main.py", "x")
    write(workspace, "build/out.txt", "x")
    assert list_files({}, ctx).content.splitlines() == [".gitignore", "main.py"]


def test_list_files_limit(workspace, ctx):
    for i in range(505):
        write(workspace, f"f{i:03}.txt", "")
    out = list_files({}, ctx).content
    assert len(out.splitlines()) == 501
    assert "共 505 个文件，只显示前 500 个" in out


# ---- search_code ----


@pytest.fixture
def code_ws(workspace):
    write(workspace, "calc.py", "def add(a, b):\n    return a + b\n\ndef Sub(a, b):\n    return a - b\n")
    write(workspace, "notes.md", "add more tests\n")
    write(workspace, "node_modules/lib.js", "function add() {}\n")
    return workspace


@pytest.fixture(params=["python", "rg"])
def search_branch(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(search, "find_rg", lambda: None)
    elif search.find_rg() is None:
        pytest.skip("本机没有安装 rg")
    return request.param


def test_search_code_basic(code_ws, ctx, search_branch):
    out = search_code({"pattern": r"def \w+"}, ctx).content.splitlines()
    assert out == ["calc.py:1:def add(a, b):", "calc.py:4:def Sub(a, b):"]


def test_search_code_glob_case_and_path(code_ws, ctx, search_branch):
    assert search_code({"pattern": "add", "glob": "*.md"}, ctx).content == "notes.md:1:add more tests"
    assert "calc.py:4:" in search_code({"pattern": "def sub", "case_insensitive": True}, ctx).content
    assert "没有找到" in search_code({"pattern": "def sub"}, ctx).content
    assert search_code({"pattern": "return a", "path": "calc.py"}, ctx).content.count("\n") == 1


def test_search_code_limit_and_bad_regex(workspace, ctx, search_branch):
    write(workspace, "many.txt", "hit\n" * 150)
    out = search_code({"pattern": "hit"}, ctx).content
    assert len(out.splitlines()) == 101 and "结果超过 100 条" in out
    assert search_code({"pattern": "("}, ctx).is_error


# ---- edit_file ----


def test_edit_file_errors(workspace, ctx):
    write(workspace, "a.py", "x = 1\ny = 1\n")
    assert "不存在" in edit_file({"path": "b.py", "old_str": "x", "new_str": "y"}, ctx).content
    assert "相同" in edit_file({"path": "a.py", "old_str": "x", "new_str": "x"}, ctx).content
    assert "没有找到" in edit_file({"path": "a.py", "old_str": "z = 1", "new_str": "z = 2"}, ctx).content
    out = edit_file({"path": "a.py", "old_str": "= 1", "new_str": "= 2"}, ctx)
    assert out.is_error and "出现了 2 次" in out.content and "1, 2" in out.content
    assert (workspace / "a.py").read_text() == "x = 1\ny = 1\n"


def test_edit_file_replace_all(workspace, ctx):
    write(workspace, "a.py", "x = 1\ny = 1\n")
    out = edit_file({"path": "a.py", "old_str": "= 1", "new_str": "= 2", "replace_all": True}, ctx)
    assert not out.is_error and "替换 2 处" in out.content
    assert (workspace / "a.py").read_text() == "x = 2\ny = 2\n"


def test_edit_file_success_snippet(workspace, ctx):
    write(workspace, "a.py", "".join(f"line{i}\n" for i in range(1, 21)))
    out = edit_file({"path": "a.py", "old_str": "line10\n", "new_str": "LINE10\nextra\n"}, ctx)
    assert not out.is_error
    lines = out.content.splitlines()[1:]
    assert lines[0] == "     7\tline7"
    assert "    10\tLINE10" in lines and "    11\textra" in lines
    assert lines[-1] == "    14\tline13"


# ---- create_file ----


def test_create_file_and_overwrite_protection(workspace, ctx):
    out = create_file({"path": "pkg/new.py", "content": "a\nb\n"}, ctx)
    assert not out.is_error and (workspace / "pkg/new.py").read_text() == "a\nb\n"
    again = create_file({"path": "pkg/new.py", "content": "c"}, ctx)
    assert again.is_error and "edit_file" in again.content
    assert not create_file({"path": "pkg/new.py", "content": "c", "overwrite": True}, ctx).is_error
    assert (workspace / "pkg/new.py").read_text() == "c"


# ---- run_command ----


def test_run_command_exit_code_and_streams(ctx):
    ok = run_command({"command": "echo out; echo err >&2"}, ctx)
    assert not ok.is_error
    assert "exit code: 0" in ok.content and "out" in ok.content and "err" in ok.content
    bad = run_command({"command": "exit 3"}, ctx)
    assert bad.is_error and "exit code: 3" in bad.content


def test_run_command_runs_in_workspace(workspace, ctx):
    assert str(workspace.resolve()) in run_command({"command": "pwd -P"}, ctx).content


def test_run_command_timeout_kills_process_group(ctx):
    started = time.monotonic()
    out = run_command({"command": "sleep 30 & sleep 30", "timeout_sec": 1}, ctx)
    assert out.is_error and "超时" in out.content
    assert time.monotonic() - started < 10


def test_run_command_truncates_each_stream(workspace):
    ctx = ToolContext(workspace=workspace, max_output_chars=1000)
    out = run_command({"command": "python3 -c \"print('x' * 5000)\""}, ctx)
    assert "已省略" in out.content
    assert len(out.content) < 1500


# ---- update_todos ----


def test_update_todos_validation_and_callback(ctx):
    received = []
    ctx.on_todos = received.append
    ok = update_todos(
        {"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}]}, ctx
    )
    assert not ok.is_error and received == [
        [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}]
    ]
    two = [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]
    assert update_todos({"todos": two}, ctx).is_error
    assert update_todos({"todos": [{"content": "a", "status": "doing"}]}, ctx).is_error
    assert len(received) == 1


# ---- 注册表与截断 ----


def test_registry_handles_unknown_tool_bad_args_and_exceptions(workspace):
    registry = builtin_registry()
    ctx = ToolContext(workspace=workspace)
    unknown = registry.run(ToolCall(id="1", name="nope", arguments={}), ctx)
    assert unknown.is_error and "未知工具" in unknown.content
    missing = registry.run(ToolCall(id="2", name="read_file", arguments={}), ctx)
    assert missing.is_error and "缺少必填参数：path" in missing.content
    wrong_type = registry.run(ToolCall(id="3", name="read_file", arguments={"path": "a", "limit": "5"}), ctx)
    assert wrong_type.is_error and "limit" in wrong_type.content
    raw = registry.run(ToolCall(id="4", name="read_file", arguments={"_raw": "{bad"}), ctx)
    assert raw.is_error and "不是合法的 JSON" in raw.content
    outside = registry.run(ToolCall(id="5", name="read_file", arguments={"path": "/etc/hosts"}), ctx)
    assert outside.is_error and "路径超出工作区" in outside.content


def test_validate_arguments_bool_is_not_integer():
    schema = {"properties": {"n": {"type": "integer"}}, "required": []}
    assert validate_arguments(schema, {"n": True}) is not None
    assert validate_arguments(schema, {"n": 3}) is None


def test_truncate_keeps_head_and_tail():
    text = "A" * 1000 + "B" * 1000
    out = truncate(text, 100)
    assert out.startswith("A" * 70) and out.endswith("B" * 30)
    assert "已省略 1900 个字符" in out
    assert truncate("short", 100) == "short"


def test_registry_truncates_output(workspace):
    write(workspace, "big.txt", "\n".join("y" * 100 for _ in range(400)))
    ctx = ToolContext(workspace=workspace, max_output_chars=2000)
    out = builtin_registry().run(ToolCall(id="1", name="read_file", arguments={"path": "big.txt"}), ctx)
    assert "已省略" in out.content and len(out.content) < 2200
