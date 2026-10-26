"""配置（设计文档第 2、13 节）：命令行 > 环境变量 > .env。

模型标识、上下文窗口和单价集中放在本文件顶部，业务代码只通过 `model_info()`
和 `estimate_cost()` 使用它们，换模型时只改这里。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .messages import Usage

# ---- 模型默认值与参数表（2026-10 按厂商文档核实，价格会变动，以官方为准）----
# Anthropic: https://platform.claude.com/docs/en/models/overview
# OpenAI:    https://developers.openai.com/api/docs/models/gpt-5.6-terra
# 注意：gpt-6-astra、gpt-6.1-sol 只支持经 Responses API 调用工具，本项目的 OpenAI 适配器走
# Chat Completions，所以默认选用支持 Chat Completions 的 gpt-5.6-terra。

DEFAULT_MODELS: dict[str, str] = {
    "mock": "mock",
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-5.6-terra",
}


@dataclass(frozen=True)
class ModelInfo:
    context_window: int
    input_usd_per_mtok: float | None = None  # 每百万输入 token 的美元单价；None 表示未知
    output_usd_per_mtok: float | None = None


MODEL_TABLE: dict[str, ModelInfo] = {
    # Mock 的窗口故意设得很小，便于演示和测试自动压缩
    "mock": ModelInfo(context_window=8_000),
    "claude-opus-5-5": ModelInfo(context_window=1_000_000, input_usd_per_mtok=4.0, output_usd_per_mtok=20.0),
    "claude-sonnet-5-5": ModelInfo(context_window=1_000_000, input_usd_per_mtok=2.0, output_usd_per_mtok=10.0),
    # 输入超过 272K token 时该请求按更高单价计费，这里不细分
    "gpt-5.6-terra": ModelInfo(context_window=1_050_000, input_usd_per_mtok=2.0, output_usd_per_mtok=12.0),
}

UNKNOWN_MODEL = ModelInfo(context_window=200_000)

PROVIDERS = ("mock", "anthropic", "openai")
MODES = ("plan", "default", "auto-edit", "yolo")
ALLOW_KINDS = ("edit", "exec", "mcp")


def model_info(model: str) -> ModelInfo:
    return MODEL_TABLE.get(model, UNKNOWN_MODEL)


def estimate_cost(model: str, usage: Usage) -> float | None:
    """按单价表估算费用（美元）；未知模型返回 None，此时预算只按 token 计。"""
    info = model_info(model)
    if info.input_usd_per_mtok is None or info.output_usd_per_mtok is None:
        return None
    return (usage.input_tokens * info.input_usd_per_mtok + usage.output_tokens * info.output_usd_per_mtok) / 1_000_000


# ---- 运行配置 ----


@dataclass
class Settings:
    workspace: Path = field(default_factory=Path.cwd)
    provider: str = "mock"
    model: str = "mock"
    mode: str = "default"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str | None = None
    allow: frozenset[str] = frozenset()  # 无头模式下预先批准的类别：edit / exec / mcp
    max_steps: int = 50
    max_tokens_total: int | None = None
    max_cost_usd: float | None = None
    max_output_tokens: int = 8192
    max_output_chars: int = 20_000
    mock_script: Path | None = None
    mcp_config: Path | None = None

    @property
    def context_window(self) -> int:
        return model_info(self.model).context_window


def read_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略空行和 # 注释，去掉值两端的引号。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _optional[T](convert: Callable[[str], T], value: object) -> T | None:
    return None if value is None else convert(str(value))


def load_settings(
    cli: Mapping[str, object],
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = Path(".env"),
) -> Settings:
    """合并三层配置。cli 中值为 None 的项视为“未指定”。"""
    env = dict(read_dotenv(dotenv_path)) if dotenv_path else {}
    env.update(os.environ if environ is None else environ)  # 进程环境变量覆盖 .env

    def pick(cli_key: str, env_key: str | None = None, default: object = None) -> object:
        value = cli.get(cli_key)
        if value is not None:
            return value
        if env_key and env.get(env_key):
            return env[env_key]
        return default

    provider = str(pick("provider", "MINICODE_PROVIDER", "mock")).lower()
    if provider not in PROVIDERS:
        raise ValueError(f"未知的 provider：{provider}（可选 {', '.join(PROVIDERS)}）")
    model = str(pick("model", "MINICODE_MODEL", DEFAULT_MODELS[provider]))

    mode = str(pick("mode", default="default"))
    if mode not in MODES:
        raise ValueError(f"未知的运行模式：{mode}（可选 {', '.join(MODES)}）")

    def optional_path(key: str) -> Path | None:
        value = cli.get(key)
        return Path(str(value)).expanduser() if value else None

    return Settings(
        workspace=Path(str(pick("workspace", default="."))).expanduser().resolve(),
        provider=provider,
        model=model,
        mode=mode,
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=env.get("OPENAI_API_KEY", ""),
        openai_base_url=env.get("OPENAI_BASE_URL") or None,
        allow=frozenset(str(kind) for kind in cli.get("allow") or ()),  # type: ignore[union-attr]
        max_steps=int(str(pick("max_steps", default=50))),
        max_tokens_total=_optional(int, cli.get("max_tokens_total")),
        max_cost_usd=_optional(float, cli.get("max_cost_usd")),
        mock_script=optional_path("mock_script"),
        mcp_config=optional_path("mcp_config"),
    )
