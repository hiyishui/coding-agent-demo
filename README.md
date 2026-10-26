# minicode：一个运行在终端里的编程 Agent

本项目是文章《如何设计和开发一款高效实用的编程 Agent》的参考实现：用 Python 从零实现一个终端编程 Agent，能读代码、搜代码、改代码、跑命令，并在权限策略和人工审批的约束下完成任务。

文章链接：待发布

- **不依赖 Agent 框架**：Agent 循环、工具、状态、权限全部自己实现，代码量小，适合对照文章阅读。
- **多模型**：中立消息模型 + Anthropic / OpenAI 兼容（OpenAI、DeepSeek、通义千问、Ollama、vLLM……）/ Mock 三个适配器。默认使用 **Mock 模型**回放脚本，没有 API Key 也能完整演示。
- **可控**：工作区路径守卫、命令分类（硬拒绝 / 联网 / 白名单）、四种运行模式、人工审批、步数 / token / 费用预算。
- **可恢复**：会话以 JSONL 事件日志持久化，状态由纯函数从日志重建，`--resume` 从中断处继续。
- **上下文工程**：AGENTS.md 项目指令、按需检索、工具输出截断、自动压缩、todo 清单、只读 explore 子代理。
- **MCP**：最小 stdio MCP 客户端（只支持 2026-07-28 版规范），把外部工具并入工具注册表。
- 163 个单元 / 集成测试，全部离线，几秒跑完。

设计说明见 [docs/design.md](./docs/design.md)。

## 快速开始

需要 Python ≥ 3.12 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv sync    # 安装依赖
```

### 离线演示：让 Mock 模型修一个 bug

[examples/buggy-calc](./examples/buggy-calc) 是一个带 bug 的小项目（`factorial` 有差一错误，`test_factorial` 失败）。
[fix_buggy_calc.json](./examples/mock_scripts/fix_buggy_calc.json) 是一段预先写好的模型响应：列计划 → 看文件 → 读代码 → 跑测试（失败）→ 修改 → 再跑测试（通过）→ 更新清单 → 总结。
Mock 模型按顺序回放这些响应，而工具调用是**真实执行**的。

```sh
DEMO=$(mktemp -d)/buggy-calc
cp -r examples/buggy-calc "$DEMO"

# 无头模式：执行一个任务后退出；auto-edit 模式自动允许修改文件，pytest 在命令白名单里
uv run minicode -C "$DEMO" --mode auto-edit \
  --mock-script examples/mock_scripts/fix_buggy_calc.json \
  --task "修复失败的测试"
echo "退出码：$?"                       # 0 = 正常结束

uv run python -m pytest -q "$DEMO"      # 4 passed：bug 真的被修好了
```

过程（流式文本、工具调用、todo 清单）输出到 stderr，最终回答输出到 stdout。会话日志在 `$DEMO/.minicode/sessions/` 下，每行一个事件。

想体验交互式审批，去掉 `--task`、使用默认的 `default` 模式（修改文件时会询问 `[y] 允许一次 [n] 拒绝 [a] 本会话总是允许`）：

```sh
cp -r examples/buggy-calc "$DEMO-2"
uv run minicode -C "$DEMO-2" --mock-script examples/mock_scripts/fix_buggy_calc.json
```

### 接入真实模型

用环境变量（或复制 [.env.example](./.env.example) 为 `.env` 后填写）：

```sh
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...
uv run minicode --provider anthropic -C /path/to/your/project

# OpenAI 兼容服务：改 OPENAI_BASE_URL 即可切换 DeepSeek、通义千问、本地 Ollama 等
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com/v1    # OpenAI 官方则不设置
uv run minicode --provider openai --model deepseek-chat -C /path/to/your/project
```

默认模型名、上下文窗口和单价集中在 [config.py](./src/minicode/config.py) 顶部的 `DEFAULT_MODELS` 与 `MODEL_TABLE`；表里没有的模型按 200k 窗口、未知单价处理（费用预算只按 token 计）。

## 命令行

```text
minicode [-C WORKSPACE] [--provider mock|anthropic|openai] [--model MODEL]
         [--mode plan|default|auto-edit|yolo] [--task TEXT] [--resume SESSION_ID]
         [--allow edit|exec|mcp ...] [--max-steps N] [--max-tokens-total N] [--max-cost-usd X]
         [--mock-script PATH] [--mcp-config PATH]
