"""会话状态文档迁移（PI-905）：v1（legacy root/default）-> v2。

迁移失败 / 不支持版本时抛 :class:`ValueError`，由 store 层捕获并按空状态继续。
"""

import sys
import logging
from typing import Any, Dict

from .schema import STATE_SCHEMA_VERSION, validate_state_document


def _logger() -> logging.Logger:
    """Return the facade logger so ``assertLogs("chatgpt_web.session_store")`` matches."""
    facade = sys.modules.get("chatgpt_web.session_store")
    if facade is not None and hasattr(facade, "logger"):
        return facade.logger
    return logging.getLogger("chatgpt_web.session_store")


def migrate_state_document(data: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate the legacy root/default + sessions format to schema v2."""
    if not isinstance(data, dict):
        raise ValueError("session state root must be an object")
    version = data.get("schema_version")
    legacy_version = data.get("version")
    if version == STATE_SCHEMA_VERSION:
        return validate_state_document(dict(data))
    if version not in (None, 1) or legacy_version not in (None, 1):
        raise ValueError(
            f"unsupported session state schema marker: {version!r}/{legacy_version!r}"
        )

    migrated = dict(data)
    migrated.pop("version", None)
    migrated["schema_version"] = STATE_SCHEMA_VERSION
    sessions = migrated.get("sessions")
    migrated["sessions"] = dict(sessions) if isinstance(sessions, dict) else {}
    _logger().info("会话状态文件已由旧格式迁移到 schema v%d。", STATE_SCHEMA_VERSION)
    return validate_state_document(migrated)
