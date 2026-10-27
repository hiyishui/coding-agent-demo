# minicode 设计文档

minicode 是文章《如何设计和开发一款高效实用的编程 Agent》的参考实现：一个运行在终端里的编程 Agent，能读代码、搜代码、改代码、跑命令，并在权限策略和人工审批的约束下完成任务。

本文档记录功能范围、架构和每个模块的实现约定，是实现与评审的依据。

## 1. 目标与非目标

### 目标

- **交互式 CLI**：在终端里多轮对话完成编程任务，模型文本流式输出，工具调用与审批实时可见。
- **无头模式**：`--task "..."` 一次性执行任务后退出，退出码表示结果，可接入 CI 与评测。
- **可恢复**：会话以 JSONL 事件日志持久化，`--resume <id>` 从中断处继续。
- **可控**：工作区路径限制、分级审批、硬拒绝清单、四种运行模式、预算护栏。
- **多模型**：中立消息模型 + Anthropic / OpenAI 兼容 / Mock 三个适配器；默认 Mock，无需 API Key 即可完整演示与测试。
- **上下文工程**：AGENTS.md 项目指令、按需检索、工具输出截断、自动压缩、todo 清单、只读探索子代理。
- **MCP**：最小 stdio MCP 客户端（只支持 2026-07-28 版规范），把外部工具并入工具注册表。

### 非目标

- 不使用 LangChain 等 Agent 框架；循环、工具、状态全部自己掌控。
- 不做可写、可并行的多代理；不做向量索引；不内置 Docker 沙箱（README 给出在容器中运行的方法）。
- 不做评测框架（评测方法论写在文章中）；仓库只包含 pytest 单元测试。
- 不支持 MCP 旧版 `initialize` 握手、资源、提示词、Sampling、Elicitation、Roots；MCP 只用工具。

## 2. 技术栈与工程约定

- Python ≥ 3.12，uv 管理，src 布局，构建后端 `uv_build`（与 llm-gateway-demo 一致）。
- 运行依赖：`anthropic`、`openai`、`pydantic`、`rich`。开发依赖：`pytest`。不引入其他依赖。
- 包名 `minicode`，命令行入口 `minicode = "minicode.cli:main"`。
- 文档、docstring、注释使用中文；标识符英文。模块 docstring 说明它对应文章的哪一节、遵循哪条原则。
- 所有测试离线运行，不访问网络，不需要 API Key。

## 3. 目录结构

```text
coding-agent-demo/
├── AGENTS.md                  # 本仓库自己的项目指令（给在本仓库工作的 Agent 看）
├── README.md
├── .env.example
├── pyproject.toml
├── docs/design.md
├── examples/
│   ├── buggy-calc/            # 带 bug 的示例项目：calc.py、test_calc.py、AGENTS.md
│   ├── mock_scripts/
│   │   └── fix_buggy_calc.json  # Mock 模型的回放脚本：修复 buggy-calc
│   ├── toy_mcp_server.py      # 玩具 MCP 服务器（stdio，2026-07-28）
│   └── mcp.json               # MCP 配置示例
├── src/minicode/
│   ├── __init__.py
│   ├── cli.py                 # 参数解析、交互式 REPL、无头模式
│   ├── config.py              # 配置：环境变量 / .env / 命令行；模型窗口与单价表
│   ├── messages.py            # 中立消息模型
│   ├── events.py              # 事件类型、JSONL 会话日志、归约器
│   ├── agent.py               # Agent 循环：调用模型、审批、执行工具、预算、重试
│   ├── permissions.py         # 运行模式、路径守卫、命令分类、审批决策
│   ├── context.py             # 系统提示词、AGENTS.md、token 估算、自动压缩
│   ├── mcp_client.py          # stdio MCP 客户端
│   ├── ui.py                  # rich 渲染、审批提示（可替换为无头实现）
│   ├── providers/
│   │   ├── base.py            # Provider 协议、错误类型
│   │   ├── anthropic.py
│   │   ├── openai_compat.py
│   │   └── mock.py
│   └── tools/
│       ├── base.py            # Tool、ToolRegistry、ToolOutput、截断
│       ├── files.py           # read_file、list_files、edit_file、create_file
│       ├── search.py          # search_code
│       ├── shell.py           # run_command
│       ├── todos.py           # update_todos
│       └── explore.py         # explore 子代理
└── tests/
```

## 4. 中立消息模型（messages.py）

用 pydantic 定义，与任何厂商协议无关：

