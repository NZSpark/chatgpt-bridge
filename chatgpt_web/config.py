"""配置加载与全部可调参数。

所有可调参数集中在这里，默认值即 ``.env.example`` 中列出的那一套。
其他模块通过 ``config.<NAME>`` **在运行时取属性**（而不是 ``from config import NAME``），
这样测试可以直接 ``patch.object(config, "NAME", value)`` 生效。
"""


import ipaddress
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


# ==================== 0. 配置加载 (.env) ====================
# 所有可调参数集中在项目根目录的 .env（模板见 .env.example）。
# 这里用一个极简的 .env 解析器，避免为读取配置引入额外依赖：
#   * 已存在的真实环境变量优先于 .env（便于临时覆盖 / CI）；
#   * 支持 `KEY=value`、`#` 注释、空行、值两侧引号。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[配置] 读取 {path} 失败，将使用默认值：{exc}")
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(ENV_FILE)


def env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ==================== 服务监听 ====================
HOST = env_str("HOST", "127.0.0.1")
PORT = env_int("PORT", 8002)


def is_loopback_host(host: str) -> bool:
    """HOST 是否只对本机可见（纯函数，便于单测与启动告警）。

    空串/``localhost`` 视为回环（与默认值 ``127.0.0.1`` 等价）；其余主机名
    一律按“可能对外”处理——安全告警宁可多喊一声，也不要默认放行。
    """
    raw = (host or "").strip().lower()
    if not raw or raw in ("localhost", "localhost.localdomain"):
        return True
    if raw.startswith("[") and raw.endswith("]"):  # [::1]
        raw = raw[1:-1]
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        # 主机名（非 IP）：只有明确的本机名才算回环，其余一律按“对外”处理
        return raw.split(".")[0] in ("localhost", "ip6-localhost")


# ==================== 路径 ====================
# 会话状态文件：记录每桶的轮数 / 体积 / 是否到顶（**不含会话 URL**）。
SESSION_FILE = Path(env_str("SESSION_FILE", "./user_data/.chatgpt_state"))
USER_DATA_DIR = env_str("USER_DATA_DIR", "./user_data")
OUTPUT_DIR = env_str("OUTPUT_DIR", "./output")


# ==================== 代码落盘 ====================
# 是否把回复里的代码块落盘到 OUTPUT_DIR。默认关闭：保存文件是本地扩展字段，
# 标准 OpenAI 客户端并不知情，默认开启会让每次请求都产生意外副作用。
# 请求体里的 save_files 仅在显式传入时覆盖（见 models.ChatCompletionRequest）。
SAVE_FILES = env_bool("SAVE_FILES")
# 落盘目录的保留策略（0 = 不限制）：超出后在启动时清理最旧的文件。
OUTPUT_MAX_FILES = env_int("OUTPUT_MAX_FILES", 0)
OUTPUT_MAX_AGE_DAYS = env_float("OUTPUT_MAX_AGE_DAYS", 0)


# ==================== 运行模式 / 调试 ====================
# 实测（2026-10）：HEADLESS=1 时 ChatGPT 会把请求挡在 Cloudflare 挑战页
# （title="Just a moment..."，body 为空、按钮数 0、composer 不渲染），
# 于是 _find_input / _open_new_chat 全部落空，表现为「找不到输入框/新建对话」。
# 因此**默认必须有头运行**（HEADLESS=false）。无显示服务器请用 xvfb-run 包一层
# 有头 Chromium，而不是设 HEADLESS=1。
HEADLESS = env_bool("HEADLESS")
# 打开后每轮轮询都打印一行状态，便于定位「为什么一直判不到结束」（CHATGPT_DEBUG=1）
DEBUG = env_bool("CHATGPT_DEBUG")

# /session/reset 的可选访问令牌：留空则维持原行为（不校验）。
# 该端点会让指定会话桶的下一轮重开对话，属于有副作用的本地操作，
# 同机多用户环境下建议设置 RESET_TOKEN，调用时带 X-Reset-Token 头。
RESET_TOKEN = env_str("RESET_TOKEN", "")

# /v1/chat/completions 流式生成期间的 keep-alive 注释间隔（秒）；0 = 关闭。
# 工具模式需要先缓冲整段回复才能判断 tool_calls，这期间客户端看不到内容，
# 用注释保活避免客户端超时断连（此前硬编码 10s）。
CHAT_KEEPALIVE_S = env_float("CHAT_KEEPALIVE_S", 10.0)


