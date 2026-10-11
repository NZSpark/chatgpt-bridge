"""Bridge 聊天命令（``/bridge ...``）：解析、执行、作用域与两条协议路径的一致性。

对照 ``doc/bridge_command.md`` §7 的测试清单：

* 合法完整命令被直接应答，**不触发浏览器生成**（``send_chat`` 全程不被调用）；
* 非法 / 缺失 / 多余参数与未知选项返回可理解的用法；
* 多行文本、代码块、引用或普通句子里的命令字样不会意外执行；
* 只检查最后一条用户消息（历史里的 ``/bridge session reset`` 不得每轮重放）；
* Chat Completions 与 Responses 两条路径行为一致；
* 指定 ``X-ChatGPT-Session`` 时只影响预期的会话桶；
* 浏览器未启动时只读命令仍能给出可靠状态，普通请求照旧 503；
* reseed 的一次性标记跨重启 / 并发不会丢失，也不会重复应用；
* 回复不泄露 ``.env``、令牌或其它桶的信息。

不开真浏览器：页面替身沿用 tests/test_page_recovery.py 的那一套，思考模式 pill 用本文件
的小替身（可编排「点击后 aria-pressed 是否真的变化」）。
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

from chatgpt_web import bridge_commands, config, models, server  # noqa: E402
from chatgpt_web.api import chat_adapter, responses_adapter  # noqa: E402
from chatgpt_web.driver import DEFAULT_SESSION_KEY, ChatGPTWebDriver  # noqa: E402
from chatgpt_web.models import ChatCompletionRequest, ChatMessage  # noqa: E402
from tests.test_page_recovery import (  # noqa: E402
    _FakeContext,
    _HealthyPage,
    _quiet_page_setup,
)

CONVERSATION_ID = "6ac9c061-5074-83ec-82f0-2f46a8c334ca"
CONVERSATION_URL = f"https://chatgpt.com/c/{CONVERSATION_ID}"


def _user(text: str):
    return [ChatMessage(role="user", content=text)]


@contextlib.contextmanager
def _quiet_setup():
    with _quiet_page_setup():
        yield


# ==================== 页面 / 控件替身 ====================


class _RecordingPage(_HealthyPage):
    """记录 ``goto`` 的活页面（用来证明只读命令**不导航**）。"""

    def __init__(self) -> None:
        super().__init__()
        self.gotos = []

    async def goto(self, url: str, wait_until=None) -> None:
        self.gotos.append(url)
        self.url = url


class _ThinkPill:
    """思考模式 pill 的替身。

    ``toggle=False`` 模拟「站点其实不允许关掉」：点击后 ``aria-pressed`` 仍是 true，
    命令必须据此如实报告失败，而不是宣称已经切换。
    """

    def __init__(self, pressed: bool = True, toggle: bool = True) -> None:
        self.pressed = pressed
        self.toggle = toggle
        self.clicks = 0

    async def inner_text(self) -> str:
        return "Think"

    async def get_attribute(self, name: str):
        if name == "aria-pressed":
            return "true" if self.pressed else "false"
        return None

    async def click(self, timeout=None) -> None:
        self.clicks += 1
        if self.toggle:
            self.pressed = not self.pressed

    async def evaluate(self, script: str) -> None:
        self.clicks += 1
        if self.toggle:
            self.pressed = not self.pressed


class _ThinkPage:
    """带思考模式 pill 的页面替身（配置选择器直接命中）。"""

    def __init__(self, pill: _ThinkPill, url: str = "https://chatgpt.com/") -> None:
        self.pill = pill
        self.url = url

    async def query_selector_all(self, selector: str):
        return [self.pill]


class _BridgeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-command-")
        self._patch = mock.patch.object(
            config, "SESSION_FILE", Path(self.tmp.name) / "state.json"
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmp.cleanup()

    def make_driver(self) -> ChatGPTWebDriver:
        return ChatGPTWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))


# ==================== 命令清单与帮助 ====================


class HelpTests(_BridgeTestBase):
    def test_help_lists_every_registered_command_and_marks_risk(self) -> None:
        reply = asyncio.run(bridge_commands.handle_command(_user("/bridge help"), self.make_driver()))
        self.assertIn("[Bridge]", reply)
        for spec in bridge_commands.REGISTRY:
            self.assertIn(spec.syntax, reply, f"帮助漏了已注册的命令：{spec.syntax}")
        self.assertIn("只读", reply)
        self.assertIn("改状态", reply)
        # 兼容别名必须写清楚，避免用户以为 /link 被替换掉了
        self.assertIn("/link", reply)
        self.assertIn("/unlink", reply)

    def test_help_topic_filters_to_one_group(self) -> None:
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge help session"), self.make_driver())
        )
        self.assertIn("session reset", reply)
        self.assertNotIn("settings save-files", reply)

    def test_bare_bridge_is_discovery_not_an_error(self) -> None:
        command = bridge_commands.parse_command("/bridge")
        self.assertEqual(command.action, "help")

    def test_unknown_help_topic_lists_available_topics(self) -> None:
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge help nosuch"), self.make_driver())
        )
        self.assertIn("命令未执行", reply)
        self.assertIn("session", reply)
        self.assertIn("settings", reply)

    def test_registry_entries_are_actually_parseable(self) -> None:
        """帮助与解析器是同一份注册表：列出来的顶层子命令必须真的被识别。"""
        for spec in bridge_commands.REGISTRY:
            with self.subTest(syntax=spec.syntax):
                self.assertIn(spec.group, ("", *bridge_commands.HELP_TOPICS))
                # settings 还要带设置名才算完整命令（没带名字 = 缺参数）
                tokens = spec.syntax.split()
                width = 3 if len(tokens) > 2 and tokens[1] == "settings" else 2
                probe = " ".join(tokens[:width])
                command = bridge_commands.parse_command(probe)
                self.assertIsNotNone(command, f"帮助里的 {probe} 解析不到")
                self.assertNotEqual(command.action, "invalid", f"帮助里的 {probe} 已失效")

    def test_extra_arguments_are_rejected_with_usage(self) -> None:
        for text in (
            "/bridge help session settings",
            "/bridge status extra",
            "/bridge models extra",
            "/bridge session status extra",
            "/bridge session reset extra",
        ):
            with self.subTest(text=text):
                reply = asyncio.run(
                    bridge_commands.handle_command(_user(text), self.make_driver())
                )
                self.assertIn("命令未执行", reply)
                self.assertIn("用法", reply)

    def test_unknown_subcommands_and_options_are_reported(self) -> None:
        for text, token in (
            ("/bridge wat", "未知子命令"),
            ("/bridge session wat", "没有这个子命令"),
            ("/bridge session link --nosuch", "不认识选项"),
            ("/bridge session link", "还需要一个会话链接"),
            ("/bridge settings", "还需要一个设置名"),
            ("/bridge settings nosuch", "没有这个设置项"),
            ("/bridge settings think maybe", "不支持取值"),
        ):
            with self.subTest(text=text):
                reply = asyncio.run(
                    bridge_commands.handle_command(_user(text), self.make_driver())
                )
                self.assertIn("命令未执行", reply)
                self.assertIn(token, reply)


# ==================== 精确匹配与「不抢普通对话」 ====================


class ParsingTests(unittest.TestCase):
    def test_ordinary_text_is_left_to_the_model(self) -> None:
        for text in (
            "请解释 /bridge session reset",
            "/bridge status\nAND MORE",
            "```\n/bridge models\n```",
            "> /bridge status",
            "看看 /bridge status 是怎么实现的",
            "/bridge-status",
            "/bridgex help",
            "/models",
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(
                    bridge_commands.parse_command(text), "不能把用户正常说的话吃掉"
                )

    def test_is_command_matches_what_the_bridge_answers(self) -> None:
        self.assertTrue(bridge_commands.is_command(_user("/bridge status")))
        self.assertTrue(bridge_commands.is_command(_user("/bridge")))
        self.assertTrue(bridge_commands.is_command(_user("/link")))
        self.assertTrue(bridge_commands.is_command(_user(f"/link {CONVERSATION_URL}")))
        self.assertTrue(bridge_commands.is_command(_user("/unlink")))
        self.assertFalse(bridge_commands.is_command(_user("你好")))
        self.assertFalse(bridge_commands.is_command(_user("/bridge status\nmore")))
        # 最后一条不是 user 消息 → 不执行任何命令（历史里的旧命令不得重放）
        self.assertFalse(
            bridge_commands.is_command([ChatMessage(role="assistant", content="/bridge status")])
        )

    def test_only_the_last_user_message_counts(self) -> None:
        driver = ChatGPTWebDriver(user_data_dir="/tmp/chatgpt-test-bridge-noop")
        messages = [
            ChatMessage(role="user", content="/bridge session reset"),
            ChatMessage(role="assistant", content="[Bridge] 已完成"),
            ChatMessage(role="user", content="继续"),
        ]
        self.assertIsNone(
            asyncio.run(bridge_commands.handle_command(messages, driver)),
            "历史里的旧命令不得重放",
        )
        self.assertFalse(
            driver._state(DEFAULT_SESSION_KEY).pending_rotation,
            "历史里的 reset 绝不能偷偷生效",
        )


# ==================== 状态摘要 ====================


class StatusTests(_BridgeTestBase):
    def test_status_reports_bucket_scoping_and_binding(self) -> None:
        driver = self.make_driver()
        driver.page = _RecordingPage()
        driver.context = _FakeContext()
        driver.link_session(CONVERSATION_URL, key="bucket-a")

        reply = asyncio.run(bridge_commands.handle_command(_user("/bridge status"), driver, "bucket-a"))

        self.assertIn("bucket-a", reply)
        self.assertIn(CONVERSATION_URL, reply)
        self.assertIn("会话分桶", reply)
        self.assertIn("可用", reply)
        self.assertEqual(driver.page.gotos, [], "只读命令不得导航网页")

    def test_status_reports_degraded_browser_with_next_step(self) -> None:
        driver = self.make_driver()
        driver.page = None
        driver.init_error = "Browser failed to start\n(second line)"

        reply = asyncio.run(bridge_commands.handle_command(_user("/bridge status"), driver))

        self.assertIn("不可用", reply)
        self.assertIn("Browser failed to start", reply)
        self.assertIn("/readiness", reply, "未就绪时必须给出下一步建议")
        self.assertNotIn("(second line)", reply, "内部错误只保留一行摘要")

    def test_status_does_not_leak_other_buckets(self) -> None:
        driver = self.make_driver()
        driver.link_session(CONVERSATION_URL, key="bucket-a")

        reply = asyncio.run(bridge_commands.handle_command(_user("/bridge status"), driver, "bucket-b"))

        self.assertNotIn(CONVERSATION_URL, reply, "别的桶的绑定不得出现在这个桶的回复里")


class ModelsTests(_BridgeTestBase):
    def test_models_reply_comes_from_the_same_source_as_the_api(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(bridge_commands.handle_command(_user("/bridge models"), driver))

        expected = [card["id"] for card in models.advertised_models()]
        self.assertTrue(expected)
        for model_id in expected:
            self.assertIn(model_id, reply)
        # 与 GET /v1/models 的公开清单逐项一致（拒绝两套清单各自漂移）
        from fastapi.testclient import TestClient

        payload = TestClient(server.app).get("/v1/models").json()
        self.assertEqual([card["id"] for card in payload["data"]], expected)

    def test_models_reply_does_not_promise_web_side_switching(self) -> None:
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge models"), self.make_driver())
        )
        self.assertIn("不强制", reply, "别把 API 模型名说成能强制切换网页端模型")


# ==================== session 子命令 ====================


class SessionCommandTests(_BridgeTestBase):
    def test_bare_session_is_the_status_view(self) -> None:
        driver = self.make_driver()
        bare = asyncio.run(bridge_commands.handle_command(_user("/bridge session"), driver, "b"))
        explicit = asyncio.run(
            bridge_commands.handle_command(_user("/bridge session status"), driver, "b")
        )
        self.assertEqual(bare, explicit)
        self.assertIn("未绑定", bare)
        self.assertIn("任务快照", bare)

    def test_reset_marks_only_the_current_bucket_and_explains_scope(self) -> None:
        driver = self.make_driver()
        driver._state("bucket-a").turns = 3

        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge session reset"), driver, "bucket-a")
        )

        self.assertIn("bucket-a", reply)
        self.assertIn("只影响这个会话桶", reply)
        self.assertIn("不是删除客户端的对话历史", reply, "不能宣传成清空聊天记录")
        self.assertTrue(driver._state("bucket-a").pending_rotation)
        self.assertFalse(driver._state("bucket-a").has_history)
        self.assertFalse(driver._state("bucket-b").pending_rotation, "别的桶必须原样不动")
        self.assertIsNone(driver._state("bucket-b").linked_url)

    def test_reset_reuses_the_driver_reset_semantics(self) -> None:
        driver = self.make_driver()
        with mock.patch.object(driver, "reset_session") as reset:
            asyncio.run(
                bridge_commands.handle_command(_user("/bridge session reset"), driver, "bucket-a")
            )
        reset.assert_called_once_with("bucket-a")

    def test_reseed_queues_once_and_survives_restart(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge session reseed"), driver, "bucket-a")
        )
        self.assertIn("下一轮重新播种", reply)
        self.assertIn("重复内容", reply, "必须说明会重发上下文（而不是清空旧消息）")
        self.assertTrue(driver._state("bucket-a").pending_reseed)
        self.assertFalse(driver._state("bucket-b").pending_reseed)

        # 换一个进程（内存状态全空）→ 标记必须还在：一次性语义不能靠临时变量
        restarted = self.make_driver()
        self.assertTrue(restarted._state("bucket-a").pending_reseed)
        self.assertTrue(restarted.session_stats("bucket-a")["pending_reseed"])

    def test_session_link_and_unlink_reuse_the_linking_implementation(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(
            bridge_commands.handle_command(
                _user(f"/bridge session link {CONVERSATION_URL} --seed"), driver, "bucket-a"
            )
        )
        self.assertIn("[Bridge]", reply)
        self.assertIn(CONVERSATION_ID, reply)
        self.assertEqual(driver.linked_url("bucket-a"), CONVERSATION_URL)
        self.assertFalse(
            driver._state("bucket-a").has_history,
            "--seed = 那条会话里没有此前记录，本轮必须播种",
        )
        self.assertIsNone(driver.linked_url("bucket-b"))

        unlink = asyncio.run(
            bridge_commands.handle_command(_user("/bridge session unlink"), driver, "bucket-a")
        )
        self.assertIn("已解除", unlink)
        self.assertIsNone(driver.linked_url("bucket-a"))
        self.assertTrue(driver._state("bucket-a").pending_rotation)

    def test_session_link_rejects_bad_urls_without_binding(self) -> None:
        driver = self.make_driver()
        for url, token in (
            ("https://evil.test/c/abcdefgh", "不支持的站点"),
            ("https://chatgpt.com/", "没有会话 ID"),
            ("https://chatgpt.com/c/local-chatgpt%3Ae03cbb49-722a-4591-8f36-c491762833be", "临时"),
        ):
            with self.subTest(url=url):
                reply = asyncio.run(
                    bridge_commands.handle_command(
                        _user(f"/bridge session link {url}"), driver, "bucket-a"
                    )
                )
                self.assertIn("没能绑定", reply)
                self.assertIn(token, reply)
        self.assertIsNone(driver.linked_url("bucket-a"))


# ==================== settings 子命令 ====================


class SettingsTests(_BridgeTestBase):
    def test_think_status_without_a_page_is_honest(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge settings think status"), driver, "b")
        )
        self.assertIn("思考模式偏好", reply)
        self.assertIn("THINK_MODE_DEFAULT", reply)
        self.assertIn("页面尚未打开", reply)

    def test_think_preference_is_per_bucket_and_persisted(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge settings think off"), driver, "bucket-a")
        )
        self.assertIn("已记录", reply)
        self.assertFalse(driver.think_mode_preference("bucket-a"))
        self.assertIsNone(driver.think_mode_preference("bucket-b"), "只有这个桶受影响")
        # 偏好必须落盘：新进程（内存全空）读回来仍是关闭
        self.assertFalse(self.make_driver().think_mode_preference("bucket-a"))

        back_to_default = asyncio.run(
            bridge_commands.handle_command(
                _user("/bridge settings think default"), driver, "bucket-a"
            )
        )
        self.assertIn("跟随全局配置", back_to_default)
        self.assertIsNone(driver.think_mode_preference("bucket-a"))

    def _driver_with_pill(self, pill: _ThinkPill) -> ChatGPTWebDriver:
        """默认桶的页面上放一个思考模式 pill（非默认桶的页面在 _pages 里，这里用默认桶）。"""
        driver = self.make_driver()
        driver.page = _ThinkPage(pill)
        driver.context = _FakeContext()
        return driver

    def test_think_off_is_applied_and_verified_on_the_page(self) -> None:
        pill = _ThinkPill(pressed=True, toggle=True)
        driver = self._driver_with_pill(pill)

        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge settings think off"), driver)
        )

        self.assertFalse(pill.pressed, "命令必须真的把网页端切到关闭")
        self.assertIn("已同步", reply)
        self.assertIn("已关闭", reply)
        self.assertFalse(driver.think_mode_preference(DEFAULT_SESSION_KEY))

    def test_think_off_reports_failure_when_the_site_refuses(self) -> None:
        # 点不动：aria-pressed 永远是 true（站点其实不允许关掉）
        pill = _ThinkPill(pressed=True, toggle=False)
        driver = self._driver_with_pill(pill)

        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge settings think off"), driver)
        )

        self.assertIn("切换未生效", reply)
        self.assertNotIn("已同步", reply, "切换失败绝不能谎报成功")
        self.assertFalse(
            driver.think_mode_preference(DEFAULT_SESSION_KEY),
            "偏好仍然记录下来（下一次会再试）",
        )

    def test_think_off_never_blind_clicks_an_unreadable_control(self) -> None:
        """读不到 ``aria-pressed`` 时不能乱点（可能反而打开思考模式）。"""
        pill = _ThinkPill(pressed=True, toggle=True)
        pill.get_attribute = mock.AsyncMock(return_value=None)  # type: ignore[method-assign]
        driver = self._driver_with_pill(pill)

        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge settings think off"), driver)
        )

        self.assertEqual(pill.clicks, 0)
        self.assertIn("未能在当前页面切换", reply)

    def test_save_files_status_documents_scope_and_disable_path(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge settings save-files status"), driver, "b")
        )
        self.assertIn("落盘偏好", reply)
        self.assertIn("优先级", reply)
        self.assertIn("save_files=false", reply, "必须写清怎么关掉")
        self.assertNotIn("落盘目录", reply, "默认关闭时不该暗示会产生文件")

    def test_save_files_preference_is_per_bucket_and_persisted(self) -> None:
        driver = self.make_driver()
        reply = asyncio.run(
            bridge_commands.handle_command(_user("/bridge settings save-files on"), driver, "bucket-a")
        )
        self.assertIn("已记录", reply)
        self.assertIn("落盘目录", reply)
        self.assertTrue(driver.save_files_preference("bucket-a"))
        self.assertIsNone(driver.save_files_preference("bucket-b"))
        self.assertTrue(self.make_driver().save_files_preference("bucket-a"))


class SaveFilesDecisionTests(_BridgeTestBase):
    """落盘决策的优先级：请求字段 > 本桶偏好 > 配置（默认 false，副作用必须显式）。"""

    def _decide(self, driver, request_save_files, key="bucket-a"):
        request = ChatCompletionRequest(
            model="chatgpt-chat", messages=_user("hi"), save_files=request_save_files
        )
        return chat_adapter.save_files_enabled(driver, key, request)

    def test_explicit_request_field_wins(self) -> None:
        driver = self.make_driver()
        driver.set_save_files_preference(True, "bucket-a")
        self.assertTrue(self._decide(driver, True))
        self.assertFalse(self._decide(driver, False), "显式 false 必须能覆盖本桶偏好")

    def test_bucket_preference_beats_config(self) -> None:
        driver = self.make_driver()
        driver.set_save_files_preference(True, "bucket-a")
        with mock.patch.object(config, "SAVE_FILES", False):
            self.assertTrue(self._decide(driver, None))

    def test_unset_preference_follows_config(self) -> None:
        driver = self.make_driver()
        with mock.patch.object(config, "SAVE_FILES", False):
            self.assertFalse(self._decide(driver, None))
        with mock.patch.object(config, "SAVE_FILES", True):
            self.assertTrue(self._decide(driver, None))

    def test_unknown_driver_shapes_fall_back_to_config(self) -> None:
        """假 driver / 替身返回的真值不能被当成「用户开了落盘」。"""
        default = object()
        with mock.patch.object(config, "SAVE_FILES", False):
            self.assertFalse(self._decide(default, None))
        with mock.patch.object(config, "SAVE_FILES", True):
            self.assertTrue(self._decide(default, None))


# ==================== reseed 的消费时机（一次性语义）====================


class ReseedConsumptionTests(_BridgeTestBase):
    def _driver_with_history(self) -> ChatGPTWebDriver:
        driver = self.make_driver()
        driver.page = _RecordingPage()
        driver.context = _FakeContext()
        driver._state(DEFAULT_SESSION_KEY).has_history = True
        return driver

    def _send(self, driver, seen):
        async def fake_locked(prompt, on_delta=None, key=None):
            seen.append(prompt)
            return "ok", []

        with _quiet_setup(), mock.patch.object(driver, "_send_chat_locked", side_effect=fake_locked):
            return asyncio.run(driver.send_chat("delta-only", seeded_prompt="SEEDED", key=None))

    def test_reseed_forces_exactly_one_seeded_send(self) -> None:
        driver = self._driver_with_history()
        driver.reseed_session(DEFAULT_SESSION_KEY)
        seen = []

        self._send(driver, seen)
        self.assertEqual(seen, ["SEEDED"], "带标记的这一轮必须用播种 prompt")
        self.assertFalse(driver._state(DEFAULT_SESSION_KEY).pending_reseed, "成功即消费标记")

        seen.clear()
        self._send(driver, seen)
        self.assertEqual(seen, ["delta-only"], "一次性标记不能影响后续轮次")

    def test_without_the_flag_nothing_changes(self) -> None:
        driver = self._driver_with_history()
        seen = []
        self._send(driver, seen)
        self.assertEqual(seen, ["delta-only"], "未排队的桶行为必须逐字不变")


# ==================== 两条协议路径：命令不进网页版 ====================


class BridgeCommandShortCircuitTests(_BridgeTestBase):
    def _driver_without_browser(self) -> ChatGPTWebDriver:
        driver = self.make_driver()
        driver.context = _FakeContext()
        driver.page = None
        return driver

    def test_chat_stream_and_nonstream_answer_without_a_browser(self) -> None:
        driver = self._driver_without_browser()
        request = ChatCompletionRequest(
            model="chatgpt-chat", messages=_user("/bridge status"), stream=True
        )

        def boom(*args, **kwargs):
            raise AssertionError("命令不得发给网页版")

        with mock.patch.object(driver, "send_chat", side_effect=boom):
            chunks = asyncio.run(
                _collect(chat_adapter.stream_chat_completion(request, "p", driver))
            )
            response = asyncio.run(
                chat_adapter.run_chat_completion(
                    request, driver, None, False, None, prompt="p", seeded_prompt="p"
                )
            )

        streamed = _sse_delta_text("".join(chunks))
        self.assertIn("[Bridge]", streamed)
        self.assertIn("data: [DONE]", "".join(chunks))
        self.assertIn("[Bridge]", response.choices[0].message.content or "")
        self.assertEqual(response.usage.prompt_tokens, 0, "桥的应答不算网页版用量")

    def test_responses_path_answers_without_a_browser(self) -> None:
        driver = self._driver_without_browser()
        request = ChatCompletionRequest(model="chatgpt-chat", messages=_user("/bridge help"))

        def boom(*args, **kwargs):
            raise AssertionError("命令不得发给网页版")

        with mock.patch.object(driver, "send_chat", side_effect=boom):
            reply, blocks, tool_calls, sent = asyncio.run(
                responses_adapter.run_chat(request, driver, None)
            )

        self.assertIn("[Bridge]", reply)
        self.assertEqual(blocks, [])
        self.assertEqual(tool_calls, [])
        self.assertEqual(sent, reply)

    def test_state_changing_command_from_responses_touches_no_page(self) -> None:
        driver = self._driver_without_browser()
        request = ChatCompletionRequest(
            model="chatgpt-chat", messages=_user("/bridge session reset")
        )

        with mock.patch.object(driver, "send_chat", side_effect=AssertionError("不得发给网页版")):
            reply, _blocks, _tool_calls, _sent = asyncio.run(
                responses_adapter.run_chat(request, driver, "codex-bucket")
            )

        self.assertIn("codex-bucket", reply)
        self.assertTrue(driver._state("codex-bucket").pending_rotation)
        self.assertEqual(driver.context.created, [], "命令阶段不该新开页面")


# ==================== HTTP 路由：浏览器不可用时命令仍可用 ====================


class BridgeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.client = TestClient(server.app)

    def _mock_down_driver(self, mock_driver):
        """浏览器初始化失败的 bridge：命令要能答，普通请求仍 503。"""
        mock_driver.page = None
        mock_driver.init_error = "Browser failed to start"
        mock_driver.bucket_page.return_value = None
        mock_driver.bucket_page_url.return_value = None
        mock_driver.linked_url.return_value = None
        mock_driver.session_stats.return_value = {"turns": 0, "est_tokens": 0}
        mock_driver.needs_seed.return_value = False
        mock_driver.sent_prompt.return_value = None
        mock_driver.bucket_busy.return_value = False
        mock_driver.send_chat = mock.AsyncMock()
        return mock_driver

    @mock.patch("chatgpt_web.server.driver")
    def test_chat_route_answers_status_when_the_browser_is_down(self, mock_driver) -> None:
        self._mock_down_driver(mock_driver)
        response = self.client.post(
            "/v1/chat/completions",
            json={"model": "chatgpt-chat", "messages": [{"role": "user", "content": "/bridge status"}]},
            headers={"X-ChatGPT-Session": "down-bucket"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("不可用", response.json()["choices"][0]["message"]["content"])
        mock_driver.send_chat.assert_not_called()

    @mock.patch("chatgpt_web.server.driver")
    def test_chat_route_still_503s_for_ordinary_text(self, mock_driver) -> None:
        self._mock_down_driver(mock_driver)
        response = self.client.post(
            "/v1/chat/completions",
            json={"model": "chatgpt-chat", "messages": [{"role": "user", "content": "你好"}]},
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["type"], "unavailable")

    @mock.patch("chatgpt_web.server.driver")
    def test_chat_route_streams_a_command_when_the_browser_is_down(self, mock_driver) -> None:
        self._mock_down_driver(mock_driver)
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-chat",
                "stream": True,
                "messages": [{"role": "user", "content": "/bridge models"}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("chatgpt-chat", _sse_delta_text(response.text))
        mock_driver.send_chat.assert_not_called()

    @mock.patch("chatgpt_web.server.driver")
    def test_responses_route_answers_status_when_the_browser_is_down(self, mock_driver) -> None:
        self._mock_down_driver(mock_driver)
        response = self.client.post(
            "/v1/responses",
            json={"model": "chatgpt-chat", "input": "/bridge models"},
            headers={"X-ChatGPT-Session": "down-bucket"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        text = "".join(
            part.get("text", "")
            for item in response.json()["output"]
            for part in item.get("content", [])
        )
        self.assertIn("chatgpt-chat", text)
        mock_driver.send_chat.assert_not_called()

    @mock.patch("chatgpt_web.server.driver")
    def test_responses_route_streams_a_command_when_the_browser_is_down(self, mock_driver) -> None:
        self._mock_down_driver(mock_driver)
        response = self.client.post(
            "/v1/responses",
            json={"model": "chatgpt-chat", "stream": True, "input": "/bridge help session"},
            headers={"X-ChatGPT-Session": "down-bucket"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("response.completed", response.text)
        self.assertIn("session reset", _sse_delta_text(response.text))
        mock_driver.send_chat.assert_not_called()

    @mock.patch("chatgpt_web.server.driver")
    def test_responses_route_still_503s_for_ordinary_text(self, mock_driver) -> None:
        self._mock_down_driver(mock_driver)
        response = self.client.post(
            "/v1/responses", json={"model": "chatgpt-chat", "input": "你好"}
        )
        self.assertEqual(response.status_code, 503, response.text)


class RealDriverDegradedRouteTests(_BridgeTestBase):
    """用**真的** driver（而不是 MagicMock）跑一次降级态 HTTP 往返。

    浏览器初始化失败时 ``driver.page is None``、``init_error`` 有值：这正是用户排障时
    的场景，也是路由层“对命令放行”真正要保护的那条路径。这里顺带覆盖真实的会话状态落盘
    （``/bridge session reseed`` 经 HTTP 写入，再能被新进程读回）。
    """

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        self.client = TestClient(server.app)
        self.driver = server.driver
        self._saved = {
            "page": self.driver.page,
            "context": self.driver.context,
            "init_error": self.driver.init_error,
            "sessions": dict(self.driver._sessions),
        }
        self.driver.page = None
        self.driver.context = None
        self.driver.init_error = "Browser failed to start"
        self.driver._sessions.clear()

    def tearDown(self) -> None:
        self.driver.page = self._saved["page"]
        self.driver.context = self._saved["context"]
        self.driver.init_error = self._saved["init_error"]
        self.driver._sessions.clear()
        self.driver._sessions.update(self._saved["sessions"])
        super().tearDown()

    def test_status_then_reseed_over_http_with_the_real_driver(self) -> None:
        headers = {"X-ChatGPT-Session": "real-down-bucket"}

        status = self.client.post(
            "/v1/chat/completions",
            json={"model": "chatgpt-chat", "messages": [{"role": "user", "content": "/bridge status"}]},
            headers=headers,
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertIn("不可用", status.json()["choices"][0]["message"]["content"])

        reseed = self.client.post(
            "/v1/chat/completions",
            json={"model": "chatgpt-chat", "messages": [{"role": "user", "content": "/bridge session reseed"}]},
            headers=headers,
        )
        self.assertEqual(reseed.status_code, 200, reseed.text)
        self.assertTrue(self.driver._state("real-down-bucket").pending_reseed)
        # 真的落盘了：换一个进程（新 driver 实例）读回来仍然在
        self.assertTrue(self.make_driver()._state("real-down-bucket").pending_reseed)

        ordinary = self.client.post(
            "/v1/chat/completions",
            json={"model": "chatgpt-chat", "messages": [{"role": "user", "content": "你好"}]},
            headers=headers,
        )
        self.assertEqual(ordinary.status_code, 503, ordinary.text)


# ==================== 不泄露 ====================


class NoLeakTests(_BridgeTestBase):
    def test_replies_never_print_secrets_or_env_values(self) -> None:
        driver = self.make_driver()
        driver.page = _RecordingPage()
        driver.context = _FakeContext()
        samples = ["/bridge", "/bridge help", "/bridge status", "/bridge models",
                   "/bridge session", "/bridge session reset", "/bridge session reseed",
                   "/bridge settings think on", "/bridge settings save-files on",
                   "/bridge settings save-files status",
                   f"/bridge session link {CONVERSATION_URL}"]
        with mock.patch.object(config, "RESET_TOKEN", "super-secret-token"), \
                mock.patch.object(config, "LINK_ALLOWED_HOSTS", "chatgpt.com"):
            for text in samples:
                with self.subTest(text=text):
                    reply = asyncio.run(
                        bridge_commands.handle_command(_user(text), driver, "bucket-a")
                    )
                    self.assertNotIn("super-secret-token", reply)
                    self.assertNotIn("Cookie", reply)
                    self.assertNotIn("cookie", reply)
                    self.assertNotIn(".env", reply)
                    self.assertNotIn("Traceback", reply)


# ==================== 工具 ====================


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
    """把所有 SSE ``data:`` 事件里的文本拼起来（回复按 64 字符分片，断言要针对拼好的文本）。"""
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
