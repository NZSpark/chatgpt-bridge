"""把客户端的消息数组转换成网页输入框里的一整段文本，以及若干纯文本工具函数。"""

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .models import ChatMessage
from .toolcalls import (
    CONTEXT_REBUILD_HEADER,
    EDIT_MD_HEADER,
    ENV_NOTE_HEADER,
    SECTION_ASSISTANT,
    SECTION_SYSTEM,
    SECTION_TOOL_RESULTS,
    SECTION_USER,
    TOOLCALL_HEADER,
    format_repeat_call_hint,
    format_tool_call_emphasis,
    format_tools_instruction,
)

# 区段标题常量统一在 toolcalls.py 定义（见 doc/prompt_design.md §2），此处仅再导出，
# 保证「生成 / 去重 / 泄漏检测」永远共用同一份字符串。
__all__ = [
    "CONTEXT_REBUILD_HEADER",
    "ENV_NOTE_HEADER",
    "SECTION_USER",
    "SECTION_TOOL_RESULTS",
]

# 图片分片降级成文本引用行时的前缀标记（与 DeepseekBridge 对齐）。
MARK_IMAGE = "[图片]"


# ==================== 图片直传：抽取层 ====================
# 参考 doc/image_upload.md：网页版 composer 是 contenteditable，无法用
# set_input_files 塞文件，必须模拟「粘贴」；而粘贴需要的原始字节 / 远程 URL
# 都要先从这里（客户端发来的 OpenAI 消息数组）抽出来。
@dataclass(slots=True)
class ImageAttachment:
    """一张待粘贴进网页输入框的图片。

    ``data`` 在 base64 已知时给出原始字节；``url`` 用于远程图片惰性下载。
    两者至少有一个非空（由 :func:`extract_images` 保证）。
    """

    name: str
    mime: str
    data: Optional[bytes] = None
    url: Optional[str] = None


def _is_image_part(part: dict) -> bool:
    """判断一个内容分片是否是 OpenAI 的图片分片。"""
    ptype = part.get("type")
    if ptype in ("image_url", "input_image"):
        return True
    # 兼容部分客户端直接给裸 url / image 字段。仅有 url 字段时也视为图片分片；
    # 这是有意的兼容行为，调用方应确保 content 数组符合消息分片协议。
    return "image_url" in part or "image" in part or "url" in part


def _image_url(part: dict) -> Optional[str]:
    """从图片分片里取出 URL（data URI 或远程地址），兼容三种写法。

    * ``{"type": "image_url", "image_url": {"url": "..."}}``（标准 OpenAI）
    * ``{"type": "image_url", "image_url": "..."}``（简化写法）
    * ``{"type": "input_image", "image_url": "..."}``（Responses 风格）
    * ``{"url": "..."}`` / ``{"image": "..."}``（裸字段）
    """
    raw = part.get("image_url")
    if isinstance(raw, dict):
        raw = raw.get("url")
    if not isinstance(raw, str) or not raw:
        raw = part.get("url") or part.get("image")
    return raw if isinstance(raw, str) and raw else None


def _parse_data_uri(url: str) -> Optional[Tuple[str, bytes]]:
    """把 ``data:<mime>;base64,<payload>`` 解析为 (mime, 字节)。

    非 base64 / 空 payload / 解码失败都返回 None（调用方跳过该分片）。
    """
    header, _, payload = url.partition(",")
    if ";base64" not in header or not payload:
        return None
    mime = header.removeprefix("data:").split(";", 1)[0] or "application/octet-stream"
    try:
        return mime, base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return None


def _messages_since_last_assistant(messages: List[ChatMessage]) -> List[ChatMessage]:
    """返回最后一条 assistant 消息之后的「本轮新增」消息。

    多轮对话里历史消息的图片会随每一轮请求被重复带上；只在「本轮新增」里抽图，
    图片就只会在它首次出现的那一轮被粘贴一次（见 doc/image_upload.md §2.4）。
    兼容 dict / 对象两种消息形态。
    """

    def _role(message: Any) -> Optional[str]:
        if isinstance(message, dict):
            return message.get("role")
        return getattr(message, "role", None)

    last = -1
    for index, message in enumerate(messages):
        if _role(message) == "assistant":
            last = index
    if last < 0 or last + 1 >= len(messages):
        # 无 assistant / 无新增：退回全部（宁多贴不漏）。
        return list(messages)
    return list(messages[last + 1:])


