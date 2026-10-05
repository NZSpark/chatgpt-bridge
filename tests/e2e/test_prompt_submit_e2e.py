"""E2E：联网真实测试「多行 prompt 能否成功提交」并拿到回复。

回归对象：工具模式下 prompt 含换行，旧代码用合成 KeyboardEvent 派发 Enter，
ProseMirror 的 keymap 忽略合成事件（isTrusted=false），提交失败。修法改为
真实键盘 Enter（chat_io._keyboard_enter）。本用例直接驱动 ChatGPTWebDriver
走真实网络，验证：

* T1 单行 prompt 提交并收到回复；
* T2 多行 prompt（含换行 / 代码块）提交并收到回复——**核心回归**；
* T3 输入框残留草稿会被清空，新 prompt 不被拼接（发送前 prefill 一段旧文本）。

默认不运行：需 CHATGPT_E2E=1（真实访问 ChatGPT）。运行：

    CHATGPT_E2E=1 E2E_HEADED=1 .venv/bin/python -m pytest \
        tests/e2e/test_prompt_submit_e2e.py -v -s

重要实现约束（踩过的坑）：
* driver 内部的 ``asyncio.Lock`` 绑定在**创建它的 event loop** 上。若在
  setUpClass 里 ``asyncio.run(init())``、又在每个用例里另起一个
  ``asyncio.run(send_chat())``，锁会跨 loop 使用而**永久阻塞**。因此这里每个
  用例在**同一个** ``asyncio.run`` 里完成 init + 发送 + 收尾。
* **只用默认会话桶**（不传 key）。默认 ``PARALLEL_BUCKETS=false`` 时，
  ``_ensure_page`` 对非默认桶直接返回、不建页，``_page_for`` 会得到 None。
* ``init()`` 内部已导航到 ChatGPT 首页并新建对话，无需再 goto。
"""

import asyncio
import os
import unittest
from pathlib import Path

from chatgpt_web import config
from chatgpt_web.driver import ChatGPTWebDriver

GATE = os.environ.get("CHATGPT_E2E") == "1"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE = (PROJECT_ROOT / config.USER_DATA_DIR).resolve()


@unittest.skipUnless(GATE, "需 CHATGPT_E2E=1（真实访问 ChatGPT）")
class PromptSubmitE2ETests(unittest.TestCase):
    """真实网络：提交 prompt 的端到端验证。

    每个用例各自启停一次浏览器；init / 发送 / 关闭全在同一个 event loop 内，
    避免 ``asyncio.Lock`` 跨 loop 造成死锁。
    """

    @classmethod
    def setUpClass(cls):
        if not PROFILE.exists():
            raise unittest.SkipTest(
                f"缺少登录目录 {PROFILE}——请先 HEADLESS=false 手动登录 ChatGPT"
            )

    async def _run_case(self, prompt: str, prefill: str = None):
        """在单个 event loop 内：init -> (可选预填) -> 发送 -> 收尾。"""
        driver = ChatGPTWebDriver(user_data_dir=str(PROFILE))
        await driver.init()
        try:
            if prefill is not None:
                page = driver.page
                chat_input = await driver._find_input(page)
                if chat_input is None:
                    raise AssertionError("找不到输入框")
                await driver._call_fill(page, chat_input, prefill, 5000)
            text, _ = await driver.send_chat(prompt)
            return text or ""
        finally:
            try:
                if driver.context is not None:
                    await driver.context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if driver.playwright is not None:
                    await driver.playwright.stop()
            except Exception:  # noqa: BLE001
                pass

    def test_single_line_prompt_submits(self):
        text = asyncio.run(self._run_case("只回复两个字：收到"))
        self.assertTrue(text.strip(), "单行 prompt 提交后未收到任何回复")

    def test_multiline_prompt_submits(self):
        """核心回归：多行 prompt（含换行 / 代码块）必须能提交成功。"""
        prompt = (
            "下面是一段多行内容，请只回复 OK 两个字母，不要解释：\n"
            "第一行\n"
            "第二行\n"
            "```python\nprint('hello')\n```\n"
            "最后一行"
        )
        text = asyncio.run(self._run_case(prompt))
        self.assertTrue(
            text.strip(),
            "多行 prompt 提交后未收到任何回复——合成 Enter 回归（应走真实键盘 Enter）",
        )

    def test_stale_draft_is_cleared(self):
        """输入框残留草稿必须被清空，不能被拼进新 prompt。"""
        marker = "STALE_DRAFT_SHOULD_NOT_APPEAR"
        text = asyncio.run(self._run_case("只回复两个字：收到", prefill=marker))
        self.assertTrue(text.strip(), "清空草稿后发送未收到回复")
        self.assertNotIn(
            marker,
            text,
            "新回复里出现了旧草稿标记，说明发送前未清空输入框",
        )


if __name__ == "__main__":
    unittest.main()
