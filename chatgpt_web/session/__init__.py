"""会话状态包（PI-905）。

把原先单文件 :mod:`chatgpt_web.session_store` 拆成：

    schema.py     SessionState + 状态文档 schema 常量/校验
    migration.py  v1 -> v2 迁移
    lock.py       落盘串行化锁
    store.py      SessionStoreMixin（分桶状态 + 磁盘读写）

**兼容契约**：:mod:`chatgpt_web.session_store` 仍是 facade，历史 import
（``from chatgpt_web.session_store import SessionState, SessionStoreMixin``）与
``patch.object(session_store, ...)`` 不变。
"""

from .schema import (
    STATE_SCHEMA_VERSION,
    SessionState,
    empty_state_document,
    session_payload_only,
    validate_state_document,
)
from .migration import migrate_state_document
from .lock import STATE_FILE_LOCK
from .store import SessionStoreMixin

__all__ = [
    "STATE_SCHEMA_VERSION",
    "SessionState",
    "SessionStoreMixin",
    "STATE_FILE_LOCK",
    "empty_state_document",
    "session_payload_only",
    "validate_state_document",
    "migrate_state_document",
]