def _iter_image_parts(content: Any):
    """遍历 content 里的图片分片，产出 (part, url)。非图片分片跳过。"""
    if not isinstance(content, list):
        return
    for part in content:
        if not (isinstance(part, dict) and _is_image_part(part)):
            continue
        url = _image_url(part)
        if url:
            yield part, url


def extract_images(messages: List[ChatMessage], only_delta: bool = False) -> List[ImageAttachment]:
    """从消息里收集图片分片；base64 解码为字节，URL 保留待下载。

    ``only_delta=True`` 时只看「本轮新增消息」——图片只粘贴一次，避免多轮里
    历史图片被反复贴进输入框（见 doc/image_upload.md §2.4）。
    """
    if only_delta:
        messages = _messages_since_last_assistant(messages)
    attachments: List[ImageAttachment] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        for _part, url in _iter_image_parts(content):
            if url.startswith("data:"):
                parsed = _parse_data_uri(url)
                if parsed is None:
                    continue
                mime, data = parsed
                if not data:  # 空 / 非法 base64 解出 b""：跳过，不贴 0 字节文件
                    continue
                ext = mime.split("/")[-1] or "png"
                attachments.append(ImageAttachment(name=f"image.{ext}", mime=mime, data=data))
            else:
                name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "image.png"
                attachments.append(ImageAttachment(name=name, mime="image/png", url=url))
    return attachments


