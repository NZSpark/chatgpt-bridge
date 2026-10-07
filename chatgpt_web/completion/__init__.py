"""Completion Pipeline 子包（PI-902）——同时作为历史 ``completion`` 模块的兼容 facade。

把原本集中在 ``chatgpt_web/completion.py`` 的会话生命周期，以及散落在
``chat_io`` / ``end_detection`` 的生成与提取逻辑，按职责拆分：

    lifecycle.py   会话生命周期（新建对话 / 轮转 / 到顶 / 恢复 / 生成探测）
    generator.py   生成等待（结束判定状态机 + 增量回调 + 阈值组装）
    extractor.py   回复 / 代码块提取（纯函数）

注意：``chatgpt_web.completion`` 现在是一个**包**，因此 ``import
chatgpt_web.completion`` / ``from chatgpt_web import completion`` 解析到本
``__init__``。历史调用方（``driver`` / 测试）依赖的两点在这里保留：

* 全部公开对象再导出（``CompletionMixin`` 等）；
* 可打补丁常量 ``_NEW_CHAT_SEARCH_TIMEOUT_S`` / ``_NEW_CHAT_POLL_INTERVAL_S``
  定义在本模块；:meth:`CompletionMixin._open_new_chat` 调用时通过
  ``sys.modules["chatgpt_web.completion"]`` 回读，故
  ``mock.patch.object(completion, ...)`` 仍生效。
"""

# 「新建对话」按钮的**总**搜索预算（秒）：页面冷启动时侧边栏可能晚于 composer
# 渲染，所以在预算内轮询；一命中就返回，不会白等（2026-10-06 线上回归修复）。
# 定义在包 __init__（facade）上，供测试 patch；lifecycle 调用时回读。
_NEW_CHAT_SEARCH_TIMEOUT_S = 8.0
_NEW_CHAT_POLL_INTERVAL_S = 0.4

from .extractor import strip_code_noise  # noqa: E402
from .generator import (  # noqa: E402
    EndLimits,
    EndState,
    EndVerdict,
    build_end_limits,
    emit_delta,
    evaluate_poll,
)
from .lifecycle import CompletionMixin  # noqa: E402

__all__ = [
    "CompletionMixin",
    "_NEW_CHAT_SEARCH_TIMEOUT_S",
    "_NEW_CHAT_POLL_INTERVAL_S",
    "EndState",
    "EndLimits",
    "EndVerdict",
    "evaluate_poll",
    "emit_delta",
    "build_end_limits",
    "strip_code_noise",
]
