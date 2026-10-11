"""Bridge 聊天命令（``/bridge ...``）：在对话里查询桥接状态、管理当前会话桶。

设计约定与分期计划见 ``doc/bridge_command.md``，这里是它的落地实现。要点：

* **精确匹配**：只有「整条消息就是那一行命令」才执行——历史消息里的旧命令不重放，
  正文 / 多行文本 / 代码块里出现的命令字样一律当普通提问（见 :func:`parse_command`）。
* **只作用于当前会话桶**：命令**没有**「指定别的桶」的参数；桶由 ``X-ChatGPT-Session`` /
  ``user`` / User-Agent 决定（见 ``server._session_key``），所以多用户 / 多 Agent 场景
  不会互相影响。
* **本地应答**：命令由桥直接回复、**不发给 ChatGPT 网页版**，因此句柄失效、浏览器
  不可用时这些命令仍然可用（只读命令尤其如此；路由层的可用性检查见 :func:`is_command`）。
* **不泄露**：回复只含状态摘要，不打印 ``.env`` 值、登录 Cookie、令牌或其它桶的信息。
* **兼容别名**：``/link``、``/unlink`` 保持原语义不变（识别与执行见 :mod:`chatgpt_web.linking`）。

:func:`handle_command` 是所有聊天命令的统一入口：先认 ``/bridge`` 命名空间，再交给
``linking`` 处理旧命令。Chat Completions 与 Responses 两条协议路径都调用它，避免行为分叉。
"""

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from . import config, linking, models
from .errors import DEFAULT_SESSION_KEY

logger = logging.getLogger(__name__)

#: 命令命名空间。只识别这一个前缀；其它 ``/`` 开头的文本照旧交给网页版。
BRIDGE_COMMAND = "/bridge"
#: 「按播种方式绑定」的开关（与 ``/link`` 共用同一个拼写）。
SEED_FLAG = "--seed"
#: ``/bridge help`` 支持的主题；``session`` / ``settings`` 分别对应下面两组命令。
HELP_TOPICS = ("session", "settings")
#: ``/bridge settings`` 支持的设置项名字。
SETTING_NAMES = ("think", "save-files")
#: ``/bridge settings <name>`` 的取值：on / off = 本桶偏好；default = 跟随全局配置。
SETTING_VALUES = ("on", "off", "default", "status")
#: ``/bridge session`` 的子命令。
SESSION_SUBCOMMANDS = ("status", "reset", "reseed", "link", "unlink")

_USAGE = (
    f"{BRIDGE_COMMAND} 用法：\n"
    f"  {BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]        查看命令列表 / 分主题帮助\n"
    f"  {BRIDGE_COMMAND} status                       桥接与当前会话桶状态摘要\n"
    f"  {BRIDGE_COMMAND} models                       本桥对外公布的模型名（同 /v1/models）\n"
    f"  {BRIDGE_COMMAND} session [status]             当前桶的会话状态摘要\n"
    f"  {BRIDGE_COMMAND} session reset                下一轮开新网页会话并播种完整历史\n"
    f"  {BRIDGE_COMMAND} session reseed               下一轮把完整历史再播种一遍（一次性）\n"
    f"  {BRIDGE_COMMAND} session link <URL> [{SEED_FLAG}]   把当前桶绑定到指定网页会话\n"
    f"  {BRIDGE_COMMAND} session unlink               解除当前桶的会话绑定\n"
    f"  {BRIDGE_COMMAND} settings <{'|'.join(SETTING_NAMES)}> [{'|'.join(SETTING_VALUES)}]\n"
    f"                                               按桶保存的设置（不带取值 = 查看当前值）\n"
    f"兼容别名：{linking.LINK_COMMAND} <URL> [{SEED_FLAG}]、{linking.LINK_COMMAND}、"    f"{linking.UNLINK_COMMAND}"
)


@dataclass(frozen=True)
class BridgeCommand:
    """一条已被识别的 ``/bridge`` 命令。"""

    action: str  # "help" | "status" | "models" | "session" | "settings" | "invalid"
    subaction: str = ""  # help 主题 / session 子命令 / settings 的查询或写入
    target: str = ""  # session link 的会话链接
    seed: bool = False  # session link 是否带 --seed
    name: str = ""  # settings 名字（think / save-files）
    value: Optional[bool] = None  # settings 目标值（None = 跟随全局默认）
    reason: str = ""  # 命令未执行的原因（invalid 时展示给用户）


