"""权限：路径守卫、命令分类、运行模式与审批决策（设计文档第 9 节）。

原则：模型给出的每个操作都要经过确定性的代码检查，而不是靠提示词“请不要……”。
- 路径守卫保证文件类工具碰不到工作区以外的文件（含符号链接逃逸）；
- 命令分类把 shell 命令分成 deny / network / safe / other 四类；
- 决策矩阵按“运行模式 × 工具类别”给出 allow / ask / deny，ask 交给人决定。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .messages import ToolCall

Action = Literal["allow", "ask", "deny"]
CommandClass = Literal["deny", "network", "safe", "other"]


# ---- 路径守卫 ----


def resolve_in_workspace(workspace: Path, path: str | Path) -> Path:
    """把（相对或绝对）路径解析为工作区内的真实路径，越界则抛 PermissionError。"""
    root = workspace.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()  # 解析 .. 与符号链接
    if not resolved.is_relative_to(root):
        raise PermissionError(f"路径超出工作区：{path}")
    return resolved


# ---- 命令分类 ----

_DENY_PATTERNS = [
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b",  # curl ... | sh
    r"\bgit\s+push\b.*\s(--force\S*|-f)(\s|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b.*\bof=/dev/",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork 炸弹 :(){ :|:& };:
]

_RM_DANGEROUS_TARGETS = {"/", "/*", "~", "~/", "~/*", "*", "$HOME", "$HOME/", "$HOME/*"}

_NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp", "rsync", "nc"}
_NETWORK_SUBCOMMANDS = {
    "git": {"push", "pull", "fetch", "clone"},
    "pip": {"install"},
    "pip3": {"install"},
    "uv": {"add", "sync"},
    "npm": {"install", "add", "i"},
    "pnpm": {"install", "add", "i"},
    "yarn": {"install", "add"},
}

_SAFE_PREFIXES = [
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("git", "branch"),
    ("ls",),
    ("pwd",),
    ("rg",),
    ("grep",),
    ("head",),
    ("tail",),
    ("wc",),
    ("cat",),
]

_SHELL_META = re.compile(r"[;&|><`\n]|\$")

# 切分命令片段：管道、&&、||、后台 &、分号、换行、子 shell、命令替换
_SEGMENT_SPLIT = re.compile(r"\|\|?|&&?|;|\n|\$\(|[()`]")
# 包装命令：真正执行的是它后面的命令，判断前先剥掉
_WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "time", "nice", "timeout", "xargs", "stdbuf"}
_SHELLS = {"sh", "bash", "zsh", "dash"}
# git 的全局选项出现在子命令之前，其中这些还带一个值
_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}

# 白名单命令里能执行其他程序或写文件的参数：出现即降级为 other
_UNSAFE_FLAGS: dict[str, tuple[str, ...]] = {
    "rg": ("--pre", "-z", "--search-zip", "--hostname-bin"),
    "git": ("--output", "--ext-diff", "--textconv", "--exec-path"),
}
_GIT_BRANCH_LIST_FLAGS = {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--list", "--show-current"}


def classify_command(command: str, workspace: Path | None = None) -> CommandClass:
    text = command.strip()
    risk = _risk(text)
    if risk:
        return risk
    if not _SHELL_META.search(text):
        words = _split(text)
        if (
            any(tuple(words[: len(prefix)]) == prefix for prefix in _SAFE_PREFIXES)
            and not _has_unsafe_flags(words)
            and _args_stay_inside(words[1:], workspace)
        ):
            return "safe"
    return "other"


def _risk(text: str, depth: int = 0) -> Literal["deny", "network"] | None:
    """整条命令里最危险的片段：deny 优先于 network；都没有返回 None。"""
    if any(re.search(p, text) for p in _DENY_PATTERNS):
        return "deny"
    found: Literal["deny", "network"] | None = None
    for segment in _SEGMENT_SPLIT.split(text):
        words = _normalize(_split(segment))
        if not words:
            continue
        name, rest = Path(words[0]).name, words[1:]
        if name == "sudo" or _is_dangerous_rm(words) or _is_force_push(words):
            return "deny"
        inner = _inner_script(name, rest)
        if inner is not None:  # bash -c "..."、eval ...：递归检查里面的命令
            if depth >= 3:  # 层层嵌套多半是在绕过检查，按最坏情况处理
                return "deny"
            sub = _risk(inner, depth + 1)
            if sub == "deny":
                return "deny"
            found = found or sub
        elif _is_network(words):
            found = "network"
    return found


def _normalize(words: list[str]) -> list[str]:
    """剥掉环境变量赋值、包装命令（env、timeout、xargs……）和 git 全局选项，露出真正的命令。"""
    words = list(words)
    while words:
        if re.match(r"^[A-Za-z_]\w*=", words[0]):
            words = words[1:]
        elif Path(words[0]).name in _WRAPPERS:
            words = words[1:]
            while words and (words[0].startswith("-") or re.match(r"^[A-Za-z_]\w*=|^\d+(\.\d+)?[smhd]?$", words[0])):
                words = words[1:]
        else:
            break
    if words and Path(words[0]).name == "git":
        rest = words[1:]
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if option in _GIT_GLOBAL_WITH_VALUE and rest:
                rest.pop(0)
        words = [words[0], *rest]
    return words


def _inner_script(name: str, rest: list[str]) -> str | None:
    if name in _SHELLS and "-c" in rest and rest.index("-c") + 1 < len(rest):
        return rest[rest.index("-c") + 1]
    if name == "eval":
        return " ".join(rest)
    return None


def _is_force_push(words: list[str]) -> bool:
    if Path(words[0]).name != "git" or words[1:2] != ["push"]:
        return False
    return any(w in ("-f", "--force") or w.startswith(("--force", "+")) for w in words[2:])


def _has_unsafe_flags(words: list[str]) -> bool:
    name, args = Path(words[0]).name, words[1:]
    if name == "git" and args[:1] == ["branch"]:  # git branch 只允许列出分支，-D、-f、-m 等一律降级
        return any(a not in _GIT_BRANCH_LIST_FLAGS for a in args[1:])
    for flag in _UNSAFE_FLAGS.get(name, ()):
        for arg in args:
            if arg == flag or arg.startswith(flag + "="):
                return True
            # 合并的短选项，如 rg -iz
            if len(flag) == 2 and arg.startswith("-") and not arg.startswith("--") and flag[1] in arg[1:]:
                return True
    return False


def _args_stay_inside(args: list[str], workspace: Path | None) -> bool:
    """白名单命令的每个非选项参数都按路径解析（含符号链接），指向工作区外则降级为 other 走审批。"""
    for arg in args:
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if value.startswith("-") or not value:
            continue
        if workspace is None:
            if value.startswith(("/", "~")) or ".." in Path(value).parts:
                return False
            continue
        try:
            resolve_in_workspace(workspace, value)
        except PermissionError:
            return False
        except (OSError, ValueError):  # 不是合法路径的参数（如正则模式），不涉及文件
            continue
    return True


def _is_dangerous_rm(words: list[str]) -> bool:
    """rm 的递归删除，目标是根目录、家目录或通配符 *。"""
    if Path(words[0]).name != "rm":
        return False
    flags = [w for w in words[1:] if w.startswith("-")]
    recursive = "--recursive" in flags or any("r" in f.lower() for f in flags if not f.startswith("--"))
    return recursive and any(w in _RM_DANGEROUS_TARGETS for w in words[1:])


def _is_network(words: list[str]) -> bool:
    name, rest = Path(words[0]).name, words[1:]
    if name in _NETWORK_COMMANDS:
        return True
    if name == "uv" and rest[:2] == ["pip", "install"]:
        return True
    return bool(rest) and rest[0] in _NETWORK_SUBCOMMANDS.get(name, set())


def _split(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:  # 引号不配对等情况：退回按空白切分
        return segment.split()


def command_head(command: str) -> str:
    """命令的第一个词，用于“本会话总是允许”的记忆。"""
    words = _split(command)
    return Path(words[0]).name if words else ""


# ---- 决策 ----


@dataclass
class Decision:
    action: Action
    reason: str = ""
    column: str = ""  # 决策矩阵的列：read / meta / edit / mcp / safe / other / network / deny


@dataclass
class ApprovalAnswer:
    allow: bool
    remember: bool = False
    reason: str = ""


class Approver(Protocol):
    name: str  # 写进 approval 事件的 by 字段：user / headless

    def ask(self, call: ToolCall, decision: Decision) -> ApprovalAnswer: ...


class HeadlessApprover:
    """无头模式：没有人可以问，ask 一律视为拒绝。"""

    name = "headless"

    def ask(self, call: ToolCall, decision: Decision) -> ApprovalAnswer:
        return ApprovalAnswer(
            allow=False,
            reason=f"无头模式下无法请求人工审批（{decision.reason}）。可用 --allow 预先批准该类操作。",
        )


# 模式 × 类别 的决策矩阵（与设计文档第 9 节的表格一一对应；read、meta 总是允许，deny 类命令总是拒绝）
MATRIX: dict[str, dict[str, Action]] = {
    "plan": {"edit": "deny", "safe": "allow", "other": "deny", "network": "deny", "mcp": "deny"},
    "default": {"edit": "ask", "safe": "allow", "other": "ask", "network": "ask", "mcp": "ask"},
    "auto-edit": {"edit": "allow", "safe": "allow", "other": "ask", "network": "ask", "mcp": "ask"},
    "yolo": {"edit": "allow", "safe": "allow", "other": "allow", "network": "ask", "mcp": "allow"},
}

# --allow 的类别对应矩阵的哪一列；联网命令不在其中，永远需要人工审批
PREAPPROVABLE = {"edit": "edit", "exec": "other", "mcp": "mcp"}

DENY_COMMAND_HINT = (
    "该命令在硬拒绝清单中（递归删除根/家目录、curl|sh、强制推送、mkfs、sudo 等），"
    "任何模式都不会执行；请换一种安全的做法"
)
PLAN_MODE_HINT = "当前为 plan 模式，只能读取；请先给出修改计划，等用户切换模式后再修改"


@dataclass
class PermissionPolicy:
    mode: str = "default"
    preapproved: frozenset[str] = frozenset()  # 命令行 --allow 给出的类别：edit / exec / mcp
    remembered_tools: set[str] = field(default_factory=set)  # edit、mcp 类按工具名记住
    remembered_commands: set[str] = field(default_factory=set)  # exec 类按命令的第一个词记住
    workspace: Path | None = None  # 用于检查白名单命令参数中的路径；Agent 构造时会自动填入

    def decide(self, kind: str, call: ToolCall) -> Decision:
        column = self._column(kind, call)
        if column in ("read", "meta"):
            return Decision("allow", column=column)
        if column == "deny":
            return Decision("deny", DENY_COMMAND_HINT, column)

        action = MATRIX[self.mode][column]
        if action == "deny":
            reason = PLAN_MODE_HINT if self.mode == "plan" else f"{self.mode} 模式不允许此操作"
            return Decision("deny", reason, column)
        if action == "ask":
            if self._remembered(column, call):
                return Decision("allow", "本会话已选择总是允许", column)
            if column in {PREAPPROVABLE.get(kind_) for kind_ in self.preapproved}:
                return Decision("allow", "命令行 --allow 预先批准", column)
            reason = "联网命令总是需要审批" if column == "network" else f"{self.mode} 模式下需要审批"
            return Decision("ask", reason, column)
        return Decision("allow", column=column)

    def remember(self, decision: Decision, call: ToolCall) -> bool:
        """记住“本会话总是允许”。联网命令不能被记住，返回 False。"""
        if decision.column in ("edit", "mcp"):
            self.remembered_tools.add(call.name)
            return True
        if decision.column == "other":
            self.remembered_commands.add(command_head(_command_of(call)))
            return True
        return False

    def _column(self, kind: str, call: ToolCall) -> str:
        return classify_command(_command_of(call), self.workspace) if kind == "exec" else kind

    def _remembered(self, column: str, call: ToolCall) -> bool:
        if column == "other":
            return command_head(_command_of(call)) in self.remembered_commands
        return column in ("edit", "mcp") and call.name in self.remembered_tools


def _command_of(call: ToolCall) -> str:
    return str(call.arguments.get("command", ""))
