"""发送 / 轮询 / 提取（``ChatIOMixin``）。

负责：把一条 prompt 送进网页输入框、轮询等待回复完成、提取代码块，
并在结束（或超时）后更新会话状态。重试阶梯与播种逻辑都在这里。
"""


import asyncio
import logging
import uuid
from typing import Any, Callable, List, Optional

from . import config
from .browser_input import BrowserInputMixin
from .errors import (
    DEFAULT_SESSION_KEY,
    ChatGPTBusyError,
    ChatGPTComposerError,
    ChatGPTContextLimitError,
    ChatGPTPageLostError,
    ChatGPTTimeoutError,
)
from .logging_setup import set_log_context
from .metrics import metrics
from .page_pool import BucketActivityMixin
from .reply_extractor import ReplyExtractorMixin
from .reply_waiter import ReplyWaiterMixin

# PI-902：结束判定的纯函数状态机与阈值组装统一由 completion 子包 re-export。
from .task_state import TaskState, TaskStateName

logger = logging.getLogger(__name__)

# 「RESPONSE_SELECTORS 一个节点都没命中」连续多少轮才打印诊断。
# 必须 >1：助手节点总是晚于提交一帧出现（正常首轮也会 nodes=0 + generating），
# 只有**持续**零节点才说明选择器失效（网页版改版）。
_EMPTY_NODE_REPORT_AFTER = 3


def _recovery_reason(exc: RuntimeError) -> str:
    """页面层恢复日志里的原因短语。

    两类故障（页面失效 / composer 不接受输入）共用「新开页面后重发」这一个动作，
    但日志与错误文案必须能分开——否则真机上又要把「站点改版」当成「标签被关」。
    """
    if isinstance(exc, ChatGPTComposerError):
        return "composer 不接受输入（页面仍在运行）"
    return "会话页面已失效"