# ==================== 命令清单（帮助的唯一来源）====================


@dataclass(frozen=True)
class CommandSpec:
    """帮助里的一个条目。只列当前版本**真正注册**的命令，避免帮助与实现漂移。"""

    syntax: str
    summary: str
    group: str  # "" = 顶层；"session" / "settings" = 分主题
    readonly: bool  # True = 只读；False = 会改变会话桶状态


REGISTRY: tuple[CommandSpec, ...] = (
    CommandSpec(f"{BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]",
                "查看命令列表 / 分主题帮助", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} status",
                "桥接、浏览器与当前会话桶的状态摘要", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} models",
                "本桥对外公布的模型名（与 GET /v1/models 同源）", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} session [status]",
                "当前桶的会话状态摘要", "session", True),
    CommandSpec(f"{BRIDGE_COMMAND} session reset",
                "下一轮开新网页会话，并把客户端历史播种进去", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} session reseed",
                "下一轮把完整历史再播种一遍（一次性标记，落盘）", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} session link <URL> [{SEED_FLAG}]",
                "把当前桶绑定到指定网页会话（--seed 表示该会话里没有此前记录）", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} session unlink",
                "解除当前桶的会话绑定", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} settings think [{'|'.join(SETTING_VALUES)}]",
                "思考模式偏好（按桶持久化；不带取值 = 查看）", "settings", False),
    CommandSpec(f"{BRIDGE_COMMAND} settings save-files [{'|'.join(SETTING_VALUES)}]",
                "代码块落盘偏好（按桶持久化；不带取值 = 查看）", "settings", False),
)


# ==================== 解析 ====================


