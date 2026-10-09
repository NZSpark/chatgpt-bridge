"""页面失效自愈（P0-J）/ 请求在飞保护（P0-L）/ 落盘调用签名（P0-K）。

背景（2026-10-09 真机故障，见参考文档 `lost_page_and_save_files_fix.md`）：

* **P0-J**：页面池只看「这条页面是我们创建的」，不保证标签还活着。用户关掉标签 /
  渲染进程崩溃后，`wait_for_selector` 会**立刻**抛 `TargetClosedError`，而定位输入框
  的实现把它归到「选择器都没命中」→ 上层提示用户去登录，池子却永不重建，
  该桶从此永久失败。崩溃的渲染进程连 `is_closed()` 都仍报 False（判活只是快路径）。
* **P0-L**：发送流程在**拿锁之前**就确定页面，而空闲回收 / LRU 淘汰只看「持锁」，
  这段窗口里页面会被别的桶顺手关掉（表现成随机 502）。用必须**可重入计数**。
* **P0-K**：`save_extracted_files` 缺 `self` / `@staticmethod` 时，按实例调用会变成
  `TypeError: takes 3 positional arguments but 4 were given`（回复已生成、最后一步 500）。
  既有测试全按「类上未绑定调用」写，恰好绕过了那条路径，所以要补**按实例调用**的用例。

这里不开真浏览器：假页面替身要能假出**两种死法**（标签被关：`is_closed()` 为真；
渲染进程崩溃：`is_closed()` 仍为假但什么都失败），它们对应两条不同的代码路径。
"""

import asyncio
import contextlib
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import completion, config  # noqa: E402
from chatgpt_web.driver import DEFAULT_SESSION_KEY, ChatGPTWebDriver  # noqa: E402
from chatgpt_web.errors import (  # noqa: E402
    ChatGPTPageLostError,
    is_page_lost_error,
    is_timeout_error,
    page_alive,
)

# ==================== 页面替身 ====================


class _FakeKeyboard:
    def __init__(self, page) -> None:
        self.page = page
        self.pressed = []
        self.typed = ""

    async def press(self, key: str) -> None:
        self.pressed.append(key)
        if key == "Enter" and self.page.die_on_submit:
            # 用户正好在提交后关掉了标签（真实故障的常见形态）
            self.page.closed = True

    async def insert_text(self, text: str) -> None:
        self.typed = text


class _FakeComposer:
    """composer 句柄替身：读文本非空（好让 _fill_prompt 认为填入成功）。"""

    def __init__(self) -> None:
        self.text = "hi"

    async def click(self, timeout=None) -> None:  # noqa: D401
        return None

    async def evaluate(self, script: str):
        return ""  # _read_input_text / 清理校验都读到空 → 不需要清空

    async def inner_text(self) -> str:
        return self.text

    async def text_content(self) -> str:
        return self.text


class _HealthyPage:
    """活着的最小页面替身。"""

    page_text = "SECRET PAGE BODY"

    def __init__(self, *, die_on_submit: bool = False) -> None:
        self.closed = False
        self.die_on_submit = die_on_submit
        self.keyboard = _FakeKeyboard(self)
        self.url = "https://chatgpt.com/"

    def is_closed(self) -> bool:
        # Playwright 的 is_closed() 是同步接口，替身也保持同步
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def goto(self, url: str, wait_until=None) -> None:
        self.url = url

    async def wait_for_selector(self, selector: str, timeout=None, state=None):
        return _FakeComposer()

    async def query_selector_all(self, selector: str):
        return []

    async def evaluate(self, script: str):
        return {"url": self.url, "ready": "complete", "dialog": False, "login": False}

    async def title(self) -> str:
        return "ChatGPT"


class _ClosedPage(_HealthyPage):
    """标签被关闭：is_closed() 为真，且任何 DOM 操作都立刻失败。"""

    def is_closed(self) -> bool:
        return True

    async def wait_for_selector(self, selector: str, timeout=None, state=None):
        raise RuntimeError("Target page, context or browser has been closed")


