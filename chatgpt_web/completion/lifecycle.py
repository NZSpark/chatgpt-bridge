"""会话生命周期（``CompletionMixin`` 主体，PI-902 的 ``runner`` 职责）。

负责：新建对话、启动恢复、上下文到顶探测、会话轮转与恢复，以及判断页面
「是否仍在生成」的诊断辅助。这是文档 ``doc/tasks_pi_9_subtasks.md`` PI-902 里
命名的 *runner*（任务/会话生命周期）在真实代码中的落点。

拆分说明（PI-902）：本模块只保留「会话级」生命周期，真正的轮询/生成等待在
:mod:`chatgpt_web.completion.generator`，回复/代码块提取在
:mod:`chatgpt_web.completion.extractor`。本模块不新增业务逻辑，行为与拆分前
的 ``chatgpt_web/completion.py`` 完全一致。
"""


import asyncio
import logging
import re
from typing import List, Optional

from .. import config
from ..errors import (
    HOME_URL,
    ChatGPTContextLimitError,
)
from ..metrics import metrics

logger = logging.getLogger(__name__)


def _completion_globals():
    """延迟取回 facade 模块 ``chatgpt_web.completion``。

    ``_NEW_CHAT_SEARCH_TIMEOUT_S`` / ``_NEW_CHAT_POLL_INTERVAL_S`` 定义在 facade
    上，历史测试用 ``mock.patch.object(completion, "_NEW_CHAT_...", ...)`` 打补丁。
    这里在调用时通过 sys.modules 取回 facade 对象，保证补丁仍然生效（PI-902）。
    """
    import sys

    return sys.modules.get("chatgpt_web.completion")


class CompletionMixin:
    # Kept here as a compatibility contract for regression tests and callers that
    # inspect the generation-detection JavaScript directly.
    _STOP_TOKEN_PATTERN = r"(^|[-_])stop([-_]|$)"
    _GENERATING_JS = r'''() => {
      const stopRe = /(^|[-_])stop([-_]|$)/i;
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
        const byClass = stopRe.test(el.className || '');
        if (words.some((w) => label.includes(w)) || byClass) return true;
      }
      return false;
    }'''
    _STOP_CANDIDATES_JS = r'''() => {
      const stopRe = /(^|[-_])stop([-_]|$)/i;
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
        const cls = el.className || '';
        const byClass = stopRe.test(cls);
        const r = el.getBoundingClientRect();
        const visible = r.width > 0 && r.height > 0;
        const bottomHalf = r.top >= window.innerHeight / 2;
        return { label, className: cls, visible, bottomHalf,
          matches: words.some((w) => label.includes(w)) || byClass };
      }).filter((x) => x.matches);
    }'''

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
        """兼容 facade：Think Mode 定位/点击统一由 DOM adapter 负责。"""
        if not config.THINK_MODE_DEFAULT:
            return False
        want = [t.strip().lower() for t in config.THINK_MODE_TEXTS.split("||") if t.strip()]
        for attempt in range(3):
            if await self.dom.find_think_mode(page, want):
                return True
            await asyncio.sleep(0.4 * (attempt + 1))
        if config.DEBUG:
            logger.warning("[会话] 未找到思考模式按钮，按默认模式继续。")
        return False

    async def _try_select_think_once(self, page, want) -> bool:
        """向旧测试/调用方保留一次 Think-mode 选择的兼容入口。"""
        return (await self.dom.find_think_mode(page, want)) is not None

    async def _open_new_chat(self, page) -> None:
        """兼容 facade：New Chat 定位/点击统一由 DOM adapter 负责。"""
        # The original implementation kept these knobs in this module; mirror
        # them into the adapter so existing tests/callers can still patch them.
        # PI-902：常量定义在 facade（chatgpt_web.completion），这里在调用时
        # 回读，保证 mock.patch.object(completion, ...) 依然生效。
        facade = _completion_globals()
        search_timeout = getattr(facade, "_NEW_CHAT_SEARCH_TIMEOUT_S", 8.0)
        poll_interval = getattr(facade, "_NEW_CHAT_POLL_INTERVAL_S", 0.4)
        self.dom.NEW_CHAT_SEARCH_TIMEOUT_S = search_timeout
        self.dom.NEW_CHAT_POLL_INTERVAL_S = poll_interval
        await self.dom.open_new_chat(page)

    async def _start_new_session(self, key: Optional[str] = None) -> None:
        """轮转到新会话，并重置会话状态（调用方必须使用“播种”prompt）。

        轮转前先 ``_ensure_page``：页面可能刚好被关掉（用户关标签 / 渲染进程崩溃），
        死页面上的 ``goto`` 会抛 ``TargetClosedError``——它不是 ``RuntimeError``，
        会一路冒到路由层变成没有文案的裸 500（P0-J）。
        """
        metrics.inc("session_rotation_total")
        await self._ensure_page(key)
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

    async def _page_shows_context_limit(self, key: Optional[str] = None) -> bool:
        """页面是否出现“对话长度上限”类提示。

        先把模型回复节点的文本从整页文本里剔除，避免把回复正文里提到
        “长度上限”误判成网页版的提示。
        """
        page = self._page_for(key)
        if page is None:
            return False
        try:
            page_text = await self.dom.page_text_without_replies(page)
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
        metrics.inc("session_recovery_total")
        page = self._page_for(key)
        if page is None:
            logger.warning("[恢复] 没有可用页面，无法恢复。")
            return False
        with metrics.timer("session_recovery_latency"):
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

    async def _page_is_generating(self, key: Optional[str] = None) -> Optional[bool]:
        """检测页面是否仍在生成回复。

        True=生成中；False=页面上找不到「停止生成」控件；None=检测失败/无法判断。
        注意：只有在观测到过 True 之后，False 才可信，调用方需自行记录。
        """
        page = self._page_for(key)
        if page is None:
            return None
        return await self.dom.is_generating(page)

    async def debug_stop_candidates(self, key: Optional[str] = None) -> List[dict]:
        """兼容 facade：停止控件诊断统一由 DOM adapter 负责。"""
        page = self._page_for(key)
        if page is None:
            return []
        return await self.dom.stop_diagnostics(page)
