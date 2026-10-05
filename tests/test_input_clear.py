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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


if __name__ == "__main__":
    unittest.main()