class _CrashedPage(_HealthyPage):
    """渲染进程崩溃：is_closed() **仍报 False**，但什么都失败。"""

    def is_closed(self) -> bool:
        return False

    async def wait_for_selector(self, selector: str, timeout=None, state=None):
        raise RuntimeError("Target page, context or browser has been closed")


class _FakePlaywrightTimeout(Exception):
    """名字里带 timeout：模拟 playwright 的 TimeoutError（测试不 import 真包）。"""


class _TimeoutPage(_HealthyPage):
    """页面活着，但每条选择器都等到超时（改版 / 未登录的样子）。"""

    async def wait_for_selector(self, selector: str, timeout=None, state=None):
        raise _FakePlaywrightTimeout("Timeout 2000ms exceeded")


class _FakeContext:
    def __init__(self, factory=None) -> None:
        self.created = []
        self._factory = factory or (lambda: _HealthyPage())

    async def new_page(self):
        page = self._factory()
        self.created.append(page)
        return page


@contextlib.contextmanager
def _quiet_page_setup():
    """把建页 / 轮询里的等待压到零，测试才不会被真超时拖住。"""
    with mock.patch.object(completion, "_NEW_CHAT_SEARCH_TIMEOUT_S", 0.0), \
            mock.patch.object(completion, "_NEW_CHAT_POLL_INTERVAL_S", 0.0), \
            mock.patch.object(config, "THINK_MODE_DEFAULT", False), \
            mock.patch.object(config, "POLL_INTERVAL_S", 0):
        yield


class _RecoveryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-recovery-")
        self._patch = mock.patch.object(
            config, "SESSION_FILE", Path(self.tmp.name) / "state.json"
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmp.cleanup()

    def make_driver(self) -> ChatGPTWebDriver:
        return ChatGPTWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))


# ==================== P0-J：判活与「改版」彻底分开 ====================


class PageLivenessClassificationTests(_RecoveryTestBase):
    def test_closed_page_raises_page_lost(self) -> None:
        driver = self.make_driver()
        with self.assertRaises(ChatGPTPageLostError) as ctx:
            asyncio.run(driver.dom.find_input(_ClosedPage()))
        self.assertIn("标签已失效", str(ctx.exception))

    def test_crashed_page_raises_page_lost_even_though_is_closed_is_false(self) -> None:
        """崩溃的渲染进程仍报 is_closed()==False——判活只是快路径，不能当唯一判据。"""
        page = _CrashedPage()
        self.assertTrue(page_alive(page), "前提：崩溃页面在快路径上被判为存活")
        driver = self.make_driver()
        with self.assertRaises(ChatGPTPageLostError):
            asyncio.run(driver.dom.find_input(page))

    def test_alive_page_with_all_selectors_missing_returns_none(self) -> None:
        """页面活着但选择器全不中（改版 / 未登录）→ 仍然返回 None，不误判成页面已死。"""
        driver = self.make_driver()
        self.assertIsNone(asyncio.run(driver.dom.find_input(_TimeoutPage())))

    def test_missing_input_records_scene_without_page_text(self) -> None:
        driver = self.make_driver()
        asyncio.run(driver.dom.find_input(_TimeoutPage()))
        hint = driver.dom.input_probe_hint()
        self.assertIn("URL=https://chatgpt.com/", hint)
        self.assertIn("readyState=complete", hint)
        self.assertIn("登录墙", hint)
        self.assertIn("选择器命中", hint)
        self.assertNotIn("SECRET PAGE BODY", hint, "现场信息不得回显页面正文")

    def test_input_missing_message_carries_the_scene(self) -> None:
        driver = self.make_driver()
        driver.dom.last_input_probe = "URL=x readyState=complete 弹层=无 登录墙=未见"
        message = driver._input_missing_message()
        self.assertIn("无法找到对话输入框", message)
        self.assertIn("readyState=complete", message)

    def test_polling_read_converts_target_closed(self) -> None:
        """生成过程中页面失效：TargetClosedError 不能冒到路由层变成裸 500。"""

        class _DeadOnQuery(_HealthyPage):
            async def query_selector_all(self, selector: str):
                raise RuntimeError("Target page, context or browser has been closed")

        driver = self.make_driver()
        with self.assertRaises(ChatGPTPageLostError):
            asyncio.run(driver.dom.find_assistant_messages(_DeadOnQuery()))

    def test_async_is_closed_double_is_treated_as_alive(self) -> None:
        """异步 is_closed（非本项目实现的替身）不能阻塞，按存活处理即可。"""

        class _AsyncClosedPage:
            async def is_closed(self) -> bool:
                return True

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.assertTrue(page_alive(_AsyncClosedPage()))

    def test_helpers_classify_errors_without_importing_playwright(self) -> None:
        self.assertTrue(is_page_lost_error(RuntimeError("Target crashed")))
        self.assertTrue(is_page_lost_error(RuntimeError("Target page, context or browser has been closed")))
        self.assertFalse(is_page_lost_error(_FakePlaywrightTimeout("Timeout 2000ms exceeded")))
        self.assertTrue(is_timeout_error(_FakePlaywrightTimeout("Timeout 2000ms exceeded")))
        self.assertFalse(is_timeout_error(RuntimeError("malformed selector")))
        self.assertTrue(page_alive(object()), "没有 is_closed 的实现按存活处理")


