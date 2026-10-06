"""发送 / 轮询 / 提取（``ChatIOMixin``）。

负责：把一条 prompt 送进网页输入框、轮询等待回复完成、提取代码块，
并在结束（或超时）后更新会话状态。重试阶梯与播种逻辑都在这里。
"""


import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from . import config
from .end_detection import EndLimits, EndState, evaluate_poll
from .errors import (
    DEFAULT_SESSION_KEY,
    ChatGPTBusyError,
    ChatGPTContextLimitError,
    ChatGPTTimeoutError,
)
from .prompting import _delta_piece, estimate_tokens
from .task_state import TaskState, TaskStateName

logger = logging.getLogger(__name__)

# 「RESPONSE_SELECTORS 一个节点都没命中」连续多少轮才打印诊断。
# 必须 >1：助手节点总是晚于提交一帧出现（正常首轮也会 nodes=0 + generating），
# 只有**持续**零节点才说明选择器失效（网页版改版）。
_EMPTY_NODE_REPORT_AFTER = 3

def _prune_output_dir(output_dir: str) -> None:
    """按 config 的保留策略清理落盘目录（0 = 不限，出错静默忽略）。"""
    max_files = config.OUTPUT_MAX_FILES
    max_age_days = config.OUTPUT_MAX_AGE_DAYS
    if not max_files and not max_age_days:
        return
    try:
        entries = [p for p in Path(output_dir).iterdir() if p.is_file()]
    except Exception:
        return
    now = time.time()
    for path in entries:
        try:
            if max_age_days and now - path.stat().st_mtime > max_age_days * 86400:
                path.unlink()
        except Exception:
            continue
    if max_files:
        try:
            remaining = sorted(
                (p for p in Path(output_dir).iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            for path in remaining[: max(0, len(remaining) - max_files)]:
                try:
                    path.unlink()
                except Exception:
                    continue
        except Exception:
            return


class ChatIOMixin:
    async def send_chat(
        self,
        prompt: str,
        on_delta=None,
        seeded_prompt: Optional[str] = None,
        key: Optional[str] = None,
        validate_reply: Optional[Callable[[str], bool]] = None,
    ) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应。

        :param prompt: 增量 prompt（网页会话已有上下文时使用）
        :param seeded_prompt: 带完整历史的“播种”prompt（需要新开会话时使用，
                              未提供则退回 ``prompt``）
        :param key: 会话桶标识（按任务隔离会话）。不同 key 各自持有一条独立
                    的网页会话与页面，互不污染上下文；None 表示默认桶。
        :param validate_reply: 可选「回复是否可接受」判定；返回 False 时会
                    在同一会话追发一次工具纠偏指令（仅一次，见 T1.1）。
                    调用方需自行保证此时未把首轮回复流式发给客户端。
                    **传 None 即表示不追发任何 prompt**：任务已进入执行阶段后，
                    无指令的纯文本回复就是收尾，桥不得自行再推模型（见
                    ``prompting.tool_nudge_predicate`` 与 update.md §2.12）。

        **重试阶梯**（本项目不做 URL 恢复，重试即重开对话 + 播种）：

        1. 首级：直接用现有会话（若已达体积预算或上次到顶，先轮转到新会话）；
        2. 中间级：退避后重试同一个页面；
        3. 最高一级：**重开新对话 + 用播种 prompt 重放历史**。

        页面完全没有新回复（超时）最常见的原因是会话已到顶 / 已失效；
        由于我们无法可靠回填会话 URL，与其重开同一个会话，不如直接新开 + 播种。
        """
        bucket = key or DEFAULT_SESSION_KEY
        seeded = seeded_prompt or prompt
        max_attempts = max(1, config.MAX_UPSTREAM_RETRIES)
        last_error: Optional[RuntimeError] = None
        task_state = TaskState()

        # 额外的会话桶需要自己的页面（默认桶就是 self.page，不涉及创建）
        await self._ensure_page(bucket)

        for attempt in range(1, max_attempts + 1):
            state = self._state(bucket)
            if state.pending_rotation:
                # 体积超预算或上次检测到“到顶”：先轮转，再播种
                await self._start_new_session(bucket)
            elif attempt == 1:
                pass
            elif attempt < max_attempts:
                # 不做 URL 恢复：退避后在同一页面重试一次（页面可能只是慢）
                logger.warning(f"[恢复] 第 {attempt}/{max_attempts} 次重试：等待后重试……")
                await asyncio.sleep(config.RETRY_BACKOFF_S * attempt)
            else:
                logger.warning("[恢复] 重试无效，改为开启新对话并重放历史……")
                await self._start_new_session(bucket)

            # 会话是新开的（或被轮转过）-> 必须播种，否则模型收不到任何上下文
            active_prompt = seeded if not self._state(bucket).has_history else prompt
            # 防死循环：若这个会话桶连续多次“到顶”，说明播种内容仍超过网页版
            # 能承受的上下文。此时把播种 prompt 进一步压缩（保留头部说明 + 尾部
            # 最近内容），否则会陷入「到顶→失败→下轮仍播种→再到顶」的死循环。
            active_prompt = self._shrink_seed_if_repeated_cap(
                bucket, active_prompt, is_seed=not self._state(bucket).has_history
            )
            # 记录真正要发出的那份 prompt，供上层估算 usage（按桶隔离，避免并发串台）
            self._last_prompts[bucket] = active_prompt

            if task_state.state in (TaskStateName.RECEIVED, TaskStateName.SESSION_RECOVERY):
                task_state.transition(TaskStateName.PROMPT_BUILT)
            task_state.transition(TaskStateName.MODEL_GENERATING)

            await self._remember_session(bucket)
            try:
                reply_text, blocks = await self._send_chat_locked(
                    active_prompt, on_delta, key=bucket
                )
            except ChatGPTContextLimitError as exc:
                # 到顶了：下次不要再恢复同一个会话，直接轮转
                last_error = exc
                if attempt < max_attempts:
                    task_state.transition(TaskStateName.SESSION_RECOVERY, error=str(exc))
                else:
                    task_state.context_limit(str(exc))
                state = self._state(bucket)
                state.pending_rotation = True
                state.cap_failures = getattr(state, "cap_failures", 0) + 1
                if state.cap_failures >= 2:
                    logger.warning(
                        f"[恢复] 第 {attempt}/{max_attempts} 次失败：会话已达上下文上限"
                        f"（连续 {state.cap_failures} 次，下次将压缩播种内容）。"
                    )
                else:
                    logger.warning(f"[恢复] 第 {attempt}/{max_attempts} 次失败：会话已达上下文上限。")
            except ChatGPTTimeoutError as exc:
                # 只有「超时 / 到顶」才可重试；找不到输入框、profile 被占用等不可重试
                last_error = exc
                if attempt < max_attempts:
                    task_state.transition(TaskStateName.SESSION_RECOVERY, error=str(exc))
                else:
                    task_state.timeout(str(exc))
                self._state(bucket).last_error = str(exc)
                logger.warning(f"[恢复] 第 {attempt}/{max_attempts} 次失败：等待回复超时。")
            else:
                # 回复达标（或调用方没给判定）——直接返回
                if validate_reply is None or validate_reply(reply_text):
                    task_state.complete()
                    return reply_text, blocks
                # T1.1 纠偏：模型完全无视了工具、直接凭知识作答。此时把它当最终答案
                # 返回，客户端（Pi/Codex）会误以为任务已经完成。这里在同一会话
                # 追发一次短纠偏指令（只一次），让它重新输出结构化 TOOL_CALL。
                # 注意：调用方（`prompting.tool_nudge_predicate`）只在**本轮任务还
                # 没调用过任何工具**时才会传判定进来；任务已进入执行阶段后的纯文本
                # 回复是**收尾**，不会走到这里——否则等于把结论重新推成一条新命令，
                # 模型只能继续下指令，任务永远结束不了（用户实测，见 update.md §2.12）。
                from .toolcalls import format_tool_retry_nudge

                logger.warning(
                    "[工具纠偏] 本轮尚未调用过任何工具，回复里没有工具调用——"
                    "追加一次纠偏指令重发（仅一次）。"
                )
                try:
                    # 纠偏是会话内的后续消息：不更新 _last_prompts（usage 仍按
                    # 客户端真正发来的 prompt 估算），也不实时吐字（首轮内容已
                    # 可能发给客户端，避免两段文本拼接错乱）。
                    retry_text, retry_blocks = await self._send_chat_locked(
                        format_tool_retry_nudge(), None, key=bucket
                    )
                except (ChatGPTTimeoutError, ChatGPTContextLimitError,
                        ChatGPTBusyError) as exc:
                    logger.warning(f"[工具纠偏] 重发失败（{exc!r}），返回原回复。")
                    task_state.complete()
                    return reply_text, blocks
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[工具纠偏] 重发异常（{exc!r}），返回原回复。")
                    task_state.complete()
                    return reply_text, blocks
                task_state.complete()
                return retry_text, retry_blocks

        if last_error is not None:
            raise last_error
        raise RuntimeError("上游请求未能发送")

    def _shrink_seed_if_repeated_cap(
        self, bucket: str, prompt: str, is_seed: bool
    ) -> str:
        """连续“到顶”时把播种 prompt 压得更小，避免陷入死循环。

        背景：会话没有历史（``has_history=False``）时每轮都必须播种。若播种内容
        本身就超过网页版能承受的上下文，就会出现「播种→到顶→失败→下轮仍播种」
        的循环，``has_history`` 永远无法置 True。

        这里在 ``cap_failures >= 2`` 后逐步压缩：保留头部说明（模型需要知道
        工具约定与上下文重建头）与尾部最近内容（真实任务通常在最后），砍掉中段。
        阈值 ``2`` 与下限由常量控制；非播种 prompt（增量）不受影响。
        """
        if not is_seed:
            return prompt
        failures = getattr(self._state(bucket), "cap_failures", 0)
        if failures < 2:
            return prompt
        # 每次连续失败进一步收紧：2 次 -> 半量，3 次 -> 1/4，最低 2000 字符
        keep = max(2000, len(prompt) // (2 ** (failures - 1)))
        if len(prompt) <= keep:
            return prompt
        head = keep // 2
        tail = keep - head
        marker = "\n\n…（播种内容因连续到顶已压缩中段，仅保留开头与最近内容）…\n\n"
        shrunk = prompt[:head] + marker + prompt[-tail:]
        logger.warning(
            f"[恢复] 会话连续到顶 {failures} 次，播种 prompt 压缩："
            f"{len(prompt)} -> {len(shrunk)} 字符。"
        )
        return shrunk

    @staticmethod
    def _strip_code_noise(code_content: str, lang: str) -> str:
        """剥离 ChatGPT 代码块界面噪声，但保留首尾空白。

        旧实现直接 .strip()，会无条件抹掉开头/结尾的空白与空行。
        对 README.md 这类要按原文匹配再改的文件是致命的：
        首行空行、末尾换行被删后，edit 工具逐字节匹配就会失败。

        这里只去掉头部的语言标签行与 Copy/Download 行，
        正文的空白、空行、首尾换行全部原样保留。
        """
        text = code_content.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        lang_alt = re.escape(lang) if lang else r"[A-Za-z0-9_+#.-]*"
        lang_line_re = re.compile(
            r"^\s*(?:" + lang_alt + r"|bash|shell|sh|python|py|json|html|javascript|js|"
            r"typescript|ts|css|sql|go|rust|java|cpp|c|markdown|md|txt)\s*$",
            re.IGNORECASE,
        )
        copy_line_re = re.compile(
            r"^\s*(?:" + lang_alt + r"|bash|shell|sh|python|py|json|html|javascript|js)?"
            r"\s*(?:Copy|Download)\s*$",
            re.IGNORECASE,
        )
        # 头部：允许先跳过空行，再剥语言标签 / Copy 行；
        # 一旦遇到第一行正文就停，避免误删正文里同名的行。
        start = 0
        while start < len(lines):
            line = lines[start]
            if not line.strip():
                start += 1
                continue
            if lang_line_re.match(line) or copy_line_re.match(line):
                start += 1
                continue
            break
        end = len(lines)
        while end > start:
            last = lines[end - 1]
            if last.strip() and copy_line_re.match(last):
                end -= 1
                continue
            break
        return "\n".join(lines[start:end])

    async def _extract_code_blocks(self, element) -> List[dict]:
        """从某条回复的 DOM 节点中提取代码块（语言 + 纯代码文本）。"""
        extracted: List[dict] = []
        if element is None:
            return extracted
        code_elements = await element.query_selector_all(config.CODE_BLOCK_SELECTOR)
        for code_el in code_elements:
            code_tag = await code_el.query_selector(config.CODE_TAG_SELECTOR)
            lang = "txt"
            if code_tag:
                # 新版 DOM：代码正文是 CodeMirror 的 div.cm-content，语言写在
                # data-language（如 "python"）；旧版 DOM：<code class="language-python">。
                lang_attr = (await code_tag.get_attribute('data-language') or "").strip()
                if lang_attr:
                    lang = lang_attr.lower()
                else:
                    class_attr = await code_tag.get_attribute('class') or ""
                    lang_match = re.search(r'language-(\w+)', class_attr)
                    if lang_match:
                        lang = lang_match.group(1)

            code_content = await self._complete_text(code_tag or code_el)
            clean_code = self._strip_code_noise(code_content, lang)

            extracted.append({"lang": lang, "code": clean_code})
        return extracted

    # 读取回复节点完整文本的 JS：ChatGPT 把流式回复按 token 渲染成一串
    # <span class="animating">，带逐字显现动画。Playwright 的 inner_text() 遵循
    # **渲染后**可见性，动画未走完的 token 取不到——表现为文本在引号/冒号处被截断
    # （TOOL_CALL 的 JSON 参数被切掉半截）。这里在克隆节点上移除动画类与 animation
    # 样式，挂到屏幕外再读 innerText，既拿到完整文本，又保留块级换行。
    # 快速探测：节点内是否还有未显现的 token（动画类）。没有时 inner_text
    # 已经是完整的，无需克隆节点（克隆 + 屏幕外挂载是明显的额外开销，T4.2）。
    _ANIMATED_JS = (
        "(n) => !!n.querySelector('.animating, .pending, .revealing, .fade-in')"
    )

    _COMPLETE_TEXT_JS = """
    (node) => {
      const clone = node.cloneNode(true);
      clone.querySelectorAll('.animating, .pending, .revealing, .fade-in')
        .forEach(e => e.classList.remove('animating', 'pending', 'revealing', 'fade-in'));
      clone.querySelectorAll('[style]').forEach(e => {
        e.style.animation = 'none';
        e.style.opacity = '1';
        e.style.visibility = 'visible';
        e.style.filter = 'none';
        e.style.transform = 'none';
      });
      const holder = document.createElement('div');
      holder.style.position = 'absolute';
      holder.style.left = '-99999px';
      holder.style.top = '0';
      holder.appendChild(clone);
      document.body.appendChild(holder);
      const text = clone.innerText || clone.textContent || '';
      holder.remove();
      return text;
    }
    """

    async def _complete_text(self, node) -> str:
        """读取回复节点的完整文本（绕过 ChatGPT 逐 token 显现动画导致的截断）。

        T4.2 优化：先探测节点里是否真的存在 ``.animating/.pending/.revealing``。
        没有动画时 ``inner_text`` 已经完整，直接返回——避开「克隆整棵子树 +
        屏幕外挂载」这些为绕过逐 token 动画才需要的昂贵操作（轮询期间每轮都跑）。

        失败时退回 text_content（无块级换行但一定完整），再退回 inner_text。
        """
        if node is None:
            return ""
        animated: Optional[bool]
        try:
            animated = bool(await node.evaluate(self._ANIMATED_JS))
        except Exception:
            animated = True  # 探测失败时保守走克隆路径
        if not animated:
            try:
                text = await node.inner_text()
                if text and text.strip():
                    return text
            except Exception:
                pass
        try:
            text = await node.evaluate(self._COMPLETE_TEXT_JS)
            if text and text.strip():
                return text
        except Exception:
            pass
        try:
            text = await node.text_content()
            if text and text.strip():
                return text
        except Exception:
            pass
        try:
            return await node.inner_text()
        except Exception:
            return ""

    async def _has_pending_tokens(self, node) -> bool:
        """回复节点里是否还有尚未显现的 token（span.pending 等）。

        ChatGPT 流式渲染时，未显现 token 带 .pending / .animating 类，
        虽已进入 DOM 但 inner_text 取不到。停止按钮消失不代表这些 token
        已经显现完毕——若此时收尾，会拿到被截断的半截 JSON。
        """
        if node is None:
            return False
        try:
            return bool(await node.evaluate(
                "(n) => !!n.querySelector('.pending, .animating')"
            ))
        except Exception:
            return False

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
        """兜底：对发送按钮派发 DOM click（同样不碰 OS 焦点）。"""
        if page is None:
            return False
        for selector in config.SEND_BUTTON_SELECTORS:
            try:
                button = await page.query_selector(selector)
                if not button:
                    continue
                await button.dispatch_event("click")
                return True
            except Exception:
                continue
        return False

    async def _find_input(self, page):
        """按 INPUT_SELECTORS 回退链定位输入框；找不到返回 None。

        调试要点：这里所有失败都必须**打印**，否则 _fill_prompt 报
        "多次重试后仍为空" 时完全无法区分是「定位不到输入框」还是
        「fill 后读到空」。真实环境的 composer 可能是 shadow DOM /
        iframe，或选择器全部落空。
        """
        errors = []
        for selector in config.INPUT_SELECTORS:
            try:
                # 关键：用 state="attached" 而非默认的 "visible"。
                # 实测 ChatGPT 的 ProseMirror composer（div#prompt-textarea）
                # 经常被判定为 not visible（y 为大幅负值、高度异常），
                # 用默认 "visible" 会永远等不到、直接超时。
                el = await page.wait_for_selector(
                    selector, timeout=2000, state="attached"
                )
                if el:
                    if config.DEBUG:
                        logger.debug(f"[输入] 命中选择器：{selector}")
                    return el
                errors.append(f"{selector}: 未命中")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{selector}: {exc}")
                continue
        logger.warning("[输入] 未找到输入框，尝试过的选择器：")
        for line in errors:
            logger.info(f"        - {line}")
        return None

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
        await ChatIOMixin._clear_input(page, chat_input)
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
            if not (await ChatIOMixin._read_input_text(chat_input)).strip():
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
        leftover = (await ChatIOMixin._read_input_text(chat_input)).strip()
        if leftover:
            logger.warning(
                f"[输入] 警告：清空输入框后仍读到残留内容（{leftover[:80]!r}），"
                "新 prompt 可能被拼接。"
            )

    async def _submit_prompt(self, page, chat_input) -> None:
        """提交 prompt：真实键盘 Enter → 合成事件 → 点发送按钮。

        首选真实键盘 Enter：合成 KeyboardEvent 对 ProseMirror 不可靠，
        prompt 含换行时合成 Enter 会被当成软换行而非提交（见 _keyboard_enter）。
        """
        if await self._keyboard_enter(page):
            return
        if await self._dispatch_enter(chat_input):
            return
        if not await self._click_send_button(page):
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

    async def _log_empty_reply_nodes(self, page) -> None:
        """回复节点持续为 0 时，打印每条 RESPONSE_SELECTORS 的命中数。

        背景（2026-10-06 线上回归）：网页版改版后助手回复换了容器，
        ``data-message-author-role`` / ``message-content`` / ``.markdown`` 全部落空。
        此时轮询表现为 ``nodes=0`` 一路空转到超时，日志里**没有任何线索**——
        无法区分「选择器失效」与「消息根本没发出去」。

        这里在首次出现「零节点 + 页面仍在生成」时打印一次逐条命中数：
        全为 0 基本可判定是网页版改版（需要重新校准选择器），
        而选择器有命中却不等于回复，则是渲染/时序问题。
        """
        lines: List[str] = []
        for selector in config.RESPONSE_SELECTORS.split(","):
            selector = selector.strip()
            if not selector:
                continue
            try:
                count = len(await page.query_selector_all(selector))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{selector}: {exc!r}")
                continue
            lines.append(f"{selector}: {count}")
        try:
            page_text_len = await page.evaluate(
                "() => (document.body ? document.body.innerText.length : 0)"
            )
        except Exception:  # noqa: BLE001
            page_text_len = -1
        logger.warning(
            "[诊断] 页面仍在生成，但 RESPONSE_SELECTORS 一个回复节点都没命中"
            "（nodes=0）：网页版可能已改版，助手回复换了容器。逐条命中数："
        )
        for line in lines:
            logger.info(f"        - {line}")
        logger.info(
            f"        页面可见文本长度={page_text_len}。"
            "请按此重新校准 .env 的 RESPONSE_SELECTORS（或 curl /_debug/selectors）。"
        )

    async def _send_chat_locked(self, prompt: str, on_delta=None,
                                key: Optional[str] = None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应及提取的代码块。

        :param on_delta: 可选异步回调，生成过程中实时吐出增量文本（用于 SSE 流式）。
        :param key: 会话桶标识（决定使用哪一条页面）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        page = self._page_for(bucket)
        state = self._state(bucket)
        # 默认所有桶共用 self.lock（串行）；只有 PARALLEL_BUCKETS=true 才按桶各持一把锁
        async with self._session_lock(bucket):
            if page is None:
                raise RuntimeError("浏览器尚未初始化：找不到可用于发送的会话页面。")
            self._touch_page(bucket)  # 正在用的页面不会被空闲回收 / LRU 淘汰

            # 1. 定位输入框。只做 DOM 查询与重试，绝不 bring_to_front / focus：
            #    那会抢 OS 前台、干扰用户正在使用的其它窗口；而提交走页面内
            #    事件派发（见 _submit_prompt），本就不依赖窗口是否在前台。
            chat_input = await self._find_input(page)
            if not chat_input:
                raise RuntimeError("无法找到对话输入框，请检查 ChatGPT 网页是否打开或处于登录状态。")

            # 记录发送前最后一条回复的文本，用来判断“新回复是否已经出现”。
            # 注意：绝不能用“回复节点数量变多”来判断。
            # ChatGPT 的消息列表会回收/替换节点，长会话下节点数可能恒为 2，
            # 新回复只会把旧节点内容改掉而不会让数量增长，
            # 那样会导致永远读不到本轮回复直接等到超时。
            before_text = ""
            before_count = 0
            try:
                before_nodes = await page.query_selector_all(config.RESPONSE_SELECTORS)
                before_count = len(before_nodes)
                if before_nodes:
                    before_text = (await self._complete_text(before_nodes[-1])).strip()
            except Exception:
                before_text = ""

            prompt = self._clamp_prompt(prompt)
            # 关键顺序：必须在填充 prompt **之前**选中「思考模式」。
            # 网页版的模式选择是 composer 上的 pill，只有在发送前处于选中态，
            # 这一轮才会以思考模型作答。若放在发送之后再选，只影响下一轮（或无效）。
            # 这里每次都确保一次（已选中则跳过），覆盖新 bucket / 轮转 / 复用全部路径。
            await self._select_think_mode(page)
            # 用带「重新定位 + 非空校验」的重试填充，规避 React 重挂载后
            # 旧句柄失效导致的 fill 超时（新会话首轮尤其常见）。
            filled = await self._fill_prompt(page, prompt)
            if filled is None:
                raise RuntimeError(
                    "填充输入框失败（多次重试后仍为空或 fill 超时）。"
                    "请检查登录状态与 INPUT_SELECTORS 配置。"
                )
            chat_input = filled
            await self._submit_prompt(page, chat_input)

            # 2. 轮询等待回复完成
            await asyncio.sleep(config.POLL_INTERVAL_S)
            last_text = ""
            last_normalized = ""
            last_len = -1
            streamed = ""            # 已经通过 on_delta 发给客户端的内容
            latest_node = None          # 本轮最新的回复节点
            poll = 0
            deadline = asyncio.get_running_loop().time() + config.RESPONSE_TIMEOUT_S
            # 总超时到点但页面仍在生成时，允许延长等待的剩余额度（秒）。
            # 用光后不再延长，仍按超时处理，避免无限等待。
            extend_budget = max(0.0, config.RESPONSE_TIMEOUT_EXTEND_S)
            extended_printed = False

            cap_check_every = max(1, config.CAP_CHECK_EVERY)

            # 判定状态与阈值：真正的判定逻辑在纯函数 evaluate_poll 里（T4.2），
            # 这里只维护状态、副作用与日志。
            end_state = EndState()
            end_limits = EndLimits(
                quiet_polls=max(1, int(config.RESUME_QUIET_POLLS)),
                stable_polls=int(config.STABLE_POLLS),
                stall_limit=max(1, int(config.STALL_POLLS)),
                extend_step_s=config.RESPONSE_TIMEOUT_S,
            )
            saw_generating = end_state.saw_generating
            # 零节点诊断：连续 N 轮才打印，且只打印一次（poll 间隔 1.5s）
            empty_node_polls = 0
            empty_nodes_reported = False

            while True:
                poll += 1
                responses = await page.query_selector_all(config.RESPONSE_SELECTORS)
                current_text = ""
                generating = None
                # 取「最后一个有正文的回复节点」而不是裸的 responses[-1]：
                # RESPONSE_SELECTORS 里 div[class*="response"] 之类会命中大量只有
                # 布局、没有文本的容器节点，排在真正的回复节点之后，inner_text()
                # 恒为空——若直接取 [-1] 会永远读到空串，导致轮询空转到超时。
                for node in reversed(responses):
                    try:
                        node_text = await self._complete_text(node)
                    except Exception:
                        continue
                    if node_text and node_text.strip():
                        latest_node = node
                        current_text = node_text
                        break
                normalized = current_text.strip()

                # 1. 本轮回复是否已经出现。判据（满足其一即可）：
                #    a) 末节点文本 != 发送前文本；
                #    b) 节点数变多（短会话常见）；
                #    c) 已经观测到过「生成中」——这说明本轮确已开始，
                #       此时即使文本暂时等于 before_text（首帧还没渲染完）也算已出现。
                #    注意：不能只看节点数——长会话下新回复会原地替换旧节点，数量不增长。
                #    也不能要求文本非空——选择器可能命中一批尚未渲染出文本的节点
                #    （表现为 nodes 很多但 len=0），那样会永远判不到「已出现」、空转到超时。
                reply_seen = (
                    (bool(normalized) and normalized != before_text)
                    or (len(responses) > before_count)
                    or saw_generating
                )

                # 1.1 还没有新回复时，周期性检查是否“会话到顶”，
                #     并主动探测「生成中」：这是唯一能证明本轮已开始、
                #     但首帧文本尚未渲染出来的信号（否则只能干等到超时）。
                #     到顶与“真的卡住”在外表上完全一样（页面不再产生新回复），
                #     不主动看提示语就只能等到超时，而那时已经分不清原因了。
                # 2.1 探测页面「生成中」状态。True=仍在生成；False=停止按钮已消失。
                pending = False
                if not reply_seen:
                    if poll % cap_check_every == 0:
                        if await self._page_shows_context_limit(bucket):
                            self._mark_context_limit(bucket)
                            raise self._context_limit_error()
                    generating = await self._page_is_generating(bucket)
                else:
                    generating = await self._page_is_generating(bucket)
                    # 停止按钮是否已消失、且无尚未显现的 token。
                    # 注意：**不能据此立即收尾**——ChatGPT 分段输出时停止按钮会
                    # 短暂消失，随后继续吐含 TOOL_CALL 的内容。必须再等静默窗口。
                    pending = await self._has_pending_tokens(latest_node)

                # 1.2 零节点 + 仍在生成：这是「网页版改版导致选择器失效」的典型
                #     指纹（消息确实发出、模型确实在答，只是我们抓不到节点）。
                #     连续若干轮都是 0 才打印（避开首帧未渲染的正常情况），
                #     且只打印一次，避免下次改版又只能从超时反推。
                empty_node_polls = empty_node_polls + 1 if not responses else 0
                if (
                    empty_node_polls >= _EMPTY_NODE_REPORT_AFTER
                    and generating
                    and not empty_nodes_reported
                ):
                    empty_nodes_reported = True
                    await self._log_empty_reply_nodes(page)

                # 判定逻辑全部在纯函数里（生成中不结束 / 停止按钮消失但内容仍在变 /
                # 分段输出恢复 / 静默窗口满足后结束 / 卡死快速失败 / 超时但仍在生成则延长）
                past_deadline = asyncio.get_running_loop().time() > deadline
                verdict = evaluate_poll(
                    end_state,
                    limits=end_limits,
                    reply_seen=reply_seen,
                    generating=generating,
                    pending=pending,
                    normalized=normalized,
                    last_normalized=last_normalized,
                    last_len=last_len,
                    past_deadline=past_deadline,
                    extend_budget=extend_budget,
                    has_partial_text=bool(last_text),
                )
                end_state = verdict.state
                saw_generating = end_state.saw_generating

                if verdict.action == "finish":
                    last_text = current_text
                    if past_deadline:
                        logger.warning("[超时] %s", verdict.note)
                    elif config.DEBUG and verdict.note:
                        logger.debug("[debug] poll=%s %s，判定结束", poll, verdict.note)
                    break

                if verdict.action == "fail_stalled":
                    await self._remember_session(bucket)
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise ChatGPTTimeoutError(
                        f"页面连续 {verdict.state.stalled} 次未产生任何回复内容"
                        "（疑似未登录或会话失效），已提前中止。"
                        "请检查 ChatGPT 登录状态或 RESPONSE_SELECTORS 配置。"
                    )

                if verdict.action == "extend":
                    extend_budget -= verdict.extend_seconds
                    deadline = asyncio.get_running_loop().time() + verdict.extend_seconds
                    if not extended_printed:
                        logger.warning(
                            "[超时] 页面仍在生成，延长等待 %gs（剩余可延长 %gs），不重发 prompt。",
                            verdict.extend_seconds, extend_budget,
                        )
                        extended_printed = True
                    continue

                if verdict.action == "fail_timeout":
                    await self._remember_session(bucket)
                    # 超时前最后确认一次是否“到顶”，否则错误信息会误导排查方向
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise ChatGPTTimeoutError(
                        f"等待 ChatGPT 响应超时（{int(config.RESPONSE_TIMEOUT_S)}s）。"
                    )

                # action == "continue"：本轮尚未结束，继续等下一轮。
                if reply_seen:
                    # 2.3 生成过程中吐出增量，供 SSE 使用。
                    #     用「已发送内容」的公共前缀做 diff，即使节点中途重排也不会漏字
                    if on_delta is not None:
                        piece, streamed = _delta_piece(streamed, current_text)
                        if piece:
                            await on_delta(piece)
                    last_text = current_text
                    last_normalized = normalized
                    last_len = len(normalized)

                if config.DEBUG:
                    logger.debug(
                        "[debug] poll=%s nodes=%s len=%s stable=%s generating=%s saw=%s "
                        "stalled=%s before_len=%s",
                        poll, len(responses), len(normalized), end_state.stable_count,
                        generating, saw_generating, end_state.stalled, len(before_text),
                    )

                await asyncio.sleep(config.POLL_INTERVAL_S)

            # 3. 从最新回复节点中提取代码块
            extracted_blocks = await self._extract_code_blocks(latest_node)

            # 4. 更新会话状态：已建立历史，并累计体积；超预算则下一轮轮转
            state.has_history = True
            state.turns += 1
            state.est_tokens += estimate_tokens(prompt) + estimate_tokens(last_text)
            state.last_error = None
            # 成功建立历史：清空连续到顶计数，恢复正常预算
            state.cap_failures = 0
            if self._session_over_budget(bucket):
                state.pending_rotation = True
                logger.info(
                    f"[轮转] 会话已达预算（轮数={state.turns}，"
                    f"估算 token={state.est_tokens}），"
                    "下一轮将开启新会话并播种上下文。"
                )

            # 成功产生回复后：刷新会话状态（可能刚创建了新会话）并续期页面使用时间
            self._touch_page(bucket)
            await self._remember_session(bucket)
            return last_text, extracted_blocks

    @staticmethod
    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        _prune_output_dir(output_dir)
        saved = []

        ext_map = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "html": "html", "css": "css", "json": "json", "cpp": "cpp",
            "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "markdown": "md"
        }

        # 同一秒内的多个请求会拿到同样的 timestamp，必须再加一段随机后缀，
        # 否则 code_<ts>_1.py / response_<ts>.md 会互相覆盖（多任务并行后很常见）
        unique = uuid.uuid4().hex[:6]

        if code_blocks:
            for idx, block in enumerate(code_blocks, start=1):
                lang = block["lang"].lower().strip()
                code = block["code"]
                ext = ext_map.get(lang, "py" if "import " in code or "def " in code else "txt")

                timestamp = int(time.time())
                filename = f"code_{timestamp}_{idx}_{unique}.{ext}"
                filepath = Path(output_dir) / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                saved.append(str(filepath))
                logger.info(f"[已保存文件] {filepath}")
        else:
            filename = f"response_{int(time.time())}_{unique}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved
