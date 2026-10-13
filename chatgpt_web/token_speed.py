"""Token 与生成速度的估算。

网页端不提供 token 级事件或精确 usage，因此这里用项目现有的
``estimate_tokens`` 估算输入、输出 token，并根据回复快照的首次可观测时间
计算保守的 prefill / decode 速度。所有时间都使用 ``time.monotonic()`` 时钟。

设计口径（务必与命令输出一致，避免误导）：

- token 数是估算（字符数 / 3），不是精确计费依据；
- prefill 速度用「输入 token / 首字延迟」，与直接 API 同构，但首字延迟包含
  网页合成与半轮询周期，因此系统性偏小；
- decode 速度用「输出 token / 解码时长」，会多算一个落定静默窗口，因此扣掉
  可明确归因的 ``settle_credit``；
- 端到端（仅流式）额外记录首个正文 SSE chunk 与 [DONE] 时刻，得到客户端体感速度。
"""

from dataclasses import dataclass, replace
from typing import Optional

from .prompting import estimate_tokens

_EPSILON_S = 1e-3

#: 短回复阈值：低于此输出 token 数时轮询粒度不足以给出可靠速度。
SHORT_SAMPLE_TOKENS = 50


@dataclass(frozen=True)
class TokenSpeed:
    """单次成功生成的 token 及速度观测值（不是模型裸速度）。"""

    prompt_chars: int
    reply_chars: int
    prompt_tokens: int
    reply_tokens: int
    ttft_s: float
    decode_s: float
    total_s: float
    ttft_adj_s: float
    decode_adj_s: float
    prefill_tps: float
    decode_tps: float
    poll_credit_s: float
    settle_credit_s: float
    saw_generating: bool
    streamed: bool = False
    rewritten: bool = False
    # 端到端（仅流式路径可观测；非流式为 None）
    e2e_ttft_s: Optional[float] = None
    e2e_decode_tps: Optional[float] = None
    bridge_overhead_ratio: Optional[float] = None

    @property
    def short_sample(self) -> bool:
        """回复过短时轮询粒度不足，速度估计误差大，命令应提示仅供参考。"""
        return self.reply_tokens < SHORT_SAMPLE_TOKENS


#: 会话状态里持久化的「最近一次」字段名 -> TokenSpeed 属性名。
#: 放在这里集中维护，reply_waiter / store / bridge_commands 都从这里取，
#: 避免同一组字段名在四处各自漂移。
LAST_FIELDS = (
    ("speed_last_prompt_tokens", "prompt_tokens"),
    ("speed_last_reply_tokens", "reply_tokens"),
    ("speed_last_ttft_s", "ttft_s"),
    ("speed_last_decode_s", "decode_s"),
    ("speed_last_total_s", "total_s"),
    ("speed_last_prefill_tps", "prefill_tps"),
    ("speed_last_decode_tps", "decode_tps"),
    ("speed_last_e2e_ttft_s", "e2e_ttft_s"),
    ("speed_last_e2e_decode_tps", "e2e_decode_tps"),
    ("speed_last_bridge_overhead", "bridge_overhead_ratio"),
    ("speed_last_saw_generating", "saw_generating"),
    ("speed_last_streamed", "streamed"),
    ("speed_last_rewritten", "rewritten"),
)

#: 会话状态里累加的「求和」字段名 -> TokenSpeed 属性名。
SUM_FIELDS = (
    ("speed_prefill_tps_sum", "prefill_tps"),
    ("speed_decode_tps_sum", "decode_tps"),
    ("speed_ttft_s_sum", "ttft_s"),
    ("speed_decode_s_sum", "decode_s"),
    ("speed_prompt_tokens_sum", "prompt_tokens"),
    ("speed_reply_tokens_sum", "reply_tokens"),
)