class EnsurePageRebuildTests(_RecoveryTestBase):
    def test_live_page_is_kept(self) -> None:
        driver = self.make_driver()
        page = _HealthyPage()
        driver._pages["b"] = page
        driver.context = _FakeContext()
        asyncio.run(driver._ensure_page("b"))
        self.assertIs(driver._pages["b"], page)
        self.assertEqual(driver.context.created, [], "页面还活着就不该重建")
        self.assertTrue(driver._page_alive("b"))

    def test_dead_bucket_page_is_rebuilt_and_state_kept(self) -> None:
        """登记页面已死 → 关掉它、重建，且轮数等会话状态保留（P0-J 第 3 条）。"""
        driver = self.make_driver()
        dead = _ClosedPage()
        driver._pages["b"] = dead
        driver._page_ids["b"] = "page-dead"
        driver._page_last_used["b"] = 1.0
        state = driver._state("b")
        state.turns = 3
        state.est_tokens = 99
        state.has_history = True
        driver.context = _FakeContext()

        with _quiet_page_setup():
            asyncio.run(driver._ensure_page("b"))

        rebuilt = driver._pages["b"]
        self.assertIsNot(rebuilt, dead)
        self.assertTrue(dead.closed, "死页面必须从池里摘掉（否则永远重建不了）")
        self.assertNotEqual(driver._page_ids["b"], "page-dead")
        self.assertIs(rebuilt, driver.context.created[0])
        after = driver._state("b")
        self.assertEqual(after.turns, 3, "重建不该丢掉会话的轮数/预算")
        self.assertFalse(after.has_history, "新页面是空白会话 → 本轮必须播种")

    def test_dead_default_bucket_page_is_rebuilt(self) -> None:
        """默认桶（不参与分桶的那条页面）死了没有任何退路，整桥会永久 502。"""
        driver = self.make_driver()
        dead = _ClosedPage()
        driver.page = dead
        driver._page_ids[DEFAULT_SESSION_KEY] = "page-dead"
        driver.context = _FakeContext()

        with _quiet_page_setup():
            asyncio.run(driver._ensure_page(None))

        self.assertIsNot(driver.page, dead)
        self.assertTrue(dead.closed)
        self.assertIs(driver.page, driver.context.created[0])

    def test_force_rebuild_does_not_depend_on_liveness(self) -> None:
        """恢复路径必须能**无条件重建**：崩溃的页面仍报存活，判活帮不上忙。"""
        driver = self.make_driver()
        crashed = _CrashedPage()
        driver._pages["b"] = crashed
        driver.context = _FakeContext()
        self.assertTrue(driver._page_alive("b"))

        with _quiet_page_setup():
            asyncio.run(driver._ensure_page("b", force=True))

        self.assertIsNot(driver._pages["b"], crashed)


