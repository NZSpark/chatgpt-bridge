"""回复结束判定的纯函数状态机（doc/tasks.md T4.2）。

:meth:`ChatIOMixin._send_chat_locked` 的轮询循环过去把「是否仍在生成 / 是否
落定 / 静默计数 / 卡死计数 / 超时延长」揉在约 200 行的嵌套分支里，只能靠真实
浏览器复现来验证。这里把它们抽成**纯函数**：

* 输入：本轮观测（``reply_seen`` / ``generating`` / ``pending``）与轮询状态
  （``quiet_count`` / ``stable_count`` / ``stalled`` / ``saw_generating``）；
* 输出：:class:`EndVerdict`，其 ``action`` 只能是
  ``continue`` / ``finish`` / ``extend`` / ``fail_stalled`` / ``fail_timeout``。

副作用（发增量、记日志、落盘、抛异常）全部留在调用方，便于穷举测试。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class EndState:
    """轮询循环的可持久状态（每轮 evaluate 后回写）。"""

    quiet_count: int = 0
    stable_count: int = 0
    stalled: int = 0
    saw_generating: bool = False


@dataclass(frozen=True)
class EndLimits:
    """判定阈值（来自 config，测试可直接构造）。"""

    quiet_polls: int          # 内容静默窗口（RESUME_QUIET_POLLS）
    stable_polls: int         # 纯文本稳定阈值（STABLE_POLLS）
    stall_limit: int          # 卡死快速失败阈值（STALL_POLLS）
    extend_step_s: float      # 单次超时延长的步长（max(1, RESPONSE_TIMEOUT_S)）


@dataclass(frozen=True)
class EndVerdict:
    """一轮判定结果。``state`` 是回写后的状态（新对象，不修改入参）。"""

    action: str
    state: EndState
    extend_seconds: float = 0.0
    note: str = ""

    @property
    def finished(self) -> bool:
        return self.action == "finish"


def evaluate_poll(
    state: EndState,
    *,
    limits: EndLimits,
    reply_seen: bool,
    generating: Optional[bool],
    pending: bool = False,
    normalized: str = "",
    last_normalized: str = "",
    last_len: int = -1,
    past_deadline: bool = False,
    extend_budget: float = 0.0,
    has_partial_text: bool = False,
) -> EndVerdict:
    """判定这一轮该继续、收尾、延长还是失败。

    判定顺序与生产行为严格一致（改动这个顺序会改变真实环境下的收尾时机）：

    1. **收尾**（仅在 ``reply_seen`` 时）：
       * 「页面落定」（``generating is False``、此前观测到过生成中、有正文、
         无未显现 token）且内容静默满 ``quiet_polls`` 轮；
       * 或从未观测到生成信号时，内容稳定满 ``max(stable_polls, quiet_polls)`` 轮。
    2. **卡死**：连续 ``stall_limit`` 轮既无正文也无生成信号 → ``fail_stalled``。
    3. **超时**：已到总超时 → 有正文就 ``finish``（返回已读到的内容，绝不重发）；
       页面仍在生成就 ``extend``（消耗延长额度）；否则 ``fail_timeout``。
    """
    quiet = state.quiet_count
    stable = state.stable_count
    stalled = state.stalled
    saw_generating = state.saw_generating or bool(generating)

    if reply_seen:
        # 内容是否又变了（分段输出恢复时清零静默计数）
        content_changed = normalized != last_normalized or len(normalized) != last_len
        if content_changed:
            quiet = 0
        elif generating is True:
            quiet = 0
        else:
            quiet += 1

        settled = (
            generating is False
            and saw_generating
            and bool(normalized)
            and not pending
        )
        if settled and quiet >= limits.quiet_polls:
            return EndVerdict(
                "finish", EndState(quiet, stable, stalled, saw_generating),
                note=f"页面已落定且内容静默 {quiet} 次",
            )

        # 停止按钮始终探测不到时的兜底：纯文本稳定（比旧逻辑多等 quiet 窗口）
        if not saw_generating and generating is not True:
            same_text = bool(normalized) and normalized == last_normalized
            stable = stable + 1 if same_text else 0
            if stable >= max(limits.stable_polls, limits.quiet_polls):
                return EndVerdict(
                    "finish", EndState(quiet, stable, stalled, saw_generating),
                    note=f"未观测到生成信号，内容稳定 {stable} 次",
                )

    # 卡死检测：既没有正文，也没有任何「生成中」信号
    if normalized or saw_generating or generating:
        stalled = 0
    else:
        stalled += 1
    state_after = EndState(quiet, stable, stalled, saw_generating)
    if stalled >= max(1, limits.stall_limit):
        return EndVerdict("fail_stalled", state_after, note=f"页面连续 {stalled} 次无任何内容")

    if past_deadline:
        if has_partial_text:
            return EndVerdict("finish", state_after, note="超时但已读到内容，直接返回，不重发")
        if extend_budget > 0 and generating:
            wait = min(extend_budget, max(1.0, limits.extend_step_s))
            return EndVerdict("extend", state_after, extend_seconds=wait)
        return EndVerdict("fail_timeout", state_after)

    return EndVerdict("continue", state_after)
