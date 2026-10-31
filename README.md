# ChatGPTBridge

把 ChatGPT 网页版包装成 **OpenAI 兼容 API** 的本地桥接服务。用 Playwright 驱动一个持久化的浏览器会话，把 `/v1/chat/completions`（以及可选 `/v1/responses`）请求转发到 ChatGPT 网页界面，再把回复转回标准 OpenAI 结构。

面向 Pi、Codex CLI、OpenAI SDK 等支持 OpenAI API 的客户端。

## 特性

- **OpenAI 兼容端点**：`/v1/models`、`/v1/chat/completions`、`/v1/responses`。
- **流式与非流式**：SSE 逐块输出，首块带 `role`，末块带 `finish_reason`，`data: [DONE]` 收尾。
- **模拟 function calling**：把 OpenAI `tools` 注入提示词，解析模型输出的 ```tool_call 围栏（围栏内 JSON）为 `tool_calls`；兼容旧的纯文本 `TOOL_CALL: {...}` 行；支持单引号 shell 命令引导与控制字符 / 双引号容错修复；解析失败按普通文本返回，并打印诊断日志。
- **会话分桶**：按 `X-ChatGPT-Session` → `user` → User-Agent 分优先级隔离会话，LRU 回收，可选同桶排队锁。
- **不丢任务**：会话轮转时按任务快照 + 历史播种，任务目标不被字符预算截断。
- **登录态持久化**：浏览器 profile 落在 `user_data/`，登录一次即可复用。
- **纯本地**：默认只监听 `127.0.0.1`。
- **有头运行（必须）**：实测 `HEADLESS=1` 会被 Cloudflare 挑战页拦截（页面停在 `Just a moment...`，输入框/按钮都不渲染），因此默认且有义务使用有头模式 `HEADLESS=false`；无显示服务器用 `xvfb-run` 包一层。

## 架构概览

ChatGPTBridge 将 ChatGPT 网页版包装为本地 OpenAI 兼容服务。它不是官方 API 客户端：请求最终通过 Playwright 操作已登录的 Chromium 页面完成，因此浏览器登录状态、网页 DOM 和站点策略都会影响运行结果。

主要请求链路：

```text
OpenAI SDK / Pi / Codex CLI
            |
            v
FastAPI 路由与 OpenAI 请求模型
            |
            v
Chat Completions / Responses 协议适配
            |
            v
会话分桶、并发协调、任务快照与历史播种
            |
            v
Playwright 浏览器驱动 -> ChatGPT 页面 DOM -> 回复提取
            |
            v
OpenAI 响应 / SSE 流式事件 / 工具调用解析
```

核心模块按职责划分：

| 层 | 主要模块 | 职责 |
| --- | --- | --- |
| HTTP 与协议 | `server.py`、`api/`、`models.py`、`protocol_adapters.py` | 路由、请求模型，以及 Chat Completions / Responses 格式转换 |
| 浏览器集成 | `browser/`、`browser_input.py`、`chat_io.py`、`driver.py` | Playwright 生命周期、DOM 定位、输入提交、回复和代码块提取 |
| 生成与流式 | `completion/`、`reply_waiter.py`、`reply_extractor.py`、`end_detection.py`、`streaming.py`、`responses.py` | 生成调度、结束判定、回复提取、SSE 输出与 Responses 事件映射 |
| 会话与状态 | `session/`、`session_store.py`、`page_pool.py`、`linking.py`、`tasks.py`、`task_state.py` | 会话持久化和绑定、页面池、会话隔离、任务快照与历史恢复 |
| 工具调用 | `tools/`、`toolcalls.py`、`markdown_io.py` | 工具定义注入、载荷解析与校验、结果序列化，以及受策略限制的 Markdown 编辑 |
| 配置与运维 | `config/`、`metrics.py`、`logging_setup.py`、`errors.py` | 配置加载、运行指标、日志上下文和错误语义 |

所有主要参数集中在项目根目录的 `.env`，模板为 `.env.example`。默认监听回环地址；浏览器 profile、会话状态和可选输出文件保存在本地目录。

## 环境要求

- Python **3.14 或更高版本**（`pyproject.toml` 的 `requires-python` 为 `>=3.14`）。
- Playwright 及其 Chromium 浏览器。
- 一个可登录 ChatGPT 网页版的账户；首次启动需要在有头浏览器中完成登录。

安装运行依赖并下载 Chromium：

```bash
python -m pip install -e .
playwright install chromium
```

开发和测试依赖：

```bash
python -m pip install -e '.[dev]'
```

## 快速开始

首次部署时，可复制配置模板（已有 `.env` 时不要覆盖自己的配置）：

```bash
# 仅在尚无 .env 时执行
test -f .env || cp .env.example .env

