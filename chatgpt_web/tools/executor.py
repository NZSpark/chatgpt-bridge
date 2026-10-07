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
    """
    if enabled is None:
        enabled = config.EDIT_MARKDOWN_LOCAL
    if backup_dir is None:
        backup_dir = config.EDIT_MARKDOWN_BACKUP_DIR
    if not enabled or not tool_calls:
        return tool_calls
    out: List[Dict[str, Any]] = []
    for call in tool_calls:
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
                    out.append({**call, "id": call_id, "result": result, "duplicate": True})
                    continue
                metrics.inc("tool_duplicate_total")
                logger.warning(
                    "重复工具执行被跳过：session_key=%r tool_call_id=%s tool=%s",
                    session_key or "default",
                    call_id,
                    existing.tool_name,
                )
                out.append({**call, "id": call_id, "result": existing.result, "duplicate": True})
                continue
            started_at = time.monotonic()
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
            out.append({**call, "id": call_id, "result": result})
        else:
            out.append(call)
    return out


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
        return {"ok": False, "error": "edit_markdown 需要合法的 start/end"}
    new_text = args.get("new_text", "")
    if not isinstance(new_text, str):
        return {"ok": False, "error": "edit_markdown 的 new_text 必须是字符串"}
    write_requested = bool(args.get("write", False))
    do_write = write_requested and config.EDIT_MARKDOWN_WRITE
    write_refused = write_requested and not do_write

    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        return {"ok": False, "error": f"文件不存在：{path}"}
    except OSError as exc:
        return {"ok": False, "error": f"无法访问文件：{exc}"}
    max_file_bytes = config.EDIT_MARKDOWN_MAX_FILE_BYTES
    if max_file_bytes > 0 and file_size > max_file_bytes:
        return {
            "ok": False,
            "error": (
                f"文件过大：{file_size} bytes > "
                f"EDIT_MARKDOWN_MAX_FILE_BYTES={max_file_bytes}"
            ),
        }

    try:
        doc = markdown_io.read_md(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"文件不存在：{path}"}
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}

    total = len(doc.lines)
    if start < 1 or end > total or start > end:
        return {"ok": False, "error": f"行号越界：{start}-{end}（共 {total} 行）"}

    try:
        edited = markdown_io.apply_edit(doc, start, end, new_text)
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}

    issues = markdown_io.verify(edited)
    if issues:
        return {
            "ok": False,
            "error": "编辑后结构校验未通过",
            "issues": [{"code": i.code, "message": i.message, "line": i.line} for i in issues],
        }

    backup_path = None
    if do_write:
        try:
            backup = markdown_io.backup_md(path, backup_dir=backup_dir)
            backup_path = str(backup.backup_path)
        except OSError as exc:
            logger.warning("edit_markdown 备份失败（%s）：未写盘。", path, exc_info=True)
            return {"ok": False, "error": f"备份失败：{exc}"}

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
            "已忽略 write=true：EDIT_MARKDOWN_WRITE=false（默认只做 dry-run，"
            "不落盘）。需要落盘请在 .env 里显式打开。"
        )
    return result
