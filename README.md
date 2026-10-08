# ChatGPTBridge

把 ChatGPT 网页版包装成 **OpenAI 兼容 API** 的本地桥接服务。用 Playwright 驱动一个持久化的浏览器会话，把 `/v1/chat/completions`（以及可选 `/v1/responses`）请求转发到 ChatGPT 网页界面，再把回复转回标准 OpenAI 结构。

面向 Pi、Codex CLI、`agy` 等只认 OpenAI 端点的客户端。

> ChatGPT 网页端使用注意
> 1. 与 ChatGPT 网页端对话时，请开启 思考模式（Thinking），以获得更稳定、完整的任务处理效果。
> 2. 请明确要求 ChatGPT 严格按指定格式下达指令；本项目的工具调用使用 ```tool_call 代码围栏（围栏内只有一条 JSON，info string 必须是 `tool_call`），并要求输出只包含规定格式的指令，不要添加额外解释或其他文本。**不要**让模型写纯文本 `TOOL_CALL: {...}` 行：网页版会把它当 markdown 渲染，吃掉反斜杠转义并折叠缩进（会把命令改坏，见 [doc/update.md](doc/update.md) §2.14）。
> 3. 新会话的提示词应加入环境声明：[环境说明] 执行环境在用户电脑上，你直接下命令就可以。不要访问GitHub。不要访问ChatGPT隔离环境。，明确告诉模型执行环境就在用户端本地，直接执行命令即可。工具说明块（`format_tools_instruction`）本身也会每轮声明「你写的东西会由本地客户端真实执行、结果会作为下一条消息返回」，这是有意的冗余。
> 4. **不要描述「模型自己有没有工具」**：既不能说「你没有 shell / 文件系统的直接访问」，也不能说「你是 agent、工具已经挂载」——模型会把两者都当成关于自身能力的事实题去核对，然后拒答（「当前会话没有实际挂载 read/write/edit/bash 执行工具，所以我不能发 tool_call」，接着改为给你补丁让你自己改），整轮没有任何工具调用、任务静默失败。要说的只有**链路**：它是 LLM、只产出文本；本地客户端读它的回复并真实执行；真实结果作为下一条消息回去；工具**不需要**出现在它的工具列表里（见 [doc/code_block_fence.md](doc/code_block_fence.md) §5.7）。

## 特性

- **OpenAI 兼容端点**：`/v1/models`、`/v1/chat/completions`、`/v1/responses`。
- **流式与非流式**：SSE 逐块输出，首块带 `role`，末块带 `finish_reason`，`data: [DONE]` 收尾。
- **模拟 function calling**：把 OpenAI `tools` 注入提示词，解析模型输出的 ```tool_call 围栏（围栏内 JSON）为 `tool_calls`；兼容旧的纯文本 `TOOL_CALL: {...}` 行；支持单引号 shell 命令引导与控制字符 / 双引号容错修复；解析失败按普通文本返回，并打印诊断日志。
- **会话分桶**：按 `X-ChatGPT-Session` → `user` → User-Agent 分优先级隔离会话，LRU 回收，可选同桶排队锁。
- **不丢任务**：会话轮转时按任务快照 + 历史播种，任务目标不被字符预算截断。
- **登录态持久化**：浏览器 profile 落在 `user_data/`，登录一次即可复用。
- **纯本地**：默认只监听 `127.0.0.1`。
- **有头运行（必须）**：实测 `HEADLESS=1` 会被 Cloudflare 挑战页拦截（页面停在 `Just a moment...`，输入框/按钮都不渲染），因此默认且有义务使用有头模式 `HEADLESS=false`；无显示服务器用 `xvfb-run` 包一层。

## 项目统计

> 统计基于 Git 当前 `main` 分支（2026-10-08）及已跟踪文件，随项目持续演进会变化。

| 指标 | 当前值 |
| --- | ---: |
| Git 已跟踪文件 | 120 |
| Python 源码文件 | 104 |
| 测试文件 | 45 |
| Python 总代码行数 | 19,109 |
| 测试代码行数 | 9,196 |
| Git 提交数 | 56 |
| 顶层目录数 | 11 |
| 贡献者 | 1 |