# 首次启动使用有头模式，手动登录 ChatGPT，登录态保存在 user_data/
HEADLESS=0 python chatgpt_api_server.py
```

登录成功后，保持有头模式启动服务：

```bash
python chatgpt_api_server.py
```

无显示服务器可通过虚拟显示器运行有头 Chromium，例如：

```bash
xvfb-run -a python chatgpt_api_server.py
```

不要为了无显示环境直接设置 `HEADLESS=1`：项目的实际测试观察到，无头模式可能被 Cloudflare 挑战页拦截，导致输入框和按钮不可用。

服务默认监听 `http://127.0.0.1:8002`。检查服务和模型发现：

```bash
curl -i http://127.0.0.1:8002/healthz
curl http://127.0.0.1:8002/v1/models
```

`/healthz` 反映浏览器初始化状态；需要确认是否具备处理请求的条件时，也可查看 `/readiness`。

### 客户端接入

任何 OpenAI 兼容客户端将 Base URL 指向 `http://127.0.0.1:8002/v1` 即可；本服务不提供 API Key 鉴权，客户端的 `api_key` 可填写任意非空值。

### OpenAI SDK 调用

安装官方 Python SDK：

```bash
pip install openai
```

非流式调用：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8002/v1",
    api_key="none",
)

response = client.chat.completions.create(
    model="chatgpt-chat",
    messages=[{"role": "user", "content": "你好"}],
)

print(response.choices[0].message.content)
```

流式调用：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8002/v1",
    api_key="none",
)

stream = client.chat.completions.create(
    model="chatgpt-chat",
    messages=[{"role": "user", "content": "请用三句话介绍这个项目"}],
    stream=True,
)

for chunk in stream:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
```

可用模型名以 `GET http://127.0.0.1:8002/v1/models` 的实际返回为准，当前通常包括 `chatgpt-chat` 和 `chatgpt-reasoner`。

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
| GET | `/v1/models` | 返回当前桥接层公开的模型列表 |
| POST | `/v1/chat/completions` | Chat Completions，支持流式与非流式请求 |
| POST | `/v1/responses` | Responses API；由 `ENABLE_RESPONSES_API` 控制 |
| GET | `/healthz` | 基础健康状态、浏览器状态、会话桶与资源信息 |
| GET | `/readiness` | 检查服务是否具备执行网页请求的最低条件；未就绪时返回 503 |
| GET | `/metrics` | 进程内请求计数、错误与延迟等运行指标快照 |
| GET | `/diagnostics` | 汇总 readiness、浏览器、会话、指标和选择器诊断信息 |
| POST | `/session/reset` | 重置指定会话桶；可通过 `RESET_TOKEN` 保护 |
| POST | `/session/link` | 将会话桶绑定到指定的 ChatGPT 网页会话 URL |
| POST | `/session/unlink` | 解除会话绑定 |
| GET | `/debug/dom` | 当前页面 DOM 诊断；需要 `CHATGPT_DEBUG=true` |
| GET | `/_debug/selectors` | 返回选择器命中情况；需要 `CHATGPT_DEBUG=true` |
| GET | `/` | 服务信息与可用端点概览 |

调试路由默认关闭。`/diagnostics`、`/healthz` 等端点可能返回会话桶和运行状态等内部信息，不应直接暴露到公网。

## 会话绑定（`/link`）：把某一桶保持在指定网页会话上

默认设计是「每桶新开对话 + 播种重放历史」，不依赖回填会话地址。但 ChatGPT 网页版
**有时会自行开启一条新的网页会话**（桥这一侧就是「句柄消失 / 上下文不在原来的会话里」），
这时你可能希望指定「就发到这条会话」。在客户端里把下面这一行当普通消息发给桥即可
（chat / responses 两条路径都支持）：

```text
/link https://chatgpt.com/c/6ac9c061-5074-83ec-82f0-2f46a8c334ca
/link https://chatgpt.com/c/6ac9c061-5074-83ec-82f0-2f46a8c334ca --seed
/unlink
```

- `/link <URL>`：把**当前会话桶**绑定到该会话。默认认为这条会话里**已有此前上下文**，
  之后只发增量；句柄失效重建、页面漂移（网页版自己跳走）、轮转、服务重启都会回到该会话。
- `/link <URL> --seed`：绑定，但下一次发送把**完整历史播种**进这条会话
  （它里面其实没有此前记录时用；代价是历史会在网页端重复一遍）。
- `/link`（无参数）：查看当前绑定；`/unlink`：解除绑定，恢复「新开会话 + 播种」的默认行为。

