"""命令行与配置：无头模式退出码、配置优先级。"""

from __future__ import annotations

import json

import pytest
from conftest import FIX_SCRIPT, call

from minicode.cli import main
from minicode.config import DEFAULT_MODELS, UNKNOWN_MODEL, estimate_cost, load_settings, model_info
from minicode.messages import Usage


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """不读开发者本地的 .env 和环境变量，保证测试始终使用 Mock、不访问网络。"""
    monkeypatch.chdir(tmp_path)
    for name in ("MINICODE_PROVIDER", "MINICODE_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_headless_fix_exits_zero(buggy_calc, capsys):
    code = main(["-C", str(buggy_calc), "--mode", "auto-edit", "--mock-script", str(FIX_SCRIPT), "--task", "修复测试"])
    out = capsys.readouterr()
    assert code == 0
    assert "range(1, n + 1)" in out.out  # 最终回答在 stdout
    assert "run_command" in out.err  # 过程在 stderr
    assert "--resume" in out.err


def test_headless_pause_exits_two(workspace, tmp_path, capsys):
    script = tmp_path / "loop.json"
    script.write_text(json.dumps([{"text": "看看", "tool_calls": [call("list_files")]}] * 5))
    code = main(["-C", str(workspace), "--mock-script", str(script), "--max-steps", "2", "--task", "看看"])
    assert code == 2


def test_bad_resume_exits_one(workspace, capsys):
    assert main(["-C", str(workspace), "--resume", "nope", "--task", "x"]) == 1


def test_settings_priority(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text('MINICODE_PROVIDER=openai\nMINICODE_MODEL="from-dotenv"\nOPENAI_API_KEY=sk-dotenv\n')
    settings = load_settings({}, environ={}, dotenv_path=dotenv)
    assert (settings.provider, settings.model, settings.openai_api_key) == ("openai", "from-dotenv", "sk-dotenv")

    settings = load_settings({}, environ={"MINICODE_MODEL": "from-env"}, dotenv_path=dotenv)
    assert settings.model == "from-env"

    settings = load_settings(
        {"model": "from-cli", "provider": "anthropic"}, environ={"MINICODE_MODEL": "x"}, dotenv_path=dotenv
    )
    assert (settings.provider, settings.model) == ("anthropic", "from-cli")

    settings = load_settings({"provider": "anthropic"}, environ={}, dotenv_path=None)
    assert settings.model == DEFAULT_MODELS["anthropic"]


def test_model_table_fallback():
    assert model_info("mock").context_window == 8000
    assert model_info("some-unknown-model") == UNKNOWN_MODEL and UNKNOWN_MODEL.context_window == 200_000
    assert estimate_cost("some-unknown-model", Usage(input_tokens=10)) is None
    assert estimate_cost(DEFAULT_MODELS["anthropic"], Usage(input_tokens=1_000_000)) > 0
