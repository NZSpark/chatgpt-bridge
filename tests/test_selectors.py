"""DOM 选择器：候选顺序、点击回退与自检端点（doc/tasks.md T3.4）。

背景：`SEND_BUTTON_SELECTORS` 曾经四条**全部**落空（真实 DOM 里按钮的
`data-testid`/`aria-label` 都已改版），bridge 只能靠键盘 Enter 兜住——属于
“静默降级”。这里锁定：

* `chat_io._click_send_button` 按候选顺序返回**第一个命中**的按钮；
* `NEW_CHAT_SELECTOR` 把稳定的 `data-testid` 放在最前（命中时优先，缺位也不阻塞）；
* `/_debug/selectors` 能在打开 `CHATGPT_DEBUG` 时报告每条选择器的命中数。

2026-10-06 线上回归：`_open_new_chat` 旧实现用 `wait_for_selector`（只认
「第一个匹配且可见」），现网侧边栏会先匹配到当前会话项（aria-current=page）
或折叠态零尺寸节点 → 整轮超时。下列测试锁定新行为：候选排序、JS click
兜底、侧边栏晚渲染时继续轮询、以及 Think pill 类名失效时按文本扫描。
"""

import asyncio
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chatgpt_web import completion, config
from chatgpt_web.driver import ChatGPTWebDriver
from chatgpt_web.server import app


class _FakeButton:
    def __init__(self, name: str) -> None:
        self.name = name
        self.clicks = 0

    async def dispatch_event(self, event: str) -> None:
        self.clicks += 1


class _FakePage:
    """只对给定选择器返回按钮，并记录查询顺序。"""

    def __init__(self, mapping: dict) -> None:
        self.mapping = mapping
        self.queried = []

    async def query_selector(self, selector: str):
        self.queried.append(selector)
        return self.mapping.get(selector)


class SendButtonFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")

    def test_returns_first_matching_candidate(self) -> None:
        selectors = config.SEND_BUTTON_SELECTORS
        self.assertGreaterEqual(len(selectors), 2, "需要至少两个候选才能验证顺序")
        first, second = selectors[0], selectors[1]
        page = _FakePage({second: _FakeButton("second")})
        clicked = asyncio.run(self.driver._click_send_button(page))
        self.assertTrue(clicked)
        # 第一个候选被查询且未命中；命中第二个后不再往下试
        self.assertEqual(page.queried, [first, second])

    def test_clicks_the_matched_button(self) -> None:
        selector = config.SEND_BUTTON_SELECTORS[0]
        button = _FakeButton("first")
        page = _FakePage({selector: button})
        self.assertTrue(asyncio.run(self.driver._click_send_button(page)))
        self.assertEqual(button.clicks, 1)

    def test_returns_false_when_nothing_matches(self) -> None:
        page = _FakePage({})
        self.assertFalse(asyncio.run(self.driver._click_send_button(page)))
        self.assertEqual(page.queried, list(config.SEND_BUTTON_SELECTORS))


class SelectorConfigTests(unittest.TestCase):
    def test_new_chat_prefers_stable_testid(self) -> None:
        first = config.NEW_CHAT_SELECTOR.split("||")[0].strip()
        self.assertEqual(first, '[data-testid="create-new-chat-button"]')

    def test_send_button_selectors_are_not_empty(self) -> None:
        self.assertTrue(config.SEND_BUTTON_SELECTORS)
        for selector in config.SEND_BUTTON_SELECTORS:
            self.assertNotIn("||", selector)


class DebugSelectorsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_disabled_without_debug(self) -> None:
        with mock.patch.object(config, "DEBUG", False):
            res = self.client.get("/_debug/selectors")
        self.assertEqual(res.status_code, 404)

    def test_reports_hit_counts_when_debug(self) -> None:
        async def fake_query(selector):
            return [object()] if "prompt-textarea" in selector else []

        with mock.patch.object(config, "DEBUG", True), \
                mock.patch("chatgpt_web.server.driver") as driver_mock:
            driver_mock.page = mock.MagicMock()
            driver_mock.page.query_selector_all = fake_query
            res = self.client.get("/_debug/selectors")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("healthy", body)
        self.assertIn("SEND_BUTTON_SELECTORS", body["selectors"])
        matches = [
            entry.get("matches", 0) for entry in body["selectors"]["INPUT_SELECTORS"]
        ]
        self.assertIn(1, matches, body)