要点：

- **命令不经过网页版**：整条消息就是那一行命令时由桥直接应答（返回一条说明性回复），
  不发给模型，因此**句柄失效、页面漂移、浏览器都没起来时它照样有效**。
- **只认「整条消息就是一行命令」**：混在正文里、多行文本、或出现在历史消息里的命令都
  当成普通提问交给模型，不会把用户正常说的话吃掉。
- **绑定落盘**（`SESSION_FILE` 里的 `linked_url`），重启后仍生效；当前绑定可从
  `GET /healthz` 的 `buckets.<key>.linked_url` 看到。
- 链接必须是 `https://<允许的站点>/c/<会话ID>`（`/share/...` 分享链接不能继续对话）；
  允许的站点由 `LINK_ALLOWED_HOSTS` 控制，默认仅官方域名。**会话 ID 按站点自己的形态收**：
  只要是一个合法 URL 路径段就行（可含 `%XX` 转义；`%3A` 与 `:` 视为同一条会话）。
- **客户端临时地址会被拒**：`.../c/local-chatgpt%3A<uuid>`（更早的版本是 `/c/WEB:<uuid>`）
  是网页版在新会话拿到正式 ID **之前**的地址栏写法，约 1 秒后就被替换成 `/c/<uuid>`；拿它去
  导航站点会重定向回首页，绑定只会让之后每一轮都发进一条陌生新会话。遇到这种地址请先在会话里
  发出第一条消息、等地址栏变成 `/c/<uuid>` 再复制（错误文案里也会这么说）。
- **绑定后第一次导航会校验落点**：站点若不认这条地址（不存在 / 已失效 → 重定向回首页），
  日志里会出现 `[绑定] … 导航后页面停在别处`，`/metrics` 的 `session_link_miss_total` 也会
  增长——看到就说明这个地址不能用，换一条或加 `--seed` 都不解决问题。
- 不想用指令时可用 HTTP：`POST /session/link?url=<URL>&session=<bucket>&seed=false`、
  `POST /session/unlink?session=<bucket>`（设置了 `RESET_TOKEN` 时同样要带 `X-Reset-Token`）。

## Bridge 命令（`/bridge ...`）：在对话里查状态、管当前会话桶

设计说明见 [doc/bridge_command.md](doc/bridge_command.md)。与 `/link` 一样，命令由桥**本地应答**、
不发给网页版；因此浏览器没起来、页面句柄已失效时它们照样可用（这也是排障时把它们放在输入框里的理由）。

```text
/bridge help [session|settings]       # 命令列表 / 分主题帮助（只读）
/bridge status                        # 桥接 + 当前桶的状态摘要（只读）
/bridge models                        # 本桥公布的模型名，与 GET /v1/models 同源（只读）
/bridge token                         # 当前桶的 token 估算与 prefill/decode 速度（只读）
/bridge session [status]              # 当前桶的会话状态摘要（只读）
/bridge session reset                 # 下一轮开新网页会话 + 播种完整历史（改状态）
/bridge session reseed                # 下一轮把完整历史再播种一遍（改状态，一次性）
/bridge session link <URL> [--seed]   # 绑定当前桶到指定网页会话（改状态）
/bridge session unlink                # 解除当前桶的绑定（改状态）
/bridge settings think on|off|status  # 思考模式偏好，按桶持久化（改状态）
/bridge settings save-files on|off|status  # 代码块落盘偏好，按桶持久化（改状态）
```

要点：

- **只作用于当前会话桶**：命令没有「指定别的桶」的参数，桶仍由 `X-ChatGPT-Session` / `user` /
  User-Agent 决定；一条命令不会影响其它客户端或其它 Agent 的会话。
- **只读命令不导航网页、不触发生成**；`/bridge status` 在浏览器未初始化时也会报告降级状态并给出
  下一步（`GET /readiness`），普通请求在该状态下仍照旧返回 503。
- **`/bridge session reset` 不是清空聊天记录**：它只是让这一桶下一轮**新开一条网页会话**并把客户端
  传来的历史播种进去（等价 `POST /session/reset`，不删客户端的对话历史、不清除账户数据）。
- **`/bridge session reseed` 是「再发一遍历史」**：用于「网页会话里还有历史但与客户端已失配」。
  标记落盘（`SESSION_FILE`），下一轮**成功发送**后才消费，因此重启 / 重试不会丢、也不会重复应用；
  代价是那条网页会话里会出现重复内容。