- `TextBlock(text)`、`ToolCall(id, name, arguments: dict)`、`ToolResult(call_id, content: str, is_error: bool)`。
- `Message(role: "user" | "assistant", blocks: list[TextBlock | ToolCall | ToolResult])`。工具结果放在 user 消息里（与 Anthropic 一致，OpenAI 适配器负责转换）。
- `Usage(input_tokens, output_tokens, cache_read_tokens=0)`。
- `ModelResponse(blocks, stop_reason: "end_turn" | "tool_use" | "max_tokens" | "refusal", usage)`；`tool_calls` 与 `text` 为便捷属性。
- `ToolSpec(name, description, input_schema)`：发给模型的工具定义。

## 5. 模型适配器（providers/）

```python
class Provider(Protocol):
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        on_text: Callable[[str], None] | None = None,
    ) -> ModelResponse: ...
```

- 错误分两类：`RetryableError(retry_after: float | None)`（429、5xx、超时、连接错误）和 `ProviderError`（其余，不重试）。重试在 agent 层统一做，适配器不重试（SDK 自带重试设为 0）。
- **anthropic.py**：Messages API，流式（`messages.stream`），`on_text` 逐段回调。转换：`ToolCall` ↔ `tool_use` 块（`input` 是对象）；`ToolResult` → user 消息里的 `tool_result` 块（`is_error`）。系统提示词与最后一个工具定义加 `cache_control: {"type": "ephemeral"}` 以命中提示词缓存。`stop_reason` 映射：`tool_use`、`end_turn`/`stop_sequence` → `end_turn`、`max_tokens`、`refusal`；`pause_turn` 视为 `end_turn`（不使用服务端工具）。
- **openai_compat.py**：Chat Completions（兼容 OpenAI、DeepSeek、Qwen、Ollama、vLLM），`base_url` 可配置，流式。转换：`ToolCall` ↔ `tool_calls`（`arguments` 是 **JSON 字符串**，解析失败时把原文放进 `{"_raw": ...}` 并由工具层报参数错误）；`ToolResult` → 独立的 `role: "tool"` 消息（`tool_call_id`），`is_error` 时在内容前加 `ERROR: `；同一条 user 消息里的文本块放在 tool 消息之后；助手消息没有文本时，带 tool_calls 则 `content` 为 null，否则为空字符串（null 会被接口拒绝）。流式时按 `index` 累积 `tool_calls` 的增量。`finish_reason` 映射：`tool_calls` → `tool_use`、`length` → `max_tokens`、`content_filter` → `refusal`、其余 → `end_turn`。
- 两个适配器都把“请求体构造”和“响应解析”写成纯函数（如 `to_anthropic_messages`、`from_openai_message`），以便离线单元测试。
- **mock.py**：`ScriptedProvider`，从 JSON 脚本按顺序回放响应，不看输入；脚本项格式：`{"text": "...", "tool_calls": [{"name": "...", "arguments": {...}}]}`，`id` 自动生成；脚本耗尽时返回 `end_turn` 文本“（Mock 脚本已结束）”。另提供 `compact` 用途：当 `complete` 以 `tools=[]` 且系统提示词以压缩标记开头时，不消耗脚本，返回根据消息确定性生成的摘要。usage 按字符数 / 4 估算。`on_text` 按词流式输出。

## 6. 事件日志与归约器（events.py）

会话 = 只追加的事件列表，存为 `<workspace>/.minicode/sessions/<session_id>.jsonl`，每行一个事件 `{"type", "ts", ...}`。

| 事件 | 字段 |
| --- | --- |
| `session_start` | session_id、workspace、provider、model、mode |
| `user_message` | text |
| `assistant_message` | blocks、stop_reason、usage |
| `approval` | call_id、tool、decision（allow / deny）、by（policy / user / headless）、reason |
| `tool_result` | call_id、content、is_error、usage（可选：工具内部调用模型的用量，如 explore 子代理） |
| `todos_updated` | todos |
| `compaction` | summary、upto（被摘要替换的最后一个事件下标）、usage（压缩调用的用量） |
| `interrupted` | reason |
| `paused` | reason（budget / consecutive_failures / max_steps） |

`reduce(events) -> SessionState`（纯函数）：

- `messages`：按事件重建中立消息；相邻的 `tool_result` 合并为同一条 user 消息；遇到 `compaction` 时，丢弃 `upto` 之前的消息，以一条内容为“【此前对话的摘要】…”的 user 消息开头。
- `todos`、累计 `usage`（含 tool_result 与 compaction 中的旁路用量，只计入预算，不参与上下文大小估算）、`steps`（assistant_message 数）、`consecutive_failures`。
- 不变式：每个 `ToolCall` 后都有对应的 `ToolResult`。归约时如发现悬空的工具调用（例如进程被杀），补一条 `is_error=True`、内容为“操作已中断，未执行”的结果。