# ==================== 回复结束检测 / 超时 ====================
# 总超时（秒）：仅在「结束判定完全失灵 / 消息压根没发出去」时才会用到的兜底。
# 必须小于 Pi 侧 HTTP 客户端的超时，否则客户端会先报错。可用 CHATGPT_TIMEOUT 覆盖。
RESPONSE_TIMEOUT_S = env_float("CHATGPT_TIMEOUT", 180)
# 总超时到点时，若页面**仍在生成**，额外延长等待的秒数（0 = 不延长，旧行为）。
# 背景：思考模式 + 大 prompt 单轮可能超过 CHATGPT_TIMEOUT，但页面其实在正常生成。
# 旧行为会抛 ChatGPTTimeoutError → 外层重试 → **把同一句 prompt 再发一遍**，
# 网页于是多出一轮、与客户端状态错位。延长等待可避免这种“假超时重发”。
RESPONSE_TIMEOUT_EXTEND_S = env_float("RESPONSE_TIMEOUT_EXTEND_S", 300)
# 轮询间隔（秒）
POLL_INTERVAL_S = env_float("POLL_INTERVAL_S", 1.5)
# 兜底判定：内容（忽略首尾空白）完全相同连续这么多次即认为生成结束
STABLE_POLLS = env_int("STABLE_POLLS", 2)
# 次保守的兜底：仅凭“长度不再增长”收尾时要多等几轮，
# 避免生成中途的长停顿（如长思考）被误判成结束
LEN_STABLE_POLLS = env_int("LEN_STABLE_POLLS", 4)
# 结束判定的「内容静默窗口」：即便停止按钮已消失 / 文本已稳定，也要求页面
# 内容在连续这么多次轮询里**完全不再变化**才收尾。
# 背景：ChatGPT 会分段输出——先给一段不含 TOOL_CALL 的正文，停顿一下
# （停止按钮短暂消失、文本短暂稳定），随后**继续**输出含 TOOL_CALL 的内容。
# 旧逻辑一看到「停止按钮消失」就立即收尾，于是把后续 TOOL_CALL 段整段丢掉。
# 用静默窗口确认「确实不再有后续内容」后才结束；期间内容一旦恢复，计数清零。
RESUME_QUIET_POLLS = env_int("RESUME_QUIET_POLLS", 4)
# 连续多少次轮询既无正文也无「生成中」信号即判定页面卡死，提前失败（不再干等到总超时）
STALL_POLLS = env_int("STALL_POLLS", 20)


# ==================== 重试 ====================
# 上游超时的最大尝试次数与退避基数（秒）
MAX_UPSTREAM_RETRIES = env_int("CHATGPT_RETRIES", 2)
RETRY_BACKOFF_S = env_float("RETRY_BACKOFF_S", 1.0)


