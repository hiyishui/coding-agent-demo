"""测试共用的工具函数与 fixture。所有测试离线运行，不访问网络。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from minicode.agent import Agent, AgentConfig
from minicode.events import SessionLog
from minicode.permissions import HeadlessApprover, PermissionPolicy
from minicode.providers.mock import ScriptedProvider
from minicode.tools import builtin_registry
from minicode.tools.base import ToolContext

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
FIX_SCRIPT = EXAMPLES / "mock_scripts" / "fix_buggy_calc.json"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def buggy_calc(tmp_path: Path) -> Path:
    target = tmp_path / "buggy-calc"
    shutil.copytree(EXAMPLES / "buggy-calc", target)
    return target


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace)


def load_fix_script() -> list[dict[str, Any]]:
    return json.loads(FIX_SCRIPT.read_text(encoding="utf-8"))


def make_agent(
    workspace: Path,
    script: list[dict[str, Any]] | None = None,
    *,
    provider: Any = None,
    mode: str = "default",
    log: SessionLog | None = None,
    approver: Any = None,
    registry: Any = None,
    **config: Any,
) -> Agent:
    return Agent(
        provider=provider or ScriptedProvider(script or []),
        registry=registry or builtin_registry(),
        log=log or SessionLog.create(workspace, {"provider": "mock", "model": "mock", "mode": mode}),
        system_prompt="你是测试用的 Agent。",
        workspace=workspace,
        policy=PermissionPolicy(mode=mode),
        approver=approver or HeadlessApprover(),
        config=AgentConfig(**config),
        sleep=lambda seconds: None,
    )


def call(name: str, **arguments: Any) -> dict[str, Any]:
    """Mock 脚本中的一个工具调用。"""
    return {"name": name, "arguments": arguments}
