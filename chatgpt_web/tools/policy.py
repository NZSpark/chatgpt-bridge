"""工具执行策略边界（PI-901-4）：允许列表、路径沙箱、写权限、运行时/网络策略。

策略在 executor 之前执行（见 :func:`check_tool_call_policy`）：任何越权请求都
在真正执行前被拒绝。
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .. import config
from ..errors import ToolCallPolicyError
from .parser import EDIT_MARKDOWN_TOOL_NAME, ToolCallRequest


def resolve_edit_path(path: Any) -> tuple[Optional[Path], Optional[str]]:
    """把模型给出的 path 解析到沙箱之内，返回 ``(沙箱内的绝对路径, 错误原因)``。

    安全约束（T1.3）：edit_markdown 是**模型可控**的写文件工具，而模型可能
    被网页内容 / 工具结果注入。因此：

    * 拒绝绝对路径（不管它是否在沙箱内）；
    * 拒绝含 ``..`` 段的路径；
    * 解析后必须落在 ``config.EDIT_MARKDOWN_ROOT``（默认项目根）之内。

    返回的第二个值非空时，调用方应直接以 ``{"ok": false, "error": ...}`` 回传。
    """
    if not isinstance(path, str) or not path.strip():
        return None, "edit_markdown 需要 path"
    raw = Path(path.strip())
    if raw.is_absolute():
        return None, f"路径越界（不允许绝对路径）：{path}"
    if ".." in raw.parts:
        return None, f"路径越界（不允许 .. 段）：{path}"
    root = Path(config.EDIT_MARKDOWN_ROOT or config.PROJECT_ROOT).resolve()
    resolved = (root / raw).resolve()
    try:
        inside = resolved.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python < 3.9 的兜底
        inside = str(resolved).startswith(str(root) + os.sep)
    if not inside:
        return None, f"路径越界（必须位于 {root} 之内）：{path}"
    # 返回**解析后的绝对路径**：读写必须落在沙箱解析结果上，
    # 而不是让 markdown_io 拿相对路径去拼当前工作目录（可能落到沙箱之外）。
    return resolved, None


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Policy boundary for model-driven local tool execution.

    The first implementation is intentionally narrow: only ``edit_markdown``
    is policy-aware. The remaining fields make the boundary explicit so future
    tools do not need to grow ad-hoc security checks in the executor.
    """

    allowed_tools: frozenset[str] = frozenset({EDIT_MARKDOWN_TOOL_NAME})
    allowed_paths: tuple[str, ...] = ()
    write_enabled: bool = False
    network_enabled: bool = False
    max_output_chars: int = 0
    max_runtime_s: float = 0.0
    confirmation_policy: str = "none"

    def validate_request(self, request: ToolCallRequest) -> ToolCallRequest:
        if request.name not in self.allowed_tools:
            raise ToolCallPolicyError(f"tool not allowed: {request.name}")
        if request.name != EDIT_MARKDOWN_TOOL_NAME:
            return request

        arguments = request.arguments
        requested_write = bool(arguments.get("write", False))
        if requested_write and not self.write_enabled:
            raise ToolCallPolicyError(
                "edit_markdown write is not allowed by the active ToolPolicy"
            )

        path = arguments.get("path")
        resolved, error = resolve_edit_path(path)
        if error:
            raise ToolCallPolicyError(error)
        assert resolved is not None

        if self.allowed_paths:
            allowed_roots = [Path(root).resolve() for root in self.allowed_paths]
            if not any(
                resolved == root or root in resolved.parents
                for root in allowed_roots
            ):
                raise ToolCallPolicyError(
                    f"path is outside ToolPolicy.allowed_paths: {path}"
                )
        return request


def check_tool_call_policy(
    requests: list[ToolCallRequest],
    allowed_tools: Optional[set[str]] = None,
    policy: Optional[ToolPolicy] = None,
) -> list[ToolCallRequest]:
    """Apply the allow-list and, when provided, the full ToolPolicy boundary."""
    if policy is not None:
        if allowed_tools is not None:
            policy = ToolPolicy(
                allowed_tools=frozenset(allowed_tools),
                allowed_paths=policy.allowed_paths,
                write_enabled=policy.write_enabled,
                network_enabled=policy.network_enabled,
                max_output_chars=policy.max_output_chars,
                max_runtime_s=policy.max_runtime_s,
                confirmation_policy=policy.confirmation_policy,
            )
        return [policy.validate_request(request) for request in requests]
    if allowed_tools is None:
        return requests
    denied = [request.name for request in requests if request.name not in allowed_tools]
    if denied:
        names = ", ".join(sorted(set(denied)))
        raise ToolCallPolicyError(f"tools not allowed: {names}")
    return requests