# ==================== 会话生命周期 ====================
# 启动时忽略已保存的会话，直接开一个新会话。搭配“播种”使用才安全（首轮会重放历史）。
NEW_SESSION_ON_START = env_bool("CHATGPT_NEW_SESSION")
# 「会话到顶」提示语的匹配规则（"||" 分隔多条正则，大小写不敏感）。
# 网页版到顶时会弹提示并停止响应，必须能与“真的卡住”区分开。
CAP_NOTICE_PATTERNS = [
    p.strip()
    for p in env_str(
        "CAP_NOTICE_PATTERNS",
        "达到对话长度上限||对话长度上限||已达到长度限制||达到长度限制||"
        "开启新对话||开始新的聊天||context length limit||start a new chat",
    ).split("||")
    if p.strip()
]
# 每 N 轮轮询检查一次“是否到顶”（避免每轮都对整页做 innerText 扫描）
CAP_CHECK_EVERY = env_int("CAP_CHECK_EVERY", 4)
# 「按任务隔离会话」使用的请求头：同一取值的请求共用一条网页会话，
# 不同取值各自维护独立的会话状态与页面（互不污染上下文）。
SESSION_KEY_HEADER = env_str("SESSION_KEY_HEADER", "X-ChatGPT-Session")
# 关闭后所有请求共用默认会话（旧行为）
SESSION_SCOPING = env_bool("SESSION_SCOPING", True)
# 当请求头与 user 字段都缺失时，是否允许**按 User-Agent 自动分桶**（不同客户端自动隔离）。
# 默认开：不同 AI 编程助手自动各用一条 ChatGPT 会话。关闭后退回旧的「默认桶，全局共用」行为。
# 注意：自动分桶会使桶数随客户端数量增长，实际受 MAX_SESSION_BUCKETS 约束（超出按 LRU 回收页面，状态保留）。
SESSION_SCOPING_BY_UA = env_bool("SESSION_SCOPING_BY_UA", True)
# 单个 key 的长度上限（防止超长头部变成文件名/JSON 键）
SESSION_KEY_MAX_LEN = env_int("SESSION_KEY_MAX_LEN", 64)
# 同时在用的会话桶数量上限。超出时**回收最久未用**的页面（状态保留，下次按 URL 恢复）。
# 0 表示不允许额外会话桶（所有请求都走默认桶）；想彻底关闭分桶用 SESSION_SCOPING=false。
MAX_SESSION_BUCKETS = env_int("MAX_SESSION_BUCKETS", 8)
# 内存里缓存的会话状态上限（超出按最久未用逐出）。
# 状态本就落盘（session_store._state 未命中会从磁盘恢复），逐出内存副本是安全的，
# 避免长跑时 _sessions / _last_prompts / _locks 三个 dict 无界增长。0 = 不限制。
MAX_SESSION_STATE_CACHE = env_int("MAX_SESSION_STATE_CACHE", 64)
# 空闲页面的回收间隔（秒）：超过这个时间没被用过的桶页面会被关闭（0 = 不按空闲回收）。
# 页面关掉不等于丢上下文：状态里的 url / turns 仍在，下次会重新打开并决定是否播种。
BUCKET_IDLE_TTL_S = env_float("BUCKET_IDLE_TTL_S", 900)
# 是否允许**按桶并发**（每个会话桶一把锁）。默认 false = 所有桶串行（更安全）。
# 打开后会同时驱动多个网页会话，可能触发风控，请自行评估。
PARALLEL_BUCKETS = env_bool("PARALLEL_BUCKETS")
# 等待某个会话桶锁的最长时间（秒）：0 = 一直等。
# >0 时，若同一会话桶已有请求在跑（同一 key 并发/重试堆叠），超过该时间就快速失败，
# 返回「上游繁忙」而不是无限排队、拖到客户端自己超时。不同桶互不影响。
# 默认 120s：网页版单轮（思考模式 + 上万字 prompt）常见几十秒，
# 早期默认 0（一直等）会让客户端先超时，默认 15 又过短，故取 120 作折中。
BUCKET_LOCK_TIMEOUT_S = env_float("BUCKET_LOCK_TIMEOUT_S", 120)
# 流式（Responses SSE）路径遇到「桶已忙」时是否排队等待（true），还是直接
# 返回 HTTP 503（false，默认）。返回 503 能让 Codex 走正常退避，避免它收到
# 一条 200 的空 SSE 后立刻重试、反复撞同一把锁（表现为「一直有另一个会话请求」）。
BUCKET_LOCK_QUEUE = env_bool("BUCKET_LOCK_QUEUE", False)
# 新建/恢复页面后等待输入框就绪的超时（毫秒）
READY_TIMEOUT_MS = env_int("READY_TIMEOUT_MS", 15000)
# 单次 fill() 填充输入框的超时（毫秒）。React 重挂载时旧句柄会失效，
# 这里给一个较短超时，由调用方重新定位输入框并重试，而不是干等 30s。
FILL_TIMEOUT_MS = env_int("FILL_TIMEOUT_MS", 10000)
# fill 的重试次数（每次都会重新定位输入框，规避 React 替换导致的失效句柄）。
FILL_RETRIES = env_int("FILL_RETRIES", 3)
# 播种（新会话时重放历史）的最大字符数预算；超出时保留最近的消息
SEED_MAX_CHARS = env_int("SEED_MAX_CHARS", 12000)
# 播种时**单条 system 消息**的最大字符数。harness（Codex / Pi）每轮都会把
# 完整的系统提示作为 system 消息发来，动辄上万字；播种时若原样重放，
# 会把简单请求灌成一大段系统提示。超出即截断。0 = 不限制（不推荐）。
SEED_SYSTEM_MAX_CHARS = env_int("SEED_SYSTEM_MAX_CHARS", 2000)
# 新会话播种时，紧跟在 [上下文重建] 头之后注入的一段环境说明。
# 用于明确告知模型：git 仓库就在本地、工作目录已就绪，直接下 git 命令即可，
# 不要反过来要求用户提供仓库地址或代为执行。留空则不注入。
SEED_ENV_NOTE = env_str(
    "SEED_ENV_NOTE",
    "[环境说明] git 仓库就在本地工作目录中，你可以直接执行 git 命令"
    "（如 git status / git add / git commit / git log）来完成提交、查看改动等操作，"
    "无需向用户索要仓库地址，也无需用户手动执行。",
)
# 单条 tool 结果（role=="tool"）注入 prompt 时的最大字符数。
# Codex/Pi 的 read 结果动辄几十万字符，直接 fill 会撑爆 ChatGPT 网页版输入框
# （Playwright fill 超时）。超出即截断并标注。0 = 不限制（不推荐）。
TOOL_RESULT_MAX_CHARS = env_int("TOOL_RESULT_MAX_CHARS", 20000)
# 工具说明注入时是否输出每个工具的完整 JSON Schema。
# 默认 false：只列 `name(必填参数): 描述`，能省下大量字符（Codex 的工具
# schema 动辄数千字，是播种 prompt 变长的隐藏大头）。true = 旧行为（全量 schema）。
TOOLS_INSTRUCTION_VERBOSE = env_bool("TOOLS_INSTRUCTION_VERBOSE", False)
# 工具说明里单条描述的最大字符数（0 = 不限制）。过长的描述无助于模型选对工具。
TOOLS_DESC_MAX_CHARS = env_int("TOOLS_DESC_MAX_CHARS", 200)
# 单次 fill() 入参（整段 prompt）的最大字符数硬上限，兜底防止输入框溢出。
# 这是发送侧最后一道护栏：无论上游怎么拼 prompt，都不超过它。0 = 不限制。
PROMPT_MAX_CHARS = env_int("PROMPT_MAX_CHARS", 1000000)
# 网页会话超过以下任一阈值后，下一轮自动轮转到新会话（0 表示禁用该维度）
SESSION_MAX_TURNS = env_int("SESSION_MAX_TURNS", 120)
SESSION_MAX_TOKENS = env_int("SESSION_MAX_TOKENS", 10000000)