def compute_speed(
    prompt: str,
    reply: str,
    *,
    submitted_at: float,
    first_text_at: Optional[float],
    finished_at: float,
    poll_interval_s: float,
    settle_credit_s: float = 0.0,
    saw_generating: bool = False,
    streamed: bool = False,
    rewritten: bool = False,
    request_started_at: Optional[float] = None,
    first_sse_at: Optional[float] = None,
    done_at: Optional[float] = None,
) -> Optional[TokenSpeed]:
    """计算单次生成速度；没有观测到首段新回复时返回 ``None``。

    ``first_text_at`` 是桥首次从页面轮询到本轮非空回复的时刻，而不是真正的
    模型首 token 时刻。半个轮询周期作为 TTFT 修正，decode 只扣除调用方能
    明确归因于结束判定的静默等待。结果应解读为桥接链路下的观测估值。

    端到端字段仅在流式路径提供 ``first_sse_at`` / ``done_at`` 时计算：

    - ``e2e_ttft_s``：请求开始（或提交）到首个正文 SSE chunk；
    - ``e2e_decode_tps``：输出 token /（[DONE] − 首个正文 chunk）；
    - ``bridge_overhead_ratio``：端到端时间里不属于模型生成的比例（诊断用）。
    """
    if first_text_at is None:
        return None

    prompt = prompt or ""
    reply = reply or ""
    ttft_s = max(0.0, first_text_at - submitted_at)
    decode_s = max(0.0, finished_at - first_text_at)
    total_s = max(0.0, finished_at - submitted_at)
    poll_credit = max(0.0, poll_interval_s) / 2.0
    settle_credit = max(0.0, settle_credit_s) if saw_generating else 0.0
    ttft_adj = max(_EPSILON_S, ttft_s - poll_credit)
    decode_adj = max(_EPSILON_S, decode_s - settle_credit)
    prompt_tokens = estimate_tokens(prompt)
    reply_tokens = estimate_tokens(reply)

    e2e_ttft_s, e2e_decode_tps, overhead = _e2e_metrics(
        reply_tokens=reply_tokens,
        submitted_at=submitted_at,
        ttft_adj=ttft_adj,
        decode_adj=decode_adj,
        request_started_at=request_started_at,
        first_sse_at=first_sse_at,
        done_at=done_at,
    )

    return TokenSpeed(
        prompt_chars=len(prompt),
        reply_chars=len(reply),
        prompt_tokens=prompt_tokens,
        reply_tokens=reply_tokens,
        ttft_s=ttft_s,
        decode_s=decode_s,
        total_s=total_s,
        ttft_adj_s=ttft_adj,
        decode_adj_s=decode_adj,
        prefill_tps=prompt_tokens / ttft_adj,
        decode_tps=reply_tokens / decode_adj,
        poll_credit_s=poll_credit,
        settle_credit_s=settle_credit,
        saw_generating=bool(saw_generating),
        streamed=bool(streamed),
        rewritten=bool(rewritten),
        e2e_ttft_s=e2e_ttft_s,
        e2e_decode_tps=e2e_decode_tps,
        bridge_overhead_ratio=overhead,
    )


def _e2e_metrics(
    *,
    reply_tokens: int,
    submitted_at: float,
    ttft_adj: float,
    decode_adj: float,
    request_started_at: Optional[float],
    first_sse_at: Optional[float],
    done_at: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """从流式时间戳推导端到端指标；缺时间戳时对应项为 None。"""
    e2e_ttft: Optional[float] = None
    e2e_decode: Optional[float] = None
    overhead: Optional[float] = None

    origin = request_started_at if request_started_at is not None else submitted_at
    if first_sse_at is not None:
        e2e_ttft = max(0.0, first_sse_at - origin)
    if first_sse_at is not None and done_at is not None:
        window = max(_EPSILON_S, done_at - first_sse_at)
        if reply_tokens > 0:
            e2e_decode = reply_tokens / window
    if request_started_at is not None and done_at is not None:
        e2e_total = max(_EPSILON_S, done_at - request_started_at)
        model_window = max(0.0, ttft_adj + decode_adj)
        overhead = max(0.0, min(1.0, 1.0 - model_window / e2e_total))
    return e2e_ttft, e2e_decode, overhead


def augment_e2e(
    speed: TokenSpeed,
    *,
    request_started_at: float,
    first_sse_at: Optional[float],
    done_at: Optional[float],
) -> TokenSpeed:
    """给已算好的 ``TokenSpeed`` 补上端到端字段（流式层在收尾时调用）。

    非流式路径不会调用它，``e2e_*`` 保持 None——命令据此不显示端到端行。
    """
    e2e_ttft, e2e_decode, overhead = _e2e_metrics(
        reply_tokens=speed.reply_tokens,
        submitted_at=request_started_at,
        ttft_adj=speed.ttft_adj_s,
        decode_adj=speed.decode_adj_s,
        request_started_at=request_started_at,
        first_sse_at=first_sse_at,
        done_at=done_at,
    )
    return replace(
        speed,
        streamed=True,
        e2e_ttft_s=e2e_ttft,
        e2e_decode_tps=e2e_decode,
        bridge_overhead_ratio=overhead,
    )


__all__ = [
    "TokenSpeed",
    "compute_speed",
    "augment_e2e",
    "LAST_FIELDS",
    "SUM_FIELDS",
    "SHORT_SAMPLE_TOKENS",
]
