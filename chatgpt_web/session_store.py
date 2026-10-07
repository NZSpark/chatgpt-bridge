"""会话状态与持久化（``SessionStoreMixin``）——兼容 facade。

PI-905 之后实现拆到 :mod:`chatgpt_web.session` 包：

    session/schema.py     SessionState + schema 常量/校验
    session/migration.py  v1 -> v2 迁移
    session/lock.py       落盘串行化锁
    session/store.py      SessionStoreMixin

本模块保留历史 import 路径，并把测试/旧代码直接 ``patch.object`` 的旋钮
（``json`` / ``_warned_bad_state`` / ``logger`` / ``STATE_SCHEMA_VERSION``）
继续暴露在这里；:mod:`chatgpt_web.session.store` 在**调用时**回读本 facade，
故这些 patch 依旧生效。
"""

import json  # noqa: F401  (facade 契约：测试 patch.object(session_store.json, ...))
import logging

from .session import (  # noqa: F401  (re-export)
    STATE_FILE_LOCK as _STATE_FILE_LOCK,
    STATE_SCHEMA_VERSION,
    SessionState,
    SessionStoreMixin,
    empty_state_document as _empty_state_document,
    migrate_state_document as _migrate_state_document,
    session_payload_only as _session_payload_only,
    validate_state_document as _validate_state_document,
)
from .session.migration import migrate_state_document  # noqa: F401
from .session.schema import (  # noqa: F401
    empty_state_document,
    session_payload_only,
    validate_state_document,
)

logger = logging.getLogger("chatgpt_web.session_store")

#: 状态文件损坏时只提醒一次的标记；由 store 层通过本 facade 读写。
_warned_bad_state = False

__all__ = [
    "SessionStoreMixin",
    "SessionState",
    "STATE_SCHEMA_VERSION",
    "_STATE_FILE_LOCK",
    "_warned_bad_state",
    "logger",
]