def parse_command(text: str) -> Optional[BridgeCommand]:
    """整条消息是否是一条 ``/bridge`` 命令；不是则返回 ``None``（照常发给网页版）。

    只有「整条消息就是这一行」才算命令：混在正文里、出现在多行文本里、或出现在历史
    消息里都当作普通提问交给模型。保留命名空间内**语法已匹配但参数不合法**的写法
    返回 ``invalid``（带用法），因为 ``/bridge`` 是桥自己保留的前缀，回一条用法提示
    比把它发给网页版更可解释；其它任何 ``/`` 开头的文本一律不是命令。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    tokens = lines[0].split()
    if not tokens or tokens[0].lower() != BRIDGE_COMMAND:
        return None

    tail = tokens[1:]
    if not tail:
        # 裸 `/bridge` = 命令发现入口（帮助），而不是「未知命令」
        return BridgeCommand("help")
    head, rest = tail[0].lower(), tail[1:]

    if head == "help":
        if len(rest) > 1:
            return _invalid(f"{BRIDGE_COMMAND} help 最多接受一个主题。")
        topic = rest[0].lower() if rest else ""
        if topic and topic not in HELP_TOPICS:
            listed = "、".join(HELP_TOPICS)
            return _invalid(f"没有这个帮助主题：{rest[0]}（可用主题：{listed}）。")
        return BridgeCommand("help", subaction=topic)
    if head == "status":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} status 不接受参数。")
        return BridgeCommand("status")
    if head == "models":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} models 不接受参数。")
        return BridgeCommand("models")
    if head == "session":
        return _parse_session(rest)
    if head == "settings":
        return _parse_settings(rest)
    return _invalid(f"未知子命令：{tail[0]}。")


def _invalid(reason: str) -> BridgeCommand:
    return BridgeCommand("invalid", reason=f"{reason}\n{_USAGE}")


def _parse_session(rest: List[str]) -> BridgeCommand:
    if not rest:
        return BridgeCommand("session", subaction="status")
    sub, args = rest[0].lower(), rest[1:]
    if sub not in SESSION_SUBCOMMANDS:
        listed = "、".join(SESSION_SUBCOMMANDS)
        return _invalid(f"session 没有这个子命令：{rest[0]}（可用：{listed}）。")
    if sub in ("status", "reset", "reseed", "unlink"):
        if args:
            return _invalid(f"{BRIDGE_COMMAND} session {sub} 不接受参数。")
        return BridgeCommand("session", subaction=sub)
    # session link <URL> [--seed]：选项个数与 URL 都单独校验，不做猜测式执行
    seed = False
    positional: List[str] = []
    for token in args:
        if token.lower() == SEED_FLAG:
            seed = True
        elif token.startswith("--"):
            return _invalid(f"session link 不认识选项 {token}（只支持 {SEED_FLAG}）。")
        else:
            positional.append(token)
    if not positional:
        return _invalid("session link 还需要一个会话链接。")
    if len(positional) > 1:
        return _invalid(f"会话链接里不能有空格：收到了 {len(positional)} 段参数。")
    return BridgeCommand("session", subaction="link", target=positional[0], seed=seed)


def _parse_settings(rest: List[str]) -> BridgeCommand:
    if not rest:
        listed = "、".join(SETTING_NAMES)
        return _invalid(f"settings 还需要一个设置名（可用：{listed}）。")
    name, args = rest[0].lower(), rest[1:]
    if name not in SETTING_NAMES:
        listed = "、".join(SETTING_NAMES)
        return _invalid(f"没有这个设置项：{rest[0]}（可用：{listed}）。")
    if not args or (len(args) == 1 and args[0].lower() == "status"):
        return BridgeCommand("settings", subaction="status", name=name)
    if len(args) > 1:
        return _invalid(f"settings {name} 最多接受一个取值。")
    value_token = args[0].lower()
    if value_token not in SETTING_VALUES:
        listed = "、".join(SETTING_VALUES)
        return _invalid(f"settings {name} 不支持取值 {args[0]}（可用：{listed}）。")
    if value_token == "status":
        return BridgeCommand("settings", subaction="status", name=name)
    # on / off = 本桶偏好；default = 跟随全局配置（值为 None）
    value: Optional[bool] = {"on": True, "off": False, "default": None}[value_token]
    return BridgeCommand("settings", subaction="set", name=name, value=value)


def is_command(messages: Optional[Sequence[Any]]) -> bool:
    """这批消息的最后一条是否会由桥自己应答（``/bridge ...`` 或 ``/link`` / ``/unlink``）。

    路由层用它把「桥内命令」从「需要浏览器才能处理的普通提问」里分出来：浏览器尚未
    初始化时命令仍应答得出来（这正是用户排障时最需要它们的时候）。
    """
    text = linking.latest_user_text(messages)
    if text is None:
        return False
    return parse_command(text) is not None or linking.parse_command(text) is not None


# ==================== 执行 ====================


async def handle_command(
    messages: Optional[Sequence[Any]], driver, session_key: Optional[str] = None
) -> Optional[str]:
    """识别并执行聊天命令，返回桥自己给出的应答；不是命令时返回 ``None``。

    这是所有聊天命令的**统一入口**：先 ``/bridge`` 命名空间，再 ``/link``、``/unlink``
    （见 :mod:`chatgpt_web.linking`）。调用方拿到应答后直接回给客户端，**不会**把这条
    消息发给网页版。
    """
    text = linking.latest_user_text(messages)
    if text is None:
        return None
    command = parse_command(text)
    if command is None:
        return await linking.handle_command(messages, driver, session_key)

    bucket = session_key or DEFAULT_SESSION_KEY
    try:
        return await _execute(command, driver, bucket)
    except Exception as exc:  # noqa: BLE001
        # 失败要可解释，但不把内部堆栈 / 敏感信息发给客户端
        logger.warning("[命令] 执行 %s 失败：%r", BRIDGE_COMMAND, exc, exc_info=True)
        return f"[Bridge] 执行命令失败：{exc!r}"


async def _execute(command: BridgeCommand, driver, bucket: str) -> str:
    if command.action == "help":
        return _help_reply(command.subaction)
    if command.action == "status":
        return _status_reply(driver, bucket)
    if command.action == "models":
        return _models_reply()
    if command.action == "invalid":
        return f"[Bridge] 命令未执行：{command.reason}"
    if command.action == "session":
        return await _session_reply(command, driver, bucket)
    if command.action == "settings":
        return await _settings_reply(command, driver, bucket)
    return f"[Bridge] 命令未执行：未知命令。\n{_USAGE}"  # 防御：注册表与执行分支必须同步


def _help_reply(topic: str) -> str:
    """分主题帮助：只列当前版本真正注册的命令，并标明是否只读。"""
    lines = [f"[Bridge] {BRIDGE_COMMAND} 命令列表（当前版本）："]
    for spec in REGISTRY:
        if topic and spec.group != topic:
            continue
        mark = "只读" if spec.readonly else "改状态"
        lines.append(f"  {spec.syntax}\n      {spec.summary}（{mark}）")
    if not topic:
        lines.append(
            f"  分主题帮助：{'、'.join(f'{BRIDGE_COMMAND} help {item}' for item in HELP_TOPICS)}"
        )
    lines.append(
        f"兼容别名：{linking.LINK_COMMAND} <URL> [{SEED_FLAG}]、{linking.LINK_COMMAND}（查看绑定）、"
        f"{linking.UNLINK_COMMAND}"
    )
    lines.append("只读命令不导航网页、不触发生成；改状态的命令只影响当前会话桶。")
    return "\n".join(lines)


def _models_reply() -> str:
    """本桥对外公布的模型名：与 ``GET /v1/models`` 用同一份数据（见 models.advertised_models）。"""
    ids = [card["id"] for card in models.advertised_models()]
    lines = ["[Bridge] 本桥对外公布的模型（与 GET /v1/models 同源）："]
    lines.extend(f"  {model_id}" for model_id in ids)
    lines.append(
        "说明：这些是 API 层的模型名，供客户端配置使用；它们**不强制**切换 ChatGPT "
        "网页端实际选用的模型（那由登录账号与网页端设置决定）。"
    )
    return "\n".join(lines)


def _status_reply(driver, bucket: str) -> str:
    """桥接与当前桶的状态摘要：只读，不导航、不触发生成。"""
    page = driver.bucket_page(bucket)
    init_error = getattr(driver, "init_error", None)
    lines = ["[Bridge] 桥接状态："]
    if init_error:
        lines.append(f"  请求处理：不可用（浏览器初始化失败：{_one_line(init_error)}）")
    elif page is None:
        lines.append("  请求处理：不可用（浏览器尚未初始化）")
    else:
        lines.append("  请求处理：可用")
    lines.append(f"  浏览器：{'已就绪' if page is not None else '未初始化'}")
    url = driver.bucket_page_url(bucket)
    lines.append(f"  当前桶页面：{url or ('未打开（按需惰性创建）' if page is None else '已打开')}")
    lines.append(f"  会话分桶：{'开启' if config.SESSION_SCOPING else '关闭'}")
    lines.append(f"  当前桶：{bucket}")
    lines.append(f"  会话绑定：{driver.linked_url(bucket) or '未绑定'}")
    if page is None:
        lines.append(
            "  下一步：GET /readiness 查看登录 / composer 状态；或本地重启桥进程。"
            "（浏览器不可用时只读命令仍可用）"
        )
    return "\n".join(lines)


async def _session_reply(command: BridgeCommand, driver, bucket: str) -> str:
    sub = command.subaction
    if sub == "status":
        return _session_status_reply(driver, bucket)
    if sub == "reset":
        return _session_reset_reply(driver, bucket)
    if sub == "reseed":
        return _session_reseed_reply(driver, bucket)
    if sub == "link":
        # 与 ``/link`` 共用同一套校验、绑定与文案（含临时 ID 拒绝、允许站点白名单）
        return await linking.execute_command(
            linking.LinkCommand("link", target=command.target, seed=command.seed), driver, bucket
        )
    if sub == "unlink":
        return await linking.execute_command(linking.LinkCommand("unlink"), driver, bucket)
    return f"[Bridge] 命令未执行：未知会话子命令。\n{_USAGE}"


def _session_status_reply(driver, bucket: str) -> str:
    """当前桶的会话状态摘要（只查当前桶：默认不提供「列出所有桶」的命令，避免泄露他桶）。"""
    stats = driver.session_stats(bucket)
    lines = [f"[Bridge] 会话桶 {bucket} 的会话状态："]
    lines.append(f"  会话绑定：{stats.get('linked_url') or '未绑定'}")
    if stats.get("pending_rotation"):
        lines.append("  网页会话：已请求重置（下一轮开新会话并把完整历史播种进去）")
    elif stats.get("needs_seed"):
        lines.append("  网页会话：需要播种（下一轮会把客户端历史重放进去）")
    else:
        lines.append("  网页会话：已有上下文（之后只发增量）")
    if stats.get("pending_reseed"):
        lines.append(f"  重新播种：已排队（下一轮会再发一遍完整历史，见 `{BRIDGE_COMMAND} session reseed`）")
    lines.append(f"  轮数：{int(stats.get('turns') or 0)}；估算 tokens：{int(stats.get('est_tokens') or 0)}")
    if stats.get("cap_hit"):
        lines.append("  上次生成：因上下文长度上限中断（下一轮会轮转到新会话）")
    failures = int(stats.get("cap_failures") or 0)
    if failures >= 2:
        lines.append(f"  连续到顶：{failures} 次（播种内容会被自动压缩，避免死循环）")
    lines.append(f"  任务快照：{'开启' if config.TASK_SNAPSHOT_ENABLED else '关闭'}")
    lines.append(
        f"  管理：`{BRIDGE_COMMAND} session reset`（换新网页会话）、"
        f"`{BRIDGE_COMMAND} session reseed`（重播历史）、`{BRIDGE_COMMAND} session link <URL>`"
    )
    return "\n".join(lines)


def _session_reset_reply(driver, bucket: str) -> str:
    """重置当前桶的**网页会话**：复用 driver.reset_session，只改状态、不碰页面。"""
    driver.reset_session(bucket)
    logger.info("[命令] key=%s 已按 %s session reset 请求重置网页会话。", bucket, BRIDGE_COMMAND)
    return (
        f"[Bridge] 已把会话桶 {bucket} 标记为「下一轮开新网页会话」。\n"
        "  影响范围：只影响这个会话桶，其它桶不动。\n"
        "  语义：下一轮会新开一条网页会话，并把客户端传来的完整历史**播种**进去（上下文不丢）。\n"
        "  注意：这不是删除客户端的对话历史，也不清除 ChatGPT 账户里的数据或远程会话记录。\n"
        f"  HTTP 等价入口：POST /session/reset?session={bucket}"
        "（设置了 RESET_TOKEN 时需带 X-Reset-Token）。"
    )


def _session_reseed_reply(driver, bucket: str) -> str:
    """排队「下一轮重新播种」：标记落盘，重启 / 并发都不会丢，成功发送后自动清除。"""
    driver.reseed_session(bucket)
    logger.info("[命令] key=%s 已按 %s session reseed 排队重新播种。", bucket, BRIDGE_COMMAND)
    linked = driver.linked_url(bucket)
    target = f"绑定的网页会话 {linked}" if linked else "当前网页会话"
    return (
        f"[Bridge] 会话桶 {bucket} 已排队「下一轮重新播种」。\n"
        f"  下一轮会把客户端传来的完整历史再发进{target}（一次性标记，已落盘，重启后仍有效）。\n"
        "  注意：这不是清除旧消息，而是**再次发送**上下文——那条会话里会出现重复内容。\n"
        "  会话绑定未变；要换新网页会话请用 `/bridge session reset`。"
    )


# ==================== 设置项（按桶持久化，不改进程全局配置）====================


def _setting_get(driver, name: str, bucket: str) -> Optional[bool]:
    if name == "think":
        return driver.think_mode_preference(bucket)
    return driver.save_files_preference(bucket)


def _setting_set(driver, name: str, value: Optional[bool], bucket: str) -> None:
    if name == "think":
        driver.set_think_mode_preference(value, bucket)
    else:
        driver.set_save_files_preference(value, bucket)


def _preference_label(value: Optional[bool]) -> str:
    if value is None:
        return "跟随全局配置"
    return "开启" if value else "关闭"


async def _settings_reply(command: BridgeCommand, driver, bucket: str) -> str:
    name = command.name
    if command.subaction == "set":
        _setting_set(driver, name, command.value, bucket)
        logger.info(
            "[命令] key=%s 已把设置 %s 记为 %s（%s）。",
            bucket, name, _preference_label(command.value), BRIDGE_COMMAND,
        )
        lines = [
            f"[Bridge] 已记录设置「{name}」（会话桶 {bucket}）：{_preference_label(command.value)}"
            "（按桶持久化，只有这个桶受影响）。"
        ]
        if name == "think":
            lines.extend(await _think_apply_lines(driver, bucket, command.value))
        else:
            lines.extend(_save_files_detail_lines(driver, bucket))
        lines.append(f"  查看：`{BRIDGE_COMMAND} settings {name} status`")
        return "\n".join(lines)

    lines = [f"[Bridge] 设置「{name}」（会话桶 {bucket}）："]
    if name == "think":
        lines.extend(await _think_status_lines(driver, bucket))
    else:
        lines.extend(_save_files_status_lines(driver, bucket))
    lines.append(f"  用法：`{BRIDGE_COMMAND} settings {name} [{'|'.join(SETTING_VALUES)}]`")
    return "\n".join(lines)


async def _think_status_lines(driver, bucket: str) -> List[str]:
    """思考模式：偏好（持久化）与**页面实际状态**分开报告，二者不能混为一谈。"""
    preference = driver.think_mode_preference(bucket)
    default = "true" if config.THINK_MODE_DEFAULT else "false"
    lines = [f"  思考模式偏好：{_preference_label(preference)}"
             + (f"（THINK_MODE_DEFAULT={default}）" if preference is None else "（按桶持久化）")]
    live = await driver.think_mode_state(bucket)
    if live is None:
        if driver.bucket_page(bucket) is None:
            lines.append("  网页实际状态：未知（该桶页面尚未打开）")
        else:
            lines.append("  网页实际状态：未知（当前页面上找不到思考模式控件，不谎报）")
    else:
        lines.append(f"  网页实际状态：{'已开启' if live else '已关闭'}")
    if preference is not None and live is not None and live is not preference:
        lines.append("  注意：偏好与网页实际状态不一致，下一次发到该桶时会再切一次。")
    return lines


async def _think_apply_lines(driver, bucket: str, value: Optional[bool]) -> List[str]:
    """写入偏好后**立即**在当前页面尝试生效，并如实报告是否验证成功。"""
    if value is None:
        return ["  网页实际状态：改为跟随全局配置，下一次发到该桶时按配置处理。"]
    if driver.bucket_page(bucket) is None:
        return ["  网页实际状态：页面尚未打开，下一次发到该桶时会应用并校验。"]
    live = await driver.apply_think_mode(bucket)
    if live is None:
        return ["  网页实际状态：未能在当前页面切换（找不到控件或切换未生效）——"
                "偏好已记录，下一次发到该桶时会再试。"]
    if live is value:
        return [f"  网页实际状态：已同步（当前页面{'已开启' if live else '已关闭'}）。"]
    return [f"  网页实际状态：仍是{'已开启' if live else '已关闭'}（切换未生效）——"
            "偏好已记录，下一次发到该桶时会再试。"]


def _save_files_status_lines(driver, bucket: str) -> List[str]:
    preference = driver.save_files_preference(bucket)
    lines = [f"  代码块落盘偏好：{_preference_label(preference)}"
             + ("（SAVE_FILES）" if preference is None else "（按桶持久化）")]
    lines.extend(_save_files_detail_lines(driver, bucket))
    return lines


def _save_files_detail_lines(driver, bucket: str) -> List[str]:
    """落盘行为说明：写到哪、会不会覆盖、怎么关。（正文默认不落盘。）"""
    preference = driver.save_files_preference(bucket)
    effective = bool(config.SAVE_FILES if preference is None else preference)
    lines = [
        f"  当前生效：{'开启' if effective else '关闭'}"
        "（优先级：请求的 save_files 字段 > 本桶偏好 > SAVE_FILES 配置）",
    ]
    if effective:
        lines.append(
            f"  落盘目录：{config.OUTPUT_DIR}（请求可用 output_dir 覆盖）"
        )
        lines.append(
            "  落盘行为：每次回复**新建**文件（code_<时间戳>_<序号>_<随机>.<扩展名>；"
            "没有代码块时 response_<时间戳>_<随机>.md），不会覆盖已有文件。"
        )
    lines.append("  关闭方式：`/bridge settings save-files off`（本桶）或请求里显式传 save_files=false。")
    return lines


def _one_line(text: str, limit: int = 200) -> str:
    """只保留错误的第一行有效内容并限长。

    回复里不带换行，也不会把整段堆栈 / 多行内部报告倒进对话：只需要足以定位问题的
    一句话，其余细节留在日志里。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    flat = " ".join(lines[0].split()) if lines else ""
    return flat[:limit] + ("…" if len(flat) > limit else "")
