"""PI-902：completion 子包拆分后的模块级回归测试。

验证：

* lifecycle / generator / extractor 三个子模块可独立导入（无循环依赖）；
* ``chatgpt_web.completion``（现在是包，同时充当兼容 facade）暴露同样的对象；
* 结束判定状态机 / 阈值组装 / 代码块去噪与拆分前一致；
* 可打补丁常量 ``_NEW_CHAT_SEARCH_TIMEOUT_S`` / ``_NEW_CHAT_POLL_INTERVAL_S``
  仍在 facade 上，且 :meth:`CompletionMixin._open_new_chat` 会回读它们。
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import completion  # noqa: E402
from chatgpt_web.completion import (  # noqa: E402
    CompletionMixin,
    EndLimits,
    EndState,
    build_end_limits,
    emit_delta,
    evaluate_poll,
    extractor,
    generator,
    lifecycle,
    strip_code_noise,
)
from chatgpt_web.driver import ChatGPTWebDriver  # noqa: E402


class PackageImportTest(unittest.TestCase):
    def test_submodules_import(self):
        self.assertTrue(all(m is not None for m in (lifecycle, generator, extractor)))

    def test_facade_reexports_implementation(self):
        self.assertIs(completion.CompletionMixin, lifecycle.CompletionMixin)
        self.assertIs(completion.CompletionMixin, CompletionMixin)
        self.assertIs(completion.evaluate_poll, generator.evaluate_poll)
        self.assertIs(completion.strip_code_noise, extractor.strip_code_noise)

    def test_generation_js_contract(self):
        # 回归测试直接检查这两个 JS 常量，必须继续在 CompletionMixin 上。
        self.assertIn("stop", CompletionMixin._GENERATING_JS.lower())
        self.assertIn("matches", CompletionMixin._STOP_CANDIDATES_JS)
        self.assertTrue(CompletionMixin._STOP_TOKEN_PATTERN)


class GeneratorTest(unittest.TestCase):
    def test_build_end_limits_matches_config(self):
        from chatgpt_web import config

        limits = build_end_limits()
        self.assertEqual(limits.quiet_polls, max(1, int(config.RESUME_QUIET_POLLS)))
        self.assertEqual(limits.stable_polls, int(config.STABLE_POLLS))
        self.assertEqual(limits.stall_limit, max(1, int(config.STALL_POLLS)))
        self.assertEqual(limits.extend_step_s, config.RESPONSE_TIMEOUT_S)

    def test_evaluate_poll_finishes_on_settled(self):
        limits = EndLimits(quiet_polls=1, stable_polls=2, stall_limit=3, extend_step_s=5.0)
        verdict = evaluate_poll(
            EndState(quiet_count=0, stable_count=0, stalled=0, saw_generating=True),
            limits=limits,
            reply_seen=True,
            generating=False,
            pending=False,
            normalized="hello",
            last_normalized="hello",
            last_len=5,
        )
        self.assertTrue(verdict.finished)

    def test_emit_delta_accepts_sync_and_none(self):
        seen = []
        asyncio.run(emit_delta(seen.append, "chunk"))
        self.assertEqual(seen, ["chunk"])
        asyncio.run(emit_delta(None, "chunk"))  # 不应抛异常


class ExtractorTest(unittest.TestCase):
    def test_strips_language_label_keeps_body_whitespace(self):
        # 语言标签行被剥掉，正文（含其尾随换行）原样保留。
        out = strip_code_noise("python\nprint('x')\n", "python")
        self.assertEqual(out, "print('x')\n")

    def test_preserves_leading_and_trailing_blank_lines(self):
        out = strip_code_noise("\n\nreal\n", "python")
        # 首尾空行属于正文，不能被删（edit 工具逐字节匹配依赖它）
        self.assertIn("real", out)


class PatchableConstantsTest(unittest.TestCase):
    def test_constants_live_on_facade(self):
        self.assertEqual(completion._NEW_CHAT_SEARCH_TIMEOUT_S, 8.0)
        self.assertEqual(completion._NEW_CHAT_POLL_INTERVAL_S, 0.4)

    def test_open_new_chat_reads_patched_facade_constants(self):
        driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        captured = {}

        async def fake_open(page):
            captured["timeout"] = driver.dom.NEW_CHAT_SEARCH_TIMEOUT_S
            captured["poll"] = driver.dom.NEW_CHAT_POLL_INTERVAL_S

        driver.dom.open_new_chat = fake_open  # type: ignore[assignment]
        with mock.patch.object(completion, "_NEW_CHAT_SEARCH_TIMEOUT_S", 1.5), \
                mock.patch.object(completion, "_NEW_CHAT_POLL_INTERVAL_S", 0.05):
            asyncio.run(driver._open_new_chat(object()))
        self.assertEqual(captured["timeout"], 1.5)
        self.assertEqual(captured["poll"], 0.05)


if __name__ == "__main__":
    unittest.main()
