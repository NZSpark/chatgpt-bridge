"""异常与常量：会话 / 上游交互过程中可区分的失败类型。

集中放在这里，便于 ``driver`` 与各 mixin 模块共同引用，
也便于上层（``server`` / ``streaming``）只依赖异常类型而不依赖 Driver。
"""


import inspect


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


# 未指定任务标识时使用的会话桶（保持与历史行为一致：全局共用一条会话）
DEFAULT_SESSION_KEY = "default"
# ChatGPT 网页版入口（每桶新开对话的落点）。
HOME_URL = "https://chatgpt.com"