`SessionLog`：`create(workspace, meta)`、`load(workspace, session_id)`、`append(event)`（追加后立即 flush）。

## 7. Agent 循环（agent.py）

```text
run_turn(user_text):
    log user_message
    while True:
        state = reduce(events)
        if 超出预算（步数 / token / 费用）: log paused; return
        if 估算上下文 > 窗口 × 0.75: compact(); continue
        resp = 带重试地调用 provider.complete(system, state.messages, registry.specs())
        log assistant_message
        if resp.stop_reason != "tool_use": return resp.text
        for call in resp.tool_calls:
            decision = policy.decide(call)           # allow / ask / deny
            if ask: decision = approver.ask(call)     # 无头模式 → deny
            log approval
            output = registry.run(call) if allow else 拒绝说明（is_error）
            log tool_result
        if 连续失败 ≥ 3: log paused; return
```

- **工具定义始终全部发送**（不随模式增减），模式只在执行时生效，保证工具列表稳定、提示词缓存可命中。
- **重试**：`RetryableError` 指数退避，`base=1s`、最多 4 次重试、上限 30s、加随机抖动；优先使用 `retry_after`。
- **中断**：调用模型或执行工具时捕获 `KeyboardInterrupt`，为尚未有结果的工具调用补“用户已取消”结果，记 `interrupted`，回到 REPL；不退出进程。
- **连续失败**：工具结果 `is_error` 连续 3 次（审批拒绝不计入）则暂停，提示用户介入。
- **预算**：`max_steps`（默认 50）、`max_tokens_total`（默认不限）、`max_cost_usd`（按 config 中的单价估算，未知模型只按 token）。
- 循环与 UI 解耦：通过 `AgentEvents` 回调（on_text、on_tool_call、on_tool_result、on_todos、on_notice）通知界面。

## 8. 工具（tools/）

`Tool(name, description, input_schema, handler, kind)`，`kind ∈ {read, edit, exec, meta, mcp}`，由权限策略使用。`handler(args, ctx) -> ToolOutput(content, is_error)`；`ctx` 含 workspace、路径守卫、会话状态回调。`ToolRegistry.run` 负责：未知工具 → 错误；按 `input_schema` 做必填与类型的轻量校验 → 错误说明；异常 → 错误（不抛出）；最后统一截断。

统一截断：结果超过 `max_output_chars`（默认 20000）时保留头 70%、尾 30%，中间写“…已省略 N 个字符；可用 offset/limit 或更精确的 pattern 缩小范围…”。

| 工具 | 参数 | 要点 |
| --- | --- | --- |
| `read_file` | path、offset=1、limit=400 | 输出带行号（`{n:>6}\t`）；末尾提示“显示第 a–b 行，共 N 行；继续读请用 offset=b+1”；二进制文件拒绝 |
| `list_files` | pattern="**/*"、path="." | git 仓库内用 `git ls-files --cached --others --exclude-standard`，否则遍历并忽略 .git、.venv、node_modules、__pycache__、.minicode；最多 500 条，超出时提示用更窄的 pattern |
| `search_code` | pattern、path="."、glob、case_insensitive=false | 有 `rg` 用 rg，否则 Python 正则遍历；输出 `path:line:text`；最多 100 条，超出提示缩小范围 |
| `edit_file` | path、old_str、new_str、replace_all=false | old_str 必须存在；不唯一且未设 replace_all 时报错并列出所有匹配的行号；old_str == new_str 报错；成功后返回修改处前后 3 行带行号的片段 |
| `create_file` | path、content、overwrite=false | 已存在且未设 overwrite 时报错并提示改用 edit_file；自动创建父目录 |
| `run_command` | command、timeout_sec=120（≤600） | `bash -c` 在 workspace 中执行；返回 exit code、stdout、stderr（各自截断）；超时则杀进程组并报错 |
| `update_todos` | todos: [{content, status}] | status ∈ pending / in_progress / done；整体替换；最多一个 in_progress；记 `todos_updated` 事件 |
| `explore` | task | 见第 11 节 |

工具描述要写清“何时用、何时不用、返回什么”（例如 read_file 提示先用 search_code 定位；edit_file 提示先 read_file 确认上下文、old_str 要包含足够上下文以唯一匹配）。

## 9. 权限（permissions.py）

### 路径守卫

`resolve_in_workspace(path) -> Path`：相对路径基于 workspace；`Path.resolve()` 解析符号链接后必须 `is_relative_to(workspace.resolve())`，否则抛 `PermissionError("路径超出工作区")`。文件类工具全部经过它。

