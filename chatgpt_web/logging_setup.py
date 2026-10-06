"""统一日志配置（doc/tasks.md T3.1）。

约定：

* 库代码一律 ``logging.getLogger(__name__)``，**不再用 ``print``**；
* 级别由 ``CHATGPT_DEBUG`` 控制（true → DEBUG，否则 INFO）；
* 只在服务入口（``chatgpt_api_server.py``）调用一次 :func:`configure_logging`；
  测试不需要它——直接 ``caplog`` / 读取 logger 级别即可；
* 每个请求用 :func:`set_request_id` 绑定一个短 id，日志格式里带 ``[request_id]``，
  多 Agent 并发时可以按 id 把一次请求的多条日志串起来（响应头也会回传）。
"""

import logging
import time
import uuid
from contextvars import ContextVar

LOGGER_NAME = "chatgpt_web"
_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
_configured = False

_request_id: ContextVar[str] = ContextVar("chatgpt_request_id", default="-")


class _RequestIdFilter(logging.Filter):
    """把当前上下文的 request_id 注入每条日志记录（格式串用 %(request_id)s）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id.get()
        return True


def configure_logging(force: bool = False) -> None:
    """按 ``config.DEBUG`` 配置 ``chatgpt_web`` 命名空间的日志级别（幂等）。"""
    global _configured
    if _configured and not force:
        return
    from . import config

    logger = logging.getLogger(LOGGER_NAME)
    # 不接管 root：uvicorn 自带 access/error 日志，交给它自己的 handler。
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_RequestIdFilter())
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if config.DEBUG else logging.INFO)
    logger.propagate = True
    _configured = True


def new_request_id() -> str:
    """短请求 id：多 Agent 并发时用于把同一请求的多条日志串起来。"""
    return f"{int(time.time() % 100000):05d}-{uuid.uuid4().hex[:6]}"


def set_request_id(request_id: str) -> None:
    """把 request_id 绑定到当前任务上下文（异步任务各自独立）。"""
    _request_id.set(request_id)


def current_request_id() -> str:
    return _request_id.get()
