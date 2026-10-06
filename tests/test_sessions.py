"""会话状态内存缓存的有界化 + 桶忙闲语义（T2.4）。"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import config  # noqa: E402
from chatgpt_web.driver import DEFAULT_SESSION_KEY, ChatGPTWebDriver  # noqa: E402


class SessionCacheEvictionTests(unittest.TestCase):
    def setUp(self):
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        self._patch = mock.patch.object(config, "SESSION_FILE", Path("/tmp/chatgpt-test-state.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Path("/tmp/chatgpt-test-state.json").unlink(missing_ok=True)

    def test_evicts_oldest_bucket_keeps_default(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 2):
            self.driver._state(DEFAULT_SESSION_KEY)
            self.driver._page_last_used["b1"] = 100.0
            self.driver._state("b1")
            self.driver._page_last_used["b2"] = 200.0
            self.driver._state("b2")
            self.assertNotIn("b1", self.driver._sessions)
            self.assertIn("b2", self.driver._sessions)
            self.assertIn(DEFAULT_SESSION_KEY, self.driver._sessions)

    def test_default_bucket_never_evicted(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 1):
            self.driver._state(DEFAULT_SESSION_KEY)
            for i in range(5):
                self.driver._page_last_used[f"b{i}"] = float(i)
                self.driver._state(f"b{i}")
            self.assertIn(DEFAULT_SESSION_KEY, self.driver._sessions)

    def test_last_prompt_evicted_with_state(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 1):
            self.driver._state(DEFAULT_SESSION_KEY)
            self.driver._last_prompts["b1"] = "x"
            self.driver._page_last_used["b1"] = 1.0
            self.driver._state("b1")
            self.driver._page_last_used["b2"] = 2.0
            self.driver._state("b2")
            self.assertNotIn("b1", self.driver._last_prompts)

    def test_zero_disables_eviction(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 0):
            for i in range(10):
                self.driver._page_last_used[f"b{i}"] = float(i)
                self.driver._state(f"b{i}")
            self.assertEqual(len(self.driver._sessions), 10)


class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class BucketBusySemanticsTests(unittest.TestCase):
    """T2.4：忙闲判断必须反映**真实桶**，而不是共享锁的状态。

    旧实现用 ``_lock_for(bucket).locked()``：在 ``PARALLEL_BUCKETS=false``
    （所有桶共用一把锁）时，只要任意桶在跑，所有桶都被判成忙——于是流式路径
    把「别的桶在跑」误报成 503，LRU 换页也永远选不出候选。
    """

    def setUp(self):
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        self._patch = mock.patch.object(
            config, "SESSION_FILE", Path("/tmp/chatgpt-test-busy.json")
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Path("/tmp/chatgpt-test-busy.json").unlink(missing_ok=True)

    def test_only_active_bucket_is_busy_in_serial_mode(self):
        with mock.patch.object(config, "PARALLEL_BUCKETS", False):
            self.driver._active_buckets.add("other")
            self.assertTrue(self.driver.bucket_busy("other"))
            self.assertFalse(self.driver.bucket_busy("mine"))
            self.assertFalse(self.driver.bucket_busy())
            self.assertEqual(self.driver.busy_keys(), ["other"])

    def test_shared_lock_held_by_other_bucket_is_not_busy(self):
        """串行模式前提校验：锁确实共享，但本桶不应被判忙。"""
        async def scenario() -> bool:
            async with self.driver._session_lock("other"):
                shared = self.driver._lock_for("mine").locked()
                self.assertTrue(self.driver.bucket_busy("other"))
                self.assertFalse(self.driver.bucket_busy("mine"))
                return shared

        with mock.patch.object(config, "PARALLEL_BUCKETS", False), \
                mock.patch.object(config, "BUCKET_LOCK_TIMEOUT_S", 5):
            self.assertTrue(asyncio.run(scenario()), "前提：串行模式下锁是共享的")
        # 锁释放后全部空闲
        self.assertFalse(self.driver.bucket_busy("other"))

    def test_lru_eviction_picks_candidate_while_other_bucket_is_busy(self):
        """别的桶忙、本桶空闲时，LRU 换页仍能选出候选（旧实现永远选不出来）。"""
        busy_page, idle_page = _FakePage(), _FakePage()
        self.driver._pages["busy"] = busy_page
        self.driver._pages["idle"] = idle_page
        self.driver._page_last_used.update({"busy": 1.0, "idle": 2.0})

        async def scenario() -> bool:
            async with self.driver._session_lock("busy"):
                return await self.driver._evict_lru_page(exclude="new")

        with mock.patch.object(config, "PARALLEL_BUCKETS", False), \
                mock.patch.object(config, "BUCKET_LOCK_TIMEOUT_S", 5):
            self.assertTrue(asyncio.run(scenario()))
        self.assertTrue(idle_page.closed)
        self.assertFalse(busy_page.closed, "正在生成的桶不得被淘汰")
        self.assertIn("busy", self.driver._pages)
        self.assertNotIn("idle", self.driver._pages)


class SeedShrinkOnRepeatedCapTests(unittest.TestCase):
    """连续“到顶”时应压缩播种 prompt，避免死循环（见 chat_io.send_chat）。"""

    def setUp(self):
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        self._patch = mock.patch.object(config, "SESSION_FILE", Path("/tmp/chatgpt-test-state2.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Path("/tmp/chatgpt-test-state2.json").unlink(missing_ok=True)

    def test_incremental_prompt_never_shrunk(self):
        self.driver._state(DEFAULT_SESSION_KEY).cap_failures = 5
        prompt = "x" * 5000
        out = self.driver._shrink_seed_if_repeated_cap(
            DEFAULT_SESSION_KEY, prompt, is_seed=False
        )
        self.assertEqual(out, prompt)

    def test_seed_below_threshold_unchanged(self):
        self.driver._state(DEFAULT_SESSION_KEY).cap_failures = 1
        prompt = "x" * 5000
        out = self.driver._shrink_seed_if_repeated_cap(
            DEFAULT_SESSION_KEY, prompt, is_seed=True
        )
        self.assertEqual(out, prompt)

    def test_seed_shrunk_after_two_failures(self):
        self.driver._state(DEFAULT_SESSION_KEY).cap_failures = 2
        prompt = "HEAD" + ("x" * 20000) + "TAIL"
        out = self.driver._shrink_seed_if_repeated_cap(
            DEFAULT_SESSION_KEY, prompt, is_seed=True
        )
        self.assertLess(len(out), len(prompt))
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))
        self.assertIn("压缩中段", out)

    def test_shrink_has_floor(self):
        self.driver._state(DEFAULT_SESSION_KEY).cap_failures = 20
        prompt = "x" * 100000
        out = self.driver._shrink_seed_if_repeated_cap(
            DEFAULT_SESSION_KEY, prompt, is_seed=True
        )
        # 下限 2000（外加少量分隔标记），不会被压到近乎空
        self.assertGreaterEqual(len(out), 2000)

    def test_cap_failures_roundtrip_and_reset_on_success(self):
        from chatgpt_web.session_store import SessionState

        s = SessionState(cap_failures=3)
        self.assertEqual(SessionState.from_payload(s.to_payload()).cap_failures, 3)
        self.assertEqual(SessionState.from_payload({}).cap_failures, 0)


if __name__ == "__main__":
    unittest.main()