# ==================== Responses API（Codex CLI）====================
# 是否启用 /v1/responses 路由。默认开启；关闭后该端点返回 404，
# 且 /v1/chat/completions（Pi）完全不受影响。
ENABLE_RESPONSES_API = env_bool("ENABLE_RESPONSES_API", True)
# 流式生成期间发送 keep-alive 注释的间隔（秒）；0 = 关闭。
# 网页版生成慢，Codex 侧 stream_idle_timeout_ms 较大时用它保活连接。
RESPONSES_KEEPALIVE_S = env_float("RESPONSES_KEEPALIVE_S", 10.0)
# ==================== 内置工具：edit_markdown ====================
# 是否允许桥接层在本地执行模型发出的 edit_markdown（Markdown 锚点编辑）。
# 关闭时 edit_markdown 仍可作为普通工具名被解析，由客户端自行执行。
EDIT_MARKDOWN_LOCAL = env_bool("EDIT_MARKDOWN_LOCAL", False)
# edit_markdown 落盘前的备份目录。
EDIT_MARKDOWN_BACKUP_DIR = env_str("EDIT_MARKDOWN_BACKUP_DIR", "output/backups")
# edit_markdown 的**路径沙箱根目录**：模型给出的相对路径必须解析到该目录之内，
# 绝对路径与含 `..` 的路径一律拒绝（否则被注入工具的模型可覆盖本机任意文件）。
# 默认项目根；留空也回退到项目根（不提供“关闭沙箱”的选项）。
EDIT_MARKDOWN_ROOT = env_str("EDIT_MARKDOWN_ROOT", str(PROJECT_ROOT))
# 是否允许 edit_markdown 真正落盘。默认 false：即使模型传 `write=true`，
# 也只返回 unified diff（dry-run），并在结果里说明被降级的原因。
EDIT_MARKDOWN_WRITE = env_bool("EDIT_MARKDOWN_WRITE", False)


# 工具模式下：是否先缓冲整段回复再判断 tool_calls（true = 需要缓冲，
# 因为要等完整文本才能解析出 function_call；false = 直接透传文本增量）。
RESPONSES_TOOL_BUFFER = env_bool("RESPONSES_TOOL_BUFFER", True)


