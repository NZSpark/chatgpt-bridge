"""异常与常量：会话 / 上游交互过程中可区分的失败类型。

集中放在这里，便于 ``driver`` 与各 mixin 模块共同引用，
也便于上层（``server`` / ``streaming``）只依赖异常类型而不依赖 Driver。
"""


import asyncio
import inspect
from typing import Optional


class BridgeError(RuntimeError):
    """Bridge 领域异常的统一基类。"""


class BrowserError(BridgeError):
    """ChatGPT Web / Playwright 交互领域错误。"""


class BrowserLookupError(BrowserError):
    """无法定位预期的网页元素或状态。"""


class BrowserInteractionError(BrowserError):
    """已定位网页元素，但交互或页面操作失败。"""


class ReplyExtractionError(BrowserError):
    """页面存在响应，但无法可靠提取最终回复。"""


class ToolError(BridgeError):
    """本地工具调用领域错误。"""


class ToolParseError(ToolError):
    """工具调用文本无法解析。"""


class ToolCallPipelineError(ToolError):
    """Base error for the explicit parse/validate/policy/execute pipeline."""

    stage = "pipeline"


class ToolCallParseError(ToolCallPipelineError, ToolParseError):
    stage = "parse"


class ToolCallValidationError(ToolCallPipelineError):
    stage = "validate"


class ToolCallPolicyError(ToolCallPipelineError):
    stage = "policy"


class ToolCallExecutionError(ToolCallPipelineError):
    stage = "execute"


class ToolCallSerializationError(ToolCallPipelineError):
    stage = "serialize"


class SessionStateError(BridgeError):
    """会话状态读取、写入或迁移失败。"""


class ConfigurationError(BridgeError):
    """配置缺失、非法或彼此冲突。"""


