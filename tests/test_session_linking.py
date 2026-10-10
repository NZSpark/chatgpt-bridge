"""会话绑定（``/link``）：把会话桶固定到用户给出的网页会话 URL。

背景（用户请求）：ChatGPT 网页版有时会**自行开启一条新的网页会话**，桥这一侧
看到的就是「句柄消失、上下文不在原来的会话里」。原设计只会新开空白会话 + 播种，
这里补上「保持连接」的能力：

* ``/link <URL>`` 把某个会话桶绑定到那条会话（默认只发增量，``--seed`` 则播种）；
* 绑定后，页面漂移 / 句柄失效重建 / 轮转 / 重启都会回到这条会话——句柄丢失不再
  意味着「这一轮被发进一条陌生的新会话」；
* 命令在 HTTP 边缘执行、不碰页面，所以**浏览器没起、句柄已死时它照样有效**。

不开真浏览器：页面替身沿用 tests/test_page_recovery.py 的那一套（两种死法 + 活页面）。
"""

import asyncio
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import config, linking, server  # noqa: E402
from chatgpt_web.driver import DEFAULT_SESSION_KEY, ChatGPTWebDriver  # noqa: E402
from chatgpt_web.errors import ChatGPTPageLostError  # noqa: E402
from chatgpt_web.models import ChatMessage  # noqa: E402
from tests.test_page_recovery import (  # noqa: E402
    _ClosedPage,
    _FakeContext,
    _HealthyPage,
    _quiet_page_setup,
    _ZombiePage,
)

CONVERSATION_ID = "6ac9c061-5074-83ec-82f0-2f46a8c334ca"
CONVERSATION_URL = f"https://chatgpt.com/c/{CONVERSATION_ID}"


def _user(text: str):
    return [ChatMessage(role="user", content=text)]


@contextlib.contextmanager
def _quiet_setup():
    """建页 / 导航里的等待压到零（沿用页面恢复测试的补丁组合）。"""
    with _quiet_page_setup():
        yield


class _RecordingPage(_HealthyPage):
    """记录 ``goto`` 的活页面（默认 URL 是首页，即「不在目标会话上」）。"""

    def __init__(self, url: str = "https://chatgpt.com/") -> None:
        super().__init__()
        self.url = url
        self.gotos = []

    async def goto(self, url: str, wait_until=None) -> None:
        self.gotos.append(url)
        self.url = url


class _LinkTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-link-")
        self._patch = mock.patch.object(
            config, "SESSION_FILE", Path(self.tmp.name) / "state.json"
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmp.cleanup()

    def make_driver(self) -> ChatGPTWebDriver:
        return ChatGPTWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))


# ==================== 链接解析 ====================