- **`/bridge settings` 是「按桶偏好」而不是改全局配置**：`think` 决定该桶网页端是否选中思考模式
  （`default` = 跟随 `THINK_MODE_DEFAULT`），`save-files` 决定该桶的回复代码块是否落盘
  （`default` = 跟随 `SAVE_FILES`，优先级：请求的 `save_files` 字段 > 本桶偏好 > 配置）。
  切换失败时回复会**如实报告**（例如页面上找不到控件、站点不允许关闭），不会谎报已切换。
- **兼容别名不变**：`/link`、`/unlink` 继续可用；`/bridge session link|unlink` 与它们共用同一套
  校验、绑定与文案。
- **`/bridge token` 是观测估算，不是模型裸速度**：网页端没有 token 级事件，token 数由字符长度
  估算（≈ 字符数 / 3）；prefill 速度 = 输入 token / 首字延迟（首字延迟含网页合成与半轮询周期，
  故偏小），decode 速度 = 输出 token / 解码时长（已扣可归因的落定静默窗口）。命令会分别给出
  「最近一次」与本桶「滚动平均」，并标注口径。流式路径还会显示端到端指标（首个正文 chunk 的
  TTFT、流式 decode 速度、桥接开销占比）；回复过短或发生重写时会附「仅供参考」提示。只统计
  成功的生成，只输出计数与时长，不回显正文。

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
| `PAGE_REBUILD_MAX` | `1` | 单次请求内「页面句柄失效 → 新开页面重发」次数上限（与重试次数解耦） |

**会话生命周期**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SESSION_KEY_HEADER` | `X-ChatGPT-Session` | 分桶键请求头 |
| `SESSION_SCOPING` | `true` | 是否启用分桶 |
| `SESSION_SCOPING_BY_UA` | `true` | 无显式键时按 UA 分桶 |
| `SESSION_SCOPING_BY_CWD` | `false` | opt-in：从受控环境元数据提取 cwd 自动分桶 |
| `SESSION_CWD_HASH_LEN` | `12` | cwd 桶键的路径 SHA-256 摘要长度 |
| `MAX_SESSION_BUCKETS` | `8` | 非默认会话桶的打开 Page 数上限（默认桶不计入）；0 = 禁止创建额外桶，非默认 key 请求报错且不合并到默认桶；关闭分桶请设置 `SESSION_SCOPING=false` |
| `MAX_SESSION_STATE_CACHE` | `64` | 内存会话状态缓存上限（LRU 逐出，0 不限） |
| `BUCKET_IDLE_TTL_S` | `900` | 空闲回收 |
| `PARALLEL_BUCKETS` | `false` | 各桶并行页面 |
| `BUCKET_LOCK_TIMEOUT_S` | `120` | 同桶排队超时，>0 超时返回 503 `upstream_busy`（建议 30：卡住时快速失败，别让客户端干等） |
| `LINK_ALLOWED_HOSTS` | `chatgpt.com,chat.openai.com` | `/link` 允许绑定的站点（逗号或 `||` 分隔，子域自动允许；`*` = 不限制） |
| `SEED_MAX_CHARS` | `12000` | 轮转播种字符预算 |
| `TOOL_RESULT_MAX_CHARS` | `200000` | 单条 tool 结果注入 prompt 的最大字符数（0 不限；调大需同时调高 `FILL_TIMEOUT_MS`） |
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

```text
chatgpt_api_server.py       启动入口与兼容导出
main.py                    简单入口
chatgpt_web/
  server.py                 FastAPI 应用、路由、readiness 与诊断端点
  api/                      Chat Completions、Responses、SSE 协议适配
  browser/                  浏览器驱动、DOM 适配、选择器与诊断
  completion/               生成调度、生命周期与回复提取
  config/                   分领域配置与环境变量加载
  session/                  会话状态 schema、存储、迁移与并发锁
  tools/                    工具解析、策略、校验、执行与序列化
  models.py                 OpenAI 兼容请求/响应模型
  prompting.py              消息转换、提示词构造与上下文播种
  browser_input.py          输入框候选定位、填充和清理
  chat_io.py                页面交互、回复等待及代码块提取
  linking.py                /link、/unlink 会话绑定逻辑
  bridge_commands.py        /bridge 命令解析、执行、帮助与状态摘要
  page_pool.py              页面资源池与恢复
  streaming.py              Chat Completions SSE 编码
  responses.py              Responses API 兼容层
  tasks.py / task_state.py  任务快照与任务状态
  metrics.py                进程内指标
  logging_setup.py           日志与请求上下文

