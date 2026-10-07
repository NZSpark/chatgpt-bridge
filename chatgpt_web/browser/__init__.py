"""Browser 层子包（PI-903）：把 Playwright/DOM 细节从上层业务隐藏起来。

    selectors.py    选择器 / JS 片段（唯一来源）
    dom_adapter.py  ChatGPTDOMAdapter（DOM 查询与启发式）
    diagnostics.py  DOM 诊断（命中数 / 停止控件候选 / 文本长度）

上层（chat_io / completion / page_pool）只通过 adapter 或本包公开接口访问 DOM，
不直接写 selector。``chatgpt_web.dom_adapter`` 仍作为兼容 facade 再导出。
"""

from . import selectors
from .diagnostics import DiagnosticsMixin
from .dom_adapter import ChatGPTDOMAdapter

__all__ = [
    "ChatGPTDOMAdapter",
    "DiagnosticsMixin",
    "selectors",
]
