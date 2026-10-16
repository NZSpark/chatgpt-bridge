"""PI-006 DOM 改版黄金回归（不联网）。

目的：把「网页版改版导致选择器失效」从「真机跑一轮、等到总超时」变成
「本地夹具跑一轮、失败层立刻指名」。这里用真实 Playwright 打开
``tests/e2e/fixtures/chatgpt_dom.html``——一份复刻 2026-10 校准结果的静态 DOM——
再让**真实的** :class:`~chatgpt_web.browser.dom_adapter.ChatGPTDOMAdapter` 跑一遍
六层探测：

1. New Chat          ``find_new_chat`` / ``open_new_chat``
2. Composer          ``find_input`` / ``input_candidates``
3. Think Mode        ``set_think_mode`` / ``read_think_mode``
4. Assistant reply   ``extract_latest_reply``
5. generation / stop ``is_generating`` / ``find_stop_button``
6. code block        ``extract_code_blocks``

与真机探测的区别：本模块**默认运行**（不需要 ``CHATGPT_E2E=1``），因为不访问网络、
不受 Cloudflare 与登录态影响；``tests/e2e/test_dom_probe.py`` 仍然负责真机校准。

运行：

    .venv/bin/python -m pytest tests/e2e/test_dom_golden.py -v
"""

import asyncio
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from chatgpt_web import config
from chatgpt_web.browser.dom_adapter import ChatGPTDOMAdapter

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "chatgpt_dom.html"