### 命令分类

`classify_command(cmd, workspace) -> "deny" | "network" | "safe" | "other"`：

先切分、再归一化，再判断：

- **切分**：按 `|`、`||`、`&&`、`&`、`;`、换行、`(`、`)`、`$(`、反引号切成片段，逐段判断，整条命令取最危险的结果。
- **归一化**：剥掉开头的环境变量赋值和包装命令（`env`、`command`、`exec`、`nohup`、`time`、`nice`、`timeout`、`xargs`、`stdbuf` 及其选项）；`git` 去掉子命令前的全局选项（`-C X`、`-c k=v`、`--no-pager`、`--git-dir=…` 等）；`sh/bash/zsh/dash -c "…"` 与 `eval …` 递归检查其中的命令（嵌套超过 3 层按 deny 处理）。

分类：

- **deny**（任何模式都拒绝）：`rm -rf /`、`rm -rf ~`、`rm -rf *` 等对根或家目录的递归删除；`curl|wget ... | sh/bash`；`git push` 带 `-f`、`--force*` 或 `+refspec`；`mkfs`、`dd of=/dev/`；fork 炸弹；`sudo`。
- **network**（总是需要用户审批）：任一片段是 curl、wget、ssh、scp、rsync、nc、`git push/pull/fetch/clone`、`pip/uv/npm/pnpm/yarn install|add`、`uv sync`。
- **safe**（白名单，可自动放行）：同时满足
  - 不含 shell 元字符（`; & | > < $ \``、换行、`$(`）；
  - 命令属于：`pytest`、`python -m pytest`、`uv run pytest`、`git status/diff/log/show/branch`、`ls`、`pwd`、`rg`、`grep`、`head`、`tail`、`wc`、`cat`；
  - 不带能执行程序或写文件的参数：`rg --pre/-z/--search-zip/--hostname-bin`，`git --output/--ext-diff/--textconv`；`git branch` 只允许列出分支的参数；
  - 每个非选项参数按路径解析（展开符号链接）后都在工作区内。
- 其余为 **other**。

### 运行模式与决策矩阵

| kind / 类别 | plan | default | auto-edit | yolo |
| --- | --- | --- | --- | --- |
| read、meta（含 explore、update_todos） | allow | allow | allow | allow |
| edit（edit_file、create_file） | deny | ask | allow | allow |
| exec：safe | allow | allow | allow | allow |
| exec：other | deny | ask | ask | allow |
| exec：network | deny | ask | ask | ask |
| exec：deny | deny | deny | deny | deny |
| mcp | deny | ask | ask | allow |

- `ask` 时交互模式下用户可选：`y` 允许一次 / `n` 拒绝（可附一句理由，回传给模型）/ `a` 本会话内总是允许（edit 类按工具名记住；exec 按命令的第一个词记住，network 类不可“总是允许”）。
- 无头模式下 `ask` 一律视为 `deny`，除非命令行给了 `--allow edit`、`--allow exec`（不含 network）或 `--allow mcp`。
- 被拒绝时回传给模型的结果：`is_error=True`，内容说明拒绝原因与可行替代（例如“当前为 plan 模式，只能读取；请先给出修改计划”）。

## 10. 上下文工程（context.py）

- **系统提示词**：角色与工作方式（先探索再修改、修改后运行测试验证、多步任务用 update_todos、大范围探索交给 explore、不确定时提问）；工作区路径、操作系统、当前日期、运行模式；若 workspace 根目录有 `AGENTS.md`，原样附在末尾（上限 8000 字符）。系统提示词在会话内保持不变，以利于缓存（todo 等动态信息不放进系统提示词）。
- **token 估算**：优先用上一次响应的 `usage.input_tokens`，加上之后新增消息按“字符数 / 4”估算。
- **自动压缩**：估算值超过 `context_window × 0.75` 时触发。保留最近 4 条 user 消息起的尾部（切分点必须落在一条“真实用户输入”或完整的 tool_result 消息之前，不能拆开 tool_use 与 tool_result），其余交给模型按固定模板总结：任务目标、已完成、修改过的文件、关键决策与发现、未解决问题、下一步。写入 `compaction` 事件。todos 由归约器独立保存，不依赖摘要。
- 每个模型的 `context_window` 在 config 中配置；Mock 默认 8000，便于测试压缩。

## 11. explore 子代理（tools/explore.py）

