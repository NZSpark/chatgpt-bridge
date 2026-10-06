"""会话状态落盘的并发安全与原子性（doc/tasks.md T3.3）。

* 并发保存不同桶时，后写不能把先写的整个覆盖（旧实现是「读改写」且无锁）；
* 写入中途失败（例如序列化炸掉）不得损坏已存在的状态文件；
* 落盘失败必须留下 warning（与 T3.1 同一契约）。
"""

import asyncio
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_web import config, session_store
from chatgpt_web.driver import ChatGPTWebDriver


class _DriverCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_file = self.tmp / ".chatgpt_state"
        patch = mock.patch.object(config, "SESSION_FILE", self.state_file)
        patch.start()
        self.addCleanup(patch.stop)
        self.driver = ChatGPTWebDriver(user_data_dir=str(self.tmp / "profile"))

    def _write(self, bucket: str, turns: int) -> None:
        self.driver._state(bucket).turns = turns
        self.driver._save_session_state(bucket)


class ConcurrentSaveTests(_DriverCase):
    def test_two_buckets_both_survive_concurrent_saves(self) -> None:
        # 放大竞态窗口：没有锁时，两个线程会读到同一份旧状态、相互覆盖。
        # 有锁（T3.3 修复）时后写会看到先写的结果，两个桶都在。
        original_read = session_store.SessionStoreMixin._read_state_file

        def slow_read(self):  # noqa: ANN001
            time.sleep(0.05)
            return original_read(self)

        with mock.patch.object(session_store.SessionStoreMixin, "_read_state_file", slow_read):
            threads = [
                threading.Thread(target=self._write, args=("bucket-a", 3)),
                threading.Thread(target=self._write, args=("bucket-b", 7)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        sessions = data.get("sessions") or {}
        self.assertIn("bucket-a", sessions, f"并发落盘丢了桶：{sessions}")
        self.assertIn("bucket-b", sessions)
        self.assertEqual(sessions["bucket-a"]["turns"], 3)
        self.assertEqual(sessions["bucket-b"]["turns"], 7)

    def test_concurrent_saves_through_async_to_thread(self) -> None:
        async def scenario() -> None:
            await asyncio.gather(
                self.driver._remember_session("async-a"),
                self.driver._remember_session("async-b"),
                self.driver._remember_session("async-c"),
            )

        asyncio.run(scenario())
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        sessions = data.get("sessions") or {}
        for bucket in ("async-a", "async-b", "async-c"):
            self.assertIn(bucket, sessions, f"asyncio.to_thread 并发落盘丢了 {bucket}")


class AtomicWriteTests(_DriverCase):
    def test_failure_before_replace_keeps_previous_file(self) -> None:
        self._write("bucket-a", 1)
        before = self.state_file.read_text(encoding="utf-8")

        with mock.patch.object(session_store.json, "dumps",
                               side_effect=RuntimeError("序列化失败")), \
                self.assertLogs("chatgpt_web.session_store", level="WARNING") as captured:
            self._write("bucket-a", 99)

        after = self.state_file.read_text(encoding="utf-8")
        self.assertEqual(after, before, "写入失败后原状态文件被破坏")
        self.assertEqual(json.loads(after)["sessions"]["bucket-a"]["turns"], 1)
        self.assertTrue(
            any("落盘失败" in r.getMessage() for r in captured.records),
            [r.getMessage() for r in captured.records],
        )
        # 临时文件不应残留
        self.assertFalse((self.tmp / ".chatgpt_state.tmp").exists())

    def test_corrupt_state_file_warns_once_and_recovers(self) -> None:
        self.state_file.write_text("{ 这不是 JSON", encoding="utf-8")
        # 只提醒一次的标记是模块级全局（跨用例共享），这里显式重置
        with mock.patch.object(session_store, "_warned_bad_state", False), \
                self.assertLogs("chatgpt_web.session_store", level="WARNING") as captured:
            self.driver._read_state_file()
            self.driver._read_state_file()
        self.assertEqual(
            len([r for r in captured.records if "解析失败" in r.getMessage()]), 1,
            f"损坏状态文件的 warning 应只提醒一次：{[r.getMessage() for r in captured.records]}",
        )
        # 恢复写入后状态正常
        self._write("bucket-a", 2)
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["sessions"]["bucket-a"]["turns"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
