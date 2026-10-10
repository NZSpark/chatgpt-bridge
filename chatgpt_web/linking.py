"""会话绑定（``/link``）：把某个会话桶固定到用户给出的网页会话 URL。

背景（真机）：ChatGPT 网页版有时会**自行开启一条新的网页会话**，桥这一侧看到的
就是「句柄消失 / 上下文已经不在原来的会话里」。原有设计在句柄失效时只会
**新开一条空白会话 + 播种重放历史**——上下文不丢，但用户真正想要的是
「保持在指定那条会话上继续发」。

于是有了这里的能力：

* ``/link <URL>``：把当前桶绑定到该会话；默认认为**这条会话里已有此前上下文**，
  之后只发增量（``has_history=True``）；
* ``/link <URL> --seed``：绑定，但下一次发送把完整历史**播种**进这条会话
  （那条会话里其实没有此前记录时用；代价是历史会在网页端重复一遍）；
* ``/link``：查看当前绑定；``/unlink``：解除绑定，恢复「轮转 + 播种」的默认行为。

绑定只决定**页面落到哪里**（见 :meth:`chatgpt_web.page_pool.PagePoolMixin._enter_linked_session`）：
页面漂移、句柄失效重建、轮转、启动恢复都会回到这条会话，所以一次句柄丢失
不会再把它丢进一条陌生的新会话里。

命令在 **HTTP 边缘**被识别（``server`` 的 chat 路由与 ``api`` 适配层）：整条消息就是
那一行命令时由桥直接应答、不发给网页版——句柄都丢了的时候这条命令必须还能用。
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set
from urllib.parse import urlsplit

from . import config
from .errors import DEFAULT_SESSION_KEY
from .prompting import _content_to_text

logger = logging.getLogger(__name__)

#: 绑定指令 / 解除绑定指令 / 「按播种方式绑定」的开关。
LINK_COMMAND = "/link"
UNLINK_COMMAND = "/unlink"
SEED_FLAG = "--seed"

_USAGE = (
    "[Bridge] 会话绑定用法：\n"
    f"  {LINK_COMMAND} https://chatgpt.com/c/<会话ID>          # 之后都发到这条会话\n"
    f"  {LINK_COMMAND} https://chatgpt.com/c/<会话ID> {SEED_FLAG}   # 绑定并把完整历史播种进去\n"
    f"  {UNLINK_COMMAND}                                       # 解除绑定，恢复新开会话 + 播种"
)

#: 会话 ID 的形态（ChatGPT 目前是 UUID；放宽到字母数字与 ``-``/``_``）。
_CONVERSATION_ID_RE = r"[A-Za-z0-9_-]{8,64}"
#: 会话链接路径里的固定段：``/c/<id>``，或带自定义 GPT 的 ``/g/<gpt>/c/<id>``。
_CONVERSATION_SEGMENT = "c"


@dataclass(frozen=True)
class LinkCommand:
    """一条已被识别的桥内命令。"""

    action: str  # "link" | "unlink" | "status" | "invalid"
    target: Optional[str] = None
    seed: bool = False
    reason: str = ""


def allowed_hosts(raw: Optional[str] = None) -> Optional[Set[str]]:
    """允许绑定的主机集合；``None`` = 不限制（配置里写了 ``*``）。

    ``LINK_ALLOWED_HOSTS`` 里用逗号或 ``||`` 分隔；子域自动允许
    （列了 ``chatgpt.com`` 就等于也允许 ``www.chatgpt.com``）。
    """
    text = config.LINK_ALLOWED_HOSTS if raw is None else raw
    items = set()
    for part in (text or "").split("||"):
        for item in part.split(","):
            item = item.strip().lower()
            if item:
                items.add(item)
    if not items or "*" in items:
        return None
    return items


def _host_allowed(host: str, hosts: Optional[Set[str]]) -> bool:
    if hosts is None:
        return True
    return any(host == item or host.endswith("." + item) for item in hosts)


def conversation_id(url: str) -> Optional[str]:
    """从会话链接里取出会话 ID（取不到返回 ``None``）。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    segments = [seg for seg in parts.path.split("/") if seg]
    for index, segment in enumerate(segments[:-1]):
        if segment == _CONVERSATION_SEGMENT:
            candidate = segments[index + 1]
            if candidate and candidate not in ("c",):
                return candidate
    return None


def canonical_conversation_url(raw: str, hosts: Optional[Set[str]] = None) -> str:
    """把用户给的链接规范化成可绑定的会话地址；不合法时抛 ``ValueError``（文案可直接展示）。

    只接受「https + 允许的站点 + ``/c/<会话ID>``」。分享链接（``/share/...``）
    不是可继续对话的会话，同样拒绝。
    """
    text = (raw or "").strip().strip("<>\"'")
    if not text:
        raise ValueError(f"缺少会话链接；用法：{LINK_COMMAND} https://chatgpt.com/c/<会话ID>")
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ValueError(f"链接解析失败：{raw}（{exc}）") from exc

    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise ValueError(f"只接受 https 链接（收到 {parts.scheme or '空'}）。")
    if not host:
        raise ValueError(f"链接里没有主机名：{raw}")
    allowed = hosts if hosts is not None else allowed_hosts()
    if not _host_allowed(host, allowed):
        listed = "、".join(sorted(allowed or ())) or "（未配置）"
        raise ValueError(
            f"不支持的站点 {host}；当前只允许 {listed}"
            "（确实要用别的站点请把它加进 LINK_ALLOWED_HOSTS）。"
        )

    conv_id = conversation_id(text)
    if not conv_id:
        raise ValueError(
            f"链接里没有会话 ID：{raw}"
            "（应形如 https://chatgpt.com/c/<会话ID>；/share/ 分享链接不能用来继续对话）。"
        )
    if not re.fullmatch(_CONVERSATION_ID_RE, conv_id):
        raise ValueError(f"会话 ID 形态异常：{conv_id}（应形如 https://chatgpt.com/c/<会话ID>）。")
    return f"https://{parts.netloc.lower()}/c/{conv_id}"