- 参数 `task`：要调查的问题。
- 新建一个**独立的** `Agent`：同一个 provider，工具只有 read_file、list_files、search_code，系统提示词要求“只调查不修改，最后用不超过 300 字总结结论和关键文件位置”，`max_steps=15`，事件写入内存，不落主会话日志。
- 只把子代理的最终文本作为工具结果返回给主 Agent，中间的搜索结果不进入主上下文。

## 12. MCP 客户端（mcp_client.py）

只实现 2026-07-28 版规范中工具相关的最小子集，传输为 stdio：子进程的 stdin/stdout 按行收发 JSON-RPC 2.0 消息，stderr 透传到日志。

- 实现前必须查阅官方规范确认字段名：`https://modelcontextprotocol.io/specification/2026-07-28/`（changelog、basic、server/discover、server/tools、transports/stdio，均可在 URL 后加 `.md` 获取 Markdown）。
- 每个请求在 `params._meta` 中携带协议版本与客户端能力（具体键名以规范为准）。
- 启动后调用 `server/discover` 校验服务器支持 `2026-07-28`，否则报错并跳过该服务器；然后 `tools/list`（处理分页游标），对每个工具注册 `mcp__<server>__<tool>`（kind=mcp），描述前加“[MCP:<server>]”。
- `tools/call` 的结果：把 `content` 中的 text 项拼接为字符串；`isError` 映射为 `is_error`；JSON-RPC 错误映射为 `is_error` 结果。
- 请求超时 60 秒；进程退出时清理子进程。
- 配置文件：`<workspace>/.minicode/mcp.json` 或 `--mcp-config`，格式 `{"servers": {"<name>": {"command": "...", "args": [...], "env": {...}}}}`。
- `examples/toy_mcp_server.py`：零依赖，实现 `server/discover`、`tools/list`、`tools/call`，提供 `word_count(text)` 和 `utc_now()` 两个工具，用于演示与测试。

## 13. CLI（cli.py、ui.py）

```text
minicode [-C WORKSPACE] [--provider mock|anthropic|openai] [--model MODEL]
         [--mode plan|default|auto-edit|yolo] [--task TEXT] [--resume SESSION_ID]
         [--allow edit|exec|mcp ...] [--max-steps N] [--max-cost-usd X]
         [--mock-script PATH] [--mcp-config PATH]
```

- 交互模式：rich 渲染模型文本（流式）、工具调用摘要（工具名 + 关键参数）、工具结果首行、todo 清单、审批提示。斜杠命令：`/mode <m>`、`/todos`、`/compact`、`/cost`、`/exit`。
- 无头模式：`--task` 执行一轮后退出；退出码 0 = 正常结束，2 = 暂停（预算 / 连续失败），1 = 错误。输出最终回答到 stdout，过程到 stderr。
- 启动时打印 session id，退出时提示 `--resume <id>`。
- 配置优先级：命令行 > 环境变量 > `.env`。环境变量：`MINICODE_PROVIDER`、`MINICODE_MODEL`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`。

## 14. 测试计划（tests/）

全部离线，用 Mock 或纯函数：

- `test_permissions.py`：路径越界（`..`、绝对路径、指向外部的符号链接）；命令分类各类样例（含白名单命令参数越界）；模式决策矩阵；“总是允许”记忆；无头 `--allow`。
- `test_tools.py`：read_file 分页与提示；list_files 忽略规则与上限；search_code（强制走 Python 回退分支也要测）；edit_file 的不存在 / 不唯一 / 相同 / replace_all / 成功片段；create_file 覆盖保护；run_command 退出码、超时、截断。
- `test_events.py`：JSONL 往返；归约器重建消息、合并 tool_result、压缩替换、悬空工具调用补结果；resume 后继续对话。
- `test_agent.py`：用 `fix_buggy_calc.json` 在 buggy-calc 的临时副本上端到端运行（auto-edit 模式，自动允许 pytest），结束后真实 pytest 通过；拒绝审批时模型收到 is_error；连续 3 次失败暂停；max_steps 暂停；可重试错误按退避重试后成功（用可注入的 sleep）；中断后 tool_use/tool_result 配对。
- `test_context.py`：系统提示词包含 AGENTS.md；超过阈值触发压缩且切分点合法；压缩后 todos 仍在。
- `test_providers.py`：两个适配器的请求构造与响应解析（用手写的响应样例，不发网络请求），包括 OpenAI `arguments` 字符串解析失败、`tool` 角色消息顺序、Anthropic `is_error` 与 cache_control。
- `test_explore.py`：子代理只能用只读工具，主会话日志里只有最终摘要。
- `test_mcp.py`：启动 `toy_mcp_server.py` 子进程，完成 discover / list / call，工具以 `mcp__toy__word_count` 注册并可调用；服务器报错映射为 is_error。
