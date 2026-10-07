"""生成等待（PI-902 的 ``generator`` 职责）。

负责：输入发送、等待生成、streaming delta。

拆分前这套逻辑的主体是 :meth:`chatgpt_web.chat_io.ChatIOMixin._send_chat_locked`
的轮询循环，判定阈值与状态机已在 :mod:`chatgpt_web.end_detection` 里抽成纯函数。
本模块做两件事：

1. 把 ``end_detection`` 的纯函数状态机再导出，作为 completion 子包对外
   「生成/结束判定」的统一入口（``runner`` 与 ``generator`` 同处一个包内便于
   将来彻底搬迁轮询循环）。
2. 提供 :func:`build_end_limits`：从 :mod:`chatgpt_web.config` 组装
   :class:`~chatgpt_web.end_detection.EndLimits`，把「读配置」与「纯判定」解耦。

本模块不持有可变全局状态，也不直接触碰页面——真正的页面交互仍由
``ChatIOMixin``（发送/轮询的副作用方）执行，行为与拆分前一致。
"""

from typing import Any, Callable, Optional

from .. import config
from ..end_detection import EndLimits, EndState, EndVerdict, evaluate_poll


async def emit_delta(on_delta: Optional[Callable[[str], Any]], text: str) -> None:
    """把一段增量文本交给 ``on_delta`` 回调（用于 SSE streaming）。

    ``on_delta`` 可能是同步或异步可调用对象；这里统一 await 结果（若返回
    可等待对象）。回调为 None 时是空操作，方便非流式调用方复用同一路径。
    """
    if on_delta is None or not text:
        return
    result = on_delta(text)
    if hasattr(result, "__await__"):
        await result


def build_end_limits() -> EndLimits:
    """按 ``config`` 组装结束判定的阈值集合。

    与拆分前 ``_send_chat_locked`` 内联构造的 ``EndLimits`` 完全一致：
    ``quiet_polls`` 至少为 1，``extend_step_s`` 用 ``RESPONSE_TIMEOUT_S``。
    """
    return EndLimits(
        quiet_polls=max(1, int(config.RESUME_QUIET_POLLS)),
        stable_polls=int(config.STABLE_POLLS),
        stall_limit=max(1, int(config.STALL_POLLS)),
        extend_step_s=config.RESPONSE_TIMEOUT_S,
    )


__all__ = [
    "EndState",
    "EndLimits",
    "EndVerdict",
    "evaluate_poll",
    "emit_delta",
    "build_end_limits",
]