# ==================== 任务快照（轮转后续接任务）====================
# 是否启用任务快照：每个会话桶在 user_data/.chatgpt_tasks/ 下维护一份轻量任务状态
# （任务目标 + 最近进展）。网页会话轮转播种时优先注入它，确保任务目标不被
# SEED_MAX_CHARS 截断，从而“不丢任务”。关闭后回到纯历史播种的旧行为。
TASK_SNAPSHOT_ENABLED = env_bool("TASK_SNAPSHOT_ENABLED", True)
# 任务快照存放目录。
TASK_FILE_DIR = env_str("TASK_FILE_DIR", "./user_data/.chatgpt_tasks")
# 任务快照命名空间：同一台机器上若跑着多个「桥」项目（如 DeepseekBridge /
# ChatGPTBridge）并共用 TASK_FILE_DIR，同名会话桶（default、ua:xxx）会互相覆盖，
# 表现为“A 项目读到 B 项目的任务”。快照会写到 TASK_FILE_DIR/<namespace>/ 下，
# 并在文件里记录 namespace，读取时校验归属。
# 留空则自动从包名派生（chatgpt_web -> chatgpt，deepseek_web -> deepseek）。
TASK_NAMESPACE = env_str("TASK_NAMESPACE", "") or (
    Path(__file__).resolve().parent.name.split("_")[0] or "default"
)
# 任务目标（第一条 user 消息）保留的最大字符数；超出截断。
TASK_GOAL_MAX_CHARS = env_int("TASK_GOAL_MAX_CHARS", 2000)
# 快照里滚动保留的最近消息条数（用于“最近进展”）。
TASK_KEEP_MESSAGES = env_int("TASK_KEEP_MESSAGES", 8)
# 任务快照里单条 recent 文本的最大字符数；防止 harness 注入的超长系统块
# （skills / permissions / collaboration_mode 等）撑爆快照，轮转播种时把 prompt 灌满。
TASK_RECENT_ITEM_MAX_CHARS = env_int("TASK_RECENT_ITEM_MAX_CHARS", 500)


