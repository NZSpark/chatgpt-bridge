"""`_send_chat_locked` 结束判定的回归测试（用假 page 驱动，不开浏览器）。

覆盖真实踩过的坑：
  * 选择器命中的末节点文本与发送前相同（ChatGPT 原地复用/替换节点），
    但页面确实在生成：旧代码只看「末节点文本 != before_text」会一直
    判不到「回复已出现」，空转到 180s 超时（poll 刷屏 + HTTP 000 time 180）。
    只有把「已观测到生成中」也算作已出现，才能正确收尾。
  * 结束判定：观测到生成中、随后停止按钮消失即结束。
  * 永远判不到结束（也无生成中信号）时，必须超时报错而非无限空转。

运行：.venv/bin/python -m pytest tests/ -q
"""

import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import config  # noqa: E402
from chatgpt_web.driver import ChatGPTTimeoutError, ChatGPTWebDriver  # noqa: E402


class FakeInput:
    def __init__(self):
        self.text = ""

    async def fill(self, text, **kwargs):
        # 真 Playwright 的 ElementHandle.fill(value, timeout=...) 接受 timeout；
        # 这里同样吞掉任意关键字参数，避免生产代码传 timeout= 时触发 TypeError。
        self.text = text
        return None

    async def click(self, **kwargs):
        # _call_fill 会先 click 聚焦；假件直接视为成功。
        return None

    async def evaluate(self, script):
        # _dispatch_enter（兜底合成事件）的脚本派发 Enter；
        # _complete_text 的脚本返回当前文本。
        if "dispatchEvent" in script or "Enter" in script:
            return True
        # _call_fill 的兜底聚焦脚本含 focus()
        if "focus()" in script:
            return None
        return self.text


class FakeKeyboard:
    def __init__(self):
        # 指向 FakePage 最近一次定位到的输入框，insert_text 写回它的 text。
        self.current_input = None
        # 记录是否用真实键盘 Enter 提交过（_submit_prompt 的首选路径）。
        self.submitted = False

    async def press(self, key):
        # Control+A / Meta+A / Backspace 清空草稿：这里把内容清掉以模拟。
        if key in ("Backspace",) and self.current_input is not None:
            self.current_input.text = ""
        # 真实键盘 Enter 是提交首选路径（见 chat_io._keyboard_enter）。
        if key == "Enter":
            self.submitted = True
        return None

    async def insert_text(self, text):
        # _call_fill 用 keyboard.insert_text 向 ProseMirror composer 输入。
        if self.current_input is not None:
            self.current_input.text = (self.current_input.text or "") + text
        return None


