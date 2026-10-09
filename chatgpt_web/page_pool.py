"""页面池与并发锁（``PagePoolMixin``）。

负责：会话桶页面的惰性创建、空闲回收 / LRU 淘汰、按桶加锁，
以及供 ``/healthz`` 观察的占用统计。页面关闭只关页面，会话状态保留。
"""


import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from . import config
from .errors import (
    DEFAULT_SESSION_KEY,
    HOME_URL,
    ChatGPTBusyError,
    page_alive,
)

logger = logging.getLogger(__name__)


class BucketActivityMixin:
    """「有请求在飞」的登记（可重入计数，P0-L）。

    由发送侧（``ChatIOMixin.send_chat``：从第一行到返回全程登记）与页面池
    （``PagePoolMixin._session_lock``：持锁期间登记）共用；两边合起来才是完整的
    「这段窗口里这个桶的页面不能被动」语义，因此单独成一个小 mixin。

    **必须是计数而不是集合**：锁与 ``send_chat`` 都会打标记，内层先退出
    （锁释放）不能把外层（整个请求）的保护撤掉。
    """

    def _active_registry(self):
        """活跃桶登记表：可重入计数 + 集合（``busy_keys`` 仍读集合）。

        延迟初始化：``ChatIOMixin`` 的最小测试替身不走 ``ChatGPTWebDriver.__init__``，
        可能还没有这两个属性。
        """
        counts = getattr(self, "_active_counts", None)
        if counts is None:
            counts = {}
            self._active_counts = counts
        buckets = getattr(self, "_active_buckets", None)
        if buckets is None:
            buckets = set()
            self._active_buckets = buckets
        return counts, buckets

    def _mark_bucket_active(self, key: Optional[str] = None) -> None:
        """标记「该桶有请求在飞」（可重入，多打一次就多活一层）。"""
        bucket = key or DEFAULT_SESSION_KEY
        counts, buckets = self._active_registry()
        counts[bucket] = counts.get(bucket, 0) + 1
        buckets.add(bucket)

    def _unmark_bucket_active(self, key: Optional[str] = None) -> None:
        bucket = key or DEFAULT_SESSION_KEY
        counts, buckets = self._active_registry()
        left = counts.get(bucket, 1) - 1
        if left > 0:
            counts[bucket] = left
        else:
            counts.pop(bucket, None)
            buckets.discard(bucket)

    def _bucket_active(self, bucket: str) -> bool:
        """该桶是否有请求在飞（集合与计数任一命中即可，兼容直接改集合的旧用法）。"""
        counts, buckets = self._active_registry()
        return bucket in buckets or counts.get(bucket, 0) > 0