# ==================== DOM 选择器 ====================
# 统一集中在这里，网页版改版时只需改这一处（也可用 .env 覆盖而无需改代码）。
# 回复节点的候选选择器（逗号分隔的 CSS 列表，直接交给 query_selector_all）
#
# 线上实测（2026-10-06，真实 DOM）：网页版改版后助手回复容器不再带
# [data-message-author-role="assistant"]，message-content / .markdown 也全部落空
# （逐条命中数都是 0），于是轮询 nodes=0 一路空转到超时、拿不到任何回复内容。
# 新版 DOM 的助手正文容器是 <div class="MarkdownRoot-<hash>" data-markdown-text-style ...>，
# 用户消息则是 [data-user-message-bubble]（**不能**选进来，否则会把用户自己发的
# 内容当成回复）。因此按「语义属性优先 + 老版属性兜底」排列：
#   [data-markdown-text-style] → 新版助手正文（非哈希属性，改版时最稳）
#   [class*="MarkdownRoot"]    → 同一容器，class 前缀兜底（哈希后缀会变）
#   [data-message-author-role="assistant"] / message-content / .markdown → 老版
# 实测排除项：composer（div.ProseMirror）**不带** data-markdown-text-style，
# 全新对话页上以上选择器命中数全为 0，不会把输入框/空白页误判成回复。
# 注意：本值直接喂 page.query_selector_all()，必须是**逗号分隔**的 CSS 列表，
# 不能用 "||"（那是 INPUT/SEND 这类逐条 wait_for_selector 的分隔符）。
RESPONSE_SELECTORS = env_str(
    "RESPONSE_SELECTORS",
    '[data-message-author-role="assistant"], [data-markdown-text-style], '
    '[class*="MarkdownRoot"], message-content, .markdown',
)
# 输入框候选选择器（.env 中用 "||" 分隔多个候选）
# 实测（真实 DOM）：composer 是 ProseMirror 的 ``div#prompt-textarea``
# （contenteditable=true, role=textbox, aria-label="Chat with ChatGPT"）。
# 旧的 ``rich-textarea`` 已不存在；且该元素常被判定为 not visible，
# 因此定位必须用 state="attached"（见 chat_io._find_input）。
INPUT_SELECTORS = [
    s.strip()
    for s in env_str(
        "INPUT_SELECTORS",
        '#prompt-textarea||div.ProseMirror[contenteditable="true"]||'
        # :not([data-language]) 排除新版代码块里的 CodeMirror 编辑器
        # （div.cm-content 也是 contenteditable + role=textbox，且有 data-language）。
        # 不排除的话，一旦 composer 选择器落空，_find_input 会把代码块里的
        # “编辑代码”编辑器当成输入框，把 prompt 写进回复正文。
        'div[contenteditable="true"][role="textbox"]:not([data-language])||textarea',
    ).split("||")
    if s.strip()
]
# 发送按钮候选选择器（.env 中用 "||" 分隔多个候选）。
# 提交优先走真实键盘 Enter，其次合成 DOM 事件，只有都无效时才点它
# （见 chat_io._submit_prompt）。
# 实测（真实 DOM）：发送按钮是 button[data-testid="send-button"]
# （aria-label="Send prompt", type=submit）。
SEND_BUTTON_SELECTORS = [
    s.strip()
    for s in env_str(
        "SEND_BUTTON_SELECTORS",
        'button[data-testid="send-button"]||'
        'button[aria-label="Send prompt"]||button[aria-label*="Send"]||'
        'button[type="submit"]',
    ).split("||")
    if s.strip()
]
# 页面就绪（输入框出现）用的选择器
# 实测：composer 是 #prompt-textarea / .ProseMirror；回复节点用 author-role。
READY_SELECTOR = env_str(
    "READY_SELECTOR",
    '#prompt-textarea, div.ProseMirror, [data-message-author-role="assistant"]',
)
# 新建对话入口：每桶首次请求与轮转时点击，确保从干净会话开始。
# 线上实测（2026-10-06）：testid 只在部分版本存在，「同一选择器匹配到多个
# 节点（当前会话项 / 折叠态零尺寸）」也是常态；选择器只负责「找得到」，
# 「点得中」由 completion._open_new_chat 的候选排序 + JS click 兜底保证。
NEW_CHAT_SELECTOR = env_str(
    "NEW_CHAT_SELECTOR",
    '[data-testid="create-new-chat-button"]||'
    'a[aria-label="New chat"]||a[aria-label="新对话"]||a[aria-label="新聊天"]||'
    'button[aria-label="New chat"]||button[aria-label="新对话"]||'
    'button[aria-label="新聊天"]||'
    # 工作区版本（如 “New chat in Techtorium”）用前缀匹配兜底
    'button[aria-label*="New chat"]||a[aria-label*="New chat"]||'
    'a[href="/"]',
)
# ---- 默认开启「思考模式」----
# 每次新建对话 / 轮转会话后，自动选中 composer 上的 Think 模式，否则网页版
# 会以简版模型作答（回复过于简单）。实测该控件是 composer 上的 pill：
#   <button class="__composer-pill ..." aria-pressed="false">Think</button>
# 选中后 aria-pressed="true"。选择器用「类名 + 文本」双条件匹配（见 completion.py）。
THINK_MODE_DEFAULT = env_bool("THINK_MODE_DEFAULT", True)
# Think 按钮的候选选择器（"||" 分隔，逐个尝试）
THINK_MODE_SELECTOR = env_str(
    "THINK_MODE_SELECTOR",
    'button.__composer-pill||button[class*="__composer-pill"]',
)
# 判定为「思考模式」按钮的文本关键词（大小写不敏感，"||" 分隔）
THINK_MODE_TEXTS = env_str("THINK_MODE_TEXTS", "think||思考")

# 代码块 DOM
#
# 2026-10-06 线上实测：新版网页版把代码块换成 div.CodeBlock-<hash>，内部**没有**
# pre/code，代码正文交给 CodeMirror 渲染：
#   div.CodeBlock-<hash>
#     div[data-markdown-copy="exclude"]        ← 语言名 / Copy / Run code 的头部（非正文）
#     div.cm-content[data-language="python"]   ← 代码正文（role=textbox）
# 因此：块容器选 [class*="CodeBlock"]（并保留 pre 兼容旧版），
# 正文/语言节点选 [data-language]（并保留 code 兼容旧版）——只选正文节点，
# 语言头就不会混进提取出的代码。
CODE_BLOCK_SELECTOR = env_str("CODE_BLOCK_SELECTOR", '[class*="CodeBlock"], pre')
CODE_TAG_SELECTOR = env_str("CODE_TAG_SELECTOR", "[data-language], code")