## 环境要求

- Python 3.10+
- Playwright + Chromium

```bash
pip install -r requirements.txt
playwright install chromium
```

## 快速开始

```bash
# 首次登录：有头模式手动登录 ChatGPT，登录态存入 user_data/
HEADLESS=0 python chatgpt_api_server.py

# 之后继续用有头模式（.env 已默认 HEADLESS=false）
python chatgpt_api_server.py

# 无显示服务器：用 xvfb 包一层有头 Chromium（不要设 HEADLESS=1）
# xvfb-run -a python chatgpt_api_server.py
```

服务默认监听 `http://127.0.0.1:8002`。冒烟测试：

```bash
curl http://127.0.0.1:8002/healthz
curl http://127.0.0.1:8002/v1/models
```

### 客户端接入

任何 OpenAI 兼容客户端指向 `http://127.0.0.1:8002/v1` 即可（`api_key` 随便填）。

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8002/v1", api_key="unused")
resp = client.chat.completions.create(
    model="chatgpt",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### Pi 接入

Pi 通过 `~/.pi/agent/models.json` 做模型发现，走 OpenAI **Chat Completions**（`/v1/chat/completions`）。

在 Pi 的 `models.json` 里加一个指向本服务的模型条目（`baseUrl` 指向 `/v1`，`apiKey` 随便填）：

```json
{
  "providers": {
    "chatgpt-web": {
      "baseUrl": "http://127.0.0.1:8002/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false
      },
      "models": [
        {
          "id": "chatgpt-chat",
          "name": "ChatGPT Pro (Web)",
          "input": ["text"],
          "contextWindow": 1000000,
          "maxTokens": 65535
        }
      ]
    }
  }
}
```

- 模型名用 `/v1/models` 返回的 `chatgpt-chat` 或 `chatgpt-reasoner`。
- Pi 会自动请求 `GET /v1/models` 做模型发现，无需手填上下文长度。
- 想固定会话桶可加请求头 `X-ChatGPT-Session: <name>`（见「配置」的 `SESSION_KEY_HEADER`）。

### Codex CLI 接入

Codex CLI 只走 **Responses API**（`POST /v1/responses`），不再用 chat 端点；该路由由 `ENABLE_RESPONSES_API` 控制（默认开启）。

在 `~/.codex/config.toml` 里加一个自定义 provider：

```toml
[model_providers.chatgpt_bridge]
name = "ChatGPT Bridge"
base_url = "http://127.0.0.1:8002/v1"
wire_api = "responses"

[profiles.chatgpt]
model_provider = "chatgpt_bridge"
model = "chatgpt-chat"
```

然后用该 profile 启动：

```bash
codex --profile chatgpt
```

- `wire_api = "responses"` 必填，否则 Codex 会去打 `/v1/chat/completions`。
- 工具调用（function calling）会被桥接层解析为 Responses 的 function_call 事件。
- 关闭 `ENABLE_RESPONSES_API` 后该端点返回 404，但 Pi 的 chat 路径不受影响。

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 可用模型列表 |
| POST | `/v1/chat/completions` | Chat Completions，支持 `stream` |
| POST | `/v1/responses` | Responses API（受 `ENABLE_RESPONSES_API` 控制） |
| GET | `/healthz` | 健康检查与 cluster 状态 |
| POST | `/session/reset` | 重置指定会话桶 |
| GET | `/debug/dom` | DOM 调试（受 `CHATGPT_DEBUG` 控制，不回显正文） |
| GET | `/` | 服务信息 |

## 配置

所有可调参数集中在项目根目录的 `.env`（已 gitignore）。已存在的真实环境变量优先于 `.env`。

**服务**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8002` | 监听地址 |
| `HEADLESS` | `false` | **必须保持 `false`（有头）**：`true` 会被 Cloudflare 挑战页拦截，输入框/按钮不渲染。无显示服务器用 `xvfb-run` |
| `CHATGPT_DEBUG` | `false` | 打印轮询状态、开放 `/debug/dom` |

**结束判定与超时**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CHATGPT_TIMEOUT` | `180` | 单轮总超时（秒） |
| `POLL_INTERVAL_S` | `1.5` | 轮询间隔 |
| `STABLE_POLLS` | `2` | 内容不变连续次数判定结束 |
| `LEN_STABLE_POLLS` | `4` | 仅长度不变时的保守阈值 |
| `CHATGPT_RETRIES` | `2` | 上游超时重试次数 |
| `RETRY_BACKOFF_S` | `1.0` | 退避基数（×n） |

**会话生命周期**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SESSION_KEY_HEADER` | `X-ChatGPT-Session` | 分桶键请求头 |
| `SESSION_SCOPING` | `true` | 是否启用分桶 |
| `SESSION_SCOPING_BY_UA` | `true` | 无键时按 UA 分桶 |
| `MAX_SESSION_BUCKETS` | `8` | 会话桶上限（LRU） |
| `MAX_SESSION_STATE_CACHE` | `64` | 内存会话状态缓存上限（LRU 逐出，0 不限） |
| `BUCKET_IDLE_TTL_S` | `900` | 空闲回收 |
| `PARALLEL_BUCKETS` | `false` | 各桶并行页面 |
| `BUCKET_LOCK_TIMEOUT_S` | `120` | 同桶排队超时，>0 超时返回 503 `upstream_busy` |
| `SEED_MAX_CHARS` | `12000` | 轮转播种字符预算 |
| `TOOL_RESULT_MAX_CHARS` | `20000` | 单条 tool 结果注入 prompt 的最大字符数（0 不限） |
| `TOOL_NUDGE_UNTIL_FIRST_CALL` | `true` | 工具纠偏只在本轮还没调用过任何工具时生效；`false` = 完全不纠偏（桥绝不自行追发 prompt） |
| `PROMPT_MAX_CHARS` | `1000000` | 单次 fill() 入参硬上限，兜底防输入框溢出（0 不限） |
| `SESSION_MAX_TURNS` | `120` | 轮数到顶阈值（0 禁用） |
| `SESSION_MAX_TOKENS` | `10000000` | 估算 token 到顶阈值（0 禁用） |

> 上表列出的是 `config.py` 的**内置默认值**。实际运行时以 `.env` 为准（真实环境变量优先）；
> 例如仓库自带 `.env` 覆盖为 `STABLE_POLLS=5`、`MAX_SESSION_BUCKETS=3`。
> 想从零起步可直接复制 `.env.example`。

**代码落盘 / 调试端点**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SAVE_FILES` | `false` | 是否把回复代码块落盘（请求字段仅在显式传入时覆盖） |
| `OUTPUT_MAX_FILES` | `0` | `output/` 保留文件数上限（0 不限） |
| `OUTPUT_MAX_AGE_DAYS` | `0` | `output/` 最长保留天数（0 不限） |
| `RESET_TOKEN` | 空 | 设置后 `/session/reset` 需带 `X-Reset-Token` 头 |
| `CHAT_KEEPALIVE_S` | `10.0` | chat 流式 keep-alive 间隔（0 关闭） |

**Responses API**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ENABLE_RESPONSES_API` | `true` | 关闭后 `/v1/responses` 返回 404 |
| `RESPONSES_KEEPALIVE_S` | `10.0` | 保活注释间隔（0 关闭） |
| `RESPONSES_TOOL_BUFFER` | `true` | 工具模式先缓冲整段再解析 |

**任务快照**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TASK_SNAPSHOT_ENABLED` | `true` | 轮转时注入任务目标，防止被截断 |
| `TASK_FILE_DIR` | `./user_data/.chatgpt_tasks` | 快照目录 |
| `TASK_NAMESPACE` | 派生自包名 | 多桥共用目录时的命名空间 |
| `TASK_GOAL_MAX_CHARS` | `2000` | 任务目标保留上限 |
| `TASK_KEEP_MESSAGES` | `8` | 滚动保留的最近消息数 |

**本地 Markdown 编辑（edit_markdown）**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EDIT_MARKDOWN_LOCAL` | `false` | 是否允许桥接层本地执行模型发出的 edit_markdown |
| `EDIT_MARKDOWN_WRITE` | `false` | 是否允许真正落盘；`false` 时即便模型传 `write=true` 也只返回 diff |
| `EDIT_MARKDOWN_BACKUP_DIR` | `output/backups` | 落盘前的备份目录 |
| `EDIT_MARKDOWN_ROOT` | 项目根（沙箱，不可关闭） | 模型给的相对路径必须落在此目录内；绝对路径与 `..` 一律拒绝 |

**DOM 选择器**（网页版改版时改这里）

`RESPONSE_SELECTORS`、`INPUT_SELECTORS`、`READY_SELECTOR`、`NEW_CHAT_SELECTOR`、`CODE_BLOCK_SELECTOR`、`CODE_TAG_SELECTOR`、`CAP_NOTICE_PATTERNS`。

改版自检（2026-10-06 实测踩过：助手回复容器换名后，轮询会一直 `nodes=0`
空转到超时，客户端拿不到任何内容）：

```bash
# 1) 运行时逐条命中数（需 CHATGPT_DEBUG=1 启动）
curl -s http://127.0.0.1:8002/_debug/selectors | python3 -m json.tool

# 2) 一次性重启自检：日志里搜「[诊断]」——连续 3 轮零节点且页面仍在生成时，
#    bridge 会打印每条 RESPONSE_SELECTORS 的命中数与页面文本长度

# 3) 真实 DOM 探测（有头，打开最近一条会话并给出建议）
CHATGPT_E2E=1 E2E_HEADED=1 .venv/bin/python -m unittest tests.e2e.test_dom_probe -v
```

要点：助手回复正文现在是 `[data-markdown-text-style]`（容器 class 为
`MarkdownRoot-<hash>`）；用户消息是 `[data-user-message-bubble]`，**不要**选进
`RESPONSE_SELECTORS`。代码块是 `[class*="CodeBlock"]` + `[data-language]`
（不再有 `pre`/`code`）。

## 项目结构

```
chatgpt_api_server.py      入口：重导出公开名字 + 启动 uvicorn
chatgpt_web/
  config.py               配置加载与全部可调参数
  models.py               OpenAI 兼容 Pydantic 模型
  prompting.py            messages -> 输入框文本、token 估算
  toolcalls.py            工具注入与解析
  markdown_io.py          Markdown 提取与工具调用解析器
  driver.py               Playwright 浏览器驱动、会话生命周期
  chat_io.py              DOM 交互、输入框等待、页面内事件提交与代码块提取
  session_store.py        会话状态落盘与持久化管理
  page_pool.py            Playwright Page 实例池管理
  completion.py           Chat Completions 逻辑处理与重试驱动
  streaming.py            SSE 流式编码
  responses.py            Responses API 映射
  tasks.py                会话桶任务快照
  server.py               FastAPI 应用与路由
  end_detection.py        回复结束判定的纯函数状态机
  errors.py               异常类型与默认值常量
  logging_setup.py        统一日志配置与 request_id
doc/tasks.md              任务分解与验收标准
doc/update.md             项目分析与改进建议（含真实 E2E 结果）
doc/e2e_test_design.md    E2E 对等测试设计（判定矩阵 / 开关 / 前置条件）
doc/code_block_fence.md   工具调用载体改造技术报告（纯文本 TOOL_CALL 行 → tool_call 围栏）
output/                   回复与代码块落盘（gitignore）
user_data/                浏览器 profile 与状态（gitignore）
```

## 设计要点

- 网页版驱动而非官方 API：服务通过 Playwright 操作持久化 Chromium 会话，因此登录态、DOM 结构和 ChatGPT 网页改版都会直接影响可用性。
- 每轮任务重播：不依赖恢复旧网页会话，而是按会话历史重新播种任务；同时由 tasks.py 保存任务目标，避免轮转或上下文压缩导致目标丢失。
- 结束判定偏保守：结合内容 / 长度双阈值，并在回复节点仍存在 .pending / .animating token 时继续等待，降低读到半截回复的概率。
- 工具调用单独解析：tools 不直接交给网页端，而是先注入结构化提示，再从模型文本中解析 ```tool_call 围栏里的 JSON；解析失败时回退为普通文本。
- **载体选围栏，不选纯文本行**：网页版把纯文本 `TOOL_CALL: {...}` 行当 markdown 渲染——值内引号前的反斜杠被吃掉（JSON 失效、整条调用被丢弃）、连续缩进空格被折叠成 1 个（命令被静默改坏）；代码块内部不做这层处理，实测同一条命令在围栏里**逐字节一致**（真机 A/B 对比见 [doc/update.md](doc/update.md) §2.14）。
- **桥不自行追发 prompt**：工具纠偏只在本轮任务「一次工具都还没调用过」时生效（`TOOL_NUDGE_UNTIL_FIRST_CALL`）。历史里一旦出现工具结果 / 工具调用，无指令的纯文本回复就作为最终答案返回，由客户端判定任务结束——避免「ChatGPT 已收尾、桥又追发一条 prompt、模型被迫再吐新指令」这种收不了尾的情形。
- 会话隔离与资源回收：通过 X-ChatGPT-Session、user、User-Agent 建立会话桶，并配合 LRU 与页面池控制浏览器资源。
- 配置与 DOM 解耦：选择器、轮询阈值、会话数量、任务快照和 Responses API 开关集中在 .env，网页版改版时优先调整配置。

## 测试

项目提供 tests/ 自动化测试，覆盖配置漂移、Markdown / 工具调用解析、会话管理、流式输出、Responses / Chat 路由等核心逻辑。建议修改后运行：

```bash
python -m pip install -e ".[dev]"   # 一次性：安装项目与 ruff / mypy / pytest
ruff check .                         # 静态检查（规则集与豁免清单见 pyproject.toml）
mypy chatgpt_web                     # 类型检查
pytest -q                            # 单元测试（e2e 默认 skip）
```

CI（`.github/workflows/ci.yml`）在 push / PR 上运行同样三条检查。端到端浏览器行为还需实际登录 ChatGPT 网页环境验证（设计见 `doc/e2e_test_design.md`）；自动化测试通过不代表当前网页 DOM 仍与选择器完全兼容。

## 安全

- 默认仅监听 `127.0.0.1`，不要暴露到公网。
- **本服务不提供鉴权（设计如此）**：客户端 `api_key` 随便填即可，`/v1/models` 与 `/v1/chat/completions` 对任意（或不带）认证头都照常服务。把 `HOST` 改成 `0.0.0.0` 之类的非回环地址会让**任何能访问该地址的人**都能用你的 ChatGPT 登录态与额度——启动时会打醒目告警，但该用法不受支持；需要远程访问请用 SSH 端口转发 / VPN 等外部手段，不要期望在服务内加 API Key。
- `.env`、`user_data/`（含登录 cookie）、`output/` 均不提交。
- `user_data/` **不要备份 / 同步**（iCloud、Dropbox 等会带走登录态）；DEBUG 日志不含消息正文。
- **`edit_markdown` 是模型可控的写文件工具**：只接受沙箱（`EDIT_MARKDOWN_ROOT`，默认项目根）内的相对路径，绝对路径与 `..` 一律拒绝；默认 **dry-run**，只有显式设置 `EDIT_MARKDOWN_WRITE=true` 才会真正落盘（写前自动备份到 `EDIT_MARKDOWN_BACKUP_DIR`）。

## 状态

配置、模型、prompting、toolcalls、driver、streaming、responses、server 均已实现；单测见 `tests/`（`pytest -q`），真实网页验证见 `doc/e2e_test_design.md`，任务分解与验收标准见 `doc/tasks.md`。