class StopTokenPatternTests(unittest.TestCase):
    """2026-10-06 线上回归：/stop/i 把侧边栏标题误判成「停止生成」控件。

    现网侧边栏会话标题的 class 带 ``stopAtEnd-<hash>``（截断样式），在视口下半部
    可见，于是 generating 恒为 True → 结束判定永远等不到「页面落定」，
    每轮都空转到总超时。修复口径：类名里必须是**独立的 stop 词**。
    """

    def setUp(self) -> None:
        self.stop_re = re.compile(
            completion.CompletionMixin._STOP_TOKEN_PATTERN, re.IGNORECASE
        )

    def test_real_sidebar_class_is_not_generating(self) -> None:
        real = "viewport-CyJYLD animateOnGroupHover-vlUIif stopAtEnd-GNPp1Y select-none"
        self.assertIsNone(self.stop_re.search(real), real)

    def test_other_non_stop_classes(self) -> None:
        for cls in ("stopwatch", "backstop", "unstoppable", "nonstop-scroll"):
            self.assertIsNone(self.stop_re.search(cls), cls)

    def test_real_stop_controls_still_match(self) -> None:
        for cls in ("stop-button", "stop", "__stop", "btn-stop"):
            self.assertIsNotNone(self.stop_re.search(cls), cls)

    def test_both_js_snippets_embed_the_pattern(self) -> None:
        for js in (
            completion.CompletionMixin._GENERATING_JS,
            completion.CompletionMixin._STOP_CANDIDATES_JS,
        ):
            self.assertNotIn("STOP_TOKEN_PATTERN", js, "占位符未被替换，JS 将报语法错")
            self.assertIn(
                completion.CompletionMixin._STOP_TOKEN_PATTERN, js,
                "JS 必须使用收紧后的 stop 口径",
            )


class _FakeCodeTag:
    """新版代码块正文：[data-language] 所在的 CodeMirror "cm-content"。"""

    def __init__(self, lang: str, code: str) -> None:
        self.lang = lang
        self.code = code

    async def evaluate(self, script: str):
        # _ANIMATED_JS 探测：新版代码块没有逐 token 动画类
        return False

    async def inner_text(self) -> str:
        return self.code

    async def text_content(self) -> str:
        return self.code

    async def get_attribute(self, name: str):
        if name == "data-language":
            return self.lang
        if name == "class":
            return "cm-content"
        return None


class _FakeCodeBlock:
    """新版代码块容器 div.CodeBlock-<hash>。"""

    def __init__(self, lang: str, code: str) -> None:
        self.tag = _FakeCodeTag(lang, code)

    async def query_selector(self, selector: str):
        return self.tag if "[data-language]" in selector else None


class _FakeReplyWithCode:
    def __init__(self, blocks) -> None:
        self.blocks = list(blocks)

    async def query_selector_all(self, selector: str):
        return list(self.blocks)


class CodeBlockExtractionTests(unittest.TestCase):
    """2026-10-06 回归：新版代码块没有 pre/code，语言写在 data-language。"""

    def setUp(self) -> None:
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")

    def test_reads_new_codeblock_language_and_body(self) -> None:
        reply = _FakeReplyWithCode([_FakeCodeBlock("python", "print('pong')")])
        blocks = asyncio.run(self.driver._extract_code_blocks(reply))
        self.assertEqual(
            blocks, [{"lang": "python", "code": "print('pong')"}], blocks
        )

    def test_config_covers_both_dom_variants(self) -> None:
        self.assertIn("CodeBlock", config.CODE_BLOCK_SELECTOR)
        self.assertIn("pre", config.CODE_BLOCK_SELECTOR)
        self.assertIn("data-language", config.CODE_TAG_SELECTOR)
        self.assertIn("code", config.CODE_TAG_SELECTOR)

    def test_composer_selector_excludes_code_editor(self) -> None:
        # 新版代码块正文也是 contenteditable + role=textbox（CodeMirror），
        # 必须把带 data-language 的那个排除掉，否则 prompt 会被写进回复正文。
        joined = " ".join(config.INPUT_SELECTORS)
        self.assertIn(":not([data-language])", joined)


class _FakeStopPage:
    def __init__(self, handles) -> None:
        self.handles = list(handles)

    async def query_selector_all(self, selector: str):
        return list(self.handles)


class _FakeReplyNode:
    def __init__(self, text: str) -> None:
        self.text = text

    async def evaluate(self, script: str):
        return self.text

    async def inner_text(self) -> str:
        return self.text

    async def text_content(self) -> str:
        return self.text


class _FakeReplyPage:
    def __init__(self, nodes) -> None:
        self.nodes = list(nodes)

    async def query_selector_all(self, selector: str):
        return list(self.nodes)


