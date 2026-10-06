"""生成结束 / 会话轮转 / 到顶判定（``CompletionMixin``）。

负责：新建对话、启动恢复、上下文到顶探测、会话轮转与恢复，
以及判断页面「是否仍在生成」的诊断辅助。
"""


import asyncio
import json
import logging
import re
import time
from typing import List, Optional

from . import config
from .errors import (
    HOME_URL,
    ChatGPTContextLimitError,
)

logger = logging.getLogger(__name__)

# 「新建对话」按钮的**总**搜索预算（秒）：页面冷启动时侧边栏可能晚于 composer
# 渲染，所以在预算内轮询；一命中就返回，不会白等（2026-10-06 线上回归修复）。
_NEW_CHAT_SEARCH_TIMEOUT_S = 8.0
_NEW_CHAT_POLL_INTERVAL_S = 0.4

class CompletionMixin:
    async def _restore_session_on_startup(self) -> None:
        """启动时一律新开对话（不做 URL 恢复）。

        会话连续性由「每桶新开对话 + 历史播种」保证：桶状态里只记录轮数 /
        体积 / 是否到顶，页面关闭后下次重开会用 build_prompt(seed=True)
        重放历史。因此这里不再读取或回填任何会话地址。
        """
        state = self._load_session_state()
        self.session_turns = int(state.get("turns") or 0)
        self.session_est_tokens = int(state.get("est_tokens") or 0)
        self.session_cap_hit = bool(state.get("cap_hit"))
        self.last_error = state.get("last_error") or None

        await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._open_new_chat(self.page)
        await self._wait_ready(self.page)
        # 新会话默认选中「思考模式」，否则网页版回复过于简单
        await self._select_think_mode(self.page)
        # 页面刚从空白对话开始，必须播种完整上下文
        self.session_has_history = False
        self.session_turns = 0
        self.session_est_tokens = 0
        self.session_cap_hit = False
        logger.info("[系统提示] 服务启动成功！请确保 ChatGPT 页面保持登录状态。\n")

    async def _warn_if_blocked(self, page) -> None:
        """启动后自检：页面是否被 Cloudflare 挑战页 / 登录页挡住。

        实测 HEADLESS=1 时 ChatGPT 返回 ``title="Just a moment..."`` 的
        Cloudflare 挑战页（body 为空、按钮数 0、composer 不渲染），随后所有
        选择器都会落空。这里提前检测并给出可操作的告警，避免用户面对
        "找不到输入框/新建对话" 却无从下手。
        """
        try:
            title = (await page.title()) or ""
            n_buttons = await page.evaluate(
                "() => document.querySelectorAll('button, a, [role=button]').length"
            )
        except Exception:  # noqa: BLE001
            return
        blocked = (
            "just a moment" in title.lower()
            or "attention required" in title.lower()
        )
        if blocked or n_buttons == 0:
            reason = (
                "命中 Cloudflare 挑战页" if blocked
                else "页面无任何可点击元素（可能未登录 / 被风控 / 仍在加载）"
            )
            logger.warning(
                "\n[启动告警] ChatGPT 页面疑似不可用：" + reason + "\n"
                + f"  title={title!r}  可点击元素={n_buttons}\n"
                + "  对策：用有头模式运行（.env 设 HEADLESS=false）；"
                + "无显示服务器请用 `xvfb-run -a python chatgpt_api_server.py`。\n"
                + "  若仍未登录，请先 HEADLESS=false 手动登录一次，"
                + "登录态会存入 user_data/。\n"
            )

    async def _select_think_mode(self, page) -> bool:
        """在新建 / 轮转后的对话上选中「思考模式」；成功或已选中返回 True。

        网页版默认可能落在简版模型上，回复过于简单。Think 模式是 composer 上
        的一个 pill 按钮（``button.__composer-pill``，文本含 "Think"），选中后
        ``aria-pressed="true"``。这里按「选择器 + 文本」双重匹配，避免点错其它 pill。

        失败不抛异常：选中失败不应阻断对话（只是回复质量可能下降），仅打印告警。
        """
        if not config.THINK_MODE_DEFAULT:
            return False
        want = [t.strip().lower() for t in config.THINK_MODE_TEXTS.split("||") if t.strip()]
        # pill 可能比输入框稍晚渲染；给一小段等待，避免「刚就绪时点空」。
        # 每轮发送前都会调用它（见 _send_chat_locked），因此这里的重试不影响正确性，
        # 只是提高首次命中率。
        for attempt in range(3):
            if await self._try_select_think_once(page, want):
                return True
            await asyncio.sleep(0.4 * (attempt + 1))
        if config.DEBUG:
            logger.warning("[会话] 未找到思考模式按钮，按默认模式继续。")
        return False

    # 兜底 JS（2026-10-06 线上回归修复）：pill 的类名可能随网页改版变化
    # （如 __composer-pill 被改名），而「文本含 Think / 思考」且带 aria-pressed 的
    # 按钮就是它。按「带 aria-pressed 且可见 > 带 aria-pressed > 可见」排序。
    _THINK_FALLBACK_JS = """
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

    async def _try_select_think_once(self, page, want) -> bool:
        """单次尝试：定位 Think pill 并（在需要时）点选。已选中/点选成功返回 True。"""
        for selector in config.THINK_MODE_SELECTOR.split("||"):
            selector = selector.strip()
            if not selector:
                continue
            try:
                buttons = await page.query_selector_all(selector)
            except Exception:  # noqa: BLE001
                continue
            for button in buttons:
                if await self._press_think_button(button, want):
                    return True
        # 兜底：类名选择器全部落空时，按文本扫描页面按钮（见 _THINK_FALLBACK_JS）
        element = None
        try:
            handle = await page.evaluate_handle(self._THINK_FALLBACK_JS, want)
            element = handle.as_element()
        except Exception:  # noqa: BLE001
            element = None
        if element is not None:
            return await self._press_think_button(element, want)
        return False

    async def _press_think_button(self, button, want) -> bool:
        """读取文本 / 选中态并按需点选单个候选按钮；成功（或已选中）返回 True。"""
        try:
            text = ((await button.inner_text()) or "").strip().lower()
        except Exception:  # noqa: BLE001
            text = ""
        if want and not any(w in text for w in want):
            return False
        try:
            pressed = (await button.get_attribute("aria-pressed")) or ""
        except Exception:  # noqa: BLE001
            pressed = ""
        if pressed.lower() == "true":
            if config.DEBUG:
                logger.debug(f"[会话] 思考模式已处于选中态（{text!r}）。")
            return True
        try:
            try:
                await button.click(timeout=3000)
            except Exception:  # noqa: BLE001
                # pill 常被相邻元素覆盖导致 click 被拦截，退回 JS 原生 click
                await button.evaluate("(el) => el.click()")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[会话] 选中思考模式失败（{text!r}）：{exc!r}")
            return False
        await asyncio.sleep(0.3)
        try:
            now = (await button.get_attribute("aria-pressed")) or ""
        except Exception:  # noqa: BLE001
            now = ""
        if now.lower() == "true":
            logger.info(f"[会话] 已选中思考模式（{text!r}），本轮起回复将更深入。")
            return True
        logger.warning(f"[会话] 思考模式点击后仍未选中（{text!r}，aria-pressed={now!r}）。")
        return False

    @staticmethod
    async def _rank_new_chat_candidate(handle) -> int:
        """候选排序（越小越优先）：可见且非当前会话项 > 可见 > 非当前 > 其它。

        现网侧边栏里同一个 ``button[aria-label="New chat"]`` 会匹配到多个节点，
        其中当前会话项带 ``aria-current="page"``、折叠态的节点尺寸为 0；
        旧实现只取第一个并要求可见，于是整轮超时。
        """
        try:
            visible = await handle.is_visible()
        except Exception:  # noqa: BLE001
            visible = False
        try:
            current = (await handle.get_attribute("aria-current")) == "page"
        except Exception:  # noqa: BLE001
            current = False
        if visible and not current:
            return 0
        if visible:
            return 1
        if not current:
            return 2
        return 3

    @staticmethod
    async def _click_new_chat_candidate(handle, visible: bool) -> bool:
        """真点击；失败退回 JS 原生 click（侧边栏图标常被相邻 <svg> 覆盖）。

        ``visible=False``（折叠态零尺寸节点）直接走 JS click：Playwright 对不可见
        元素的 ``click()`` 必定等满超时，纯浪费总预算。
        """
        if visible:
            try:
                await handle.click(timeout=3000)
                return True
            except Exception:  # noqa: BLE001
                pass
        try:
            await handle.evaluate("(el) => el.click()")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _open_new_chat(self, page) -> None:
        """点击「新建对话」，确保从干净会话开始（点不到就沿用当前页）。

        2026-10-06 线上回归修复：旧实现用 ``page.wait_for_selector(selector)``
        ——默认语义是「等到**第一个**匹配且**可见**」，而现网侧边栏同一选择器
        会先匹配到当前会话项（aria-current=page）或折叠态零尺寸节点，于是明明
        有可点的按钮也会整轮超时（实测 9 个选择器 × 3s 全部落空）。

        现改为：
        1. 在**总**预算内轮询 ``query_selector_all``（只要 attached，不要求可见）；
        2. 跨选择器收集候选并排序：可见且非当前项 > 可见 > 非当前 > 其它；
        3. 真点击失败再退回 JS 原生 click；候选存在但都点不动时继续轮询。
        """
        deadline = time.monotonic() + _NEW_CHAT_SEARCH_TIMEOUT_S
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
                    rank = await self._rank_new_chat_candidate(handle)
                    candidates.append((rank, order, selector, handle))
            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1]))
                # 只尝试排序最靠前的几个，给单轮一个时间上界
                for rank, _order, selector, handle in candidates[:5]:
                    if await self._click_new_chat_candidate(handle, visible=rank <= 1):
                        await asyncio.sleep(0.5)
                        if config.DEBUG:
                            logger.debug(
                                "[会话] 已点击新建对话：%s（rank=%d，候选 %d 个）",
                                selector, rank, len(candidates),
                            )
                        return
                    errors.append(f"{selector}: 点击失败（rank={rank}）")
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_NEW_CHAT_POLL_INTERVAL_S)
        logger.warning("[会话] 未找到新建对话按钮，沿用当前会话页。尝试过的选择器：")
        for line in errors:
            logger.info(f"        - {line}")

    async def _start_new_session(self, key: Optional[str] = None) -> None:
        """轮转到新会话，并重置会话状态（调用方必须使用“播种”prompt）。"""
        page = self._page_for(key)
        if page is None:
            return
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._open_new_chat(page)
        await self._wait_ready(page)
        # 新会话默认选中「思考模式」，否则网页版回复过于简单
        await self._select_think_mode(page)
        state = self._state(key)
        state.has_history = False
        state.turns = 0
        state.est_tokens = 0
        state.cap_hit = False
        state.pending_rotation = False
        state.last_error = None
        # 文件 I/O 放到线程里，避免卡住事件循环（T3.3）
        await asyncio.to_thread(self._save_session_state, key)
        logger.info("[轮转] 已开启新的网页会话（本轮会用完整历史播种上下文）。")

    _CAP_CHECK_JS_TEMPLATE = (
        "() => { let text = document.body ? (document.body.innerText || '') : '';"
        " for (const node of document.querySelectorAll(%s)) {"
        " const t = node.innerText || ''; if (t) text = text.replace(t, ' '); }"
        " return text; }"
    )

    async def _page_shows_context_limit(self, key: Optional[str] = None) -> bool:
        """页面是否出现“对话长度上限”类提示。

        先把模型回复节点的文本从整页文本里剔除，避免把回复正文里提到
        “长度上限”误判成网页版的提示。
        """
        page = self._page_for(key)
        if page is None:
            return False
        js = self._CAP_CHECK_JS_TEMPLATE % json.dumps(config.RESPONSE_SELECTORS)
        try:
            page_text = await page.evaluate(js)
        except Exception:
            return False
        for pattern in config.CAP_NOTICE_PATTERNS:
            try:
                if re.search(pattern, page_text or "", re.IGNORECASE):
                    return True
            except re.error:
                continue
        return False

    def _mark_context_limit(self, key: Optional[str] = None) -> None:
        state = self._state(key)
        state.cap_hit = True
        state.last_error = "context_length_exceeded"
        self._save_session_state(key=key)

    def _context_limit_error(self) -> "ChatGPTContextLimitError":
        return ChatGPTContextLimitError(
            "ChatGPT 网页会话已达上下文长度上限（网页版会停止响应）。"
            "本服务会自动轮转到新会话并播种历史；若仍失败，请检查登录状态。"
        )

    async def _recover_session(self, key: Optional[str] = None) -> bool:
        """超时后重开一个干净对话（不做 URL 恢复），成功返回 True。

        上下文不会丢：调用方在本轮失败后会以「播种」prompt 重发历史。
        """
        page = self._page_for(key)
        if page is None:
            logger.warning("[恢复] 没有可用页面，无法恢复。")
            return False
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await self._open_new_chat(page)
            if not await self._wait_ready(page):
                logger.warning("[恢复] 已打开页面，但未检测到输入框，请检查登录状态。")
                return False
            self._state(key).has_history = False
            logger.info("[恢复] 已重开新对话（本轮将重新播种上下文）。")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[恢复] 重开会话失败: {exc}")
            return False

    # 「类名里出现 stop 词」的严格口径（主判定与诊断共用）。
    #
    # 2026-10-06 线上回归：旧实现用裸的 /stop/i.test(cls)，而现网侧边栏会话标题
    # 带 ``stopAtEnd-<hash>`` 类（截断用的样式类）。于是可见、位于视口下半部的
    # 侧边栏标题被当成「停止生成」控件 → generating 恒为 True → 结束判定永远
    # 等不到「页面落定」，每轮都空转到总超时（日志里 generating=True 全程不变）。
    # 收紧成「独立 stop 词」：stop / stop-button / _stop_ 命中，stopAtEnd / stopwatch 不命中。
    # 该正则源（含锚点）同时给下面的 JS 用，见 _GENERATING_JS 的 /STOP_TOKEN_PATTERN/i。
    _STOP_TOKEN_PATTERN = r"(^|[-_])stop([-_]|$)"

    # 主判定所用的 JS：扫描页面上可见的「停止生成」控件
    _GENERATING_JS = """
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
        // 必须可见，且位于视口下半部（停止按钮就在底部输入框区域），
        // 避免把正文里含有 stop / 停止 字样的元素误判成生成中
        if (rect.width > 0 && rect.height > 0 && rect.top > window.innerHeight * 0.5) {
          return true;
        }
      }
      return false;
    }
    """.replace("STOP_TOKEN_PATTERN", _STOP_TOKEN_PATTERN)

    async def _page_is_generating(self, key: Optional[str] = None) -> Optional[bool]:
        """检测页面是否仍在生成回复。

        True=生成中；False=页面上找不到「停止生成」控件；None=检测失败/无法判断。
        注意：只有在观测到过 True 之后，False 才可信，调用方需自行记录。
        """
        page = self._page_for(key)
        if page is None:
            return None
        try:
            return bool(await page.evaluate(self._GENERATING_JS))
        except Exception:
            return None

    _STOP_CANDIDATES_JS = """
    () => {
      const stopRe = /STOP_TOKEN_PATTERN/i;
      const words = ['\\u505c\\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        '[data-testid*="stop"], button, [role="button"],'
        + ' div[class*="stop"], span[class*="stop"], svg[class*="stop"], [aria-label]'
      );
      const out = [];
      for (const el of nodes) {
        const testid = el.getAttribute('data-testid') || '';
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const text = (el.textContent || '').slice(0, 40);
        const cls = typeof el.className === 'string' ? el.className : '';
        const label = [testid, aria, title, text].join(' ');
        const byClass = stopRe.test(cls) || stopRe.test(testid);
        if (!words.some((w) => label.includes(w)) && !byClass) continue;
        const r = el.getBoundingClientRect();
        out.push({
          tag: el.tagName,
          cls: cls.slice(0, 120),
          aria,
          title,
          text: text.slice(0, 40),
          visible: r.width > 0 && r.height > 0,
          top: Math.round(r.top),
          vh: window.innerHeight,
        });
        if (out.length >= 20) break;
      }
      return out;
    }
    """.replace("STOP_TOKEN_PATTERN", _STOP_TOKEN_PATTERN)

    async def debug_stop_candidates(self, key: Optional[str] = None) -> List[dict]:
        """诊断用：列出页面上所有「可能表示生成中」的控件及其位置。"""
        page = self._page_for(key)
        if page is None:
            return []
        try:
            return await page.evaluate(self._STOP_CANDIDATES_JS)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]