class PagePoolMixin(BucketActivityMixin):
    #: 「默认桶」的页面由宿主持有（``ChatGPTWebDriver.page``）。这里显式声明类型：
    #: 既写明 mixin 对宿主的契约，也避免 mypy 从本模块内的 ``self.page = None``
    #: 反推出无法确定的类型（has-type）。
    page: Any

    def busy_keys(self) -> List[str]:
        """当前正在处理请求（有请求在飞 / 已拿到锁、正在生成）的会话桶，供多 Agent 观察占用。"""
        return sorted(self._active_registry()[1])

    def cluster_stats(self) -> Dict[str, Any]:
        """多会话 / 多 Agent 运行概况（并发开关、桶上限、占用、已开页面数）。"""
        return {
            "parallel": config.PARALLEL_BUCKETS,
            "max_buckets": config.MAX_SESSION_BUCKETS,
            "open_pages": len(self._pages),
            "bucket_lock_timeout_s": config.BUCKET_LOCK_TIMEOUT_S,
            "busy": self.busy_keys(),
            "keys": self.session_keys(),
        }

    def bucket_map(self) -> Dict[str, Any]:
        """每个会话桶 → 页面 / 会话绑定关系（供 /healthz 观察，T3.1/T1.2）。

        只读且**不创建**任何状态（不调用 ``_state()``，避免健康检查副作用）。
        """
        out: Dict[str, Any] = {}
        buckets = sorted(set(self._pages) | set(self._sessions) | {DEFAULT_SESSION_KEY})
        for bucket in buckets:
            state = self._sessions.get(bucket)
            out[bucket] = {
                "page_open": bucket in self._pages,
                "busy": self.bucket_busy(bucket),
                "last_used": self._page_last_used.get(bucket),
                "has_history": bool(getattr(state, "has_history", False)),
                "turns": int(getattr(state, "turns", 0) or 0),
            }
        return out

    def _lock_for(self, key: Optional[str] = None) -> asyncio.Lock:
        """取某个会话桶的锁。

        默认（``PARALLEL_BUCKETS=false``）所有桶共用 ``self.lock``，即**串行**：
        分桶只是上下文隔离，不是并发能力。只有显式打开开关才会按桶各持一把锁。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if not config.PARALLEL_BUCKETS or bucket == DEFAULT_SESSION_KEY:
            return self.lock
        lock = self._locks.get(bucket)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[bucket] = lock
        return lock

    @asynccontextmanager
    async def _session_lock(self, key: Optional[str] = None):
        """获取某个会话桶的锁；超过 ``BUCKET_LOCK_TIMEOUT_S`` 则抛 ``ChatGPTBusyError``。

        ``BUCKET_LOCK_TIMEOUT_S=0``（默认）表示一直等，保持旧行为；
        设成正数后，同一会话桶的请求堆叠时会快速失败，而不是排到客户端超时之后。
        """
        lock = self._lock_for(key)
        timeout = config.BUCKET_LOCK_TIMEOUT_S
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                bucket = key or DEFAULT_SESSION_KEY
                raise ChatGPTBusyError(
                    f"会话桶 {bucket} 正在处理另一个请求（等待超过 {timeout:g}s）。"
                    "请稍后重试；若要并发访问，请为每个 Agent 使用不同的会话标识。"
                ) from None
        else:
            await lock.acquire()
        bucket = key or DEFAULT_SESSION_KEY
        # 持锁期间也算「在飞」：与 send_chat 的外层标记是同一套可重入计数
        self._mark_bucket_active(bucket)
        try:
            yield
        finally:
            self._unmark_bucket_active(bucket)
            lock.release()

    def _touch_page(self, key: Optional[str] = None) -> None:
        self._page_last_used[key or DEFAULT_SESSION_KEY] = time.monotonic()

    def bucket_busy(self, key: Optional[str] = None) -> bool:
        """该桶是否有请求在跑 / 正在生成回复（供 session_store / responses 共用）。

        判据是「请求在飞」的可重入计数：``send_chat`` 一开始就打标记（P0-L），
        覆盖「已定页面、还没拿到锁」那段窗口——旧实现只在持锁期间登记，
        于是这段窗口里页面会被别的桶的空闲回收 / LRU 淘汰顺手关掉。
        并行模式下本桶自己的锁被持有同样算忙（锁可能先于计数被拿到）。

        **不能**只看锁：串行模式（``PARALLEL_BUCKETS=false``）下所有桶共用
        ``self.lock``，用锁判断会把「别的桶在跑」误判成本桶忙（T2.4）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if self._bucket_active(bucket):
            return True
        if not config.PARALLEL_BUCKETS or bucket == DEFAULT_SESSION_KEY:
            return False
        return self._lock_for(bucket).locked()

    async def _close_bucket_page(self, bucket: str, reason: str) -> bool:
        """关闭某个会话桶的页面（**只关页面，状态保留**）。

        返回是否真的关掉了一个页面。会话状态里的轮数 / 体积不动，下次用到该桶时会
        重新打开一条页面，``has_history=False`` 会触发本轮的「播种」重放上下文。

        默认桶也支持（它的页面是 ``self.page``）：它死了没有任何退路，整桥会
        永久 502，因此同样需要能重建（P0-J）。空闲回收 / LRU 淘汰不会碰默认桶。
        """
        if bucket == DEFAULT_SESSION_KEY:
            page = self.page
            self.page = None
        else:
            page = self._pages.pop(bucket, None)
        self._page_ids.pop(bucket, None)
        self._page_last_used.pop(bucket, None)
        if page is None:
            return False
        try:
            await page.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[回收] 关闭 key={bucket} 的页面时出错（已忽略）：{exc}")
        else:
            logger.info(f"[回收] 已关闭 key={bucket} 的页面（{reason}），会话状态保留。")
        return True

    async def _recycle_idle_pages(self, exclude: Optional[str] = None) -> int:
        """关闭空闲超过 ``BUCKET_IDLE_TTL_S`` 的桶页面，返回关闭数量。"""
        ttl = config.BUCKET_IDLE_TTL_S
        if ttl <= 0:
            return 0
        now = time.monotonic()
        closed = 0
        for bucket in list(self._pages):
            if bucket == exclude or self.bucket_busy(bucket):
                continue
            last_used = self._page_last_used.get(bucket, now)
            if now - last_used > ttl:
                closed += 1 if await self._close_bucket_page(bucket, f"空闲超过 {int(ttl)}s") else 0
        return closed

    async def _evict_lru_page(self, exclude: Optional[str] = None) -> bool:
        """淘汰最久未用的桶页面（状态保留），腾出一个位置。

        正在生成回复的桶与 ``exclude`` 永不淘汰：淘汰它们会直接中断正在进行的一轮对话。
        """
        candidates = [
            bucket for bucket in self._pages
            if bucket != exclude and not self.bucket_busy(bucket)
        ]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda b: self._page_last_used.get(b, 0.0))
        return await self._close_bucket_page(oldest, "超出会话桶上限，按 LRU 淘汰")

    async def _wait_ready(self, page) -> bool:
        """等页面的输入框就绪；超时只警告，不抛错（调用方还有自己的等待）。"""
        try:
            await page.wait_for_selector(
                config.READY_SELECTOR, timeout=config.READY_TIMEOUT_MS, state="visible"
            )
            return True
        except Exception:
            logger.warning("[会话] 页面已打开，但未检测到输入框，请检查登录状态。")
            return False

    def _page_alive(self, key: Optional[str] = None) -> bool:
        """该桶当前登记的页面是否还活着（标签未被关闭 / 渲染进程未崩溃）。

        没有 ``is_closed`` 的实现（测试替身）按存活处理；崩溃的渲染进程常常仍报
        ``is_closed() == False``，所以判定只是**快路径**，恢复路径必须能无条件重建。
        """
        return page_alive(self._page_for(key))

    async def _ensure_page(self, key: Optional[str], force: bool = False) -> None:
        """保证该桶有一条**可用**的页面：没有 / 已死则创建或重建。

        旧实现只看 ``bucket in self._pages``（"这条页面是我们建的"），不保证标签
        还活着：用户关掉标签 / 渲染进程崩溃后页面对象仍在池里，之后每次请求都会
        在 ``wait_for_selector`` 上**立刻**失败，而池子永不重建 → 该桶从此永久报错
        （P0-J）。现在：登记页面已死 → 关掉并从池里摘除 → 重建。

        ``force=True`` 供重试阶梯使用：崩溃的页面可能仍报 ``is_closed()==False``，
        因此恢复路径不能依赖判活，而是**无条件重建**。

        重建不回到旧会话 URL（本项目本来就不做 URL 恢复，见 `_restore_session_on_startup`）：
        新页面回到一个空白新对话并把 ``has_history`` 置 False，本轮由调用方用
        「播种」prompt 重放历史，上下文因此不丢。

        桶数量达到 ``MAX_SESSION_BUCKETS`` 时**不再直接报错**：先回收空闲页面，
        再按 LRU 淘汰最久未用的页面（**只关页面、状态保留**）。只有显式把
        ``MAX_SESSION_BUCKETS=0`` 设成“不允许额外桶”时才拒绝。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if not force and self._page_alive(bucket):
            return
        async with self._page_lock:
            if not force and self._page_alive(bucket):  # 并发请求可能已经建好了
                return
            if self.context is None:
                raise RuntimeError("浏览器尚未初始化，无法创建新的会话页面。")
            # 死页面先从池里摘干净：否则下面 ``len(self._pages)`` 会把它算成占用，
            # 而且它还继续让 ``_page_alive`` 判死 → 每次请求都重建失败
            await self._close_bucket_page(bucket, "页面已失效（标签被关闭 / 渲染进程崩溃）")
            if bucket == DEFAULT_SESSION_KEY:
                # 默认桶没有退路：它死了整桥会永久 502，必须同样能重建
                await self._open_bucket_page(bucket)
                logger.info(f"[会话] 默认会话页面已重建（{HOME_URL}）")
                return
            limit = config.MAX_SESSION_BUCKETS
            if limit <= 0:
                raise RuntimeError(
                    "MAX_SESSION_BUCKETS=0 表示不允许额外的会话桶（所有请求共用默认会话）。"
                    "如需按任务隔离，请把它设为 >=1；想彻底关闭分桶请用 SESSION_SCOPING=false。"
                )
            # 先回收空闲页面，仍不够就按 LRU 淘汰最久未用的（两者都不丢会话状态）
            await self._recycle_idle_pages(exclude=bucket)
            while len(self._pages) >= limit:
                if not await self._evict_lru_page(exclude=bucket):
                    raise RuntimeError(
                        f"会话桶数量已达上限（{limit}），且当前没有可回收的页面"
                        "（正在生成回复的会话不会被淘汰）。请稍后重试。"
                    )
            await self._open_bucket_page(bucket)
        logger.info(f"[会话] 已为 key={bucket} 创建独立会话页面（{HOME_URL}）")

    async def _open_bucket_page(self, bucket: str):
        """把 ``bucket`` 指向一条**新的、干净的**页面，并准备到「可发送」状态。

        首次建页与页面失效重建共用这一条路径：``goto(HOME_URL)`` → 新建对话 →
        等输入框就绪 → 选中思考模式。``has_history=False`` 表示这个网页会话里
        没有上下文，本轮必须「播种」（上下文靠重放历史重建）。
        """
        page = await self.context.new_page()
        if bucket == DEFAULT_SESSION_KEY:
            self.page = page
        else:
            self._pages[bucket] = page
        self._page_ids[bucket] = f"page-{id(page):x}"
        self._touch_page(bucket)
        # 每桶始终新开对话；上下文靠本轮的「播种」重建
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._open_new_chat(page)
        await self._wait_ready(page)
        # 新会话默认选中「思考模式」；发送前 _send_chat_locked 还会再确认一次
        await self._select_think_mode(page)
        self._state(bucket).has_history = False
        return page
