"""ChatGPT web composer input and prompt submission helpers (PI-026)."""

import asyncio
import logging

from . import config
from .errors import ChatGPTPageLostError, page_alive, page_lost_reason

logger=logging.getLogger(__name__)


class BrowserInputMixin:
    """Input-side browser operations kept separate from reply polling."""

    _ENTER_JS = """
    (el) => {
        const opts = {key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                      bubbles: true, cancelable: true};
        el.dispatchEvent(new KeyboardEvent('keydown', opts));
        el.dispatchEvent(new KeyboardEvent('keypress', opts));
        el.dispatchEvent(new KeyboardEvent('keyup', opts));
        return true;
    }
    """

    async def _keyboard_enter(self, page) -> bool:
        """用真实键盘事件提交（首选）。

        合成 KeyboardEvent（dispatchEvent）的 isTrusted=false，ProseMirror 的
        keymap 会直接忽略——尤其当 prompt 含换行、composer 内已分成多段时，
        合成 Enter 更可能被当成「软换行/新段」而非提交。真实键盘 Enter 才能
        稳定触发 ProseMirror 的提交 handler。

        之所以敢用全局键盘通道：_call_fill 已经 click() 聚焦过 composer，
        焦点就在输入框上，Enter 会落到它。这里不调 bring_to_front，窗口不在
        前台也不影响——Playwright 的 keyboard 事件走 CDP，不依赖 OS 前台焦点。
        """
        if page is None:
            return False
        try:
            await page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    async def _dispatch_enter(self, chat_input) -> bool:
        """兜底：在页面内对输入框派发合成 Enter 键事件（纯 DOM）。

        合成事件对 ProseMirror 常被忽略，仅作真实键盘 Enter 不可用时的退路。
        """
        if chat_input is None:
            return False
        try:
            return bool(await chat_input.evaluate(self._ENTER_JS))
        except Exception:
            return False

    async def _click_send_button(self, page) -> bool:
        """兼容 facade：发送按钮定位/点击统一由 DOM adapter 负责。"""
        return await self.dom.click_send_button(page)

    async def _find_input(self, page):
        """兼容 facade：输入框定位统一由 DOM adapter 负责。"""
        return await self.dom.find_input(page)

    async def _fill_prompt(self, page, prompt: str):
        """填充输入框并返回可用的句柄；成功返回句柄，失败返回 None。

        ``fill`` 对 ChatGPT 的 React ``contenteditable`` composer 不可靠：
        新建对话 / 节点重挂载后，之前拿到的句柄可能已失效，``fill`` 会一直
        等到超时（ElementHandle.fill: Timeout 30000ms exceeded）。这里每次
        重试都**重新定位**输入框，并对填完的内容做非空校验。
        """
        retries = max(1, config.FILL_RETRIES)
        timeout = max(1000, config.FILL_TIMEOUT_MS)
        for attempt in range(retries):
            chat_input = await self._find_input(page)
            if not chat_input:
                logger.warning(f"[输入] 第 {attempt + 1}/{retries} 次重试：未定位到输入框。")
                await asyncio.sleep(0.5)
                continue
            try:
                await self._call_fill(page, chat_input, prompt, timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[输入] 第 {attempt + 1}/{retries} 次输入失败：{exc!r}")
                await asyncio.sleep(0.5)
                continue
            # contenteditable 的 fill 可能“成功”但内容为空，必须校验。
            # 真实 composer 读到空时，额外用 input_value/value 兜底探测。
            try:
                text = await self._complete_text(chat_input)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[输入] 第 {attempt + 1}/{retries} 次读取文本异常：{exc!r}")
                text = ""
            if text and text.strip():
                if config.DEBUG:
                    logger.debug(f"[输入] fill 成功，读到 {len(text)} 字符。")
                return chat_input
            logger.warning(
                f"[输入] fill 第 {attempt + 1}/{retries} 次后内容为空"
                f"（读到 {text!r}），重试。"
            )
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    async def _call_fill(page, chat_input, prompt: str, timeout: int) -> None:
        """向 composer 输入文本。

        实测 ChatGPT 的 composer 是 ProseMirror（div#prompt-textarea），
        ``ElementHandle.fill()`` 对它不可靠：受控组件不吃直接设值，且元素常被
        判定为 not visible 导致 fill 超时。正确做法是聚焦后逐字 ``insert_text``
        （触发 beforeinput/input 事件，ProseMirror 才会更新内部 state）。

        优先 click 聚焦；元素不可见/被遮挡时退回 JS focus，再插入文本。
        """
        focused = False
        try:
            await chat_input.click(timeout=timeout)
            focused = True
        except Exception:  # noqa: BLE001
            try:
                await chat_input.evaluate("(el) => el.focus()")
                focused = True
            except Exception:  # noqa: BLE001
                focused = False
        if not focused:
            raise RuntimeError("无法聚焦输入框（click 与 JS focus 均失败）")
        await BrowserInputMixin._clear_input(page, chat_input)
        await page.keyboard.insert_text(prompt)

    @staticmethod
    async def _read_input_text(chat_input) -> str:
        """读取 composer 当前文本；任何异常都当作「读不到」返回空串。"""
        try:
            text = await chat_input.evaluate(
                "(el) => el.innerText ?? el.textContent ?? el.value ?? ''"
            )
            return text or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    async def _clear_input(page, chat_input) -> None:
        """把 composer 清空到「读到的文本为空」为止。

        ProseMirror 会把草稿持久化到浏览器存储，重新进入页面 / 新建对话后
        输入框里可能残留上一次没发出去的内容；若不清空，``insert_text`` 会
        把它和新 prompt 拼在一起发出去。

        单一手法都不可靠：Control+A 在 macOS 上未必被识别、Meta+A 在其它
        平台无意义、Backspace 对空段落无效、DOM 直接改 textContent 会被
        ProseMirror 回滚。这里组合使用并**循环校验**，直到读回空串。
        """
        max_rounds = 3
        for round_index in range(max_rounds):
            if not (await BrowserInputMixin._read_input_text(chat_input)).strip():
                return
            # 手法 1：全选后删除。两个平台的修饰键都按一遍，谁生效算谁。
            for modifier in ("Control+A", "Meta+A"):
                try:
                    await page.keyboard.press(modifier)
                    await page.keyboard.press("Backspace")
                except Exception:  # noqa: BLE001
                    pass
            # 手法 2：Playwright 的 fill("") 对可编辑元素会派发清空输入事件
            try:
                await chat_input.fill("")
            except Exception:  # noqa: BLE001
                pass
            # 手法 3：JS 清空并派发 input 事件，让 ProseMirror 同步内部 state
            try:
                await chat_input.evaluate(
                    """
                    (el) => {
                      el.focus();
                      if (el.isContentEditable) {
                        el.innerHTML = '';
                      } else {
                        el.value = '';
                      }
                      el.dispatchEvent(new InputEvent('input', {
                        bubbles: true, cancelable: true, inputType: 'deleteContentBackward',
                      }));
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.1)
        leftover = (await BrowserInputMixin._read_input_text(chat_input)).strip()
        if leftover:
            logger.warning(
                f"[输入] 警告：清空输入框后仍读到残留内容（{leftover[:80]!r}），"
                "新 prompt 可能被拼接。"
            )

    async def _submit_prompt(self, page, chat_input) -> None:
        """提交 prompt：真实键盘 Enter → 合成事件 → 点发送按钮。

        首选真实键盘 Enter：合成 KeyboardEvent 对 ProseMirror 不可靠，
        prompt 含换行时合成 Enter 会被当成软换行而非提交（见 _keyboard_enter）。

        三条路径全部失败时要先分辨原因：页面已经没了（标签被关 / 渲染进程崩溃）
        属于可自愈故障，抛 :class:`ChatGPTPageLostError` 让重试阶梯重建页面；
        页面活着却没提交出去才是真正的“发送失败”（P0-J）。
        """
        if not page_alive(page):
            raise ChatGPTPageLostError(f"提交 prompt 失败：{page_lost_reason(page)}")
        if await self._keyboard_enter(page):
            return
        if await self._dispatch_enter(chat_input):
            return
        if not await self._click_send_button(page):
            if not page_alive(page):
                raise ChatGPTPageLostError(f"提交 prompt 失败：{page_lost_reason(page)}")
            raise RuntimeError(
                "无法提交 prompt：键盘 Enter、输入框 Enter 事件均无效，"
                "且未找到发送按钮。"
            )

    @staticmethod
    def _clamp_prompt(prompt: str) -> str:
        """发送侧最后一道护栏：把整段 prompt 压到输入框能承受的字符上限内。

        ChatGPT 网页版 composer 有字符上限，超出后 Playwright ``fill`` 会超时
        （ElementHandle.fill: Timeout 30000ms exceeded）。这里保留**头部**
        （系统/工具说明、任务目标通常在前）与**尾部**（最新用户指令）各一半，
        中间截断并标注，保证最新指令一定送达。
        """
        limit = config.PROMPT_MAX_CHARS
        if not limit or len(prompt) <= limit:
            return prompt
        head = limit // 2
        tail = limit - head
        dropped = len(prompt) - limit
        return (
            prompt[:head]
            + f"\n\n…（prompt 过长，已省略中间 {dropped} 字符）\n\n"
            + prompt[-tail:]
        )
