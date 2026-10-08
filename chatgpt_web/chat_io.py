"""发送 / 轮询 / 提取（``ChatIOMixin``）。

负责：把一条 prompt 送进网页输入框、轮询等待回复完成、提取代码块，
并在结束（或超时）后更新会话状态。重试阶梯与播种逻辑都在这里。
"""


import asyncio
import logging
import time
import uuid
from typing import Callable, List, Optional

from . import config
from .logging_setup import set_log_context
from .metrics import metrics
from .errors import (
    DEFAULT_SESSION_KEY,
    ChatGPTPageLostError,
    ChatGPTBusyError,
    ChatGPTContextLimitError,
    ChatGPTTimeoutError,
)
# PI-902：结束判定的纯函数状态机与阈值组装统一由 completion 子包 re-export。
from .task_state import TaskState, TaskStateName

logger = logging.getLogger(__name__)

# 「RESPONSE_SELECTORS 一个节点都没命中」连续多少轮才打印诊断。
# 必须 >1：助手节点总是晚于提交一帧出现（正常首轮也会 nodes=0 + generating），
# 只有**持续**零节点才说明选择器失效（网页版改版）。
_EMPTY_NODE_REPORT_AFTER = 3

from .browser_input import BrowserInputMixin
from .page_pool import BucketActivityMixin
from .reply_extractor import ReplyExtractorMixin
from .reply_waiter import ReplyWaiterMixin

class ChatIOMixin(
    BucketActivityMixin, BrowserInputMixin, ReplyExtractorMixin, ReplyWaiterMixin
):
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

        **页面失效（P0-J）单独一条支线**：标签被用户关掉 / 渲染进程崩溃时，
        driver 抛 :class:`~chatgpt_web.errors.ChatGPTPageLostError`。这类故障**不占**
        重试阶梯额度，也不走「轮转 / 换新会话」分支——重建一条新页面后直接重发
        （新页面本身就是空白会话，``has_history=False`` 会让本轮用播种 prompt 重放
        历史，上下文不丢）。否则会把刚恢复的会话又轮转掉，白白丢上下文。

        另外，「有请求在飞」从本方法第一行就登记（可重入计数）：
        ``_ensure_page`` → ``_session_lock`` 之间页面还没有锁保护，空闲回收 /
        LRU 淘汰只跳过「在飞」的桶，否则这段窗口里页面会被别的桶顺手关掉
        （表现成随机 502，P0-L）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        self._mark_bucket_active(bucket)
        try:
            return await self._send_chat_with_ladder(
                prompt,
                on_delta=on_delta,
                seeded_prompt=seeded_prompt,
                key=bucket,
                validate_reply=validate_reply,
            )
        finally:
            self._unmark_bucket_active(bucket)

    async def _send_chat_with_ladder(
        self,
        prompt: str,
        on_delta=None,
        seeded_prompt: Optional[str] = None,
        key: Optional[str] = None,
        validate_reply: Optional[Callable[[str], bool]] = None,
    ) -> tuple[str, List[dict]]:
        """发送与重试阶梯本体，供 :meth:`send_chat` 调用（见那里的说明）。"""
        bucket = key or DEFAULT_SESSION_KEY
        seeded = seeded_prompt or prompt
        max_attempts = max(1, config.MAX_UPSTREAM_RETRIES)
        # 页面失效的自愈额度：重建页面**不占**重试阶梯次数（见下面的页面失效分支）
        page_rebuilds_left = max(1, config.MAX_UPSTREAM_RETRIES)
        last_error: Optional[RuntimeError] = None
        task_state = TaskState()

        # 额外的会话桶需要自己的页面；页面已死（标签被关 / 渲染进程崩溃）时会在这里重建
        await self._ensure_page(bucket)

        attempt = 1              # 已消耗的重试阶梯次数（页面失效重建不消耗）
        rebuild_pending = False  # 上一轮刚重建过页面：下一轮直接重发，不轮转
        while attempt <= max_attempts:
            attempt_id = f"attempt-{uuid.uuid4().hex[:10]}"
            set_log_context(session_key=bucket, attempt_id=attempt_id)
            if attempt > 1:
                metrics.inc("request_retry_total")
            state = self._state(bucket)
            if rebuild_pending:
                # 页面刚重建：新页面已是空白会话（has_history=False → 本轮播种），
                # 这里退避 / 轮转 / 换新会话都只会白等或丢掉刚恢复的上下文
                rebuild_pending = False
                logger.info("[恢复] 页面已重建，直接重发同一会话（不轮转、不退避）。")
            elif state.pending_rotation:
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
            except ChatGPTPageLostError as exc:
                # 页面没了（标签被关 / 渲染进程崩溃）——与「改版 / 未登录」完全不同：
                # 重建一条页面后直接重发，**不进**下面的轮转 / 换新会话分支。
                last_error = exc
                self._state(bucket).last_error = str(exc)
                if page_rebuilds_left > 0:
                    page_rebuilds_left -= 1
                    # 任务状态机只允许「生成中 → 会话恢复 → 重新构建 prompt」这条链，
                    # 下一轮开头的 transition(PROMPT_BUILT/MODEL_GENERATING) 会接着走
                    task_state.transition(TaskStateName.SESSION_RECOVERY, error=str(exc))
                    logger.warning(
                        f"[恢复] 会话页面已失效（{exc}），重建页面后重发"
                        f"（剩余重建额度 {page_rebuilds_left} 次，不占重试阶梯）。"
                    )
                    # force=True：崩溃的页面可能仍报 is_closed()==False，
                    # 恢复路径不能依赖判活，必须无条件重建
                    await self._ensure_page(bucket, force=True)
                    # 新页面 = 新的网页会话，状态里的轮转请求已无意义
                    self._state(bucket).pending_rotation = False
                    rebuild_pending = True
                    continue  # 不消耗 attempt
                if attempt < max_attempts:
                    task_state.transition(TaskStateName.SESSION_RECOVERY, error=str(exc))
                attempt += 1
                continue
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
                attempt += 1
                continue
            except ChatGPTTimeoutError as exc:
                # 只有「超时 / 到顶」才可重试；找不到输入框、profile 被占用等不可重试
                last_error = exc
                if attempt < max_attempts:
                    task_state.transition(TaskStateName.SESSION_RECOVERY, error=str(exc))
                else:
                    task_state.timeout(str(exc))
                self._state(bucket).last_error = str(exc)
                logger.warning(f"[恢复] 第 {attempt}/{max_attempts} 次失败：等待回复超时。")
                attempt += 1
                continue
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
