"""DOM 诊断（PI-903-4）：选择器命中数、停止控件候选、页面可读文本长度。

这些方法不参与业务判定，只用于「网页版改版导致选择器失效」时定位问题
（``/_debug/selectors`` 与空节点日志都走这里）。单独成 mixin，让
:class:`~chatgpt_web.browser.dom_adapter.ChatGPTDOMAdapter` 只组合而不内联诊断逻辑。

约定：诊断方法**不抛异常**，失败时返回可读的错误字符串/占位，避免诊断本身
把请求打挂。
"""

import logging
from typing import List

from .. import config
from . import selectors

logger = logging.getLogger(__name__)


class DiagnosticsMixin:
    async def reply_diagnostics(self, page) -> tuple[List[str], int]:
        """返回每条 RESPONSE_SELECTORS 的命中数与整页可见文本长度。"""
        lines: List[str] = []
        for selector in config.RESPONSE_SELECTORS.split(","):
            selector = selector.strip()
            if not selector:
                continue
            try:
                count = len(await page.query_selector_all(selector))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{selector}: {exc!r}")
                continue
            lines.append(f"{selector}: {count}")
        try:
            body_text_len = await page.evaluate(
                "() => (document.body ? document.body.innerText.length : 0)"
            )
        except Exception:  # noqa: BLE001
            body_text_len = -1
        return lines, body_text_len

    async def input_probe(self, page) -> str:
        """定位输入框失败时的现场信息（**只回布尔，不回显页面正文**）。

        旧文案只有一句「请检查是否登录」，把「页面已经没了」这种**可自愈**的
        故障说成用户操作问题，真机上因此被误认很久（P0-J）。这里带上 URL /
        readyState / 是否有弹层 / 是否像登录墙，以及每条 INPUT_SELECTORS 的命中数，
        让「改版 / 未登录 / 页面已死」三种情况在日志里就能分开。
        """
        hits: List[str] = []
        for selector in config.INPUT_SELECTORS:
            selector = selector.strip()
            if not selector:
                continue
            try:
                hits.append(f"{selector}:{len(await page.query_selector_all(selector))}")
            except Exception as exc:  # noqa: BLE001
                hits.append(f"{selector}:{type(exc).__name__}")
        try:
            info = await page.evaluate(selectors.INPUT_PROBE_JS) or {}
        except Exception as exc:  # noqa: BLE001
            return (
                f"现场探测失败（{type(exc).__name__}: {exc}）；"
                f"选择器命中 {', '.join(hits)}"
            )
        return (
            f"URL={info.get('url') or '?'} readyState={info.get('ready') or '?'} "
            f"弹层={'有' if info.get('dialog') else '无'} "
            f"登录墙={'疑似' if info.get('login') else '未见'}；"
            f"选择器命中 {', '.join(hits)}"
        )

    async def stop_diagnostics(self, page) -> List[dict]:
        """返回候选停止控件（不含页面文本），供 DOM 诊断。"""
        if page is None:
            return []
        try:
            return await page.evaluate(selectors.STOP_CANDIDATES_JS)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]

    async def selector_diagnostics(self, page, groups) -> tuple[dict, dict]:
        """统计配置的选择器命中数，把 selector 访问限制在 adapter 内。"""
        result = {}
        healthy = {}
        for name, selectors_list in groups.items():
            entries = []
            for selector in selectors_list:
                selector = selector.strip()
                if not selector:
                    continue
                try:
                    count = len(await page.query_selector_all(selector))
                    entries.append({"selector": selector, "matches": count})
                except Exception as exc:  # noqa: BLE001
                    entries.append({"selector": selector, "error": repr(exc)})
            result[name] = entries
            healthy[name] = any((entry.get("matches") or 0) > 0 for entry in entries)
        return result, healthy


__all__ = ["DiagnosticsMixin"]