class _FakeEmptyNodesPage:
    """所有回复选择器都落空、但仍能报告页面文本长度的页面替身。"""

    def __init__(self, page_text: str = "ChatGPT said:\n\nhi") -> None:
        self.page_text = page_text
        self.queried = []

    async def query_selector_all(self, selector: str):
        self.queried.append(selector)
        return []

    async def evaluate(self, script: str):
        return len(self.page_text)


class DOMAdapterFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        self.adapter = self.driver.dom

    def test_extract_latest_reply_returns_last_non_empty_node(self) -> None:
        page = _FakeReplyPage([
            _FakeReplyNode("old answer"),
            _FakeReplyNode(""),
            _FakeReplyNode("latest answer"),
        ])
        reply = asyncio.run(self.adapter.extract_latest_reply(page))
        self.assertEqual(reply, "latest answer")

    def test_find_stop_button_skips_hidden_and_unrelated_controls(self) -> None:
        hidden = _FakeHandle("hidden", visible=False, text="Stop")
        unrelated = _FakeHandle("other", visible=True, text="Submit")
        stop = _FakeHandle("stop", visible=True, text="Stop generating")
        page = _FakeStopPage([hidden, unrelated, stop])
        found = asyncio.run(self.adapter.find_stop_button(page))
        self.assertIs(found, stop)


class EmptyReplyNodeDiagnosticsTests(unittest.TestCase):
    """2026-10-06 回归：选择器全部失效时必须打印逐条命中数。

    改版后轮询表现为 nodes=0 一路空转到超时，日志里没有任何线索。
    诊断输出把「网页版改版」与「消息根本没发出去」区分开。
    """

    def setUp(self) -> None:
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")

    def test_logs_every_selector_hit_count(self) -> None:
        page = _FakeEmptyNodesPage()
        with self.assertLogs("chatgpt_web.chat_io", level="INFO") as captured:
            asyncio.run(self.driver._log_empty_reply_nodes(page))
        text = "\n".join(captured.output)
        self.assertIn("[诊断]", text)
        for selector in config.RESPONSE_SELECTORS.split(","):
            selector = selector.strip()
            if selector:
                self.assertIn(f"{selector}: 0", text)
        self.assertIn("可见文本长度", text)
        self.assertEqual(
            page.queried,
            [s.strip() for s in config.RESPONSE_SELECTORS.split(",") if s.strip()],
        )

    def test_broken_selector_does_not_raise(self) -> None:
        class _BrokenPage(_FakeEmptyNodesPage):
            async def query_selector_all(self, selector: str):
                raise RuntimeError("malformed selector")

        with self.assertLogs("chatgpt_web.chat_io", level="INFO") as captured:
            asyncio.run(self.driver._log_empty_reply_nodes(_BrokenPage()))
        text = "\n".join(captured.output)
        self.assertIn("[诊断]", text)
        self.assertIn("malformed selector", text)


class _FakeHandle:
    """最小 ElementHandle 替身：可见性 / aria-current / 点击行为可编排。"""

    def __init__(
        self,
        name: str,
        *,
        visible: bool = True,
        current: bool = False,
        click_fails: bool = False,
        js_click_fails: bool = False,
        text: str = "",
        pressed: bool = False,
    ) -> None:
        self.name = name
        self.visible = visible
        self.current = current
        self.click_fails = click_fails
        self.js_click_fails = js_click_fails
        self.text = text
        self.pressed = pressed
        self.clicks = 0
        self.js_clicks = 0

    async def is_visible(self) -> bool:
        return self.visible

    async def get_attribute(self, name: str):
        if name == "aria-current":
            return "page" if self.current else None
        if name == "aria-pressed":
            return "true" if self.pressed else "false"
        return None

    async def click(self, timeout=None) -> None:
        if self.click_fails:
            raise TimeoutError("element is not visible")
        self.clicks += 1
        self.pressed = True

    async def evaluate(self, script: str) -> None:
        if self.js_click_fails:
            raise RuntimeError("js click failed")
        self.js_clicks += 1
        self.pressed = True

    async def inner_text(self) -> str:
        return self.text