tests/                      单元、契约、回归与可选端到端测试
doc/                        设计说明、任务清单、排查记录和端到端测试方案
output/                     可选输出与备份目录（默认不提交）
user_data/                  持久化浏览器 profile 与会话状态（默认不提交）
```

配置已按职责拆分为 `chatgpt_web/config/` 包；会话存储、生成流程、浏览器、API 适配器和工具执行也分别有独立子包。上表突出常用入口，并非完整文件清单。

## 设计要点

- 网页版驱动而非官方 API：服务通过 Playwright 操作持久化 Chromium 会话，因此登录态、DOM 结构和 ChatGPT 网页改版都会直接影响可用性。
- 每轮任务重播：不依赖恢复旧网页会话，而是按会话历史重新播种任务；同时由 tasks.py 保存任务目标，避免轮转或上下文压缩导致目标丢失。
- 结束判定偏保守：结合内容 / 长度双阈值，并在回复节点仍存在 .pending / .animating token 时继续等待，降低读到半截回复的概率。
- 工具调用单独解析：tools 不直接交给网页端，而是先注入结构化提示，再从模型文本中解析 ```tool_call 围栏里的 JSON；解析失败时回退为普通文本。
- **载体选围栏，不选纯文本行**：网页版把纯文本 `TOOL_CALL: {...}` 行当 markdown 渲染——值内引号前的反斜杠被吃掉（JSON 失效、整条调用被丢弃）、连续缩进空格被折叠成 1 个（命令被静默改坏）；代码块内部不做这层处理，实测同一条命令在围栏里**逐字节一致**（真机 A/B 对比见 [doc/update.md](doc/update.md) §2.14）。
- **桥不自行追发 prompt**：工具纠偏只在本轮任务「一次工具都还没调用过」时生效（`TOOL_NUDGE_UNTIL_FIRST_CALL`）。历史里一旦出现工具结果 / 工具调用，无指令的纯文本回复就作为最终答案返回，由客户端判定任务结束——避免「ChatGPT 已收尾、桥又追发一条 prompt、模型被迫再吐新指令」这种收不了尾的情形。
- 会话隔离与资源回收：通过 X-ChatGPT-Session、user、User-Agent 建立会话桶，并配合 LRU 与页面池控制浏览器资源。
- **保持连接 vs 新开会话**：默认「轮转 + 播种」不依赖会话地址，但用户用 `/link` 显式绑定后，
  这一桶就固定发到那条会话（页面漂移 / 句柄重建 / 轮转 / 重启都回到它），命令本身不经过网页版。
- **桥内命令是本地能力，不走网页版**：`/link`、`/unlink` 与 `/bridge ...` 在 HTTP 边缘被识别并
  直接应答（[chatgpt_web/bridge_commands.py](chatgpt_web/bridge_commands.py) 是所有命令的统一入口），
  因此浏览器不可用时仍能查询状态、改绑定或排队重新播种；会改状态的命令只作用于当前桶，
  且回执里写清影响范围（例如 reset 不是删除客户端历史）。
- 配置与 DOM 解耦：选择器、轮询阈值、会话数量、任务快照和 Responses API 开关集中在 .env，网页版改版时优先调整配置。
- **composer 定位按候选枚举**：改版后 composer 已无 `#prompt-textarea` id（实测 0 命中），是 `form`
  内的 ProseMirror；页面 DOM 退化时同一页还会出现多个这样的编辑器（实测 3 个）。驱动枚举**全部**
  候选、可见的优先，逐个试填并校验读回，读到非空才算成功——「第一个 attached 就返回」会把 prompt
  填进一个点不动的僵尸节点（真机现象与处置见 [doc/update.md](doc/update.md) §2.18）。

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

## 更多文档

- `doc/update.md`：项目分析、改动记录和问题排查经验。
- `doc/e2e_test_design.md`：真实网页端到端测试的开关、前置条件和判定矩阵。
- `doc/session_linking_port.md`：会话绑定与恢复实现说明。
- `doc/tasks.md`、`doc/tasks_pi.md`、`doc/tasks_pi_9.md`：任务分解与验收记录。
- `doc/code_block_fence.md`：工具调用使用 `tool_call` 围栏的设计背景。

## 当前限制

- 这是对 ChatGPT 网页版的浏览器自动化，不是官方 OpenAI API；网页改版、登录失效、网络状态和站点反自动化策略都可能影响可用性。
- `pytest -q` 默认不会访问真实 ChatGPT 网页；真实端到端测试需要显式设置 `CHATGPT_E2E=1`，部分浏览器探测还要求 `E2E_HEADED=1`。详细条件见 `doc/e2e_test_design.md`。
- 默认服务不验证客户端 API Key，并依赖本机浏览器登录态。因此默认仅监听 `127.0.0.1`，不要把服务直接暴露到可信边界之外。
