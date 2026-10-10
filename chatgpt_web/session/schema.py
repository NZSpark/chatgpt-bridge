"""会话状态数据模型与 schema 版本（PI-905）。

从原 ``session_store.py`` 抽出：``SessionState``、schema 版本常量、
空文档构造、v2 文档校验、以及 payload 字段过滤。
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

#: 当前磁盘状态文档的 schema 版本。
STATE_SCHEMA_VERSION = 2

#: 会话桶 payload 中允许持久化的字段（其余字段加载时丢弃）。
_SESSION_FIELDS = frozenset({
    "has_history",
    "turns",
    "est_tokens",
    "cap_hit",
    "pending_rotation",
    "last_error",
    "cap_failures",
    "updated_at",
    "linked_url",
})


def empty_state_document() -> Dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "sessions": {}}


def validate_state_document(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a v2 state document without changing session payload semantics."""
    if not isinstance(data, dict):
        raise ValueError("session state root must be an object")
    version = data.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported session state schema_version: {version!r}")
    sessions = data.get("sessions")
    if sessions is None:
        data["sessions"] = {}
    elif not isinstance(sessions, dict):
        raise ValueError("session state sessions must be an object")
    else:
        data["sessions"] = {
            key: value
            for key, value in sessions.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
    return data


def session_payload_only(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: payload[key] for key in _SESSION_FIELDS if key in payload}


@dataclass
class SessionState:
    """单个会话桶的状态。会话的“是否新开 / 能否复用”都由它决定。"""

    has_history: bool = False
    turns: int = 0
    est_tokens: int = 0
    cap_hit: bool = False
    pending_rotation: bool = False
    last_error: Optional[str] = None
    # 连续“到顶”失败次数：播种过大时会陷入「到顶→失败→下轮仍播种→再
    # 到顶」的死循环。累计到阈值后，重试时会改用更小的播种预算。成功
    # 收到回复或轮转到新会话时清零。
    cap_failures: int = 0
    updated_at: int = 0
    # 用户用 ``/link`` 绑定的网页会话 URL（None = 未绑定，见 :mod:`chatgpt_web.linking`）。
    # 绑定的桶不再落到空白新对话：页面漂移 / 句柄失效重建 / 轮转 / 启动恢复都会回到
    # 这条会话，所以一次句柄丢失不会把上下文丢在别处。
    linked_url: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionState":
        state = cls()
        if not isinstance(payload, dict):
            return state
        for name in ("has_history", "cap_hit", "pending_rotation"):
            if name in payload:
                setattr(state, name, bool(payload.get(name)))
        for name in ("turns", "est_tokens", "updated_at", "cap_failures"):
            try:
                setattr(state, name, int(payload.get(name) or 0))
            except (TypeError, ValueError):
                setattr(state, name, 0)
        last_error = payload.get("last_error")
        state.last_error = last_error if isinstance(last_error, str) else None
        linked = payload.get("linked_url")
        state.linked_url = linked.strip() if isinstance(linked, str) and linked.strip() else None
        return state