class SendChatPageRecoveryTests(_RecoveryTestBase):
    def test_rebuilds_page_and_resends_without_rotating(self) -> None:
        driver = self.make_driver()
        original = _HealthyPage()
        driver.page = original
        driver.context = _FakeContext()
        driver._state(DEFAULT_SESSION_KEY).has_history = True

        seen = []

        async def fake_locked(prompt, on_delta=None, key=None):
            # 第一轮：页面在发送途中失效（用户关标签的典型时序）
            seen.append(prompt)
            if len(seen) == 1:
                raise ChatGPTPageLostError("标签已失效（页面已关闭，URL=https://chatgpt.com/）")
            return "recovered", []

        rotations = []

        async def fake_rotation(key=None):
            rotations.append(key)

        with _quiet_page_setup(), \
                mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 2), \
                mock.patch.object(driver, "_send_chat_locked", side_effect=fake_locked), \
                mock.patch.object(driver, "_start_new_session", side_effect=fake_rotation):
            reply, blocks = asyncio.run(
                driver.send_chat("delta-only", seeded_prompt="SEEDED-HISTORY", key=None)
            )

        self.assertEqual((reply, blocks), ("recovered", []))
        self.assertEqual(len(seen), 2, "页面失效后应该重发一轮")
        self.assertEqual(seen[0], "delta-only", "页面活着时照旧只发增量 prompt")
        self.assertEqual(seen[1], "SEEDED-HISTORY", "重建后必须用播种 prompt 重放上下文")
        self.assertEqual(rotations, [], "页面失效不该触发轮转 / 换新会话")
        self.assertTrue(original.closed, "失效页面必须被关掉并从池里摘除")
        self.assertIsNot(driver.page, original)
        self.assertIs(driver.page, driver.context.created[0])
        self.assertFalse(driver.bucket_busy(DEFAULT_SESSION_KEY), "请求结束后不得残留忙标记")

    def test_reports_page_lost_when_rebuilt_page_dies_too(self) -> None:
        driver = self.make_driver()
        driver.context = _FakeContext()

        async def always_lost(prompt, on_delta=None, key=None):
            raise ChatGPTPageLostError("标签已失效（页面已关闭，URL=https://chatgpt.com/）")

        with _quiet_page_setup(), \
                mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1), \
                mock.patch.object(driver, "_send_chat_locked", side_effect=always_lost):
            with self.assertRaises(ChatGPTPageLostError) as ctx:
                asyncio.run(driver.send_chat("hi", key=None))

        self.assertIn("标签已失效", str(ctx.exception))
        self.assertIn("标签已失效", driver._state(DEFAULT_SESSION_KEY).last_error or "")

    def test_generation_dying_midway_is_reported_as_page_lost(self) -> None:
        """生成过程中页面失效：轮询每轮判活，抛专用异常而不是裸 500。"""
        driver = self.make_driver()
        driver.page = _HealthyPage(die_on_submit=True)
        driver.context = _FakeContext(factory=lambda: _HealthyPage(die_on_submit=True))

        with _quiet_page_setup(), mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
            with self.assertRaises(ChatGPTPageLostError) as ctx:
                asyncio.run(driver.send_chat("hi", key=None))

        self.assertIn("生成过程中页面失效", str(ctx.exception))


# ==================== P0-L：请求在飞的桶不能被顺手回收 ====================


