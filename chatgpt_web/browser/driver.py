"""浏览器生命周期（PI-903-3）：Playwright 启动 / persistent context / 页面创建与关闭。

把原先内联在 :class:`chatgpt_web.driver.ChatGPTWebDriver` 里的 Playwright 细节抽到
这个 mixin，让 driver 只负责**组装**（mixin 组合 + ``build_prompt`` 静态入口）。

设计约束（保持既有行为不变）：

* Playwright **惰性 import**：``from playwright.async_api import async_playwright``
  放到方法内部。driver 被 ``chatgpt_web/__init__.py`` 顶层导入，若在模块顶层 import
  Playwright，任何 ``import chatgpt_web``（哪怕只跑纯逻辑测试）都会在未安装
  Playwright 的环境里 ImportError。
* 启动 persistent context 失败时给出**可操作**的提示（profile 被占用），而不是原始堆栈。
* 属性名 ``playwright`` / ``context`` / ``page`` 保持不变，历史调用方与测试可继续访问。
"""

import logging
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)

# 惰性 import Playwright（见模块 docstring）。模块级缓存，避免每次 init 都 import。
async_playwright = None


class BrowserLifecycleMixin:
    async def _start_playwright(self):
        """启动 Playwright（首次调用时惰性 import）。"""
        global async_playwright
        if async_playwright is None:
            try:
                from playwright.async_api import async_playwright as _async_playwright
            except ImportError as exc:  # noqa: BLE001
                raise RuntimeError(
                    "缺少 Playwright 依赖，无法启动浏览器。请先安装：\n"
                    "  pip install -r requirements.txt\n"
                    "  playwright install chromium"
                ) from exc
            async_playwright = _async_playwright
        self.playwright = await async_playwright().start()
        return self.playwright

    async def launch_persistent_context(self, user_data_dir: Optional[str] = None):
        """启动 persistent context 并返回它。

        失败时（尤其是 profile 被另一个 Chromium 实例占用）给出可操作提示，
        而不是把原始异常直接抛给调用方。
        """
        profile_dir = user_data_dir or self.user_data_dir or config.USER_DATA_DIR
        await self._start_playwright()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=config.HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # noqa: BLE001
            # persistent context 不能被两个进程共用；给出可操作的提示而不是原始堆栈
            await self.playwright.stop()
            self.playwright = None
            message = str(exc)
            if (
                "existing browser session" in message
                or "profile is already in use" in message
                or "SingletonLock" in message
            ):
                raise RuntimeError(
                    f"浏览器用户目录 {profile_dir} 已被另一个 Chromium 实例占用。\n"
                    "通常是因为已有一个 chatgpt_api_server.py 仍在运行，"
                    "或上一次的浏览器窗口没有关闭。\n"
                    "请先结束旧实例再重试：\n"
                    "  pkill -f chatgpt_api_server.py\n"
                    "或直接关闭占用该 profile 的 Chromium 窗口。"
                ) from exc
            raise
        return self.context

    async def new_browser_page(self):
        """在已启动的 context 中开一个新页面。"""
        if self.context is None:
            raise RuntimeError("浏览器尚未初始化，无法创建页面（请先 launch_persistent_context）。")
        return await self.context.new_page()

    async def close(self):
        """关闭 context 与 Playwright（幂等：未启动时是空操作）。"""
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()


__all__ = ["BrowserLifecycleMixin", "async_playwright"]
