"""工具调用请求验证与规范化（PI-901-3）。

承载类型化边界（:class:`~chatgpt_web.tools.parser.ToolCallRequest`）上的：

* 参数 / 名字 / id 的结构校验；
* 可选 per-tool 参数校验器；
* 名字与参数的规范化（normalize）；
* 按 ``tool_call_id`` 去重。

错误类型保持兼容：校验失败抛 :class:`~chatgpt_web.errors.ToolCallValidationError`。
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from ..errors import ToolCallValidationError
from .parser import ToolCallRequest

logger = logging.getLogger(__name__)


def validate_tool_call_requests(
    requests: List[ToolCallRequest],
    validators: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None,
) -> List[ToolCallRequest]:
    """Validate the typed boundary and optional per-tool argument validators."""
    validators = validators or {}
    validated: List[ToolCallRequest] = []
    for request in requests:
        if not isinstance(request, ToolCallRequest):
            raise ToolCallValidationError("tool call must be a ToolCallRequest")
        if not request.id.strip():
            raise ToolCallValidationError("tool call id is required")
        if not request.name.strip():
            raise ToolCallValidationError("tool call name is required")
        if not isinstance(request.arguments, dict):
            raise ToolCallValidationError(
                f"tool call arguments must be an object: {request.name}"
            )
        validator = validators.get(request.name)
        if validator is not None:
            try:
                accepted = validator(dict(request.arguments))
            except Exception as exc:  # noqa: BLE001
                raise ToolCallValidationError(
                    f"invalid arguments for tool {request.name}: {exc}"
                ) from exc
            if accepted is False:
                raise ToolCallValidationError(
                    f"invalid arguments for tool {request.name}"
                )
        validated.append(request)
    return validated


def normalize_tool_call_requests(
    requests: List[ToolCallRequest],
) -> List[ToolCallRequest]:
    """Normalize names and copy arguments without changing tool semantics."""
    return [
        ToolCallRequest(
            id=request.id.strip(),
            name=request.name.strip(),
            arguments=dict(request.arguments),
            source_span=request.source_span,
            raw_text=request.raw_text,
        )
        for request in requests
    ]


def deduplicate_tool_call_requests(
    requests: List[ToolCallRequest],
) -> List[ToolCallRequest]:
    """Drop repeated call ids while preserving distinct parallel calls."""
    seen_ids: set[str] = set()
    unique: List[ToolCallRequest] = []
    for request in requests:
        if request.id in seen_ids:
            logger.warning(
                "重复 tool_call 被丢弃：tool_call_id=%s tool=%s",
                request.id,
                request.name,
            )
            continue
        seen_ids.add(request.id)
        unique.append(request)
    return unique
