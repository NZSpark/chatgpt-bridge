"""Playwright 浏览器 Driver：把网页对话驱动成可编程的请求/响应。

实现已按职责拆分到同包内若干 mixin 模块，这里只做组装与浏览器生命周期管理：

* :mod:`chatgpt_web.errors`         —— 异常与常量
* :mod:`chatgpt_web.session_store`  —— 会话状态与持久化（``SessionStoreMixin``）
* :mod:`chatgpt_web.page_pool`      —— 页面池与并发锁（``PagePoolMixin``）
* :mod:`chatgpt_web.completion`     —— 生成结束 / 新建对话 / 到顶判定（``CompletionMixin``）
* :mod:`chatgpt_web.chat_io`        —— 发送 / 轮询 / 提取（``ChatIOMixin``）

所有可调参数都从 ``config`` 模块按属性读取（``config.X``），
因此测试可以直接 ``patch.object(config, "X", ...)`` 生效，无需重新 import。
为保持向后兼容，本模块 re-export 了原先定义在这里的异常、常量与
``SessionState``，既有 ``from chatgpt_web.driver import ...`` 与测试里对
``driver._xxx`` 私有成员的访问都无需改动。
"""

import asyncio
from typing import Any, Dict, List, Optional

from . import (  # noqa: F401
    completion,
    config,
    errors,
    page_pool,
    prompting,
    session_store,
)
from .browser.driver import BrowserLifecycleMixin
from .browser.driver import async_playwright  # noqa: F401  (兼容再导出：历史上是模块全局)
from .chat_io import ChatIOMixin
from .completion import CompletionMixin
from .dom_adapter import ChatGPTDOMAdapter
from .errors import (  # noqa: F401  (re-export)
    DEFAULT_SESSION_KEY,
    HOME_URL,
    ChatGPTBusyError,
    ChatGPTContextLimitError,
    ChatGPTTimeoutError,
)
from .models import ChatMessage
from .page_pool import PagePoolMixin
from .session_store import SessionState, SessionStoreMixin  # noqa: F401  (re-export)


class ChatGPTWebDriver(
    BrowserLifecycleMixin, PagePoolMixin, SessionStoreMixin, CompletionMixin, ChatIOMixin
):

    def __init__(self, user_data_dir: Optional[str] = None):
        user_data_dir = user_data_dir or config.USER_DATA_DIR
        self.user_data_dir = user_data_dir
        self.playwright = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        # 仅用于“惰性创建会话桶页面”，避免并发请求为同一 key 重复建页
        self._page_lock = asyncio.Lock()
        # 浏览器初始化失败时记录原因，让服务仍能启动并对外暴露可读错误
        self.init_error: Optional[str] = None
        # 每个会话桶最近一次 send_chat **实际**发给网页版的 prompt（增量或播种
        # 由 driver 内部按会话是否有历史决定）。server / streaming 用它估算 usage，
        # 避免用调用方“预估”的那份；必须按桶隔离，否则并发时会互相覆盖。
        self._last_prompts: Dict[str, str] = {}

        # ---- 会话状态（按任务分桶，见 config.SESSION_KEY_HEADER）----
        # 每个桶持有：一条独立页面 + 独立会话状态。
        # 「默认桶」继续使用 self.page，因此不涉及分桶的旧调用/测试行为不变。
        # 会话状态里的 has_history 为 False 时必须“播种”完整上下文，
        # 否则 build_prompt 只发增量会让模型收到一条没有前因的孤立消息。
        self._sessions: Dict[str, SessionState] = {}
        self._pages: Dict[str, Any] = {}
        self._page_ids: Dict[str, str] = {}
        # 每个桶页面的最后一次使用时间（time.monotonic），用于空闲回收 / LRU 淘汰
        self._page_last_used: Dict[str, float] = {}
        # 按桶并发时的锁（PARALLEL_BUCKETS=true 才启用；默认桶始终用 self.lock）
        self._locks: Dict[str, asyncio.Lock] = {}
        # 正在处理请求的会话桶（``send_chat`` 全程登记、``_session_lock`` 持锁期间登记），
        # 供 /healthz 观察多 Agent 占用，也用于避免空闲回收 / LRU 淘汰误杀在飞的页面。
        # 不能用“锁是否被持有”来推断：串行模式下所有桶共用一把锁，会把所有桶都算成忙。
        # ``_active_counts`` 是可重入计数（内层退出不撤掉外层的保护，P0-L），
        # ``_active_buckets`` 保留为同一批桶的集合视图（``busy_keys`` / 旧测试读它）。
        self._active_counts: Dict[str, int] = {}
        self._active_buckets: set = set()
        # 所有 ChatGPT Web DOM 查询集中经过这一 adapter；mixin 仅保留兼容 facade。
        self.dom = ChatGPTDOMAdapter()

    async def init(self):
        """初始化浏览器实例（生命周期委托 :class:`BrowserLifecycleMixin`）。"""
        await self.launch_persistent_context(self.user_data_dir)
        self.page = await self.new_browser_page()
        self._page_ids[DEFAULT_SESSION_KEY] = f"page-{id(self.page):x}"
        await self._restore_session_on_startup()
        await self._warn_if_blocked(self.page)

    # ---------- 会话上下文 -> 单条 prompt ----------
    @staticmethod
    def build_prompt(
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> str:
        """把客户端发来的消息数组转换成要发给网页输入框的文本。

        具体实现见 :func:`chatgpt_web.prompting.build_prompt`（这里保留为
        静态方法是为了向后兼容既有的调用方式）。
        """
        return prompting.build_prompt(messages, tools, tool_choice)
