"""工具运行时子包（PI-901）。

把原本集中在 ``chatgpt_web/toolcalls.py`` 的工具调用逻辑拆成职责单一的模块：

    parser.py      解析：文本 -> 结构化调用（ToolCallRequest）
    validator.py   校验 / 规范化 / 去重
    policy.py      策略边界：允许列表、路径沙箱、写权限
    executor.py    执行：ToolCallRequest -> ToolResult，及内置 edit_markdown
    ledger.py      会话隔离的执行台账（去重 + 记录 + 结果哈希）
    serializer.py  结果序列化

``chatgpt_web.toolcalls`` 仍作为兼容 facade 存在，历史 import 路径不变。
"""

from .executor import (
    execute_edit_markdown,
    execute_tool_call_requests,
    run_local_edit_markdown,
    run_tool_call_pipeline,
    tool_call_request_mappings,
)
from .ledger import (
    TOOL_EXECUTION_LEDGER,
    ToolExecutionLedger,
    ToolExecutionRecord,
)
from .parser import (
    BUILTIN_TOOLS,
    EDIT_MARKDOWN_TOOL,
    EDIT_MARKDOWN_TOOL_NAME,
    ToolCallRequest,
    parse_reply_tool_calls,
    parse_tool_call_requests,
    parse_tool_calls,
    to_tool_call_models,
    tool_parameter_names,
)
from .policy import ToolPolicy, check_tool_call_policy, resolve_edit_path
from .serializer import serialize_tool_call_results
from .validator import (
    deduplicate_tool_call_requests,
    normalize_tool_call_requests,
    validate_tool_call_requests,
)

__all__ = [
    "ToolCallRequest",
    "parse_tool_calls",
    "parse_reply_tool_calls",
    "parse_tool_call_requests",
    "to_tool_call_models",
    "tool_parameter_names",
    "validate_tool_call_requests",
    "normalize_tool_call_requests",
    "deduplicate_tool_call_requests",
    "ToolPolicy",
    "check_tool_call_policy",
    "resolve_edit_path",
    "execute_tool_call_requests",
    "run_tool_call_pipeline",
    "tool_call_request_mappings",
    "run_local_edit_markdown",
    "execute_edit_markdown",
    "serialize_tool_call_results",
    "ToolExecutionRecord",
    "ToolExecutionLedger",
    "TOOL_EXECUTION_LEDGER",
    "BUILTIN_TOOLS",
    "EDIT_MARKDOWN_TOOL",
    "EDIT_MARKDOWN_TOOL_NAME",
]
