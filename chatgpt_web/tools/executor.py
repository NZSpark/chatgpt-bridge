"""工具执行（PI-901-5）：把 ``ToolCallRequest`` 交给注入的 executor，并驱动完整管线。

executor **不依赖模型输出格式**：它只消费类型化的 :class:`ToolCallRequest`
及其映射；文本解析发生在 :mod:`chatgpt_web.tools.parser`。

同时承载桥接层内置工具 ``edit_markdown`` 的本地执行（server / responses 共用）。
"""

import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .. import config
from ..errors import ToolCallExecutionError, ToolCallParseError
from ..logging_setup import log_context
from ..metrics import metrics
from . import policy as _policy
from .ledger import TOOL_EXECUTION_LEDGER, ToolExecutionLedger
from .parser import (
    _TOOL_CALL_FENCE_RE,
    _TOOL_CALL_LINE_RE,
    EDIT_MARKDOWN_TOOL_NAME,
    ToolCallRequest,
    parse_tool_call_requests,
)
from .serializer import serialize_tool_call_results
from .validator import (
    deduplicate_tool_call_requests,
    normalize_tool_call_requests,
    validate_tool_call_requests,
)

logger = logging.getLogger(__name__)


def execute_tool_call_requests(
    requests: List[ToolCallRequest],
    executor: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """Execute the typed batch through an injected executor boundary."""
    mappings = tool_call_request_mappings(requests)
    if executor is None:
        return mappings
    try:
        result = executor(mappings)
    except Exception as exc:  # noqa: BLE001
        raise ToolCallExecutionError(f"tool execution failed: {exc}") from exc
    if not isinstance(result, list):
        raise ToolCallExecutionError("tool executor must return a list")
    return result


def run_tool_call_pipeline(
    text: str,
    *,
    allowed_tools: Optional[set[str]] = None,
    policy: Optional[_policy.ToolPolicy] = None,
    validators: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None,
    executor: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """Run parse → validate → normalize → deduplicate → policy → execute → serialize."""
    requests = parse_tool_call_requests(text)
    if not requests:
        marker = bool(
            _TOOL_CALL_LINE_RE.search(text or "")
            or _TOOL_CALL_FENCE_RE.search(text or "")
            or re.search(r"(?m)^\s*tool[-_]?call(?:\s|$)", text or "", re.IGNORECASE)
        )
        if marker:
            raise ToolCallParseError("tool call marker was present but no valid call was parsed")
        return []
    requests = validate_tool_call_requests(requests, validators)
    requests = normalize_tool_call_requests(requests)
    requests = deduplicate_tool_call_requests(requests)
    requests = _policy.check_tool_call_policy(requests, allowed_tools, policy)
    results = execute_tool_call_requests(requests, executor)
    return serialize_tool_call_results(results)


def tool_call_request_mappings(
    requests: List[ToolCallRequest],
) -> List[Dict[str, Any]]:
    """Compatibility conversion for call sites that still require dictionaries."""
    return [request.as_mapping() for request in requests]


def _edit_sort_key(call: Dict[str, Any]) -> int:
    """同一文件多调用时按 start 降序执行的排序键（越大越先执行）。

    模型给出的 start/end 都是**同一份原始文件**上的绝对行号；若从前往后
    执行，前面的编辑改变行数后，后面的行号就会漂移（改错位置或越界）。
    先改后面的行、再改前面的行，行号语义始终对齐原始快照（见
    doc/edit_markdown.md「同批多调用」）。缺省/非法 start 视为 0（最后执行）。
    """
    args = call.get("arguments")
    if not isinstance(args, dict):
        return 0
    try:
        return int(args.get("start"))
    except (TypeError, ValueError):
        return 0


def _order_same_file_edits(
    indices: List[int], tool_calls: List[Dict[str, Any]]
) -> List[int]:
    """把「同一批里对同一文件」的 edit_markdown 索引按 start 降序重排。

    只调整执行顺序，不改动返回顺序（返回仍按模型给出的原始顺序）。
    """
    return sorted(indices, key=lambda i: _edit_sort_key(tool_calls[i]), reverse=True)


def run_local_edit_markdown(
    tool_calls: List[Dict[str, Any]],
    *,
    session_key: Optional[str] = None,
    backup_dir: Optional[str] = None,
    enabled: Optional[bool] = None,
    ledger: ToolExecutionLedger = TOOL_EXECUTION_LEDGER,
) -> List[Dict[str, Any]]:
    """本地执行 edit_markdown：把结果挂回对应调用（server / responses 共用）。

    非 ``edit_markdown`` 的调用原样透传，顺序与内容不变。未开启本地执行或
    没有调用时原样返回。

    同一批里针对**同一文件**的多个 edit_markdown 按 ``start`` 降序执行，
    避免模型用原始行号从前往后改时行号漂移（见 :func:`_edit_sort_key`）。
    返回列表仍保持入参顺序。
    """
    if enabled is None:
        enabled = config.EDIT_MARKDOWN_LOCAL
    if backup_dir is None:
        backup_dir = config.EDIT_MARKDOWN_BACKUP_DIR
    if not enabled or not tool_calls:
        return tool_calls

    # 先按 (path) 分组，组内按 start 降序决定执行顺序；最终结果按原始下标写回。
    by_path: Dict[str, List[int]] = {}
    for idx, call in enumerate(tool_calls):
        if call.get("name") != EDIT_MARKDOWN_TOOL_NAME:
            continue
        args = call.get("arguments")
        raw_path = args.get("path") if isinstance(args, dict) else None
        path_key = raw_path.strip() if isinstance(raw_path, str) else ""
        by_path.setdefault(path_key, []).append(idx)
    execution_order: List[int] = [
        idx for idx, call in enumerate(tool_calls)
        if call.get("name") != EDIT_MARKDOWN_TOOL_NAME
    ]
    for group in by_path.values():
        if len(group) > 1:
            logger.info(
                "同批同文件 edit_markdown 多调用（%d 个），按 start 降序执行以避免行号漂移",
                len(group),
            )
        execution_order.extend(_order_same_file_edits(group, tool_calls))

    # 按执行顺序算结果，再按原始下标回填，保证输出顺序 == 入参顺序。
    results: Dict[int, Dict[str, Any]] = {}
    for idx in execution_order:
        call = tool_calls[idx]
        if call.get("name") != EDIT_MARKDOWN_TOOL_NAME:
            results[idx] = call
            continue
        if call.get("name") == EDIT_MARKDOWN_TOOL_NAME:
            metrics.inc("tool_call_total")
            call_id = str(call.get("id") or f"call_{uuid.uuid4().hex[:16]}")
            arguments = dict(call.get("arguments") or {})
            existing, done, owner = ledger.claim(session_key, call_id)
            if not owner:
                if existing is None:
                    done.wait()
                    existing = ledger.get(session_key, call_id)
                if existing is None:
                    result = {"ok": False, "error": "tool execution did not produce a ledger result"}
                    results[idx] = {**call, "id": call_id, "result": result, "duplicate": True}
                    continue
                metrics.inc("tool_duplicate_total")
                with log_context(tool_call_id=call_id):
                    logger.warning(
                        "重复工具执行被跳过：session_key=%r tool_call_id=%s tool=%s",
                        session_key or "default",
                        call_id,
                        existing.tool_name,
                    )
                results[idx] = {**call, "id": call_id, "result": existing.result, "duplicate": True}
                continue
            started_at = time.monotonic()
            # PI-021：把 tool_call_id 绑进日志关联上下文，退出后自动恢复，
            # 于是这一段的 ledger / metrics 日志都能从入口 request_id 追到具体工具调用。
            with log_context(tool_call_id=call_id):
                with metrics.timer("tool_execution_latency"):
                    try:
                        result = execute_edit_markdown(arguments, backup_dir=backup_dir)
                        success = bool(result.get("ok"))
                        error_type = None if success else "ToolExecutionError"
                    except Exception as exc:  # noqa: BLE001
                        error_type = type(exc).__name__
                        result = {"ok": False, "error": str(exc)}
                        success = False
                if not success:
                    metrics.inc("tool_execution_failure_total")
                ledger.record(
                    session_key=session_key,
                    tool_call_id=call_id,
                    tool_name=EDIT_MARKDOWN_TOOL_NAME,
                    normalized_arguments=arguments,
                    started_at=started_at,
                    success=success,
                    error_type=error_type,
                    result=result,
                )
            results[idx] = {**call, "id": call_id, "result": result}

    return [results[i] for i in range(len(tool_calls))]


def execute_edit_markdown(args: Dict[str, Any], *, backup_dir: str = "output/backups") -> Dict[str, Any]:
    """桥接层本地执行 edit_markdown。返回可直接回传的结构化结果。

    - 路径沙箱：只接受沙箱内的相对路径（见 :func:`resolve_edit_path`）。
    - 默认 dry-run：只返回统一 diff，不落盘。
    - ``write=true`` 仅在 ``EDIT_MARKDOWN_WRITE=true`` 时真正落盘；否则降级为
      dry-run 并在结果里说明（模型不能自己“申请”写权限）。
    - 任何结构性错误（围栏不配对、行号越界）都作为 error 返回，不抛给上层。
    """
    from .. import markdown_io

    requested_path = args.get("path")
    path, path_error = _policy.resolve_edit_path(requested_path)
    if path_error:
        return {"ok": False, "error": path_error}
    assert path is not None
    try:
        start = int(args["start"])
        end = int(args["end"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "edit_markdown requires integer start/end"}
    new_text = args.get("new_text", "")
    if not isinstance(new_text, str):
        return {"ok": False, "error": "edit_markdown requires new_text to be a string"}
    write_requested = bool(args.get("write", False))
    do_write = write_requested and config.EDIT_MARKDOWN_WRITE
    write_refused = write_requested and not do_write

    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except OSError as exc:
        return {"ok": False, "error": f"cannot access file: {exc}"}
    max_file_bytes = config.EDIT_MARKDOWN_MAX_FILE_BYTES
    if max_file_bytes > 0 and file_size > max_file_bytes:
        return {
            "ok": False,
            "error": (
                f"file too large: {file_size} bytes > "
                f"EDIT_MARKDOWN_MAX_FILE_BYTES={max_file_bytes}"
            ),
        }

    try:
        doc = markdown_io.read_md(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}

    total = len(doc.lines)
    if start < 1 or end > total or start > end:
        return {
            "ok": False,
            "error": f"line range out of bounds: {start}-{end} (file has {total} lines)",
        }

    try:
        edited = markdown_io.apply_edit(doc, start, end, new_text)
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}

    issues = markdown_io.verify(edited)
    if issues:
        return {
            "ok": False,
            "error": "structural validation failed after edit",
            "issues": [{"code": i.code, "message": i.message, "line": i.line} for i in issues],
        }

    backup_path = None
    if do_write:
        try:
            backup = markdown_io.backup_md(path, backup_dir=backup_dir)
            backup_path = str(backup.backup_path)
        except OSError as exc:
            logger.warning("edit_markdown 备份失败（%s）：未写盘。", path, exc_info=True)
            return {"ok": False, "error": f"backup failed, file not written: {exc}"}

    diff = markdown_io.write_md(edited, path=path, dry_run=not do_write)
    result: Dict[str, Any] = {
        "ok": True,
        "path": requested_path,
        "resolved_path": str(path),
        "start": start,
        "end": end,
        "write_requested": write_requested,
        "written": do_write,
        "backup": backup_path,
        "diff": diff,
    }
    if write_refused:
        result["note"] = (
            "write=true was ignored: EDIT_MARKDOWN_WRITE=false (default is dry-run, "
            "nothing written). Enable it in .env to allow persisting."
        )
    return result