class _FakeNewChatPage:
    """按轮次返回 query_selector_all 结果（模拟侧边栏晚渲染）。"""

    def __init__(self, *rounds: dict) -> None:
        self._rounds = list(rounds) or [{}]
        per_round = len([s for s in config.NEW_CHAT_SELECTOR.split("||") if s.strip()])
        self._per_round = max(1, per_round)
        self.calls = 0

    async def query_selector_all(self, selector: str):
        index = min(self.calls // self._per_round, len(self._rounds) - 1)
        self.calls += 1
        return list(self._rounds[index].get(selector, []))


class OpenNewChatTests(unittest.TestCase):
    """2026-10-06 回归：多候选排序 + JS click 兜底 + 总预算轮询。"""

    def setUp(self) -> None:
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        self.selector = config.NEW_CHAT_SELECTOR.split("||")[0].strip()

    def _run_open(self, page) -> None:
        with mock.patch.object(completion, "_NEW_CHAT_SEARCH_TIMEOUT_S", 2.0), \
                mock.patch.object(completion, "_NEW_CHAT_POLL_INTERVAL_S", 0.01):
            asyncio.run(self.driver._open_new_chat(page))

    def test_prefers_visible_non_current_candidate(self) -> None:
        hidden = _FakeHandle("hidden", visible=False)
        current = _FakeHandle("current", visible=True, current=True)
        wanted = _FakeHandle("wanted", visible=True)
        page = _FakeNewChatPage({self.selector: [hidden, current, wanted]})
        self._run_open(page)
        self.assertEqual(wanted.clicks, 1, "应点击「可见且非当前会话」的候选")
        self.assertEqual(hidden.clicks + current.clicks, 0)

    def test_falls_back_to_js_click(self) -> None:
        button = _FakeHandle("covered", visible=True, click_fails=True)
        page = _FakeNewChatPage({self.selector: [button]})
        self._run_open(page)
        self.assertEqual(button.js_clicks, 1, "真点击被拦截时应退回 JS click")

    def test_waits_for_late_sidebar(self) -> None:
        button = _FakeHandle("late", visible=True)
        page = _FakeNewChatPage({}, {self.selector: [button]})
        self._run_open(page)
        self.assertEqual(button.clicks, 1, "侧边栏晚渲染时应在预算内继续轮询")

    def test_current_item_is_last_resort(self) -> None:
        current = _FakeHandle("current", visible=True, current=True)
        page = _FakeNewChatPage({self.selector: [current]})
        self._run_open(page)
        self.assertEqual(current.clicks, 1, "只剩当前会话项时仍应尝试（总比沿用旧页好）")

    def test_gives_up_quietly_when_nothing_matches(self) -> None:
        page = _FakeNewChatPage({})
        with mock.patch.object(completion, "_NEW_CHAT_SEARCH_TIMEOUT_S", 0.0):
            asyncio.run(self.driver._open_new_chat(page))  # 不应抛异常
        self.assertEqual(page.calls, len(config.NEW_CHAT_SELECTOR.split("||")))


class _FakeJSHandle:
    def __init__(self, element) -> None:
        self._element = element

    def as_element(self):
        return self._element


class _FakeThinkPage:
    """配置选择器全部落空，只响应文本扫描兜底。"""

    def __init__(self, element) -> None:
        self.element = element
        self.evaluated = 0

    async def query_selector_all(self, selector: str):
        return []

    async def evaluate_handle(self, script: str, want):
        self.evaluated += 1
        return _FakeJSHandle(self.element)


class ThinkModeFallbackTests(unittest.TestCase):
    """2026-10-06 回归：类名（__composer-pill）失效时按文本扫描兜底。"""

    def setUp(self) -> None:
        self.driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-noprofile")
        self.want = [t.strip().lower() for t in config.THINK_MODE_TEXTS.split("||") if t.strip()]

    def test_text_scan_selects_unseen_pill(self) -> None:
        pill = _FakeHandle("pill", text="Think", pressed=False)
        page = _FakeThinkPage(pill)
        selected = asyncio.run(self.driver._try_select_think_once(page, self.want))
        self.assertTrue(selected, "类名失效时应由文本扫描兜底选中 Think pill")
        self.assertEqual(pill.clicks, 1)
        self.assertEqual(page.evaluated, 1)

    def test_text_scan_ignores_unrelated_buttons(self) -> None:
        other = _FakeHandle("other", text="Deep research", pressed=False)
        page = _FakeThinkPage(other)
        selected = asyncio.run(self.driver._try_select_think_once(page, self.want))
        self.assertFalse(selected)
        self.assertEqual(other.clicks, 0)

    def test_configured_selector_still_wins_without_scan(self) -> None:
        pill = _FakeHandle("pill", text="Think", pressed=False)
        page = _FakeThinkPage(None)

        async def fake_query(selector: str):
            return [pill] if "composer-pill" in selector else []

        page.query_selector_all = fake_query  # type: ignore[method-assign]
        selected = asyncio.run(self.driver._try_select_think_once(page, self.want))
        self.assertTrue(selected)
        self.assertEqual(pill.clicks, 1)
        self.assertEqual(page.evaluated, 0, "配置选择器命中时不应触发兜底扫描")


if __name__ == "__main__":
    unittest.main(verbosity=2)
