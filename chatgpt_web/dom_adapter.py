"""ChatGPT Web DOM adapter——**兼容 facade**（PI-903）。

实现已于 PI-903 拆分到 :mod:`chatgpt_web.browser` 子包：

    browser/selectors.py    选择器 / JS 片段（唯一来源）
    browser/dom_adapter.py  ChatGPTDOMAdapter（DOM 查询与启发式）
    browser/diagnostics.py  DOM 诊断（命中数 / 停止控件候选 / 文本长度）

本模块只做再导出，历史 import 路径不变：

* ``from chatgpt_web.dom_adapter import ChatGPTDOMAdapter``；
* 测试读取 ``ChatGPTDOMAdapter.GENERATING_JS`` / ``.STOP_TOKEN_PATTERN`` 等类属性。
"""

from .browser.dom_adapter import ChatGPTDOMAdapter  # noqa: F401
from .browser.selectors import (  # noqa: F401  (兼容再导出)
    ANIMATED_JS,
    CAP_CHECK_JS_TEMPLATE,
    COMPLETE_TEXT_JS,
    GENERATING_JS,
    SELECTOR_VERSION,
    STOP_CANDIDATES_JS,
    STOP_CONTROL_SELECTOR,
    STOP_TOKEN_PATTERN,
    THINK_FALLBACK_JS,
)

__all__ = [
    "ChatGPTDOMAdapter",
    "SELECTOR_VERSION",
    "STOP_TOKEN_PATTERN",
    "THINK_FALLBACK_JS",
    "COMPLETE_TEXT_JS",
    "ANIMATED_JS",
    "GENERATING_JS",
    "STOP_CANDIDATES_JS",
    "CAP_CHECK_JS_TEMPLATE",
    "STOP_CONTROL_SELECTOR",
]
