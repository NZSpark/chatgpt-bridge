"""异常与常量：会话 / 上游交互过程中可区分的失败类型。

集中放在这里，便于 ``driver`` 与各 mixin 模块共同引用，
也便于上层（``server`` / ``streaming``）只依赖异常类型而不依赖 Driver。
"""


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


# 未指定任务标识时使用的会话桶（保持与历史行为一致：全局共用一条会话）
DEFAULT_SESSION_KEY = "default"
# ChatGPT 网页版入口（每桶新开对话的落点）。
HOME_URL = "https://chatgpt.com"