class FakeNode:
    """假回复节点。

    ``pending`` 模拟 ChatGPT 逐 token 显现动画：尚未显现的 token 带 .pending，
    inner_text 取不到它们（只有显现后的前缀），但 text_content 能拿到全文——
    正是生产环境踩到的截断根因。
    """

    def __init__(self, text, pending=False):
        self._text = text
        self._pending = pending

    async def inner_text(self):
        # 模拟：有 pending token 时只能读到已显现的前缀。
        if self._pending:
            return self._text[: max(1, len(self._text) // 2)]
        return self._text

    async def text_content(self):
        return self._text

    async def evaluate(self, script):
        # _has_pending_tokens 的探测脚本形如 "(n) => !!n.querySelector('.pending...')"
        if "querySelector('.pending" in script:
            return self._pending
        # _complete_text 的 JS 返回完整文本
        return self._text

    async def query_selector_all(self, selector):
        return []

    async def query_selector(self, selector):
        return None

    async def get_attribute(self, name):
        return None


class FakePage:
    """query_selector_all 第一次调用是发送前的 baseline，之后按脚本返回。"""

    def __init__(self, baseline, script, generating=(False,)):
        self.baseline = list(baseline)
        self.script = list(script)
        self.generating = list(generating)
        self.query_calls = 0
        self.eval_calls = 0
        self.url = "https://chatgpt.com"
        self.keyboard = FakeKeyboard()

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        # 每次定位返回同一个输入框句柄，并把 keyboard 指向它，
        # 这样 _call_fill 里的 keyboard.insert_text 能写回 _complete_text 读到的对象。
        if not hasattr(self, "_input"):
            self._input = FakeInput()
        self.keyboard.current_input = self._input
        return self._input

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        if index == 0:
            out = []
            for item in self.baseline:
                out.append(FakeNode(item[0], pending=item[1]) if isinstance(item, tuple) else FakeNode(item))
            return out
        texts = self.script[min(index - 1, len(self.script) - 1)]
        nodes = []
        for item in texts:
            if isinstance(item, tuple):
                nodes.append(FakeNode(item[0], pending=item[1]))
            else:
                nodes.append(FakeNode(item))
        return nodes

    async def evaluate(self, script):
        # 生成中探测脚本返回 bool；上下文到顶探测脚本返回页面文本（这里恒为空串）。
        if "stop" in script or "\u505c\u6b62" in script:
            index = self.eval_calls
            self.eval_calls += 1
            return self.generating[min(index, len(self.generating) - 1)]
        return ""


class EndDetectionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_session = Path(self.id().replace(".", "_") + ".session")
        patch = unittest.mock.patch.object(config, "SESSION_FILE", self._tmp_session)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(lambda: self._tmp_session.exists() and self._tmp_session.unlink())

        for name, value in (
            ("POLL_INTERVAL_S", 0),
            ("RESPONSE_TIMEOUT_S", 5.0),
            ("PARALLEL_BUCKETS", False),
            ("BUCKET_LOCK_TIMEOUT_S", 0),
        ):
            p = unittest.mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def driver_for(page):
        driver = ChatGPTWebDriver()
        driver.page = page
        return driver

    def run_chat(self, driver, prompt="go", on_delta=None):
        return asyncio.run(driver.send_chat(prompt, on_delta))


class ReplySeenViaGeneratingTests(EndDetectionTestCase):
    """真实故障回归：末节点文本始终等于 before_text，靠「生成中」判定本轮已出现。"""

    def test_generating_proves_reply_seen_and_finishes(self):
        # 末节点文本始终为「旧答案」（== before_text），节点数也不变。
        # 旧代码：reply_seen 永远 False → 空转到超时。
        # 新代码：观测到 generating=True 后 reply_seen=True，
        #         随后 generating 变 False 即判定结束并返回内容。
        page = FakePage(
            baseline=["旧答案"],
            script=[["旧答案"], ["旧答案"], ["旧答案"], ["旧答案"]],
            generating=[True, True, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "旧答案")


class GeneratingStateTests(EndDetectionTestCase):
    def test_stop_button_disappearing_ends_immediately(self):
        page = FakePage(
            baseline=["旧"],
            script=[["答案1"], ["答案2"], ["答案3"], ["答案4"]],
            generating=[True, True, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "答案3")

    def test_replaced_content_detected_without_node_growth(self):
        page = FakePage(
            baseline=["旧答案", "旧答案"],
            script=[["旧答案", "新答案一段"], ["旧答案", "新答案一段"]],
            generating=[False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "新答案一段")

    def test_no_signal_times_out_instead_of_spinning(self):
        # 文本永远等于 before_text、也无生成中信号：必须超时，而不是无限空转。
        page = FakePage(
            baseline=["旧答案"],
            script=[["旧答案"]],
            generating=[False],
        )
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "RESPONSE_TIMEOUT_S", 0.05):
            with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
                with self.assertRaises(ChatGPTTimeoutError):
                    self.run_chat(driver)



class PendingTokenTests(EndDetectionTestCase):
    """真实故障回归：ChatGPT 逐 token 显现动画导致回复被截断。

    现象：停止按钮一消失就收尾，但此时后面还有 .pending token 未显现，
    inner_text 只读到半截 JSON（如 TOOL_CALL 的 arguments 被切掉）。
    修复：1) 读文本走 _complete_text（克隆节点去动画类后取全文）；
         2) 停止按钮消失时若仍有 pending token 则继续等待。
    """

    def test_pending_tokens_delay_finish_until_revealed(self):
        # poll1/2: 仍有 pending，停止按钮已消失也不能收尾；
        # poll3: pending 清除，读到完整文本。
        page = FakePage(
            baseline=["旧"],
            script=[
                [("TOOL_CALL: {\"cmd\":", True)],
                [("TOOL_CALL: {\"cmd\":", True)],
                [("TOOL_CALL: {\"cmd\": \"echo hi\"}", False)],
            ],
            generating=[True, False, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, 'TOOL_CALL: {"cmd": "echo hi"}')

    def test_complete_text_reads_full_text_when_pending(self):
        # 即便节点还有 pending，_complete_text 也应拿到全文（text_content 兜底）。
        driver = self.driver_for(FakePage(baseline=["x"], script=[["x"]]))
        node = FakeNode("a " + chr(34) + "b" + chr(34) + " c", pending=True)
        out = asyncio.run(driver._complete_text(node))
        self.assertEqual(out, "a " + chr(34) + "b" + chr(34) + " c")

if __name__ == "__main__":
    unittest.main()
