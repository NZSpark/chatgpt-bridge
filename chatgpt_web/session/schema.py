"""会话状态数据模型与 schema 版本（PI-905）。

从原 ``session_store.py`` 抽出：``SessionState``、schema 版本常量、
空文档构造、v2 文档校验、以及 payload 字段过滤。
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

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
    "pending_reseed",
    "think_mode",
    "save_files",
    "speed_samples",
    "speed_recent_samples",
    "speed_prefill_tps_sum",
    "speed_decode_tps_sum",
    "speed_ttft_s_sum",
    "speed_decode_s_sum",
    "speed_prompt_tokens_sum",
    "speed_reply_tokens_sum",
    "speed_last_prompt_tokens",
    "speed_last_reply_tokens",
    "speed_last_ttft_s",
    "speed_last_decode_s",
    "speed_last_total_s",
    "speed_last_ttft_adj_s",
    "speed_last_decode_adj_s",
    "speed_last_poll_credit_s",
    "speed_last_settle_credit_s",
    "speed_last_prefill_tps",
    "speed_last_decode_tps",
    "speed_last_e2e_ttft_s",
    "speed_last_e2e_decode_tps",
    "speed_last_bridge_overhead",
    "speed_last_e2e_valid",
    "speed_last_saw_generating",
    "speed_last_streamed",
    "speed_last_rewritten",
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
    # ``/bridge session reseed`` 排队的一次性「下一轮重新播种」（见 chat_io）。
    # 必须落盘：只在当前处理函数里放个临时变量会在重启 / 并发 / 重试后丢失。
    pending_reseed: bool = False
    # ``/bridge settings think``：该桶的思考模式偏好（None = 跟随 config.THINK_MODE_DEFAULT）。
    # 按桶保存而不是改进程全局配置，避免一条聊天命令悄悄改变所有客户端的行为。
    think_mode: Optional[bool] = None
    # ``/bridge settings save-files``：该桶的代码块落盘偏好（None = 跟随 config.SAVE_FILES）。
    # 请求体里的 save_files 仍然优先级最高（显式声明 > 本桶偏好 > 全局配置）。
    save_files: Optional[bool] = None
    # /bridge token：会话桶内滚动统计。sum + sample count 可跨重启恢复，
    # 只保留聚合值和最近一次样本，避免状态文件随请求次数不断膨胀。
    speed_samples: int = 0
    speed_recent_samples: List[Dict[str, float]] = field(default_factory=list)
    speed_prefill_tps_sum: float = 0.0
    speed_decode_tps_sum: float = 0.0
    speed_ttft_s_sum: float = 0.0
    speed_decode_s_sum: float = 0.0
    speed_prompt_tokens_sum: int = 0
    speed_reply_tokens_sum: int = 0
    speed_last_prompt_tokens: int = 0
    speed_last_reply_tokens: int = 0
    speed_last_ttft_s: float = 0.0
    speed_last_decode_s: float = 0.0
    speed_last_total_s: float = 0.0
    speed_last_ttft_adj_s: float = 0.0
    speed_last_decode_adj_s: float = 0.0
    speed_last_poll_credit_s: float = 0.0
    speed_last_settle_credit_s: float = 0.0
    speed_last_prefill_tps: float = 0.0
    speed_last_decode_tps: float = 0.0
    # 端到端（仅流式路径；非流式保持 0.0，命令据此判断要不要显示端到端行）。
    speed_last_e2e_ttft_s: float = 0.0
    speed_last_e2e_decode_tps: float = 0.0
    speed_last_bridge_overhead: float = 0.0
    speed_last_e2e_valid: bool = False
    speed_last_saw_generating: bool = False
    speed_last_streamed: bool = False
    speed_last_rewritten: bool = False

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionState":
        state = cls()
        if not isinstance(payload, dict):
            return state
        for name in (
            "has_history", "cap_hit", "pending_rotation", "pending_reseed",
            "speed_last_saw_generating", "speed_last_streamed",
            "speed_last_rewritten", "speed_last_e2e_valid",
        ):
            if name in payload:
                setattr(state, name, bool(payload.get(name)))
        for name in (
            "turns", "est_tokens", "updated_at", "cap_failures", "speed_samples",
            "speed_prompt_tokens_sum", "speed_reply_tokens_sum",
            "speed_last_prompt_tokens", "speed_last_reply_tokens",
        ):
            try:
                setattr(state, name, max(0, int(payload.get(name) or 0)))
            except (TypeError, ValueError):
                setattr(state, name, 0)
        for name in (
            "speed_prefill_tps_sum", "speed_decode_tps_sum", "speed_ttft_s_sum",
            "speed_decode_s_sum", "speed_last_ttft_s", "speed_last_decode_s",
            "speed_last_total_s", "speed_last_ttft_adj_s", "speed_last_decode_adj_s",
            "speed_last_poll_credit_s", "speed_last_settle_credit_s",
            "speed_last_prefill_tps", "speed_last_decode_tps",
            "speed_last_e2e_ttft_s", "speed_last_e2e_decode_tps",
            "speed_last_bridge_overhead",
        ):
            try:
                value = float(payload.get(name) or 0.0)
                setattr(state, name, value if value >= 0 and value < float("inf") else 0.0)
            except (TypeError, ValueError):
                setattr(state, name, 0.0)
        recent = payload.get("speed_recent_samples")
        if isinstance(recent, list):
            allowed = {
                "prefill_tps", "decode_tps", "ttft_s", "decode_s",
                "ttft_adj_s", "decode_adj_s", "poll_credit_s", "settle_credit_s",
                "prompt_tokens", "reply_tokens",
                "e2e_ttft_s", "e2e_decode_tps", "bridge_overhead_ratio", "e2e_valid",
            }
            clean = []
            for sample in recent[-12:]:
                if not isinstance(sample, dict):
                    continue
                row = {}
                for name in allowed:
                    try:
                        value = float(sample.get(name, 0.0))
                    except (TypeError, ValueError):
                        continue
                    if value >= 0 and value < float("inf"):
                        row[name] = value
                if row:
                    clean.append(row)
            state.speed_recent_samples = clean
        last_error = payload.get("last_error")
        state.last_error = last_error if isinstance(last_error, str) else None
        linked = payload.get("linked_url")
        state.linked_url = linked.strip() if isinstance(linked, str) and linked.strip() else None
        # 三态偏好：缺失 / null = 未设置（跟随全局配置），必须与 False 区分开
        for name in ("think_mode", "save_files"):
            if name in payload:
                setattr(state, name, None if payload.get(name) is None else bool(payload[name]))
        return state
