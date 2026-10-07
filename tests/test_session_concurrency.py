"""PI-017 session concurrency regression tests."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import config  # noqa: E402
from chatgpt_web.driver import ChatGPTWebDriver  # noqa: E402
from chatgpt_web.errors import ChatGPTBusyError  # noqa: E402


class SessionConcurrencyMatrixTests(unittest.TestCase):
    def setUp(self):
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        self._patch = mock.patch.object(
            config, "SESSION_FILE", Path("/tmp/chatgpt-test-concurrency.json")
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Path("/tmp/chatgpt-test-concurrency.json").unlink(missing_ok=True)

    def test_same_session_ten_requests_never_overlap(self):
        async def scenario():
            active = 0
            peak = 0

            async def worker():
                nonlocal active, peak
                async with self.driver._session_lock("same"):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0)
                    active -= 1

            await asyncio.gather(*(worker() for _ in range(10)))
            return peak

        with mock.patch.object(config, "PARALLEL_BUCKETS", True):
            peak = asyncio.run(scenario())
        self.assertEqual(peak, 1)

    def test_two_sessions_can_overlap_when_parallel_enabled(self):
        async def scenario():
            first_entered = asyncio.Event()
            release = asyncio.Event()
            active = 0
            peak = 0

            async def worker(bucket, entered):
                nonlocal active, peak
                async with self.driver._session_lock(bucket):
                    active += 1
                    peak = max(peak, active)
                    entered.set()
                    await release.wait()
                    active -= 1

            first = asyncio.create_task(worker("a", first_entered))
            await first_entered.wait()
            second_entered = asyncio.Event()
            second = asyncio.create_task(worker("b", second_entered))
            await asyncio.wait_for(second_entered.wait(), timeout=0.5)
            release.set()
            await asyncio.gather(first, second)
            return peak

        with mock.patch.object(config, "PARALLEL_BUCKETS", True):
            peak = asyncio.run(scenario())
        self.assertEqual(peak, 2)

    def test_lock_timeout_raises_upstream_busy(self):
        async def scenario():
            with mock.patch.object(config, "PARALLEL_BUCKETS", True), \
                    mock.patch.object(config, "BUCKET_LOCK_TIMEOUT_S", 0.01):
                async with self.driver._session_lock("busy"):
                    with self.assertRaises(ChatGPTBusyError):
                        async with self.driver._session_lock("busy"):
                            pass

        asyncio.run(scenario())

    def test_reset_while_request_running_keeps_bucket_busy_until_release(self):
        async def scenario():
            async with self.driver._session_lock("running"):
                self.driver._state("running").has_history = True
                self.driver.reset_session("running")
                self.assertTrue(self.driver._state("running").pending_rotation)
                self.assertTrue(self.driver.bucket_busy("running"))
            self.assertFalse(self.driver.bucket_busy("running"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