class ChatGPTTimeoutError(BrowserError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


class ChatGPTContextLimitError(BrowserError):
    """网页会话已达上下文长度上限（网页版会停止响应，必须换新会话）。"""


class ChatGPTBusyError(BrowserError):
    """某个会话桶正忙（同一会话已有请求在跑且等待超时）。

    与「上游出错」区分开：这是本地的排队保护，客户端稍后重试即可，
    因此会被映射成 HTTP 503 / SSE ``upstream_busy``，而**不会**触发重试阶梯。
    """


class ChatGPTPageLostError(BrowserError):
    """会话页面已失效：标签被用户关掉 / 渲染进程崩溃 / 页面已被回收。

    必须在语义上与「页面还活着，但选择器全不中」（网页版改版 / 未登录）分开：

    * 定位输入框时若抛出 ``TargetClosedError`` / ``Target crashed`` 这类异常，
      说明**页面对象已经没了**，不是改版——旧实现把它归到「选择器都没命中」，
      于是提示用户去检查登录，而该桶会从此刻起永久失败（页面池留着一具尸体）；
    * 本异常由重试阶梯捕获：重建页面（回到新的空白会话并按需播种上下文）后
      直接重发，**不轮转 / 不换新会话**，避免把刚恢复的上下文又丢掉。

    继承 :class:`BrowserError`（即 ``RuntimeError``）：重建仍失败时，
    上层依旧按「上游不可用」映射成 502 / ``upstream_error``，而不是裸 500。
    """


#: 页面失效类异常在 Playwright 里的措辞（不依赖 playwright 包，见 page_alive 注释）。
_PAGE_LOST_TOKENS = (
    "target page, context or browser has been closed",
    "target page or browser has been closed",
    "target crashed",
    "browser has been closed",
    "page has been closed",
    "context has been closed",
)


def is_page_lost_error(exc: BaseException) -> bool:
    """该异常是否表示「页面已经没了」（而不是选择器没命中 / 普通交互失败）。

    按**类型名 + 消息**判定，不 import playwright：``chatgpt_web`` 顶层导入时
    Playwright 是惰性导入的（见 :mod:`chatgpt_web.browser.driver`），这里不能破例。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in _PAGE_LOST_TOKENS)


def is_timeout_error(exc: BaseException) -> bool:
    """该异常是否只是「等超时」——定位元素时只有它算「没命中」。

    其余异常要么是页面已失效（见 :func:`is_page_lost_error`），要么是选择器写法
    有问题，都不该被“静默当成未命中”后让用户去查登录状态。
    """
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in type(exc).__name__.lower()


def page_alive(page) -> bool:
    """页面是否还活着（**快路径**，用于跳过明显已死的页面）。

    * ``page is None`` → False（没有页面可用）；
    * 没有 ``is_closed`` 的实现（测试替身 / 旧版对象）按**存活**处理，
      否则恢复路径会被误触发，测试替身也会被误判成死页面；
    * ``is_closed()`` 自身抛错时按存活处理——崩溃的渲染进程常常仍报
      ``is_closed() == False``，所以判活只能当快路径，真正的恢复必须能
      **无条件重建**（见 ``chat_io.send_chat`` 的页面失效分支）。
    """
    if page is None:
        return False
    is_closed = getattr(page, "is_closed", None)
    if is_closed is None:
        return True
    try:
        closed = is_closed()  # Playwright 的 is_closed() 是同步接口（不需要 await）
    except Exception:  # noqa: BLE001
        return True
    if inspect.isawaitable(closed):
        # 异步 is_closed（非本项目的实现）：这里不能阻塞，按存活处理
        return True
    return not closed


#: 判活的**慢路径**：一次最廉价的 JS 求值。崩溃 / 挂起的渲染进程连它都回不来，
#: 而 ``is_closed()`` 往往仍报 False（真机上表现为「输入框能定位、能点击，但读回来
#: 永远是空串」）。
_PAGE_PROBE_JS = "() => 1"
#: 探测等待上限（秒）：挂起的渲染进程会让求值**永不返回**，探测本身不能把请求拖死。
PAGE_PROBE_TIMEOUT_S = 5.0


async def page_responds(page, timeout_s: float = PAGE_PROBE_TIMEOUT_S) -> bool:
    """页面是否**还能执行 JS**（慢路径判活，用于区分「页面失效」与「配置问题」）。

    为什么需要它：:func:`page_alive` 只看 ``is_closed()``，而渲染进程崩溃 / 挂起时
    它仍报 False（快路径帮不上忙）。这类故障在真机上表现为「输入框定位成功、click
    成功、``insert_text`` 也成功，但校验读回来永远是空串」——旧实现据此提示用户
    「请检查登录状态与 INPUT_SELECTORS」，而页面池里那具尸体**永不重建**，
    该会话桶从此每次请求都 502（P0-J 的第二种死法）。

    没有 ``evaluate`` 的实现（测试替身 / 旧版对象）按**可响应**处理，与
    :func:`page_alive` 对缺失 ``is_closed`` 的处理一致：探测失败会触发页面重建，
    而无谓的重建会把刚恢复的上下文又丢掉。
    """
    if page is None:
        return False
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return True
    try:
        probe = evaluate(_PAGE_PROBE_JS)
        if inspect.isawaitable(probe):
            await asyncio.wait_for(probe, timeout=max(0.1, float(timeout_s)))
    except Exception:  # noqa: BLE001
        return False
    return True


def page_lost_reason(page) -> str:
    """给日志/错误文案用的可读原因（不回显页面正文）。

    文案以「标签已失效」开头：真机上这种故障被误认成「未登录」很久，
    错误信息必须能自证它属于**可自愈**的页面失效，而不是用户操作问题。
    """
    if page is None:
        return "标签已失效（没有可用的会话页面）"
    try:
        url = getattr(page, "url", "") or "?"
    except Exception:  # noqa: BLE001
        url = "?"
    if not page_alive(page):
        return f"标签已失效（页面已关闭，URL={url}）"
    return f"标签已失效（页面不可用，URL={url}，疑似渲染进程崩溃或页面已被回收）"


async def page_usable_reason(page, timeout_s: float = PAGE_PROBE_TIMEOUT_S) -> Optional[str]:
    """页面当前是否可用：不可用时返回可读原因（可直接拼进错误文案），可用时 None。

    判定顺序是**快路径 + 慢路径**：:func:`page_alive`（标签被用户关掉）→
    :func:`page_responds`（渲染进程崩溃 / 挂起，``is_closed()`` 仍报 False）。
    两者都过才算可用——只看快路径就会把「可自愈的页面失效」误判成「改版 / 未登录」，
    于是那个桶从此永久失败（P0-J），而那正是 2026-10-10 真机故障的形态。
    """
    if not page_alive(page):
        return page_lost_reason(page)
    if not await page_responds(page, timeout_s=timeout_s):
        return page_lost_reason(page) + "（页面已无响应：JS 求值报错或超时）"
    return None


# 未指定任务标识时使用的会话桶（保持与历史行为一致：全局共用一条会话）
DEFAULT_SESSION_KEY = "default"
# ChatGPT 网页版入口（每桶新开对话的落点）。
HOME_URL = "https://chatgpt.com"