def _image_reference(url: str) -> str:
    """图片分片降级成文本引用行。**绝不包含完整 base64 字节**（§2.5）。"""
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        media = header.removeprefix("data:").split(";", 1)[0] or "image"
        approx_kb = max(1, len(payload) * 3 // 4 // 1024)
        return f"{MARK_IMAGE} ({media}, ~{approx_kb}KB base64)"
    return f"{MARK_IMAGE} {url}"

# ==================== 工具结果「没有输出」的识别与说明 ====================
# 背景（用户实测死循环）：命令成功但没有任何 stdout 时，客户端（Pi / Codex）会把
# 结果渲染成 ``$ git status --short`` + ``(no output)`` + ``Took 0.0s`` 这类文字。
# 模型看到「没有输出」会理解成「命令没生效 / 工具坏了」，于是把**同一条命令原样
# 再发一遍**；客户端再执行一次、又是一样的空输出——死循环，用户侧只看到同一条
# 命令被反复执行。
#
# 因此空输出必须被**显式**说明（而不是把 ``(no output)`` 原样扔给模型）：
#   1. 逐条结果：替换成「已完成、无输出、请给下一条指令」（见 EMPTY_TOOL_RESULT_NOTE）；
#   2. 同一命令已重复 ≥2 次时再追加点名提醒（见 build_prompt / format_repeat_call_hint）。
EMPTY_TOOL_RESULT_NOTE = (
    "The command finished successfully but printed NOTHING. Empty output is a valid "
    "result - not a failure, and not a sign that the tool broke.\n"
    "Give the next command or the final answer. Do not re-run the same command (it will "
    "print nothing again); if you need to see state, use a different command."
)

# 空输出的等价写法（均为括号/尖括号包裹形式，避免把真实输出里的普通句子误判成空）
_EMPTY_TOOL_OUTPUT_MARKERS = (
    "(no output)", "(no stdout)", "(empty output)", "(empty)",
    "(no output produced)", "(空输出)", "(无输出)", "<no output>", "[no output]",
)

# 客户端外壳行：耗时、退出码、token 统计、空 Output: 块、分隔线。
# 只有「除了外壳与空标记之外什么都没有」才判定为「没有输出」；
# 真实输出里出现这些字样不受影响。
_TOOL_CHROME_RES = (
    re.compile(r"^took\s+[\d.]+\s*s$", re.IGNORECASE),      # Took 0.0s
    re.compile(r"^wall time:\s*\S.*$", re.IGNORECASE),      # Wall time: 2.9 seconds
    re.compile(r"^chunk id:\s*\S+$", re.IGNORECASE),
    re.compile(r"^process exited with code\s+\d+$", re.IGNORECASE),
    re.compile(r"^original token count:\s*\d+$", re.IGNORECASE),
    re.compile(r"^output:\s*$", re.IGNORECASE),             # Codex 的 “Output:” 空块
    re.compile(r"^[-=_*~#]{3,}$"),                          # ---- / ===== 分隔线
)
# 命令回显：harness 会把执行过的命令回显在第一行（"$ git status --short"）。
# 只在**首行**识别，避免把「输出本身以 $ 开头」误判成外壳。
_TOOL_ECHO_RE = re.compile(r"^\$\s+\S.*$")


def _is_empty_tool_output(raw: str) -> bool:
    """工具结果是否等价于「命令执行完成，但确实没有任何输出」。

    判定方式：去掉外壳行（命令回显 / 耗时 / 退出码 / token 统计 / 分隔线）与空标记后，
    是否什么都不剩。这样 ``git status --short``（干净）与空白内容都算空，
    而任何含真实内容的输出都会被逐字节保留（edit 工具的 oldText 匹配依赖这一点）。
    """
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return True
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if lines and _TOOL_ECHO_RE.match(lines[0]):
        lines = lines[1:]
    return all(_is_tool_chrome_or_marker(line) for line in lines)


def _is_tool_chrome_or_marker(line: str) -> bool:
    if line.lower() in _EMPTY_TOOL_OUTPUT_MARKERS:
        return True
    return any(pattern.match(line) for pattern in _TOOL_CHROME_RES)


def _tool_call_signature(call: Any) -> str:
    """把一条 assistant 工具调用归一化成「命令签名」（用于识别重复调用）。

    客户端回传的 arguments 是 JSON 字符串，键序 / 空白可能不同，因此先解析再用
    ``sort_keys`` 重新序列化；解析失败就退回原文（宁可不判重，也不要误判）。
    """
    if isinstance(call, dict):
        name = call.get("name")
        args = call.get("arguments")
    else:
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", None)
        args = getattr(fn, "arguments", None)
    if not name:
        return ""
    if isinstance(args, str):
        try:
            args = json.dumps(json.loads(args), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            pass
    elif isinstance(args, (dict, list)):
        args = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return f"{name}|{args if args is not None else ''}"


def _repeated_empty_calls(
    messages: List[ChatMessage], empty_call_ids: List[Optional[str]]
) -> List[Tuple[str, int]]:
    """找出「同一命令已调用 ≥2 次」且本轮结果为空的那几条，返回 [(签名, 次数)]。

    靠 ``tool_call_id`` 把 tool 结果映射回发起它的 assistant 调用，再对历史里
    所有 assistant 工具调用按签名计数——只有真的重复过才提醒，不会误伤首次执行。
    """
    signature_of: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    for message in messages:
        for call in message.tool_calls or []:
            signature = _tool_call_signature(call)
            if not signature:
                continue
            counts[signature] = counts.get(signature, 0) + 1
            call_id = getattr(call, "id", None)
            if call_id:
                signature_of[str(call_id)] = signature
    out: List[Tuple[str, int]] = []
    for call_id in empty_call_ids:
        if not call_id:
            continue
        matched = signature_of.get(str(call_id))
        if matched and counts.get(matched, 0) >= 2:
            pair = (matched, counts[matched])
            if pair not in out:
                out.append(pair)
    return out


def _content_to_text(content: Any) -> str:
    """把 OpenAI 的 content 归一化为纯文本。

    content 可能是：
      - None
      - 字符串
      - 内容分片数组，例如 [{"type": "text", "text": "hi"}]
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                # 图片分片：降级为一行文本引用（历史图片仍会以 `[图片] …` 出现，
                # 作为兜底锚点；但绝不含完整 base64）。
                if _is_image_part(part):
                    url = _image_url(part)
                    if url:
                        pieces.append(_image_reference(url))
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return str(content)


def estimate_tokens(text: str) -> int:
    """估算 token 数（仅用于填充 OpenAI 的 usage 字段，不是精确值）。

    CJK 字符约 1 char/token，其余字符约 4 char/token。
    不引入 tiktoken：那是 OpenAI 的分词器，算 ChatGPT 的 token 只会
    得到一个“看起来很精确但其实是错的”数字，反而更容易误导客户端做上下文裁剪。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u30ff")
    other = len(text) - cjk
    return max(1, cjk + other // 4)


def _delta_piece(streamed: str, current: str) -> tuple[Optional[str], str]:
    """计算 ``current`` 相对「已经发给客户端的内容」真正新增的部分。

    网页版在生成中可能重排 / 替换回复节点，导致 ``current`` 不再以之前的内容为前缀。
    此时有两种做法：

    * 退回「公共前缀之后的部分」追加——但这会让客户端把**改写后的旧内容**
      拼在旧内容后面，得到重复 / 错乱的文本（旧尾巴不会撤回）；
    * **停止发送增量**（返回 ``(None, streamed)``）——保留已发内容不动，
      等本轮结束由调用方一次性给全量文本，客户端最终拿到的是完整且不重复的回复。

    这里选后者：宁可少发几次增量，也不要让客户端拼出错乱文本。真正的
    前缀扩展（最常见情形）仍然逐块下发。

    :return: (需要补发的内容, 客户端补发后实际拥有的内容)；无新增 / 已停发时为 None。
    """
    if current == streamed:
        return None, streamed
    if current.startswith(streamed):
        return current[len(streamed):], current
    # 非前缀：节点被整体替换 / 重排。停发增量，保留已发内容，交由收尾补全。
    return None, streamed


# 播种时的默认字符预算（server 会传入 config.SEED_MAX_CHARS 覆盖）
DEFAULT_SEED_MAX_CHARS = 12000


def _render_message(message: ChatMessage) -> str:
    """把单条消息渲染成喂给网页版的一段文本。

    ``role == "tool"`` 的正文必须逐字节保留：Pi/Codex 的 read 结果会原样
    作为 edit 工具的 oldText，任何 .strip() 抹掉的首尾空行/换行都会导致
    "Could not find the exact text ... including all whitespace and newlines"。
    其它角色仍是历史对话文本，去掉首尾空白无副作用。
    """
    raw = _content_to_text(message.content)
    if message.role == "tool":
        tag = f" {message.tool_call_id}" if message.tool_call_id else ""
        limit = config.TOOL_RESULT_MAX_CHARS
        if limit and len(raw) > limit:
            dropped = len(raw) - limit
            raw = (
                raw[:limit]
                + f"\n…（工具结果过长，已截断 {dropped} 字符）"
            )
        if _is_empty_tool_output(raw):
            # 空输出必须显式说明（见 EMPTY_TOOL_RESULT_NOTE 的背景）：把
            # "(no output)" 原样交给模型，它会以为命令没生效而反复重发同一条命令。
            # 真实（非空）结果仍逐字节保留——那是 edit 工具 oldText 匹配的前提。
            return f"{SECTION_TOOL_RESULTS}{tag}\n{EMPTY_TOOL_RESULT_NOTE}"
        return f"{SECTION_TOOL_RESULTS}{tag}\n{raw}"
    content = raw.strip()
    if message.role == "system":
        return f"{SECTION_SYSTEM}\n{content}"
    if message.role == "assistant":
        return f"{SECTION_ASSISTANT}\n{content}"
    return f"{SECTION_USER}\n{content}"


def has_prior_tool_use(messages: List[ChatMessage]) -> bool:
    """本轮请求的历史里是否**已经用过工具**（跨轮判断「任务是否已开始执行」）。

    客户端只有在执行完一个工具后才会回传 ``role="tool"`` 的结果（或带 ``tool_calls``
    的 assistant 消息），因此这两者任一出现就意味着任务**已经进入执行阶段**。
    用途：决定「模型这次只回纯文本」到底是「无视工具」还是「任务收尾」——
    见 :func:`tool_nudge_predicate`。
    """
    for message in messages:
        if message.role == "tool":
            return True
        if message.tool_calls:
            return True
    return False


def tool_nudge_predicate(
    messages: List[ChatMessage],
    tools: Optional[List[Dict[str, Any]]],
    tool_choice: Optional[Any] = None,
) -> Optional[Any]:
    """按需返回「本轮回复是否可接受」判定，供 ``send_chat(validate_reply=...)``。

    返回 ``None`` = **不纠偏**（把模型这次回复当最终答案）。仅在「带工具、且本轮任务
    一次工具都还没调用过」时返回判定函数。

    为什么任务执行中不再纠偏（用户实测）：模型用过工具后用纯文本收尾（“已完成”）时，
    立刻追发纠偏指令等于把结论又推成一条新命令，模型只能再发指令 → 任务永远收不了尾。
    反之首轮（还没调过工具）的纠偏仍保留，因为它治的是另一种毛病：
    模型完全无视工具、直接凭自身知识编结果（E2E C1/C2）。

    关闭 ``config.TOOL_NUDGE_UNTIL_FIRST_CALL`` = **完全不纠偏**（桥不再自行追发
    任何 prompt，模型没调用工具时直接把纯文本当最终答案返回）。
    """
    if not config.TOOL_NUDGE_UNTIL_FIRST_CALL:
        return None
    if not tools or tool_choice == "none":
        return None
    if has_prior_tool_use(messages):
        return None
    from .toolcalls import tool_call_predicate

    return tool_call_predicate(tools)


def _last_assistant_index(messages: List[ChatMessage]) -> int:
    last = -1
    for index, message in enumerate(messages):
        if message.role == "assistant":
            last = index
    return last


def _is_harness_noise(m: ChatMessage) -> bool:
    """harness 注入的元提示（如 Codex 的“生成任务标题”请求）与环境包装块
    都不是真实对话内容，播种/增量时都不应重放。"""
    from .tasks import _is_environment_wrapper, _is_meta_prompt

    text = _content_to_text(m.content)
    return _is_meta_prompt(text) or _is_environment_wrapper(text)


def _run_messages(messages: List[ChatMessage]) -> List[ChatMessage]:
    """取出“最后一条 assistant 之后”的新增消息。

    harness（Codex / Pi）每轮会把完整系统提示作为 system 消息重新发来，也会
    内联“生成任务标题”之类的元提示与 ``<environment_context>`` 环境块。这些
    都不是用户真正说的话，增量发送时必须丢弃，否则每轮都会把它们当成新指令
    重发一遍。
    """
    last_assistant = _last_assistant_index(messages)
    delta = messages[last_assistant + 1:] if last_assistant >= 0 else messages
    # 增量模式下 system 消息一律丢弃：harness 每轮重发完整系统提示，而网页
    # 会话早已带着它，没必要也不应该把上万字的系统提示当新指令再发一遍。
    delta = [
        m
        for m in delta
        if m.role != "system" and not _is_harness_noise(m)
    ]
    if not delta:
        # 兜底：没有新消息时，退回最后一条真实 user 消息
        delta = [
            m
            for m in messages
            if m.role == "user" and not _is_harness_noise(m)
        ][-1:]
    # 第 2 块内部互斥（doc/prompt_design.md §1.2）：最后一条 user 之后若还跟着
    # 工具结果，说明这条指令已提交、本轮只发结果；反之（畸形输入：旧 tool 结果 +
    # 一条新 user）必须丢掉新 user 之前的旧结果，否则两块同时出现，模型会把旧结果
    # 当成本轮刚发生的事。
    last_user = -1
    for index, message in enumerate(delta):
        if message.role == "user":
            last_user = index
    if last_user >= 0:
        delta = [m for i, m in enumerate(delta) if i >= last_user or m.role != "tool"]
    return delta


def _seed_messages(messages: List[ChatMessage], max_chars: int):
    """为新会话准备“播种”内容：尽量带上完整上下文，超出预算时保留最近的。

    网页会话一旦轮转（新开会话），后端的上下文就清空了。此时如果还只发增量，
    模型会收到一条“没有前因”的孤立消息——不报错，但会胡编。

    :return: (system 消息, 保留的其余消息, 是否发生了截断)
    """
    systems = [m for m in messages if m.role == "system" and not _is_harness_noise(m)]
    rest = [m for m in messages if m.role != "system" and not _is_harness_noise(m)]

    truncated = False

    # system 消息同样计入预算。harness（Codex / Pi）每轮都把完整系统提示作为
    # system 消息发来，常达上万字；若像以前那样“原样全发”，播种 prompt 就会被
    # 这段系统提示灌满，用户的真实请求被淹没。这里逐条按 SEED_SYSTEM_MAX_CHARS
    # 截断，并从最旧的开始丢弃，直到 system 总量不超过总预算的一半。
    system_budget = max_chars // 2
    per_system_limit = config.SEED_SYSTEM_MAX_CHARS
    systems = list(reversed(systems))  # 保留最近的 system
    kept_systems: List[ChatMessage] = []
    system_used = 0
    for message in systems:
        text = _content_to_text(message.content)
        # 剩余可用预算：既要满足单条 SEED_SYSTEM_MAX_CHARS，也不能突破 system_budget。
        # 取两者较小值，保证第一条巨型 system 也会被截断（而不是无条件放行或整条丢弃）。
        remaining = system_budget - system_used
        limit = per_system_limit or len(text)
        limit = min(limit, remaining) if remaining > 0 else 0
        if limit <= 0:
            truncated = True
            break
        if len(text) > limit:
            text = text[:limit] + "…（系统提示已截断）"
            truncated = True
        kept_systems.append(ChatMessage(role="system", content=text))
        system_used += len(text)
    kept_systems.reverse()

    kept: List[ChatMessage] = []
    used = system_used
    for message in reversed(rest):
        size = len(_content_to_text(message.content))
        if kept and used + size > max_chars:
            truncated = True
            break
        kept.append(message)
        used += size
    kept.reverse()
    return kept_systems, kept, truncated


def build_prompt(
    messages: List[ChatMessage],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    seed: bool = False,
    seed_max_chars: Optional[int] = None,
    task_block: Optional[str] = None,
) -> str:
    """把客户端发来的完整 OpenAI 消息数组，转换成要发给网页输入框的文本。

    * ``seed=False``（默认）：网页版是一个持续存在的会话，无需每轮重发全部历史，
      只发送“最后一条 assistant 消息之后”的新增消息（新的 user 指令或 tool 结果）。
    * ``seed=True``：当前会话是**新开的**，必须把既有上下文一次性播种进去，
      否则模型会收到一条没有前因的孤立消息。
    * ``task_block``：任务快照（见 ``tasks.resume_block``）。仅在 ``seed=True`` 时
      生效，会被放在**历史之前、上下文重建头之后**，因此**不会**被
      ``seed_max_chars`` 的尾部截断逻辑丢掉——这是“轮转不丢任务”的关键。

    带 ``tools`` 时的注入约定（T1.1，实测校准）：

    * 增量路径：工具说明在**用户任务之前**（prompt 开头）；
    * 播种路径：格式强调块在开头兜底，工具说明**紧贴本轮任务（最后一条消息）
      之前**——既有近因效应，又不违反「工具说明先于任务」的既有约定。
    """
    if seed:
        systems, kept, truncated = _seed_messages(messages, seed_max_chars or DEFAULT_SEED_MAX_CHARS)
        parts: List[str] = [
            f"{CONTEXT_REBUILD_HEADER} 这是一个新会话。以下是本次任务此前的对话记录，"
            "请据此继续，不要从头重做。",
            # 明确告知：git 仓库就在本地，模型直接下命令即可，无需请求用户提供
            # 远程地址 / 手动执行。放在重建头之后、历史之前，避免被尾部截断丢掉。
            config.SEED_ENV_NOTE,
        ]
        if task_block:
            parts.append(task_block)
        if truncated:
            parts.append("（更早的部分因长度限制已省略，如需可向我确认。）")
    else:
        parts = []

    use_tools = bool(tools) and tool_choice != "none"
    if seed and use_tools:
        # 新 bucket / 重置后的第一轮：在播种开头再放一次格式强调（强制要求），
        # 模型最容易在这种时候退回原生 DSML 标记或干脆无视工具。传入 tools 让它
        # 也带一个**具体**示例（与主注入块共用同一份生成逻辑，两处形态永不漂移）。
        parts.insert(1, format_tool_call_emphasis(list(tools or [])))

    # 历史（播种）或增量消息从这里开始追加；工具说明要插在它们**内部**。
    history_start = len(parts)
    if seed:
        used_messages = systems + kept
    else:
        used_messages = _run_messages(messages)
    rendered = [_render_message(m) for m in used_messages]
    parts.extend(rendered)

    # 去重只看**入站消息**（harness 可能已内联一份说明）；不能用 parts 整体判定，
    # 否则我们自己注入的格式强调块里提到标题就会被误判成「已存在」而不注入工具清单。
    inbound = "\n".join(rendered)

    # 工具说明的插入位置（T1.1 实测结论）：
    #   * 增量路径：放最前（用户任务之前）——模型先看到「有哪些工具、必须调用、
    #     怎么调用」，再看到具体请求，降低「无视工具、直接凭知识作答」的概率；
    #   * 播种路径：**紧贴本轮任务（最后一条消息）之前**。旧实现把它放在整段
    #     历史之前，与真正的任务之间隔着环境说明与成百上千行历史，实测模型
    #     倾向于直接凭知识作答（C1/C2 失败）；贴到任务之前既有近因效应，
    #     又不违反「工具说明在用户任务之前」的既有约定。
    # 去重：入站 system 消息可能已带同一份说明（harness 会内联一份），
    # 再追加一遍会造成同一 prompt 出现两份说明、互相干扰。判定与生成必须
    # 共用 TOOLCALL_HEADER，否则去重恒不生效（见 T2.3）。
    if use_tools and TOOLCALL_HEADER not in inbound:
        tool_list = tools or []
        if seed:
            insert_at = len(parts) - 1 if len(parts) > history_start else history_start
            parts.insert(max(history_start, insert_at), format_tools_instruction(tool_list))
        elif config.PROMPT_CONTRACT_LAST:
            # A/B 开关（doc/prompt_tasks.md T3.2）：DeepseekBridge 的「契约放最后」排法。
            # 仅供同一套 E2E 上对比，默认关闭——本项目 T1.1 的实测结论是契约在前。
            parts.append(format_tools_instruction(tool_list))
        else:
            parts.insert(0, format_tools_instruction(tool_list))

    if use_tools and any(
        (t.get("function", t) or {}).get("name") == "edit_markdown" for t in (tools or [])
    ):
        from .toolcalls import edit_markdown_spec

        if EDIT_MD_HEADER not in inbound:
            parts.append(edit_markdown_spec())

    # 死循环防护（用户实测：同一条命令被反复执行）：本轮工具结果为空、且该调用在
    # 历史里已经出现过 ≥2 次时，直接点名这条命令并禁止重发。放在 prompt 末尾，
    # 紧贴模型的下一步动作（近因效应），是比“逐条说明”更硬的一道保险。
    empty_call_ids = [
        m.tool_call_id
        for m in used_messages
        if m.role == "tool" and _is_empty_tool_output(_content_to_text(m.content))
    ]
    if empty_call_ids:
        repeats = _repeated_empty_calls(messages, empty_call_ids)
        if repeats:
            parts.append(format_repeat_call_hint(repeats))

    return "\n\n".join(part for part in parts if part).strip()
