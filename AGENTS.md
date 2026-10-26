# AGENTS.md

本仓库是 minicode（一个终端编程 Agent）的参考实现，配合文章《如何设计和开发一款高效实用的编程 Agent》。
`docs/design.md` 是功能范围与实现约定的唯一依据，修改行为前先读对应章节，行为变化时同步更新它。

## 常用命令

- 安装依赖：`uv sync`
- 运行全部测试：`uv run pytest -q`（全部离线，几秒内完成）
- 运行单个文件：`uv run pytest -q tests/test_agent.py`
- 代码检查（可选，不是项目依赖）：`uvx ruff check src tests examples && uvx ruff format --check src tests examples`
- 离线演示：见 README.md 的“快速开始”

## 约定

- Python ≥ 3.12，uv 管理，src 布局，构建后端 `uv_build`。
- 运行依赖只有 anthropic、openai、pydantic、rich；开发依赖只有 pytest。不要引入新依赖。
- 用 `uv add` / `uv add --dev` 管理依赖，不要手改 `uv.lock`。
- 文档、docstring、注释用简体中文；标识符用英文。每个模块的 docstring 说明它对应设计文档的哪一节。
- 注释只写在需要解释“为什么”的地方；优先清晰而不是巧妙——这是教学用的参考实现。
- 测试必须离线运行：不访问网络、不需要 API Key；需要等待的逻辑（重试退避等）通过注入 `sleep` 保持测试快速。
- 行宽 120（见 `pyproject.toml` 中的 ruff 配置）。
- `examples/buggy-calc` 故意带一个 bug（`factorial`），不要修它；`examples/mock_scripts/fix_buggy_calc.json` 依赖它的原文。
