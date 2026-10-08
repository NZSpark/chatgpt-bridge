"""Generation polling and completion waiting helpers (PI-026)."""

import asyncio
import logging
import time

from . import config
from .logging_setup import set_log_context
from .completion.generator import EndState, build_end_limits, evaluate_poll
from .errors import DEFAULT_SESSION_KEY, ChatGPTContextLimitError, ChatGPTTimeoutError
from .metrics import metrics
from .prompting import _delta_piece, estimate_tokens

logger=logging.getLogger("chatgpt_web.chat_io")
_EMPTY_NODE_REPORT_AFTER=3


class ReplyWaiterMixin:
    """Own the browser generation polling loop while keeping driver APIs intact."""

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
        lines, page_text_len = await self.dom.reply_diagnostics(page)
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
        page_id = self._page_ids.get(bucket, f"page-{id(page):x}" if page is not None else "-")
        state = self._state(bucket)
        # 默认所有桶共用 self.lock（串行）；只有 PARALLEL_BUCKETS=true 才按桶各持一把锁
        set_log_context(session_key=bucket, page_id=page_id)
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
                before_nodes = await self.dom.find_assistant_messages(page)
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
            generation_started_at = time.monotonic()

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
            end_limits = build_end_limits()
            saw_generating = end_state.saw_generating
            # 零节点诊断：连续 N 轮才打印，且只打印一次（poll 间隔 1.5s）
            empty_node_polls = 0
            empty_nodes_reported = False

            while True:
                poll += 1
                responses = await self.dom.find_assistant_messages(page)
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
                            metrics.observe("browser_generation_latency", time.monotonic() - generation_started_at)
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
                    metrics.inc("browser_selector_miss_total")
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
                        metrics.observe("browser_generation_latency", time.monotonic() - generation_started_at)
                        raise self._context_limit_error()
                    metrics.observe("browser_generation_latency", time.monotonic() - generation_started_at)
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
                        metrics.observe("browser_generation_latency", time.monotonic() - generation_started_at)
                        raise self._context_limit_error()
                    metrics.observe("browser_generation_latency", time.monotonic() - generation_started_at)
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
            extraction_started_at = time.monotonic()
            try:
                extracted_blocks = await self._extract_code_blocks(latest_node)
            except Exception:
                metrics.inc("reply_extraction_failure_total")
                metrics.observe("reply_extraction_latency", time.monotonic() - extraction_started_at)
                raise
            metrics.observe("reply_extraction_latency", time.monotonic() - extraction_started_at)
            metrics.observe("browser_generation_latency", time.monotonic() - generation_started_at)

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