class ChatIOMixin(
    BucketActivityMixin, BrowserInputMixin, ReplyExtractorMixin, ReplyWaiterMixin
):
    #: 在页面内合成 paste 事件，把真实图片文件贴进 contenteditable composer。
    #: Playwright 的 evaluate 只能传可序列化数据，字节必须以 ``list[int]`` 传入，
    #: 页面侧再用 ``new Uint8Array`` 还原（见 doc/image_upload.md §2.1）。
    _PASTE_IMAGE_JS = """
    ([name, mime, bytesArray]) => {
      const el = document.activeElement
        || document.querySelector('[contenteditable="true"]');
      if (!el) return false;
      const file = new File([new Uint8Array(bytesArray)], name, {type: mime});
      const dt = new DataTransfer();
      dt.items.add(file);
      el.dispatchEvent(new ClipboardEvent('paste', {
        clipboardData: dt, bubbles: true, cancelable: true, composed: true,
      }));
      return true;
    }
    """

    async def _focus_composer(self, page, composer) -> None:
        """粘贴前确保输入框获得焦点。

        合成 paste 事件时页面侧用 ``document.activeElement`` 定位编辑器；若不先聚焦，
        焦点可能不在 composer 上，图片会贴到错误的元素。click 失败时退回 JS focus
        （元素被弹层遮挡等情况下 click 不可靠）。失败非致命：由调用方降级。
        """
        if composer is None:
            return
        try:
            await composer.click(timeout=config.FILL_TIMEOUT_MS)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            await composer.evaluate("(el) => el.focus()")
        except Exception:  # noqa: BLE001
            pass

    async def _download_image(self, url: str) -> Optional[bytes]:
        """用浏览器上下文下载远程图片（复用登录态 / cookie）。

        不要用裸 HTTP 客户端：远程图片可能要求与网页会话相同的 cookie / 来源。
        任何失败都返回 None，由调用方降级（只记录日志，不阻断文本提交）。
        """
        context = getattr(self, "context", None)
        if context is None:
            return None
        try:
            response = await context.request.get(
                url, timeout=config.IMAGE_DOWNLOAD_TIMEOUT_MS
            )
            if response.ok:
                return await response.body()
            logger.warning("[图片] 下载失败 status=%s url=%s", response.status, url[:80])
        except Exception as exc:  # noqa: BLE001 下载失败一律降级
            logger.warning("[图片] 下载异常 %s: url=%s", type(exc).__name__, url[:80])
        return None

    async def _paste_images(self, page, composer, images: Optional[List[Any]]) -> int:
        """把真实图片文件以合成 paste 事件粘贴进输入框，返回成功派发的张数。

        任何一步失败（下载失败、focus 失败、事件派发异常）都**只记录日志**，
        不阻断文本提交——文本 prompt 已带 `[图片]` 引用行，最差退回到文本引用语义。
        """
        if not images or not config.IMAGE_PASTE_ENABLED:
            return 0
        pasted = 0
        for image in images:
            data = getattr(image, "data", None)
            if data is None and getattr(image, "url", None):
                data = await self._download_image(image.url)
            if not data:
                continue
            name = getattr(image, "name", "image.png") or "image.png"
            mime = getattr(image, "mime", "image/png") or "image/png"
            try:
                # 粘贴前确保输入框聚焦，否则 document.activeElement 不是编辑器。
                await self._focus_composer(page, composer)
                ok = await page.evaluate(
                    self._PASTE_IMAGE_JS, [name, mime, list(data)]
                )
                if ok:
                    pasted += 1
                    if config.DEBUG:
                        logger.debug("[图片] 已粘贴 %s（%s, %d 字节）", name, mime, len(data))
            except Exception as exc:  # noqa: BLE001 粘贴失败一律降级，不阻断提交
                logger.warning("[图片] 粘贴失败 %s: name=%s", type(exc).__name__, name)
        return pasted

    async def send_chat(
        self,
        prompt: str,
        on_delta=None,
        seeded_prompt: Optional[str] = None,
        key: Optional[str] = None,
        validate_reply: Optional[Callable[[str], bool]] = None,
        images: Optional[List[Any]] = None,
    ) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应。

        :param prompt: 增量 prompt（网页会话已有上下文时使用）
        :param seeded_prompt: 带完整历史的“播种”prompt（需要新开会话时使用，
                              未提供则退回 ``prompt``）
        :param key: 会话桶标识（按任务隔离会话）。不同 key 各自持有一条独立
                    的网页会话与页面，互不污染上下文；None 表示默认桶。
        :param images: 可选「本轮新增」的图片附件（:class:`prompting.ImageAttachment`）。
                    在文本写进输入框之后、提交之前粘进去（见 doc/image_upload.md §3.3）。
                    失败非致命：只告警，不阻断文本提交。
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
        由于我们无法可靠回填会话 URL，与其重开同一个会话，不如直接新开 + 播种
        （例外：用户用 ``/link`` 明确绑定了会话 URL 的桶会回到那条会话，见
        :mod:`chatgpt_web.linking`）。

        **重新播种标记**（``/bridge session reseed``）：标记是从会话状态里读的
        （``pending_reseed``，已落盘），带标记的本轮强制用“播种”prompt；**发送成功**
        才消费并落盘。因此失败 / 重启 / 重试都不会丢掉这个动作，也不会重复应用。

        **页面不可用单独一条支线**（不占重试阶梯额度，也不走「轮转 / 换新会话」分支）：

        * :class:`~chatgpt_web.errors.ChatGPTPageLostError`：标签被用户关掉 /
          渲染进程崩溃或挂起；
        * :class:`~chatgpt_web.errors.ChatGPTComposerError`：页面活着也响应 JS，但
          composer 不接受输入（同一页出现多个 ProseMirror 编辑器 / 弹层遮挡 /
          登录墙）——真机实测**重开页面能修好**这种 DOM 退化（干净加载的同一条会话
          只有一个 composer），因此与页面失效共用同一个恢复动作。

        两者的处置都是「新开一条页面后直接重发」（新页面本身就是空白会话，
        ``has_history=False`` 会让本轮用播种 prompt 重放历史，上下文不丢）。
        否则会把刚恢复的会话又轮转掉，白白丢上下文。

        **终止条件**：句柄失效的重建额度是 ``PAGE_REBUILD_MAX``（默认 1，专设、不跟
        ``MAX_UPSTREAM_RETRIES`` 联动）。额度用尽后**立即**按上游不可用结束，而不是
        退化成普通重试——页面已经没了，重发只会对着同一具尸体报同一个错，而每次
        重建都在真实地新开标签页（浏览器整体不可用时就是建页风暴）。

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
                images=images,
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
        images: Optional[List[Any]] = None,
    ) -> tuple[str, List[dict]]:
        """发送与重试阶梯本体，供 :meth:`send_chat` 调用（见那里的说明）。"""
        bucket = key or DEFAULT_SESSION_KEY
        seeded = seeded_prompt or prompt
        max_attempts = max(1, config.MAX_UPSTREAM_RETRIES)
        # 页面句柄失效的自愈额度：**独立**于重试阶梯（见下面的页面失效分支）。
        # 与 ``CHATGPT_RETRIES`` 解耦的理由：重建页面换的是「可用的浏览器句柄」，
        # 不是重试上游；跟着重试次数放大会在浏览器整体不可用时反复建页。
        # 默认 1 = 句柄丢失只新开一次页面，额度用尽即结束本次请求。
        page_rebuilds_left = max(0, config.PAGE_REBUILD_MAX)
        last_error: Optional[RuntimeError] = None
        task_state = TaskState()

        # 额外的会话桶需要自己的页面；页面已死（标签被关 / 渲染进程崩溃）时会在这里重建
        await self._ensure_page(bucket)
        # 绑定了会话 URL 的桶（``/link``）：发送前先确认页面就停在那条会话上。
        # ChatGPT 网页版有时会自行开启新会话（页面漂移），句柄重建后的新页面也可能
        # 不在目标会话上——不对齐就会把这一轮发进一条陌生的新会话，上下文在网页端丢掉。
        await self._ensure_linked_target(bucket)

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

            # 会话是新开的（或被轮转过）-> 必须播种，否则模型收不到任何上下文。
            # ``/bridge session reseed`` 排队的一次性标记同样强制走播种版：它正是为
            # 「网页会话里还有历史、但与客户端传来的历史已经失配」准备的。
            state = self._state(bucket)
            # getattr：浅替身 / 旧状态对象可能没有这个新字段（同 cap_failures 的处理）
            force_seed = bool(getattr(state, "pending_reseed", False))
            is_seed = force_seed or not state.has_history
            active_prompt = seeded if is_seed else prompt
            # 防死循环：若这个会话桶连续多次“到顶”，说明播种内容仍超过网页版
            # 能承受的上下文。此时把播种 prompt 进一步压缩（保留头部说明 + 尾部
            # 最近内容），否则会陷入「到顶→失败→下轮仍播种→再到顶」的死循环。
            active_prompt = self._shrink_seed_if_repeated_cap(
                bucket, active_prompt, is_seed=is_seed
            )
            # 记录真正要发出的那份 prompt，供上层估算 usage（按桶隔离，避免并发串台）
            self._last_prompts[bucket] = active_prompt

            if task_state.state in (TaskStateName.RECEIVED, TaskStateName.SESSION_RECOVERY):
                task_state.transition(TaskStateName.PROMPT_BUILT)
            task_state.transition(TaskStateName.MODEL_GENERATING)

            await self._remember_session(bucket)
            try:
                # 只在真的带图时才传 images：这样既让生产 driver 收到图片，也不破坏
                # 既有测试里那些只接受 (prompt, on_delta, key) 的 _send_chat_locked 替身。
                locked_kwargs = {"key": bucket}
                if images:
                    locked_kwargs["images"] = images
                reply_text, blocks = await self._send_chat_locked(
                    active_prompt, on_delta, **locked_kwargs
                )
            except (ChatGPTPageLostError, ChatGPTComposerError) as exc:
                # 页面不可继续使用：句柄没了（标签被关 / 渲染进程崩溃 / 挂起），或页面
                # 活着但 composer 不接受输入（DOM 退化 / 弹层遮挡 / 登录墙）。两者都与
                # 不可自愈的「改版 / 未登录」不同：新开一条页面后直接重发，
                # **不进**下面的轮转 / 换新会话分支。
                last_error = exc
                self._state(bucket).last_error = str(exc)
                if page_rebuilds_left <= 0:
                    # 终止条件（``PAGE_REBUILD_MAX``）：只给一次新开页面的机会，额度
                    # 用尽就结束本次请求。**不能**在这里退化成普通重试：页面已经不可用，
                    # 在阶梯里退避重发只会对着同一具尸体报同一个错，
                    # 而每一次重建都在真实地新开标签页（浏览器整体不可用时就是建页风暴）。
                    logger.warning(
                        f"[恢复] {_recovery_reason(exc)}（{exc}），且本请求的页面重建额度"
                        f"已用尽（PAGE_REBUILD_MAX={config.PAGE_REBUILD_MAX}），直接按"
                        "「上游不可用」结束；下一次请求会在 _ensure_page 里惰性重建。"
                    )
                    raise
                page_rebuilds_left -= 1
                # 任务状态机只允许「生成中 → 会话恢复 → 重新构建 prompt」这条链，
                # 下一轮开头的 transition(PROMPT_BUILT/MODEL_GENERATING) 会接着走
                task_state.transition(TaskStateName.SESSION_RECOVERY, error=str(exc))
                logger.warning(
                    f"[恢复] {_recovery_reason(exc)}（{exc}），新开页面后重发"
                    f"（剩余重建额度 {page_rebuilds_left} 次，不占重试阶梯）。"
                )
                try:
                    # force=True：崩溃的页面可能仍报 is_closed()==False，
                    # 恢复路径不能依赖判活，必须无条件重建
                    await self._ensure_page(bucket, force=True)
                except Exception as rebuild_exc:  # noqa: BLE001
                    # 连新页面都建不出来（context 已关 / profile 被占 / 浏览器已退出）：
                    # 上游整体不可用，再进循环只会反复建页 → 立刻结束，并把两个原因都留下。
                    logger.error(f"[恢复] 新开页面失败（{rebuild_exc!r}），放弃恢复：{exc}")
                    raise type(exc)(
                        f"{exc}（新开页面也失败：{rebuild_exc!r}）"
                    ) from rebuild_exc
                # 新页面 = 新的网页会话，状态里的轮转请求已无意义
                self._state(bucket).pending_rotation = False
                rebuild_pending = True
                continue  # 不消耗 attempt
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
                if force_seed:
                    # 一次性语义：播种版已经真正发出去（调用成功）→ 消费这个标记并
                    # 立即落盘，避免进程重启后又重放一遍。同桶并发由会话锁保证：
                    # 不会有两个请求同时消费同一个标记。
                    self._state(bucket).pending_reseed = False
                    await self._remember_session(bucket)
                    logger.info("[命令] 已按排队标记完成一次重新播种，标记已消费。")
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
                from .toolcalls import (
                    format_tool_json_format_nudge,
                    format_tool_retry_nudge,
                )
                from .tools.parser import has_tool_call_syntax_error

                # 两类失败用不同文案：
                #   * 语法不合法（有调用但 JSON 解析不了）——点明换行/引号/逗号怎么转义；
                #   * 完全没调用——要求它别再凭知识作答、直接给出结构化调用。
                # 用通用文案去纠正 JSON 错误是无效的（实测模型会原样重犯同一种语法错），
                # 所以这里必须区分。
                if has_tool_call_syntax_error(reply_text or ""):
                    nudge = format_tool_json_format_nudge()
                    nudge_reason = "json_syntax"
                    logger.warning(
                        "[工具纠偏] 回复里的工具调用 JSON 语法不合法——追加一次"
                        "「严格遵循 JSON 格式重发」的纠偏指令（仅一次）。"
                    )
                else:
                    nudge = format_tool_retry_nudge()
                    nudge_reason = "no_call"
                    logger.warning(
                        "[工具纠偏] 本轮尚未调用过任何工具，回复里没有工具调用——"
                        "追加一次纠偏指令重发（仅一次）。"
                    )
                # 纠偏有效率统计（doc/prompt_tasks.md T5.2）：触发记一条，结果再记
                # 一条 recovered/failed，二者按 reason 配对即可算比率。
                logger.info(
                    "nudge_fired reason=%s bucket=%s", nudge_reason, bucket
                )
                try:
                    # 纠偏是会话内的后续消息：不更新 _last_prompts（usage 仍按
                    # 客户端真正发来的 prompt 估算），也不实时吐字（首轮内容已
                    # 可能发给客户端，避免两段文本拼接错乱）。
                    retry_text, retry_blocks = await self._send_chat_locked(
                        nudge, None, key=bucket
                    )
                except (ChatGPTTimeoutError, ChatGPTContextLimitError,
                        ChatGPTBusyError) as exc:
                    logger.warning(f"[工具纠偏] 重发失败（{exc!r}），返回原回复。")
                    logger.info(
                        "nudge_failed reason=%s bucket=%s cause=%r",
                        nudge_reason, bucket, exc,
                    )
                    task_state.complete()
                    return reply_text, blocks
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[工具纠偏] 重发异常（{exc!r}），返回原回复。")
                    logger.info(
                        "nudge_failed reason=%s bucket=%s cause=%r",
                        nudge_reason, bucket, exc,
                    )
                    task_state.complete()
                    return reply_text, blocks
                # 重发后仍不合格也记 failed（判定函数可能是无状态纯函数，直接复用）。
                recovered = validate_reply is None or validate_reply(retry_text)
                logger.info(
                    "nudge_%s reason=%s bucket=%s",
                    "recovered" if recovered else "failed",
                    nudge_reason, bucket,
                )
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
