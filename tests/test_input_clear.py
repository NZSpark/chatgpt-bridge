"""输入框清空行为的单元测试（不联网，纯假件）。

背景：ChatGPT 的 composer 是 ProseMirror，会把未发送的草稿持久化到浏览器
存储。重新进入页面 / 新建对话后，输入框里可能残留上一次没发出去的内容；
若发送前不清空，``insert_text`` 会把它和新 prompt 拼在一起发出去。

本用例用一个「真正会残留草稿」的假 composer 验证：
1. ``_clear_input`` 会把残留内容清掉（读到空串）；
2. 端到端 ``_call_fill`` 之后，输入框里**只有**新 prompt，没有旧草稿。
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import config  # noqa: E402
from chatgpt_web.browser.dom_adapter import ChatGPTDOMAdapter  # noqa: E402
from chatgpt_web.chat_io import ChatIOMixin  # noqa: E402


class FakeComposer:
    """假 ProseMirror composer：DOM 直改会被“回滚”，只有全选删除生效。

    * ``innerText`` 返回当前内容；
    * ``evaluate`` 收到 ``innerHTML = ''`` 时**不**真的清空，模拟 ProseMirror
      对直接改 DOM 的回滚；只有键盘全选删除路径才清空。
    """

    def __init__(self, initial: str = ""):
        self.text = initial

    async def click(self, **kwargs):
        return None

    async def fill(self, value, **kwargs):
        # Playwright 的 fill("") 走「清空 + 设值」，这里如实模拟。
        self.text = value
        return None

    async def evaluate(self, script):
        if "innerText" in script:
            return self.text
        if "innerHTML = ''" in script:
            # 模拟 ProseMirror 回滚：直接改 DOM 不生效。
            return None
        return None


class FakeKeyboard:
    def __init__(self, composer):
        self.composer = composer
        self.submitted = False

    async def press(self, key):
        # 全选删除是唯一有效的清空路径（对应假 composer 的回滚语义）。
        if key == "Backspace":
            self.composer.text = ""
        if key == "Enter":
            self.submitted = True
        return None

    async def insert_text(self, text):
        self.composer.text = (self.composer.text or "") + text
        return None


class FakePage:
    def __init__(self, composer):
        self.keyboard = FakeKeyboard(composer)


class InputClearTests(unittest.TestCase):

    def test_clear_input_removes_persisted_draft(self):
        composer = FakeComposer(initial="上一次没发出去的草稿")
        page = FakePage(composer)

        asyncio.run(ChatIOMixin._clear_input(page, composer))

        self.assertEqual(composer.text, "")

    def test_call_fill_leaves_only_new_prompt(self):
        composer = FakeComposer(initial="旧的残留草稿")
        page = FakePage(composer)

        asyncio.run(
            ChatIOMixin._call_fill(page, composer, "新的 prompt", timeout=1000)
        )

        self.assertEqual(composer.text, "新的 prompt")

    def test_clear_input_noop_when_already_empty(self):
        composer = FakeComposer(initial="")
        page = FakePage(composer)

        asyncio.run(ChatIOMixin._clear_input(page, composer))

        self.assertEqual(composer.text, "")


class _Composer:
    """候选 composer 替身：``actionable=False`` 模拟「点不动」的僵尸编辑器。"""

    def __init__(self, page, *, actionable: bool = True, accepts: bool = True) -> None:
        self.page = page
        self.actionable = actionable
        self.accepts = accepts
        self.text = ""

    async def click(self, timeout=None, **kwargs):
        if not self.actionable:
            raise RuntimeError("element is not visible")
        self.page.focused = self
        return None

    async def fill(self, value, **kwargs):
        if self.accepts:
            self.text = value
        return None

    async def evaluate(self, script):
        if "focus" in script:
            self.page.focused = self
            return None
        return self.text if self.accepts else ""


class _ComposerPage:
    """假页面：keyboard 插字落到「最后一个被聚焦的 composer」上（贴近真实路径）。"""

    def __init__(self) -> None:
        self.focused = None
        self.keyboard = _ComposerKeyboard(self)


class _ComposerKeyboard:
    def __init__(self, page) -> None:
        self.page = page

    async def press(self, key):
        if key == "Backspace" and self.page.focused is not None:
            self.page.focused.text = ""
        return None

    async def insert_text(self, text):
        target = self.page.focused
        if target is not None and target.accepts:
            target.text = (target.text or "") + text
        return None


class _StubDom:
    """最小 DOM 替身：只提供 ``_fill_prompt`` 需要的两个入口。"""

    def __init__(self, candidates) -> None:
        self.candidates = list(candidates)

    async def input_candidates(self, page):
        return list(self.candidates)

    async def complete_text(self, node):
        return node.text if node.accepts else ""


class _CandidateHandle:
    """``input_candidates`` 的元素句柄替身（去重标记 / 可见性）。"""

    def __init__(self, mark, *, visible=True) -> None:
        self.mark = mark
        self.visible = visible

    async def evaluate(self, script):
        return self.mark if "bridgeInputCandidate" in script else ""

    async def is_visible(self):
        return self.visible


class _HandlePage:
    """按选择器返回句柄的页面替身（走 ``query_selector_all`` 路径）。"""

    def __init__(self, by_selector) -> None:
        self.by_selector = by_selector

    async def wait_for_selector(self, selector, timeout=None, state=None):
        handles = self.by_selector.get(selector) or []
        return handles[0] if handles else None

    async def query_selector_all(self, selector):
        return list(self.by_selector.get(selector) or [])


class ComposerCandidateTests(unittest.TestCase):
    """真机（2026-10-10，第二例）：页面 DOM 退化时同页会出现多个 ProseMirror 编辑器，
    「第一个 attached」往往点不动、读回为空 —— 必须枚举全部候选、可见优先、逐个试填。"""

    def test_input_candidates_dedupes_and_prefers_visible(self):
        visible = _CandidateHandle("visible")
        hidden = _CandidateHandle("hidden", visible=False)
        selectors = ['form div.ProseMirror[contenteditable="true"]', "textarea"]
        page = _HandlePage({selectors[0]: [hidden, visible], selectors[1]: [visible]})

        with mock.patch.object(config, "INPUT_SELECTORS", selectors):
            candidates = asyncio.run(ChatGPTDOMAdapter().input_candidates(page))

        self.assertEqual(
            [handle.mark for handle in candidates],
            ["visible", "hidden"],
            "同一元素只保留一次，且可见的排在前面",
        )

    def test_fill_prompt_moves_to_the_next_candidate_when_text_is_empty(self):
        page = _ComposerPage()
        dead = _Composer(page, actionable=False, accepts=False)
        live = _Composer(page)
        mixin = ChatIOMixin()
        mixin.dom = _StubDom([dead, live])

        handle = asyncio.run(mixin._fill_prompt(page, "新的 prompt"))

        self.assertIs(handle, live, "第一个候选读回为空时必须换下一个候选")
        self.assertEqual(live.text, "新的 prompt")
        self.assertEqual(dead.text, "", "僵尸候选不该拿到内容")

    def test_fill_prompt_returns_none_when_no_candidate_accepts_text(self):
        page = _ComposerPage()
        dead = _Composer(page, actionable=False, accepts=False)
        mixin = ChatIOMixin()
        mixin.dom = _StubDom([dead])

        with mock.patch.object(config, "FILL_RETRIES", 1):
            self.assertIsNone(asyncio.run(mixin._fill_prompt(page, "hi")))


if __name__ == "__main__":
    unittest.main()
