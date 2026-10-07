"""工具结果序列化：校验 JSON 可序列化并返回独立副本（PI-901）。"""

import json
from typing import Any, Dict, List

from ..errors import ToolCallSerializationError


def serialize_tool_call_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate JSON serializability and return detached result mappings."""
    try:
        return [json.loads(json.dumps(result, ensure_ascii=False)) for result in results]
    except (TypeError, ValueError) as exc:
        raise ToolCallSerializationError(
            f"tool result is not JSON serializable: {exc}"
        ) from exc
