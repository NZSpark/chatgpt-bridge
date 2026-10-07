"""把一次上游对话编码成 OpenAI 兼容的 SSE 流。"""


import asyncio
import json
import logging
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

from . import config
from .driver import ChatGPTBusyError, ChatGPTContextLimitError, ChatGPTTimeoutError
from .events import AssistantTextDelta, ToolCall, completion_events
from .models import ChatCompletionRequest, ChatMessage
from .prompting import build_prompt, estimate_tokens, tool_nudge_predicate
from .protocol_adapters import chat_sse_choice_for_event, responses_function_call_arguments
from .toolcalls import (
    EDIT_MARKDOWN_TOOL_NAME,
    parse_reply_tool_calls,
    run_local_edit_markdown,
)

logger = logging.getLogger(__name__)

def _chunk_text(text: str, size: int = 64) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


async def _stream_chat_completion(
    request: ChatCompletionRequest,
    prompt: str,
    driver,
    seeded_prompt: Optional[str] = None,
    session_key: Optional[str] = None,
    auto_local_edit_markdown: bool = False,
    task_block: Optional[str] = None,
):
    """以 OpenAI SSE 格式输出 chunk，兼容 Pi 的 openai-completions 流式解析。

    ``driver`` 由调用方（server）注入，避免与本模块形成循环依赖。
    ``seeded_prompt`` 在需要轮转到新会话时使用（重放历史）。
    ``session_key`` 为按任务隔离会话的桶（None = 默认桶）。
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = request.model
    wants_tools = bool(request.tools) and request.tool_choice != "none"

    def encode(delta: Optional[Dict[str, Any]], finish: Optional[str] = None,
               choices: Optional[list] = None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices if choices is not None else [
                {"index": 0, "delta": delta or {}, "finish_reason": finish}
            ],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 先发 role 头
    yield encode({"role": "assistant"})

    queue: "asyncio.Queue[tuple]" = asyncio.Queue()

    async def on_delta(piece: str):
        await queue.put(("delta", piece))

    async def runner():
        try:
            working_messages = list(request.messages)
            current_prompt = prompt
            current_seeded_prompt = seeded_prompt
            local_rounds = 0

            while True:
                reply, blocks = await driver.send_chat(
                    current_prompt,
                    on_delta=None if wants_tools else on_delta,
                    seeded_prompt=current_seeded_prompt,
                    key=session_key,
                    validate_reply=tool_nudge_predicate(
                        working_messages, request.tools, request.tool_choice
                    ),
                )

                parsed_tool_calls = parse_reply_tool_calls(reply, request.tools) if wants_tools else []
                bridge_events = completion_events(reply, parsed_tool_calls)
                bridge_tool_calls = [event for event in bridge_events if isinstance(event, ToolCall)]
                local_events = (
                    [event for event in bridge_tool_calls if event.name == EDIT_MARKDOWN_TOOL_NAME]
                    if auto_local_edit_markdown else []
                )
                external_events = [event for event in bridge_tool_calls if event not in local_events]

                # 客户端声明的工具调用保持原来的 tool-call 路径；若同轮混合出现两者，
                # 不擅自执行本地调用，整轮交给客户端处理。
                if external_events or not local_events:
                    await queue.put(("done", (reply, blocks, None, None)))
                    return

                local_rounds += 1
                if local_rounds > 4:
                    raise RuntimeError("本地 edit_markdown 连续执行超过 4 轮，停止自动继续。")

                local_calls = [
                    {
                        "id": event.tool_call_id,
                        "name": event.name,
                        "arguments": event.arguments,
                    }
                    for event in local_events
                ]
                executed = await asyncio.to_thread(
                    run_local_edit_markdown,
                    local_calls,
                    session_key=session_key,
                )

                working_messages.append(ChatMessage(role="assistant", content=reply))
                for call in executed:
                    working_messages.append(ChatMessage(
                        role="tool",
                        content=json.dumps(call.get("result"), ensure_ascii=False),
                        tool_call_id=str(call.get("id") or ""),
                    ))

                current_prompt = build_prompt(
                    working_messages, request.tools, request.tool_choice
                )
                current_seeded_prompt = build_prompt(
                    working_messages,
                    request.tools,
                    request.tool_choice,
                    seed=True,
                    seed_max_chars=config.SEED_MAX_CHARS,
                    task_block=task_block,
                )
                if not current_prompt:
                    raise ValueError("本地 edit_markdown 执行后没有可继续生成的 prompt")
                if driver.needs_seed(session_key):
                    current_prompt = current_seeded_prompt
        except ChatGPTContextLimitError as exc:
            # 给客户端一个可区分的类型，而不是笼统的 server_error
            logger.error("\n[ERR] 网页会话已达上下文长度上限:", exc_info=True)
            await queue.put(("done", (None, [], str(exc), "context_length_exceeded")))
        except ChatGPTBusyError as exc:
            # 本地排队保护：同一会话桶已有请求在跑且等锁超时，对应 HTTP 503 / upstream_busy
            logger.warning(f"\n[繁忙] {exc}")
            await queue.put(("done", (None, [], str(exc), "upstream_busy")))
        except ChatGPTTimeoutError as exc:
            # 与 server.py 的非流式分支保持一致：超时是 504/timeout，而不是 500
            logger.error("\n[ERR] 等待 ChatGPT 回复超时（已重试）:", exc_info=True)
            await queue.put(("done", (None, [], str(exc), "timeout")))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc), "server_error")))

    task = asyncio.create_task(runner())

    streamed = False
    reply_content = ""
    error: Optional[str] = None
    error_type = "server_error"
    keepalives = 0

    keepalive_s = config.CHAT_KEEPALIVE_S
    while True:
        try:
            kind, payload = await asyncio.wait_for(
                queue.get(), timeout=keepalive_s if keepalive_s and keepalive_s > 0 else None
            )
        except asyncio.TimeoutError:
            # 网页版生成较慢，发送 SSE 注释保活，避免 Pi 侧超时断连。
            # 带 tools 时回复必须先完整缓冲才能判断是不是 tool_calls，
            # 因此这段时间客户端看不到内容 —— 用注释保活 + 日志保持可观测。
            keepalives += 1
            if config.DEBUG:
                logger.debug(
                    f"[debug] 等待上游回复中（已发 {keepalives} 次 keep-alive，"
                    f"工具模式={wants_tools}）"
                )
            yield ": keep-alive\n\n"
            continue

        if kind == "delta":
            streamed = True
            choice = chat_sse_choice_for_event(AssistantTextDelta(payload))
            assert choice is not None  # 文本增量一定被适配器投影为 choice（断言只为类型收窄）
            yield encode(choice["delta"], finish=choice["finish_reason"])
        else:
            reply_content, _blocks, error, error_type = payload
            break

    await task

    if error:
        yield f"data: {json.dumps({'error': {'message': error, 'type': error_type}}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    parsed_tool_calls = parse_reply_tool_calls(reply_content, request.tools) if wants_tools else []
    bridge_events = completion_events(reply_content, parsed_tool_calls)
    bridge_tool_calls = [event for event in bridge_events if isinstance(event, ToolCall)]

    if bridge_tool_calls:
        for index, event in enumerate(bridge_tool_calls):
            choice = chat_sse_choice_for_event(event, tool_index=index)
            assert choice is not None  # 工具调用事件带 index，一定投影为 choice
            yield encode(choice["delta"], finish=choice["finish_reason"])
            arguments_str = responses_function_call_arguments(event)
            for piece in _chunk_text(arguments_str):
                yield encode({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
        yield encode(None, finish="tool_calls")
    else:
        if not streamed and reply_content:
            for piece in _chunk_text(reply_content):
                choice = chat_sse_choice_for_event(AssistantTextDelta(piece))
                assert choice is not None  # 文本增量一定被适配器投影为 choice
                yield encode(choice["delta"], finish=choice["finish_reason"])
        yield encode(None, finish="stop")

    include_usage = isinstance(request.stream_options, dict) and bool(
        request.stream_options.get("include_usage")
    )
    if include_usage:
        # usage 用真正发出去的 prompt 估算（driver 可能选了播种版 / 中途轮转过）。
        # 按会话桶读取，并发时不会拿到别的 Agent 的 prompt；回退到入参 prompt 兼容假 driver。
        sent_prompt = driver.sent_prompt(session_key) or prompt
        completion_tokens = estimate_tokens(reply_content)
        usage_payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": estimate_tokens(sent_prompt),
                "completion_tokens": completion_tokens,
                "total_tokens": estimate_tokens(sent_prompt) + completion_tokens,
            },
        }
        yield f"data: {json.dumps(usage_payload, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"