```

- 交互模式下的斜杠命令：`/mode <m>` 切换模式、`/todos` 查看清单、`/compact` 立即压缩上下文、`/cost` 查看用量与估算费用、`/exit` 退出。按 Ctrl-C 中断当前的模型输出或工具执行，回到输入提示符。
- 无头模式（`--task`）的退出码：`0` 正常结束，`2` 暂停（预算 / 连续失败 / 步数上限），`1` 错误或中断。
- 启动时打印会话 ID，退出时提示 `minicode -C <workspace> --resume <id>`。
- 配置优先级：命令行 > 环境变量 > `.env`（当前目录下）。

## 运行模式与权限

| 工具类别 | plan | default | auto-edit | yolo |
| --- | --- | --- | --- | --- |
| 读取（read_file、list_files、search_code）与 meta（update_todos、explore） | 允许 | 允许 | 允许 | 允许 |
| 修改（edit_file、create_file） | 拒绝 | 审批 | 允许 | 允许 |
| 命令：白名单（pytest、git status/diff/log、ls、grep……，且不含 shell 元字符） | 允许 | 允许 | 允许 | 允许 |
| 命令：其他 | 拒绝 | 审批 | 审批 | 允许 |
| 命令：联网（curl、ssh、git push/pull、pip/npm install、uv sync……） | 拒绝 | 审批 | 审批 | 审批 |
| 命令：硬拒绝（`rm -rf /`、`curl … \| sh`、`git push -f`、mkfs、sudo……） | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| MCP 工具 | 拒绝 | 审批 | 审批 | 允许 |

- 所有文件类工具都经过**路径守卫**：解析 `..` 和符号链接后必须仍在工作区内。
- “本会话总是允许”：修改类与 MCP 按工具名记住，命令按第一个词记住；联网命令不能“总是允许”。
- 无头模式没有人可以审批，需要审批的操作一律拒绝；可以用 `--allow edit exec mcp` 预先批准（`exec` 不包括联网命令）。
- 被拒绝时，模型会收到 `is_error` 的工具结果，说明原因和可行的替代做法。
- 工具定义**始终全部发送**给模型，模式只在执行时生效，这样工具列表稳定，提示词缓存可以命中。

## 项目结构

| 模块 | 文件 | 设计文档 |
| --- | --- | --- |
| 中立消息模型 | [messages.py](./src/minicode/messages.py) | 第 4 节 |
| 模型适配器 | [providers/](./src/minicode/providers)：`base.py`、`anthropic.py`、`openai_compat.py`、`mock.py` | 第 5 节 |
| 事件日志与归约器 | [events.py](./src/minicode/events.py) | 第 6 节 |
| Agent 循环（预算、重试、中断、审批） | [agent.py](./src/minicode/agent.py) | 第 7 节 |
| 工具与注册表 | [tools/](./src/minicode/tools)：`base.py`、`files.py`、`search.py`、`shell.py`、`todos.py` | 第 8 节 |
| 权限：路径守卫、命令分类、决策矩阵 | [permissions.py](./src/minicode/permissions.py) | 第 9 节 |
| 上下文工程：系统提示词、AGENTS.md、token 估算、自动压缩 | [context.py](./src/minicode/context.py) | 第 10 节 |
| explore 只读子代理 | [tools/explore.py](./src/minicode/tools/explore.py) | 第 11 节 |
| MCP 客户端 | [mcp_client.py](./src/minicode/mcp_client.py) | 第 12 节 |
| 命令行、REPL、无头模式 | [cli.py](./src/minicode/cli.py)、[ui.py](./src/minicode/ui.py) | 第 13 节 |
| 配置、模型窗口与单价表 | [config.py](./src/minicode/config.py) | 第 2、13 节 |

```text
examples/
├── buggy-calc/                        带 bug 的示例项目（calc.py、test_calc.py、AGENTS.md）
├── mock_scripts/fix_buggy_calc.json   Mock 模型的回放脚本
├── toy_mcp_server.py                  零依赖的玩具 MCP 服务器（stdio，2026-07-28）
└── mcp.json                           MCP 配置示例
tests/                                 单元与集成测试（全部离线）
```

一次 `run_turn` 的流程（对应 [agent.py](./src/minicode/agent.py)）：

```text
记录 user_message
循环：
    state = reduce(events)                      ← 状态总是从事件日志重建
    超出预算（步数 / token / 费用）→ 记录 paused，返回
    估算上下文 > 窗口 × 0.75 → 压缩，继续
    带重试地调用模型（指数退避 + 抖动，优先 retry_after），记录 assistant_message
    没有工具调用 → 返回最终回答
    对每个工具调用：权限决策 →（需要时）审批 → 执行 → 记录 approval 与 tool_result
    连续 3 次工具失败（审批拒绝不计）→ 记录 paused，返回
