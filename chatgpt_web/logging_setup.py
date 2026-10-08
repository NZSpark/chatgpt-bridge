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
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

LOGGER_NAME = "chatgpt_web"
_CORRELATION_FIELDS = (
    "request_id",
    "session_key",
    "tool_call_id",
    "attempt_id",
    "page_id",
)
_FORMAT = (
    "%(asctime)s %(levelname)s "
    "[request_id=%(request_id)s session_key=%(session_key)s "
    "tool_call_id=%(tool_call_id)s attempt_id=%(attempt_id)s page_id=%(page_id)s] "
    "%(name)s: %(message)s"
)
_configured = False

_log_context: ContextVar[dict[str, str]] = ContextVar(
    "chatgpt_log_context",
    default={field: "-" for field in _CORRELATION_FIELDS},
)


class _RequestIdFilter(logging.Filter):
    """把当前关联上下文注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _log_context.get()
        for field in _CORRELATION_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, context.get(field, "-"))
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


def set_log_context(**fields: str) -> None:
    """更新当前异步任务的日志关联字段。"""
    unknown = set(fields) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"未知日志关联字段：{sorted(unknown)}")
    current = dict(_log_context.get())
    for field, value in fields.items():
        current[field] = str(value) if value is not None else "-"
    _log_context.set(current)


def clear_log_context(*fields: str) -> None:
    """清除指定关联字段；不传参数时清除全部字段。"""
    target = fields or _CORRELATION_FIELDS
    unknown = set(target) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"未知日志关联字段：{sorted(unknown)}")
    current = dict(_log_context.get())
    for field in target:
        current[field] = "-"
    _log_context.set(current)


@contextmanager
def log_context(**fields: str) -> Iterator[None]:
    """临时绑定日志关联字段，退出时恢复调用方原上下文。"""
    unknown = set(fields) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"未知日志关联字段：{sorted(unknown)}")
    previous = _log_context.get()
    current = dict(previous)
    for field, value in fields.items():
        current[field] = str(value) if value is not None else "-"
    token = _log_context.set(current)
    try:
        yield
    finally:
        _log_context.reset(token)


def set_request_id(request_id: str) -> None:
    """把 request_id 绑定到当前任务上下文（异步任务各自独立）。"""
    set_log_context(request_id=request_id)


def current_request_id() -> str:
    return str(_log_context.get().get("request_id", "-"))


def current_log_context() -> dict[str, str]:
    """返回当前日志关联上下文的快照。"""
    return dict(_log_context.get())
