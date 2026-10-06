"""日志体系与异常可见性（doc/tasks.md T3.1）。

覆盖：
* ``CHATGPT_DEBUG`` 切换 ``chatgpt_web`` 命名空间的级别；
* 请求级 ``request_id`` 注入日志记录（格式串用 ``%(request_id)s``）；
* 「本应不失败」的落盘点（会话状态 / 任务快照）失败时产生 warning，不再静默；
* 库代码里不再有业务 ``print``（否则面板上看不到日志、也无法分级）。
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_web import config, logging_setup, tasks
from chatgpt_web.driver import DEFAULT_SESSION_KEY, ChatGPTWebDriver

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LevelSwitchTests(unittest.TestCase):
    def tearDown(self) -> None:
        # 还原级别，避免污染其它用例
        logging.getLogger(logging_setup.LOGGER_NAME).setLevel(logging.INFO)

    def test_level_follows_chatgpt_debug(self) -> None:
        logger = logging.getLogger(logging_setup.LOGGER_NAME)
        with mock.patch.object(config, "DEBUG", True):
            logging_setup.configure_logging(force=True)
            self.assertEqual(logger.level, logging.DEBUG)
        with mock.patch.object(config, "DEBUG", False):
            logging_setup.configure_logging(force=True)
            self.assertEqual(logger.level, logging.INFO)

    def test_handler_format_includes_request_id(self) -> None:
        logging_setup.configure_logging(force=True)
        handler = logging.getLogger(logging_setup.LOGGER_NAME).handlers[0]
        self.assertIn("request_id", handler.formatter._fmt or "")

    def test_debug_records_only_when_enabled(self) -> None:
        module_logger = logging.getLogger("chatgpt_web.chat_io")
        with mock.patch.object(config, "DEBUG", False):
            logging_setup.configure_logging(force=True)
            with self.assertLogs(module_logger, level="DEBUG") as captured:
                module_logger.info("普通日志可见")
            self.assertEqual(captured.records[-1].levelno, logging.INFO)
        with mock.patch.object(config, "DEBUG", True):
            logging_setup.configure_logging(force=True)
            with self.assertLogs(module_logger, level="DEBUG") as captured:
                module_logger.debug("调试日志可见")
            self.assertEqual(captured.records[-1].levelno, logging.DEBUG)


class RequestIdTests(unittest.TestCase):
    def test_new_request_id_is_short_and_unique(self) -> None:
        first, second = logging_setup.new_request_id(), logging_setup.new_request_id()
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 24)
        self.assertRegex(first, r"^\d{5}-[0-9a-f]{6}$")

    def test_set_request_id_reaches_log_records(self) -> None:
        logging_setup.configure_logging(force=True)
        handler = logging.getLogger(logging_setup.LOGGER_NAME).handlers[0]
        filter_ = next(f for f in handler.filters if f.__class__.__name__ == "_RequestIdFilter")

        logging_setup.set_request_id("abc-123")
        record = logging.LogRecord("chatgpt_web.test", logging.INFO, __file__, 1, "x", (), None)
        filter_.filter(record)
        self.assertEqual(record.request_id, "abc-123")
        self.assertEqual(logging_setup.current_request_id(), "abc-123")

    def test_request_id_resets_outside_context(self) -> None:
        logging_setup.set_request_id("-")
        self.assertEqual(logging_setup.current_request_id(), "-")


class NonSilentFailureTests(unittest.TestCase):
    """落盘失败必须留下 warning（旧代码是静默 ``except: pass``）。"""

    def test_session_state_write_failure_logs_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 把状态文件指向一个**目录**：write_text 必然失败
            as_dir = Path(tmp) / "state-is-a-dir"
            as_dir.mkdir()
            driver = ChatGPTWebDriver(user_data_dir=str(Path(tmp) / "profile"))
            with mock.patch.object(config, "SESSION_FILE", as_dir), \
                    self.assertLogs("chatgpt_web.session_store", level="WARNING") as captured:
                driver._save_session_state(DEFAULT_SESSION_KEY)
            self.assertTrue(
                any("落盘失败" in r.getMessage() for r in captured.records),
                [r.getMessage() for r in captured.records],
            )

    def test_task_snapshot_write_failure_logs_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 把快照目录指向一个**普通文件**：mkdir 必然失败
            as_file = Path(tmp) / "not-a-dir"
            as_file.write_text("x", encoding="utf-8")
            with mock.patch.object(config, "TASK_FILE_DIR", str(as_file)), \
                    mock.patch.object(config, "TASK_SNAPSHOT_ENABLED", True), \
                    self.assertLogs("chatgpt_web.tasks", level="WARNING") as captured:
                tasks.record("default", [])
            self.assertTrue(
                any("落盘失败" in r.getMessage() for r in captured.records),
                [r.getMessage() for r in captured.records],
            )


class NoPrintTests(unittest.TestCase):
    def test_library_code_has_no_business_print(self) -> None:
        offenders = []
        for path in sorted((PROJECT_ROOT / "chatgpt_web").glob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 允许 traceback.print_exc()：那是异常栈，不是业务输出
                if "print(" in line and "traceback.print_exc(" not in line:
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
        self.assertFalse(
            offenders,
            "库代码仍有 print（应改用 logging）：\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