def same_conversation(left: str, right: str) -> bool:
    """两个地址是否指向同一条会话。

    ``page.url`` 上常带查询串（``?model=...``）或尾斜杠，因此优先比对会话 ID；
    任一侧取不到 ID 时才退回「去掉查询 / 尾斜杠」的整串比较；两侧都读不到时
    一律返回 ``False``（判不准就重新导航，代价只是一次跳转）。
    """
    left_id = conversation_id(left or "")
    right_id = conversation_id(right or "")
    if left_id and right_id:
        return left_id == right_id

    def _normalized(url: str) -> str:
        parts = urlsplit((url or "").strip())
        return (parts.netloc.lower() + parts.path.rstrip("/")).lower()

    a, b = _normalized(left), _normalized(right)
    if not a or not b:
        return False
    return a == b


def latest_user_text(messages: Optional[Sequence[object]]) -> Optional[str]:
    """最后一条消息的文本，仅当它是 ``user`` 消息时返回（否则 ``None``）。

    只看最后一条：历史里的旧命令是**已经执行过**的会话内容，不能被重放执行。
    """
    if not messages:
        return None
    last = messages[-1]
    if getattr(last, "role", None) != "user":
        return None
    return _content_to_text(getattr(last, "content", None))


def parse_command(text: str) -> Optional[LinkCommand]:
    """整条消息是否是一条桥内命令；不是则返回 ``None``（照常发给网页版）。

    只有「整条消息就是这一行」才算命令：混在正文里、出现在多行文本里、或
    出现在历史消息里都当作普通提问交给模型，避免把用户正常说的话吃掉。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    tokens = lines[0].split()
    if not tokens:
        return None
    head, tail = tokens[0].lower(), tokens[1:]

    if head == UNLINK_COMMAND:
        if tail:
            return LinkCommand("invalid", reason=f"{UNLINK_COMMAND} 不接受参数。\n{_USAGE}")
        return LinkCommand("unlink")
    if head != LINK_COMMAND:
        return None

    seed = False
    rest: List[str] = []
    for token in tail:
        if token.lower() == SEED_FLAG:
            seed = True
        else:
            rest.append(token)
    if not rest:
        if seed:
            return LinkCommand(
                "invalid", reason=f"{LINK_COMMAND} {SEED_FLAG} 还必须给出会话链接。\n{_USAGE}"
            )
        return LinkCommand("status")
    if len(rest) > 1:
        return LinkCommand(
            "invalid", reason=f"链接里不能有空格：收到了 {len(rest)} 段参数。\n{_USAGE}"
        )
    return LinkCommand("link", target=rest[0], seed=seed)


def _link_reply(bucket: str, url: str, seed: bool) -> str:
    conv = conversation_id(url) or url
    if seed:
        tail = (
            "下一次发送会把**完整历史**播种进这条会话（适合它里面其实没有此前记录的情形），"
            "之后只发增量。"
        )
    else:
        tail = (
            "之后只发增量（默认认为这条会话里已有此前上下文）；"
            f"若它里面其实没有此前记录，请改用 `{LINK_COMMAND} <链接> {SEED_FLAG}`。"
        )
    return (
        f"[Bridge] 已把会话桶 {bucket} 绑定到网页会话 {conv}。\n"
        f"{tail}\n"
        "句柄失效、页面被 ChatGPT 跳到别的会话、服务重启后都会回到这条会话；"
        f"用 `{UNLINK_COMMAND}` 解除绑定。"
    )


def _unlink_reply(bucket: str, had: bool) -> str:
    if had:
        return (
            f"[Bridge] 已解除会话桶 {bucket} 的绑定；下一轮会开启新会话并播种完整历史。"
        )
    return f"[Bridge] 会话桶 {bucket} 本来就没有绑定网页会话。"


async def handle_command(messages, driver, session_key: Optional[str] = None) -> Optional[str]:
    """识别并执行桥内命令，返回桥自己给出的应答；不是命令时返回 ``None``。

    调用方（``server`` / ``api`` 适配层）拿到应答后直接回给客户端，**不会**把这条
    消息发给网页版：命令要在句柄失效、乃至页面漂移时都可用。
    """
    text = latest_user_text(messages)
    if text is None:
        return None
    command = parse_command(text)
    if command is None:
        return None

    bucket = session_key or DEFAULT_SESSION_KEY
    try:
        if command.action == "status":
            current = driver.linked_url(bucket)
            if current:
                return (
                    f"[Bridge] 会话桶 {bucket} 当前绑定到 {current}；"
                    f"用 `{UNLINK_COMMAND}` 解除绑定。"
                )
            return f"[Bridge] 会话桶 {bucket} 当前未绑定网页会话。\n{_USAGE}"
        if command.action == "invalid":
            return f"[Bridge] 命令未执行：{command.reason}"
        if command.action == "unlink":
            return _unlink_reply(bucket, driver.unlink_session(bucket))
        url = canonical_conversation_url(command.target or "")
        driver.link_session(url, key=bucket, seed=command.seed)
        logger.info("[绑定] 已按 %s 指令绑定 key=%s → %s（播种=%s）", LINK_COMMAND, bucket, url, command.seed)
        return _link_reply(bucket, url, command.seed)
    except ValueError as exc:
        return f"[Bridge] 没能绑定会话：{exc}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[绑定] 执行 %s 失败：%r", LINK_COMMAND, exc, exc_info=True)
        return f"[Bridge] 执行命令失败：{exc!r}"