class ConversationUrlTests(unittest.TestCase):
    def test_accepts_the_forms_users_actually_paste(self) -> None:
        cases = {
            CONVERSATION_URL: CONVERSATION_URL,
            f"{CONVERSATION_URL}?model=auto": CONVERSATION_URL,
            f"{CONVERSATION_URL}/": CONVERSATION_URL,
            f"chatgpt.com/c/{CONVERSATION_ID}": CONVERSATION_URL,
            f"https://www.chatgpt.com/c/{CONVERSATION_ID}": (
                f"https://www.chatgpt.com/c/{CONVERSATION_ID}"
            ),
            f"https://chatgpt.com/g/g-abc123/c/{CONVERSATION_ID}": CONVERSATION_URL,
            f"<{CONVERSATION_URL}>": CONVERSATION_URL,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(linking.canonical_conversation_url(raw), expected)

    def test_rejects_foreign_host_by_default(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            linking.canonical_conversation_url(f"https://example.com/c/{CONVERSATION_ID}")
        self.assertIn("不支持的站点", str(ctx.exception))
        self.assertIn("LINK_ALLOWED_HOSTS", str(ctx.exception), "错误文案要指出怎么放宽")

    def test_custom_host_is_allowed_when_configured(self) -> None:
        with mock.patch.object(config, "LINK_ALLOWED_HOSTS", "mirror.example.com"):
            self.assertEqual(
                linking.canonical_conversation_url(f"https://mirror.example.com/c/{CONVERSATION_ID}"),
                f"https://mirror.example.com/c/{CONVERSATION_ID}",
            )
            with self.assertRaises(ValueError):
                linking.canonical_conversation_url(CONVERSATION_URL)

    def test_wildcard_allows_any_host(self) -> None:
        with mock.patch.object(config, "LINK_ALLOWED_HOSTS", "*"):
            self.assertTrue(
                linking.canonical_conversation_url(f"https://anywhere.test/c/{CONVERSATION_ID}")
            )

    def test_rejects_non_conversation_links(self) -> None:
        for raw, token in (
            ("https://chatgpt.com/share/abc-def-123", "没有会话 ID"),
            ("https://chatgpt.com/", "没有会话 ID"),
            (f"http://chatgpt.com/c/{CONVERSATION_ID}", "https"),
            ("", "缺少会话链接"),
            ("https://chatgpt.com/c/short", "会话 ID"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    linking.canonical_conversation_url(raw)
                self.assertIn(token, str(ctx.exception))

    def test_same_conversation_ignores_query_and_trailing_slash(self) -> None:
        self.assertTrue(linking.same_conversation(f"{CONVERSATION_URL}?model=auto", CONVERSATION_URL))
        self.assertTrue(linking.same_conversation(f"{CONVERSATION_URL}/", CONVERSATION_URL))
        self.assertFalse(linking.same_conversation("https://chatgpt.com/c/other-conversation", CONVERSATION_URL))
        # 读不到 URL 时一律按「不在目标会话上」处理（宁可多导航一次）
        self.assertFalse(linking.same_conversation("", CONVERSATION_URL))
        self.assertFalse(linking.same_conversation("https://chatgpt.com/", CONVERSATION_URL))


# ==================== 命令识别 ====================


class CommandParsingTests(unittest.TestCase):
    def test_standalone_line_is_a_command(self) -> None:
        command = linking.parse_command(f"/link {CONVERSATION_URL}")
        self.assertEqual(command.action, "link")
        self.assertEqual(command.target, CONVERSATION_URL)
        self.assertFalse(command.seed)

    def test_seed_flag_in_either_position(self) -> None:
        for text in (f"/link {CONVERSATION_URL} --seed", f"/link --seed {CONVERSATION_URL}"):
            with self.subTest(text=text):
                self.assertTrue(linking.parse_command(text).seed)

    def test_bare_link_is_status_and_unlink_clears(self) -> None:
        self.assertEqual(linking.parse_command("/link").action, "status")
        self.assertEqual(linking.parse_command("/unlink").action, "unlink")
        self.assertEqual(linking.parse_command("/LINK").action, "status")

    def test_invalid_shapes_are_reported_not_swallowed(self) -> None:
        self.assertEqual(linking.parse_command("/link --seed").action, "invalid")
        self.assertEqual(linking.parse_command("/unlink extra").action, "invalid")
        self.assertEqual(linking.parse_command(f"/link {CONVERSATION_URL} extra").action, "invalid")

    def test_ordinary_text_is_not_a_command(self) -> None:
        for text in (
            "请解释 /link 命令",
            f"/link {CONVERSATION_URL}\nAND MORE",
            f"看看这个 {CONVERSATION_URL}",
            "links are useful",
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(linking.parse_command(text), "不能把用户正常说的话吃掉")

    def test_only_the_last_user_message_counts(self) -> None:
        messages = [
            ChatMessage(role="user", content=f"/link {CONVERSATION_URL}"),
            ChatMessage(role="assistant", content="[Bridge] 已绑定"),
            ChatMessage(role="tool", content="done", tool_call_id="c1"),
        ]
        self.assertIsNone(linking.latest_user_text(messages), "历史里的旧命令不得重放")
        self.assertEqual(
            linking.latest_user_text(_user(f"/link {CONVERSATION_URL}")), f"/link {CONVERSATION_URL}"
        )


# ==================== 命令执行 ====================


class LinkCommandHandlingTests(_LinkTestBase):
    def test_link_command_binds_bucket_and_replies(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(linking.handle_command(_user(f"/link {CONVERSATION_URL}"), driver))
        self.assertIn("[Bridge]", reply)
        self.assertIn(CONVERSATION_ID, reply)
        self.assertEqual(driver.linked_url(DEFAULT_SESSION_KEY), CONVERSATION_URL)
        # 默认语义：这条会话被视为「已有上下文」→ 之后只发增量
        self.assertTrue(driver._state(DEFAULT_SESSION_KEY).has_history)

    def test_command_touches_no_page(self) -> None:
        """绑定只是改状态：浏览器没起来（没有 page / context）时命令也必须能执行。"""
        driver = self.make_driver()
        driver.context = _FakeContext()
        driver.page = None

        reply = asyncio.run(linking.handle_command(_user(f"/link {CONVERSATION_URL}"), driver))

        self.assertIn("[Bridge]", reply)
        self.assertEqual(driver.context.created, [], "命令阶段不该新开页面")
        self.assertEqual(driver.linked_url(DEFAULT_SESSION_KEY), CONVERSATION_URL)

    def test_seed_variant_asks_for_seeding(self) -> None:
        driver = self.make_driver()
        driver._state(DEFAULT_SESSION_KEY).turns = 5
        reply = asyncio.run(
            linking.handle_command(_user(f"/link {CONVERSATION_URL} --seed"), driver)
        )
        self.assertIn("播种", reply)
        self.assertFalse(
            driver._state(DEFAULT_SESSION_KEY).has_history,
            "--seed = 那条会话里没有此前记录，本轮必须把完整历史播种进去",
        )

    def test_status_and_unlink(self) -> None:
        driver = self.make_driver()
        empty_status = asyncio.run(linking.handle_command(_user("/link"), driver))
        self.assertIn("未绑定", empty_status)

        asyncio.run(linking.handle_command(_user(f"/link {CONVERSATION_URL}"), driver))
        bound_status = asyncio.run(linking.handle_command(_user("/link"), driver))
        self.assertIn(CONVERSATION_URL, bound_status)

        unlink_reply = asyncio.run(linking.handle_command(_user("/unlink"), driver))
        self.assertIn("已解除", unlink_reply)
        state = driver._state(DEFAULT_SESSION_KEY)
        self.assertIsNone(state.linked_url)
        self.assertTrue(state.pending_rotation, "解除绑定后下一轮要回到「新开会话 + 播种」")
        self.assertFalse(state.has_history)

    def test_bad_link_is_reported_without_binding(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(linking.handle_command(_user("/link https://evil.test/c/abcdefgh"), driver))
        self.assertIn("没能绑定", reply)
        self.assertIsNone(driver.linked_url(DEFAULT_SESSION_KEY))

    def test_non_command_message_is_left_to_the_model(self) -> None:
        driver = self.make_driver()
        self.assertIsNone(asyncio.run(linking.handle_command(_user("你好"), driver)))

    def test_binding_survives_state_reload(self) -> None:
        driver = self.make_driver()
        asyncio.run(linking.handle_command(_user(f"/link {CONVERSATION_URL}"), driver))
        # 换一个进程（内存状态全空）→ 从磁盘读回来的绑定必须还在
        restarted = self.make_driver()
        self.assertEqual(restarted.linked_url(DEFAULT_SESSION_KEY), CONVERSATION_URL)
        self.assertEqual(restarted.session_stats(DEFAULT_SESSION_KEY)["linked_url"], CONVERSATION_URL)


# ==================== 页面落到绑定会话上 ====================


class LinkedPageTargetingTests(_LinkTestBase):
    def _linked_driver(self, page) -> ChatGPTWebDriver:
        driver = self.make_driver()
        driver.page = page
        driver.context = _FakeContext()
        driver.link_session(CONVERSATION_URL, key=DEFAULT_SESSION_KEY)
        return driver

    def test_rebuild_after_handle_loss_lands_on_the_linked_conversation(self) -> None:
        """核心场景：句柄失效 → 新开页面 → 直接落到用户给的链接，且不重复播种。"""
        driver = self._linked_driver(_ZombiePage())
        seen = []

        async def first_round_lost(prompt, on_delta=None, key=None):
            seen.append(prompt)
            if len(seen) == 1:
                raise ChatGPTPageLostError("标签已失效（页面已关闭，URL=https://chatgpt.com/）")
            return "recovered", []

        with _quiet_setup(), \
                mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1), \
                mock.patch.object(config, "PAGE_REBUILD_MAX", 1), \
                mock.patch.object(driver, "_send_chat_locked", side_effect=first_round_lost):
            reply, _ = asyncio.run(
                driver.send_chat("delta-only", seeded_prompt="SEEDED-HISTORY", key=None)
            )

        self.assertEqual(reply, "recovered")
        rebuilt = driver.context.created[0]
        self.assertEqual(rebuilt.url, CONVERSATION_URL, "重建的页面必须落到绑定的会话上")
        self.assertEqual(
            seen, ["delta-only", "delta-only"],
            "绑定的会话里本来就有上下文 → 重发用增量，不做无谓的整段播种",
        )

    def test_send_realigns_a_drifted_page(self) -> None:
        """ChatGPT 自己跳到别的会话（页面漂移）→ 下一轮发送把它拉回来。"""
        page = _RecordingPage(url="https://chatgpt.com/c/some-other-conversation")
        driver = self._linked_driver(page)
        seen = []

        async def fake_locked(prompt, on_delta=None, key=None):
            seen.append((prompt, driver.page.url))
            return "ok", []

        with _quiet_setup(), mock.patch.object(driver, "_send_chat_locked", side_effect=fake_locked):
            asyncio.run(driver.send_chat("delta-only", key=None))

        self.assertEqual(seen, [("delta-only", CONVERSATION_URL)])
        self.assertIn(CONVERSATION_URL, page.gotos)

    def test_no_navigation_when_already_on_the_conversation(self) -> None:
        page = _RecordingPage(url=f"{CONVERSATION_URL}?model=auto")
        driver = self._linked_driver(page)

        async def fake_locked(prompt, on_delta=None, key=None):
            return "ok", []

        with _quiet_setup(), mock.patch.object(driver, "_send_chat_locked", side_effect=fake_locked):
            asyncio.run(driver.send_chat("delta-only", key=None))

        self.assertEqual(page.gotos, [], "已经在目标会话上就不该再导航（省一次跳转）")

    def test_unbound_bucket_keeps_the_old_behaviour(self) -> None:
        page = _RecordingPage()
        driver = self.make_driver()
        driver.page = page
        driver.context = _FakeContext()

        with _quiet_setup():
            self.assertFalse(asyncio.run(driver._ensure_linked_target(DEFAULT_SESSION_KEY)))
        self.assertEqual(page.gotos, [], "没有绑定就不该有任何导航")

    def test_new_page_for_unbound_bucket_still_opens_a_blank_chat(self) -> None:
        driver = self.make_driver()
        driver.context = _FakeContext()
        with _quiet_setup():
            page = asyncio.run(driver._open_bucket_page("b"))
        self.assertIn("chatgpt.com", page.url)
        self.assertNotIn("/c/", page.url, "未绑定的桶照旧新开空白对话（靠播种续上下文）")
        self.assertFalse(driver._state("b").has_history)

    def test_rotation_returns_to_the_linked_conversation(self) -> None:
        """轮转对绑定的桶不成立：它会把用户指定的那条会话丢掉，改为回到该会话。"""
        page = _RecordingPage()
        driver = self._linked_driver(page)
        driver._state(DEFAULT_SESSION_KEY).turns = 99
        driver._state(DEFAULT_SESSION_KEY).pending_rotation = True

        with _quiet_setup():
            asyncio.run(driver._start_new_session(DEFAULT_SESSION_KEY))

        self.assertEqual(page.url, CONVERSATION_URL)
        state = driver._state(DEFAULT_SESSION_KEY)
        self.assertFalse(state.pending_rotation)
        self.assertEqual(state.turns, 0)

    def test_startup_restores_the_bound_conversation(self) -> None:
        driver = self.make_driver()
        driver.link_session(CONVERSATION_URL, key=DEFAULT_SESSION_KEY)

        restarted = self.make_driver()
        restarted.page = _RecordingPage()
        with _quiet_setup():
            asyncio.run(restarted._restore_session_on_startup())

        self.assertEqual(restarted.page.url, CONVERSATION_URL)
        self.assertTrue(
            restarted.session_has_history,
            "恢复后这条会话里已有上下文 → 不要重播整段历史",
        )

    def test_closed_page_is_rebuilt_onto_the_linked_conversation(self) -> None:
        driver = self._linked_driver(_ClosedPage())
        with _quiet_setup():
            asyncio.run(driver._ensure_page(DEFAULT_SESSION_KEY))
        self.assertEqual(driver.context.created[0].url, CONVERSATION_URL)


# ==================== HTTP 端点（/link、/unlink 的 HTTP 版）====================


class LinkEndpointTests(_LinkTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.driver = server.driver
        self._saved = dict(self.driver._sessions)
        self.driver._sessions.clear()

    def tearDown(self) -> None:
        self.driver._sessions.clear()
        self.driver._sessions.update(self._saved)
        super().tearDown()

    def test_link_endpoint_binds_and_reports_stats(self) -> None:
        response = asyncio.run(
            server.link_session(url=CONVERSATION_URL, session="http-bucket", seed=False)
        )
        body = json.loads(response.body)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["linked_url"], CONVERSATION_URL)
        self.assertEqual(body["session_stats"]["linked_url"], CONVERSATION_URL)
        self.assertEqual(self.driver.linked_url("http-bucket"), CONVERSATION_URL)

    def test_link_endpoint_rejects_bad_url_with_400(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(server.link_session(url="https://chatgpt.com/", session="http-bucket"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("没有会话 ID", ctx.exception.detail)
        self.assertIsNone(self.driver.linked_url("http-bucket"))

    def test_endpoints_honour_reset_token(self) -> None:
        from fastapi import HTTPException

        with mock.patch.object(config, "RESET_TOKEN", "s3cret"):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    server.link_session(url=CONVERSATION_URL, session="http-bucket", seed=False)
                )
            self.assertEqual(ctx.exception.status_code, 403)
            asyncio.run(
                server.link_session(
                    url=CONVERSATION_URL, session="http-bucket", seed=False, x_reset_token="s3cret"
                )
            )
        self.assertEqual(self.driver.linked_url("http-bucket"), CONVERSATION_URL)

    def test_unlink_endpoint_clears_the_binding(self) -> None:

        asyncio.run(server.link_session(url=CONVERSATION_URL, session="http-bucket", seed=False))
        response = asyncio.run(server.unlink_session(session="http-bucket"))
        body = json.loads(response.body)
        self.assertTrue(body["unlinked"])
        self.assertIsNone(self.driver.linked_url("http-bucket"))
        # 重复解除：幂等，第二次报告「本来就没绑定」
        again = json.loads(asyncio.run(server.unlink_session(session="http-bucket")).body)
        self.assertFalse(again["unlinked"])


# ==================== 桥内命令不经过网页版 ====================


class BridgeCommandShortCircuitTests(_LinkTestBase):
    """chat / responses 两条协议路径都要「命令不进网页版」。"""

    def test_chat_stream_and_nonstream_reply_without_touching_the_page(self) -> None:
        from chatgpt_web.api import chat_adapter
        from chatgpt_web.models import ChatCompletionRequest

        request = ChatCompletionRequest(
            model="chatgpt-chat",
            messages=_user(f"/link {CONVERSATION_URL}"),
            stream=True,
        )
        driver = self.make_driver()
        driver.context = _FakeContext()
        driver.page = None

        def boom(*args, **kwargs):
            raise AssertionError("命令不得发给网页版")

        with mock.patch.object(driver, "send_chat", side_effect=boom):
            chunks = asyncio.run(_collect(chat_adapter.stream_chat_completion(request, "p", driver)))
            response = asyncio.run(
                chat_adapter.run_chat_completion(
                    request, driver, None, False, None, prompt="p", seeded_prompt="p"
                )
            )
            from chatgpt_web.api import responses_adapter

            reply, _blocks, tool_calls, _sent = asyncio.run(
                responses_adapter.run_chat(request, driver, None)
            )

        self.assertIn(CONVERSATION_ID, _sse_delta_text("".join(chunks)))
        self.assertIn("data: [DONE]", "".join(chunks))
        self.assertIn(CONVERSATION_ID, response.choices[0].message.content)
        self.assertIn(CONVERSATION_ID, reply)
        self.assertEqual(tool_calls, [])
        self.assertEqual(driver.linked_url(DEFAULT_SESSION_KEY), CONVERSATION_URL)


class LinkRouteTests(unittest.TestCase):
    """HTTP 路由层：两种协议的命令都在桥内收尾（网页版完全不被调用）。"""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.client = TestClient(server.app)

    def _mock_driver(self, mock_driver):
        mock_driver.page = mock.MagicMock()
        mock_driver.needs_seed.return_value = False
        mock_driver.sent_prompt.return_value = None
        mock_driver.bucket_busy.return_value = False
        mock_driver.send_chat = mock.AsyncMock()
        return mock_driver

    @mock.patch("chatgpt_web.server.tasks")
    @mock.patch("chatgpt_web.server.driver")
    def test_chat_route_stream(self, mock_driver, mock_tasks) -> None:
        self._mock_driver(mock_driver)
        mock_tasks.resume_block.return_value = None
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-chat",
                "stream": True,
                "messages": [{"role": "user", "content": f"/link {CONVERSATION_URL}"}],
            },
            headers={"X-ChatGPT-Session": "link-route"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(CONVERSATION_ID, _sse_delta_text(response.text))
        self.assertIn("[DONE]", response.text)
        mock_driver.send_chat.assert_not_called()
        mock_driver.link_session.assert_called_once()

    @mock.patch("chatgpt_web.server.tasks")
    @mock.patch("chatgpt_web.server.driver")
    def test_chat_route_non_stream(self, mock_driver, mock_tasks) -> None:
        self._mock_driver(mock_driver)
        mock_tasks.resume_block.return_value = None
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-chat",
                "messages": [{"role": "user", "content": f"/link {CONVERSATION_URL}"}],
            },
            headers={"X-ChatGPT-Session": "link-route"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        choice = response.json()["choices"][0]
        self.assertIn(CONVERSATION_ID, choice["message"]["content"])
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(response.json()["usage"]["prompt_tokens"], 0)
        mock_driver.send_chat.assert_not_called()

    @mock.patch("chatgpt_web.api.responses_adapter.tasks")
    @mock.patch("chatgpt_web.server.driver")
    def test_responses_route_stream(self, mock_driver, mock_tasks) -> None:
        self._mock_driver(mock_driver)
        mock_tasks.resume_block.return_value = None
        response = self.client.post(
            "/v1/responses",
            json={
                "model": "chatgpt-chat",
                "stream": True,
                "input": f"/link {CONVERSATION_URL}",
            },
            headers={"X-ChatGPT-Session": "link-route"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("response.completed", response.text)
        self.assertIn(CONVERSATION_ID, _sse_delta_text(response.text))
        mock_driver.send_chat.assert_not_called()


async def _collect(agen):
    return [chunk async for chunk in agen]


def _text_values(node):
    """递归抽出一段 SSE 载荷里的文本字段（chat 的 content/delta、responses 的 delta/text）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("content", "delta", "text") and isinstance(value, str):
                yield value
            else:
                yield from _text_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _text_values(item)


def _sse_delta_text(raw: str) -> str:
    """把所有 SSE ``data:`` 事件里的文本拼起来。

    回复是按 64 字符分片下发的，会话 ID 可能正好跨界，所以断言必须针对**拼好的**
    文本而不是原始 SSE 串（否则测的是分片位置这种无关细节）。
    """
    pieces = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        pieces.extend(_text_values(data))
    return "".join(pieces)


if __name__ == "__main__":
    unittest.main(verbosity=2)