class InflightProtectionTests(_RecoveryTestBase):
    def test_recycle_skips_inflight_bucket(self) -> None:
        """请求已开始但还没拿到锁的窗口里，空闲回收不得关掉它的页面。"""
        driver = self.make_driver()
        inflight, idle = _HealthyPage(), _HealthyPage()
        driver._pages.update({"inflight": inflight, "idle": idle})
        stale = time.monotonic() - 1000
        driver._page_last_used.update({"inflight": stale, "idle": stale})
        driver._mark_bucket_active("inflight")

        with mock.patch.object(config, "BUCKET_IDLE_TTL_S", 1.0):
            closed = asyncio.run(driver._recycle_idle_pages())

        self.assertEqual(closed, 1)
        self.assertFalse(inflight.closed)
        self.assertIn("inflight", driver._pages)
        self.assertTrue(idle.closed)

    def test_evict_lru_skips_inflight_bucket(self) -> None:
        driver = self.make_driver()
        inflight = _HealthyPage()
        driver._pages["inflight"] = inflight
        driver._page_last_used["inflight"] = time.monotonic() - 1000
        driver._mark_bucket_active("inflight")

        self.assertFalse(asyncio.run(driver._evict_lru_page()))
        self.assertFalse(inflight.closed)

    def test_reentrant_count_keeps_bucket_busy_after_lock_release(self) -> None:
        """内层（锁）先退出不能撤掉外层（整个请求）的保护。"""
        driver = self.make_driver()

        async def scenario() -> bool:
            driver._mark_bucket_active("b")  # 外层：整个 send_chat
            try:
                async with driver._session_lock("b"):
                    self.assertTrue(driver.bucket_busy("b"))
                return driver.bucket_busy("b")
            finally:
                driver._unmark_bucket_active("b")

        self.assertTrue(asyncio.run(scenario()), "内层（锁）退出后外层保护必须还在")
        self.assertFalse(driver.bucket_busy("b"))
        self.assertFalse(driver.bucket_busy("b"))
        self.assertEqual(driver.busy_keys(), [])

    def test_send_chat_marks_bucket_inflight_before_taking_the_lock(self) -> None:
        driver = self.make_driver()
        driver.context = _FakeContext()
        driver.page = _HealthyPage()
        seen = {}

        async def fake_ensure(key, force=False):
            seen["busy"] = driver.bucket_busy(key or DEFAULT_SESSION_KEY)

        async def fake_locked(prompt, on_delta=None, key=None):
            return "ok", []

        with _quiet_page_setup(), \
                mock.patch.object(driver, "_ensure_page", side_effect=fake_ensure), \
                mock.patch.object(driver, "_send_chat_locked", side_effect=fake_locked):
            asyncio.run(driver.send_chat("hi", key="b"))

        self.assertTrue(seen["busy"], "旧实现只在持锁期间登记 → 这段窗口里页面可能被别的桶回收")
        self.assertFalse(driver.bucket_busy("b"))


# ==================== P0-K：落盘签名（按实例调用） ====================


class SaveExtractedFilesSignatureTests(_RecoveryTestBase):
    """调用点就是 ``driver.save_extracted_files``（实例属性访问）→ 缺 self 必炸。"""

    def test_instance_call_saves_code_block(self) -> None:
        driver = self.make_driver()
        with tempfile.TemporaryDirectory(prefix="bridge-save-") as out:
            saved = driver.save_extracted_files(
                "reply text", [{"lang": "python", "code": "print(1)"}], out
            )
            self.assertEqual(len(saved), 1, saved)
            self.assertTrue(Path(saved[0]).is_file())
            self.assertTrue(saved[0].endswith(".py"))

    def test_bound_method_through_to_thread_saves_response(self) -> None:
        """线上调用形态：``asyncio.to_thread(driver.save_extracted_files, raw, blocks, dir)``。"""
        driver = self.make_driver()
        with tempfile.TemporaryDirectory(prefix="bridge-save-") as out:
            saved = asyncio.run(asyncio.to_thread(
                driver.save_extracted_files, "reply text", [], out
            ))
            self.assertEqual(len(saved), 1, saved)
            self.assertTrue(Path(saved[0]).is_file())
            self.assertEqual(Path(saved[0]).read_text(encoding="utf-8"), "reply text")


if __name__ == "__main__":
    unittest.main(verbosity=2)