```

## 运行测试

```sh
uv run pytest -q
```

测试全部离线运行，不需要 API Key，也不访问网络（Anthropic / OpenAI 适配器用手写的响应样例和 SDK 的 MockTransport 测试；MCP 测试启动本地的玩具服务器子进程）。重试退避通过注入的 `sleep` 跳过等待。

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_permissions.py` | 路径越界（`..`、绝对路径、符号链接）、命令分类、模式决策矩阵、“总是允许”、`--allow` |
| `test_tools.py` | 各工具的分页、忽略规则、上限、错误提示、超时、截断；search_code 的 rg 与 Python 两条分支 |
| `test_events.py` | JSONL 往返、消息重建、tool_result 合并、压缩替换、悬空调用补结果、resume |
| `test_agent.py` | buggy-calc 端到端修复、审批拒绝、连续失败暂停、步数与预算、退避重试、中断后的配对 |
| `test_context.py` | 系统提示词与 AGENTS.md、token 估算、压缩触发与切分点、压缩后 todos 仍在 |
| `test_providers.py` | 两个适配器的请求构造与响应解析、流式拼接、错误分类、Mock 回放 |
| `test_explore.py` | 子代理只能用只读工具，主会话只收到最终结论 |
| `test_mcp.py` | discover / list（分页）/ call、`mcp__toy__*` 注册与调用、错误映射、版本不匹配 |
| `test_cli.py` | 无头模式退出码、配置优先级、模型表回退 |

## 在容器中运行（yolo 模式）

yolo 模式会自动执行大多数命令，建议放在容器里跑：容器只挂载项目目录，最坏情况也只影响这个目录（配合 git 可以回滚）。下面的例子把 minicode 源码只读挂载到 `/opt/minicode`，项目挂载到 `/workspace`：

```sh
docker run --rm -it \
  -v /path/to/coding-agent-demo:/opt/minicode:ro \
  -v "$PWD":/workspace \
  -e UV_PROJECT_ENVIRONMENT=/opt/venv \
  -e ANTHROPIC_API_KEY \
  ghcr.io/astral-sh/uv:python3.13-bookworm-slim \
  uv run --project /opt/minicode minicode -C /workspace --provider anthropic --mode yolo
```

- `UV_PROJECT_ENVIRONMENT` 把虚拟环境放到容器内的可写路径（源码目录是只读挂载的）。
- 联网命令在 yolo 模式下仍然需要审批；使用 Mock 模型时可以再加 `--network none` 彻底断网。
- 如果要在容器里运行项目自己的测试，请基于上面的镜像写一个装好项目依赖的 Dockerfile。

## MCP

minicode 启动时读取 `<workspace>/.minicode/mcp.json`（或 `--mcp-config` 指定的文件），格式：

```json
{
  "servers": {
    "toy": {
      "command": "python",
      "args": ["examples/toy_mcp_server.py"],
      "env": {}
    }
  }
}
```

服务器以工作区为当前目录启动，`env` 合并到当前环境变量之上。对每个服务器，客户端先调用 `server/discover` 确认支持 `2026-07-28`，再用 `tools/list`（处理 `nextCursor` 分页）收集工具，注册为 `mcp__<server>__<tool>`，描述前加 `[MCP:<server>]`。2026-07-28 版没有 `initialize` 握手，每个请求都在 `params._meta` 中携带 `io.modelcontextprotocol/protocolVersion`、`io.modelcontextprotocol/clientCapabilities` 和 `io.modelcontextprotocol/clientInfo`。服务器的 stderr 写入 `<workspace>/.minicode/mcp-stderr.log`。

在本仓库根目录试用玩具服务器（MCP 工具在 default 模式下需要审批）：

```sh
uv run minicode --mcp-config examples/mcp.json
```

## 已知限制

- 命令白名单只看命令本身，不检查参数中的路径：例如 `cat /etc/hosts` 属于白名单命令。需要更强的隔离时请在容器中运行。
- explore 子代理和上下文压缩消耗的 token 不计入会话预算。
- Mock 模型只按顺序回放脚本、不理解输入，用于演示和测试 Agent 循环本身。