class DomGoldenRegression(unittest.TestCase):
    """用真实 adapter + 真实 Playwright + 静态夹具做六层黄金回归。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.exists():
            raise unittest.SkipTest(f"缺少夹具 {FIXTURE}")
        cls._pw = None
        cls._browser = None
        cls._loop = asyncio.new_event_loop()
        try:
            cls._pw = cls._loop.run_until_complete(async_playwright().start())
            cls._browser = cls._loop.run_until_complete(cls._pw.chromium.launch())
        except Exception as exc:  # noqa: BLE001
            cls._teardown_pw()
            raise unittest.SkipTest(f"Playwright chromium 不可用：{exc!r}")

    @classmethod
    def _teardown_pw(cls) -> None:
        if cls._browser is not None:
            try:
                cls._loop.run_until_complete(cls._browser.close())
            except Exception:  # noqa: BLE001
                pass
            cls._browser = None
        if cls._pw is not None:
            try:
                cls._loop.run_until_complete(cls._pw.stop())
            except Exception:  # noqa: BLE001
                pass
            cls._pw = None

    @classmethod
    def tearDownClass(cls) -> None:
        cls._teardown_pw()
        try:
            cls._loop.close()
        except Exception:  # noqa: BLE001
            pass

    def setUp(self) -> None:
        self.adapter = ChatGPTDOMAdapter()
        self.page = self._loop.run_until_complete(self._browser.new_page())
        self._loop.run_until_complete(self.page.goto(FIXTURE.as_uri()))

    def tearDown(self) -> None:
        try:
            self._loop.run_until_complete(self.page.close())
        except Exception:  # noqa: BLE001
            pass

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    # ---- 逐层回归：每层失败都指名字，不依赖总超时 ----

    def test_layer_1_new_chat(self) -> None:
        """层 1：New Chat 入口可定位、可点击。"""
        found = self._run(self.adapter.find_new_chat(self.page))
        self.assertIsNotNone(
            found,
            "New Chat 层失效：NEW_CHAT_SELECTOR 在夹具上一个都没命中。"
            "真机含义：侧边栏新建对话入口改版。",
        )
        _handle, rank = found
        self.assertLessEqual(rank, 1, f"New Chat 候选 rank 异常：{rank}")
        self.assertTrue(
            self._run(self.adapter.open_new_chat(self.page)),
            "New Chat 层失效：候选存在但点击全部失败。",
        )

    def test_layer_2_composer(self) -> None:
        """层 2：composer 可定位且是首选 ProseMirror。"""
        candidates = self._run(self.adapter.input_candidates(self.page))
        self.assertTrue(
            candidates,
            "Composer 层失效：INPUT_SELECTORS 在夹具上零命中。"
            "真机含义：输入框改版（历史 bug：composer 已无 id）。",
        )
        handle = self._run(self.adapter.find_input(self.page))
        self.assertIsNotNone(handle, "Composer 层失效：find_input 未返回首选候选。")
        role = self._run(handle.get_attribute("role"))
        self.assertEqual(role, "textbox", "Composer 层失效：首选候选不是 role=textbox。")
        self.assertEqual(
            self.adapter.input_probe_hint(),
            "",
            "Composer 层：成功定位后 last_input_probe 应为空。",
        )

    def test_layer_3_think_mode(self) -> None:
        """层 3：Think 模式可选中、可读回。"""
        want = [t for t in config.THINK_MODE_TEXTS.split("||") if t]
        state = self._run(self.adapter.read_think_mode(self.page, want))
        self.assertEqual(
            state,
            False,
            "Think Mode 层失效：THINK_MODE_SELECTOR 未命中 pill（read_think_mode=None）。"
            "真机含义：composer pill 改版。",
        )
        self.assertTrue(
            self._run(self.adapter.set_think_mode(self.page, want, on=True)),
            "Think Mode 层失效：点击后未能验证 aria-pressed=true。",
        )
        self.assertEqual(
            self._run(self.adapter.read_think_mode(self.page, want)),
            True,
            "Think Mode 层：set 之后 read 未回到 true。",
        )

    def test_layer_4_assistant_reply(self) -> None:
        """层 4：助手回复文本可提取。"""
        nodes = self._run(self.adapter.find_assistant_messages(self.page))
        self.assertTrue(
            nodes,
            "Assistant reply 层失效：RESPONSE_SELECTORS 零命中。"
            "真机含义：回复容器改版（已从 data-message-author-role 迁到 markdown 容器）。",
        )
        text = self._run(self.adapter.extract_latest_reply(self.page))
        self.assertIn(
            "Hello from the fixture",
            text,
            f"Assistant reply 层失效：提取到的文本不含预期正文，得到 {text!r}。",
        )

    def test_layer_5_generation_and_stop(self) -> None:
        """层 5：生成中判定 + 停止控件可定位。"""
        self._run(
            self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        )
        self._run(self.page.wait_for_timeout(100))
        stop = self._run(self.adapter.find_stop_button(self.page))
        self.assertIsNotNone(
            stop,
            "generation/stop 层失效：STOP_CONTROL_SELECTOR 未命中停止控件。",
        )
        generating = self._run(self.adapter.is_generating(self.page))
        self.assertTrue(
            generating,
            "generation/stop 层失效：可见停止控件存在但 GENERATING_JS 判 false"
            "（检查 rect.top > innerHeight/2 的位置约束）。",
        )

    def test_layer_6_code_blocks(self) -> None:
        """层 6：代码块语言与正文可提取，且不混入 Copy/Run 头。"""
        nodes = self._run(self.adapter.find_assistant_messages(self.page))
        self.assertTrue(nodes, "code block 层前置失败：没有回复节点。")
        blocks = self._run(self.adapter.extract_code_blocks(nodes[-1]))
        self.assertTrue(
            blocks,
            "code block 层失效：CODE_BLOCK_SELECTOR 零命中。"
            "真机含义：代码块容器改版（新版无 pre/code）。",
        )
        block = blocks[0]
        self.assertEqual(
            block["lang"],
            "python",
            f"code block 层失效：语言未从 [data-language] 读出，得到 {block['lang']!r}。",
        )
        self.assertIn(
            'print("hi")',
            block["code"],
            f"code block 层失效：正文不含预期代码，得到 {block['code']!r}。",
        )
        self.assertNotIn(
            "Copy",
            block["code"],
            "code block 层失效：Copy/Run 头部混进了代码正文。",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
