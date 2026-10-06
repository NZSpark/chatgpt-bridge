"""ChatGPT Web DOM adapter.

Keeps CSS selectors, DOM heuristics and Playwright-specific browser probing out of
higher-level chat/session logic.  The adapter deliberately owns only web-UI
concerns; it does not know about OpenAI protocol objects or session state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, List, Optional

from . import config

logger = logging.getLogger(__name__)


class ChatGPTDOMAdapter:
    """Stable facade over the current ChatGPT web DOM."""

    NEW_CHAT_SEARCH_TIMEOUT_S = 8.0
    NEW_CHAT_POLL_INTERVAL_S = 0.4
    STOP_TOKEN_PATTERN = r"(^|[-_])stop([-_]|$)"

    THINK_FALLBACK_JS = """
    (want) => {
      const textOf = (el) => ((el.innerText || el.textContent || '').trim().toLowerCase());
      const boxed = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const hits = [...document.querySelectorAll('button, [role="button"]')]
        .filter((el) => want.some((w) => textOf(el).includes(w)));
      return hits.find((el) => el.hasAttribute('aria-pressed') && boxed(el))
        || hits.find((el) => el.hasAttribute('aria-pressed'))
        || hits.find(boxed)
        || null;
    }
    """

    COMPLETE_TEXT_JS = """
    (node) => {
      const clone = node.cloneNode(true);
      clone.querySelectorAll('.animating, .pending, .revealing, .fade-in')
        .forEach(e => e.classList.remove('animating', 'pending', 'revealing', 'fade-in'));
      clone.querySelectorAll('[style]').forEach(e => {
        e.style.animation = 'none';
        e.style.opacity = '1';
        e.style.visibility = 'visible';
        e.style.filter = 'none';
        e.style.transform = 'none';
      });
      const holder = document.createElement('div');
      holder.style.position = 'absolute';
      holder.style.left = '-99999px';
      holder.style.top = '0';
      holder.appendChild(clone);
      document.body.appendChild(holder);
      const text = clone.innerText || clone.textContent || '';
      holder.remove();
      return text;
    }
    """

    ANIMATED_JS = "(n) => !!n.querySelector('.animating, .pending, .revealing, .fade-in')"

    GENERATING_JS = """
    () => {
      const stopRe = /STOP_TOKEN_PATTERN/i;
      const words = ['\\u505c\\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        '[data-testid*="stop"], button, [role="button"],'
        + ' div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
      );
      for (const el of nodes) {
        const testid = el.getAttribute('data-testid') || '';
        const label = [
          testid,
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          (el.textContent || '').slice(0, 40),
        ].join(' ');
        const cls = typeof el.className === 'string' ? el.className : '';
        const byClass = stopRe.test(cls) || stopRe.test(testid);
        if (!words.some((w) => label.includes(w)) && !byClass) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0 && rect.top > window.innerHeight * 0.5) {
          return true;
        }
      }
      return false;
    }
    """.replace("STOP_TOKEN_PATTERN", STOP_TOKEN_PATTERN)

    STOP_CANDIDATES_JS = """
    () => {
      const stopRe = /STOP_TOKEN_PATTERN/i;
      const words = ['\\u505c\\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        '[data-testid*="stop"], button, [role="button"],'
        + ' div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
      );
      return [...nodes].map((el) => {
        const testid = el.getAttribute('data-testid') || '';
        const label = [
          testid,
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          (el.textContent || '').slice(0, 40),
        ].join(' ');
        const cls = typeof el.className === 'string' ? el.className : '';
        const byClass = stopRe.test(cls) || stopRe.test(testid);
        const rect = el.getBoundingClientRect();
        const visible = rect.width > 0 && rect.height > 0;
        const bottomHalf = rect.top > window.innerHeight * 0.5;
        return { label, className: cls, visible, bottomHalf,
          matches: words.some((w) => label.includes(w)) || byClass };
      }).filter((x) => x.matches);
    }
    """.replace("STOP_TOKEN_PATTERN", STOP_TOKEN_PATTERN)

    CAP_CHECK_JS_TEMPLATE = (
        "() => { let text = document.body ? (document.body.innerText || '') : '';"
        " for (const node of document.querySelectorAll(%s)) {"
        " const t = node.innerText || ''; if (t) text = text.replace(t, ' '); }"
        " return text; }"
    )

    async def find_input(self, page):
        """Find the composer using attached-state fallback selectors."""
        errors = []
        for selector in config.INPUT_SELECTORS:
            try:
                element = await page.wait_for_selector(
                    selector, timeout=2000, state="attached"
                )
                if element:
                    if config.DEBUG:
                        logger.debug("[输入] 命中选择器：%s", selector)
                    return element
                errors.append(f"{selector}: 未命中")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{selector}: {exc}")
        logger.warning("[输入] 未找到输入框，尝试过的选择器：")
        for line in errors:
            logger.info("        - %s", line)
        return None

    async def click_send_button(self, page) -> bool:
        """Find and DOM-click the configured send button."""
        if page is None:
            return False
        for selector in config.SEND_BUTTON_SELECTORS:
            try:
                button = await page.query_selector(selector)
                if not button:
                    continue
                await button.dispatch_event("click")
                return True
            except Exception:
                continue
        return False

    async def find_assistant_messages(self, page):
        """Return nodes matching the configured assistant-reply selectors."""
        return await page.query_selector_all(config.RESPONSE_SELECTORS)

    async def find_stop_button(self, page):
        """Return the first visible stop-generation control, if any."""
        if page is None:
            return None
        selector = (
            '[data-testid*="stop"], button, [role="button"], '
            'div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
        )
        try:
            handles = await page.query_selector_all(selector)
        except Exception:
            return None
        words = ("停止", "stop")
        for handle in handles:
            try:
                if not await handle.is_visible():
                    continue
            except Exception:
                continue
            try:
                testid = (await handle.get_attribute("data-testid")) or ""
                aria = (await handle.get_attribute("aria-label")) or ""
                title = (await handle.get_attribute("title")) or ""
                cls = (await handle.get_attribute("class")) or ""
                text = (await handle.inner_text()) or ""
                haystack = " ".join((testid, aria, title, cls, text)).lower()
            except Exception:
                continue
            if any(word in haystack for word in words) or re.search(self.STOP_TOKEN_PATTERN, haystack, re.I):
                return handle
        return None

    async def extract_latest_reply(self, page) -> str:
        """Return the latest non-empty assistant reply text."""
        try:
            nodes = await self.find_assistant_messages(page)
        except Exception:
            return ""
        for node in reversed(nodes):
            try:
                text = (await self.complete_text(node)).strip()
            except Exception:
                continue
            if text:
                return text
        return ""

    async def reply_diagnostics(self, page) -> tuple[List[str], int]:
        """Return per-selector hit counts and total body text length."""
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

    async def complete_text(self, node) -> str:
        """Read full node text, bypassing token-reveal animation when needed."""
        if node is None:
            return ""
        try:
            animated = bool(await node.evaluate(self.ANIMATED_JS))
        except Exception:
            animated = True
        if not animated:
            try:
                text = await node.inner_text()
                if text and text.strip():
                    return text
            except Exception:
                pass
        try:
            text = await node.evaluate(self.COMPLETE_TEXT_JS)
            if text and text.strip():
                return text
        except Exception:
            pass
        try:
            text = await node.text_content()
            if text and text.strip():
                return text
        except Exception:
            pass
        try:
            return await node.inner_text()
        except Exception:
            return ""

    async def has_pending_tokens(self, node) -> bool:
        """Return whether the reply node contains unrevealed tokens."""
        if node is None:
            return False
        try:
            return bool(await node.evaluate(
                "(n) => !!n.querySelector('.pending, .animating')"
            ))
        except Exception:
            return False

    async def extract_code_blocks(self, element) -> List[dict]:
        """Extract code blocks from a reply node."""
        extracted: List[dict] = []
        if element is None:
            return extracted
        code_elements = await element.query_selector_all(config.CODE_BLOCK_SELECTOR)
        for code_el in code_elements:
            code_tag = await code_el.query_selector(config.CODE_TAG_SELECTOR)
            lang = "txt"
            if code_tag:
                lang_attr = (await code_tag.get_attribute("data-language") or "").strip()
                if lang_attr:
                    lang = lang_attr.lower()
                else:
                    class_attr = await code_tag.get_attribute("class") or ""
                    lang_match = re.search(r"language-(\w+)", class_attr)
                    if lang_match:
                        lang = lang_match.group(1)
            code_content = await self.complete_text(code_tag or code_el)
            extracted.append({"lang": lang, "code": self._strip_code_noise(code_content, lang)})
        return extracted

    @staticmethod
    def _strip_code_noise(code: str, lang: str) -> str:
        """Remove common CodeMirror/UI noise without changing real code."""
        lines = (code or "").splitlines()
        if not lines:
            return ""
        copy_line_re = re.compile(r"^(?:copy|复制|run|运行)(?:\s|$)", re.IGNORECASE)
        start = 0
        while start < len(lines) and not lines[start].strip():
            start += 1
        end = len(lines)
        while end > start:
            last = lines[end - 1]
            if last.strip() and copy_line_re.match(last):
                end -= 1
                continue
            break
        return "\n".join(lines[start:end])

    async def find_think_mode(self, page, want) -> Optional[Any]:
        """Find the Think-mode button using selectors, then text fallback."""
        for selector in config.THINK_MODE_SELECTOR.split("||"):
            selector = selector.strip()
            if not selector:
                continue
            try:
                buttons = await page.query_selector_all(selector)
            except Exception:
                continue
            for button in buttons:
                if await self._press_think_button(button, want):
                    return button
        try:
            handle = await page.evaluate_handle(self.THINK_FALLBACK_JS, want)
            button = handle.as_element()
        except Exception:
            button = None
        if button is not None and await self._press_think_button(button, want):
            return button
        return None

    async def _press_think_button(self, button, want) -> bool:
        try:
            text = ((await button.inner_text()) or "").strip().lower()
        except Exception:
            text = ""
        if want and not any(item in text for item in want):
            return False
        try:
            pressed = (await button.get_attribute("aria-pressed")) or ""
        except Exception:
            pressed = ""
        if pressed.lower() == "true":
            return True
        try:
            try:
                await button.click(timeout=3000)
            except Exception:
                await button.evaluate("(el) => el.click()")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[会话] 选中思考模式失败（%r）：%r", text, exc)
            return False
        await asyncio.sleep(0.3)
        try:
            now = (await button.get_attribute("aria-pressed")) or ""
        except Exception:
            now = ""
        return now.lower() == "true"

    @staticmethod
    async def rank_new_chat_candidate(handle) -> int:
        """Rank new-chat candidates by visibility/current-session state."""
        try:
            visible = await handle.is_visible()
        except Exception:
            visible = False
        try:
            current = (await handle.get_attribute("aria-current")) == "page"
        except Exception:
            current = False
        if visible and not current:
            return 0
        if visible:
            return 1
        if not current:
            return 2
        return 3

    @staticmethod
    async def click_new_chat_candidate(handle, visible: bool) -> bool:
        if visible:
            try:
                await handle.click(timeout=3000)
                return True
            except Exception:
                pass
        try:
            await handle.evaluate("(el) => el.click()")
            return True
        except Exception:
            return False

    async def find_new_chat(self, page):
        """Find the best currently available New Chat candidate."""
        candidates = []
        for order, selector in enumerate(config.NEW_CHAT_SELECTOR.split("||")):
            selector = selector.strip()
            if not selector:
                continue
            try:
                handles = await page.query_selector_all(selector)
            except Exception:
                continue
            for handle in handles:
                rank = await self.rank_new_chat_candidate(handle)
                candidates.append((rank, order, handle))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        rank, _order, handle = candidates[0]
        return handle, rank

    async def open_new_chat(self, page) -> bool:
        """Find/click New Chat with bounded polling."""
        deadline = time.monotonic() + self.NEW_CHAT_SEARCH_TIMEOUT_S
        errors: List[str] = []
        while True:
            errors = []
            candidates = []
            for order, selector in enumerate(config.NEW_CHAT_SELECTOR.split("||")):
                selector = selector.strip()
                if not selector:
                    continue
                try:
                    handles = await page.query_selector_all(selector)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{selector}: {exc!r}")
                    continue
                if not handles:
                    errors.append(f"{selector}: 未命中")
                    continue
                for handle in handles:
                    rank = await self.rank_new_chat_candidate(handle)
                    candidates.append((rank, order, selector, handle))
            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1]))
                for rank, _order, selector, handle in candidates[:5]:
                    if await self.click_new_chat_candidate(handle, visible=rank <= 1):
                        await asyncio.sleep(0.5)
                        if config.DEBUG:
                            logger.debug(
                                "[会话] 已点击新建对话：%s（rank=%d，候选 %d 个）",
                                selector, rank, len(candidates),
                            )
                        return True
                    errors.append(f"{selector}: 点击失败（rank={rank}）")
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self.NEW_CHAT_POLL_INTERVAL_S)
        logger.warning("[会话] 未找到新建对话按钮，沿用当前会话页。尝试过的选择器：")
        for line in errors:
            logger.info("        - %s", line)
        return False

    async def page_text_without_replies(self, page) -> str:
        """Return body text after removing assistant reply nodes."""
        js = self.CAP_CHECK_JS_TEMPLATE % json.dumps(config.RESPONSE_SELECTORS)
        try:
            return await page.evaluate(js)
        except Exception:
            return ""

    async def is_generating(self, page) -> Optional[bool]:
        if page is None:
            return None
        try:
            return bool(await page.evaluate(self.GENERATING_JS))
        except Exception:
            return None

    async def stop_diagnostics(self, page) -> List[dict]:
        """Return candidate stop controls for DOM diagnostics without exposing page text."""
        if page is None:
            return []
        try:
            return await page.evaluate(self.STOP_CANDIDATES_JS)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]

    async def selector_diagnostics(self, page, groups) -> tuple[dict, dict]:
        """Count configured selectors while keeping selector access inside the adapter."""
        result = {}
        healthy = {}
        for name, selectors in groups.items():
            entries = []
            for selector in selectors:
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
