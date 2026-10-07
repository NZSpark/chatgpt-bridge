"""会话桶状态与磁盘持久化（PI-905）：``SessionStoreMixin``。

负责：按任务分桶的 :class:`~chatgpt_web.session.schema.SessionState`、磁盘读写、
轮转判定、以及默认桶的属性别名（``session_has_history`` 等），保持既有调用与
测试不变。

**facade 契约**：``json`` / ``_warned_bad_state`` / ``logger`` / ``STATE_SCHEMA_VERSION``
都在**调用时**从 :mod:`chatgpt_web.session_store`（facade）取，因此
``patch.object(session_store, ...)`` 与 ``assertLogs("chatgpt_web.session_store")``
依旧生效。
"""

import asyncio
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from .. import config
from ..errors import DEFAULT_SESSION_KEY
from .lock import STATE_FILE_LOCK
from .migration import migrate_state_document
from .schema import (
    STATE_SCHEMA_VERSION,
    SessionState,
    empty_state_document,
    session_payload_only,
)


#: Fallback logger name must match the historical module path.
logger = logging.getLogger("chatgpt_web.session_store")


def _facade():
    """Return the facade module (``chatgpt_web.session_store``) if importable.

    The facade holds the patchable knobs (``json``, ``_warned_bad_state``,
    ``logger``). Fall back to this module when imported in isolation.
    """
    return sys.modules.get("chatgpt_web.session_store", sys.modules[__name__])


def _json():
    mod = _facade()
    return getattr(mod, "json", None) or _stdlib_json()


def _stdlib_json():
    import json as _json_mod

    return _json_mod


def _log():
    return getattr(_facade(), "logger", logger)


def _schema_version() -> int:
    return getattr(_facade(), "STATE_SCHEMA_VERSION", STATE_SCHEMA_VERSION)


class SessionStoreMixin:
    # ---------- 会话桶 ----------
    def _state(self, key: Optional[str] = None) -> SessionState:
        """取出某个会话桶的状态；首次访问时从磁盘恢复。"""
        bucket = key or DEFAULT_SESSION_KEY
        state = self._sessions.get(bucket)
        if state is None:
            state = SessionState.from_payload(self._load_session_state(bucket))
            self._sessions[bucket] = state
            self._evict_session_cache()
        return state

    def _evict_session_cache(self) -> None:
        """内存会话缓存超限时按最久未用逐出（状态已落盘，安全）。

        默认桶永不逐出；正在持有锁 / 活跃的桶也不逐出，避免打断进行中的请求。
        """
        limit = config.MAX_SESSION_STATE_CACHE
        if limit <= 0 or len(self._sessions) <= limit:
            return
        candidates = [
            bucket for bucket in self._sessions
            if bucket != DEFAULT_SESSION_KEY and not self.bucket_busy(bucket)
        ]
        # 用页面最近使用时间作为 LRU 依据；没有页面记录的排在最前（最旧）。
        candidates.sort(key=lambda b: self._page_last_used.get(b, 0.0))
        for bucket in candidates[: max(0, len(self._sessions) - limit)]:
            self._sessions.pop(bucket, None)
            self._last_prompts.pop(bucket, None)

    def _page_for(self, key: Optional[str] = None):
        """取出某个会话桶的页面；默认桶就是 ``self.page``。"""
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY:
            return self.page
        return self._pages.get(bucket)

    def sent_prompt(self, key: Optional[str] = None) -> Optional[str]:
        """某个会话桶最近一次真正发给网页版的 prompt（可能因轮转由增量改选播种版）。"""
        return self._last_prompts.get(key or DEFAULT_SESSION_KEY)

    # ---- 默认桶的状态：保留为属性，兼容既有调用与测试 ----
    @property
    def session_has_history(self) -> bool:
        return self._state().has_history

    @session_has_history.setter
    def session_has_history(self, value: bool) -> None:
        self._state().has_history = bool(value)

    @property
    def session_turns(self) -> int:
        return self._state().turns

    @session_turns.setter
    def session_turns(self, value: int) -> None:
        self._state().turns = int(value)

    @property
    def session_est_tokens(self) -> int:
        return self._state().est_tokens

    @session_est_tokens.setter
    def session_est_tokens(self, value: int) -> None:
        self._state().est_tokens = int(value)

    @property
    def session_cap_hit(self) -> bool:
        return self._state().cap_hit

    @session_cap_hit.setter
    def session_cap_hit(self, value: bool) -> None:
        self._state().cap_hit = bool(value)

    @property
    def last_error(self) -> Optional[str]:
        return self._state().last_error

    @last_error.setter
    def last_error(self, value: Optional[str]) -> None:
        self._state().last_error = value

    @property
    def _pending_rotation(self) -> bool:
        return self._state().pending_rotation

    @_pending_rotation.setter
    def _pending_rotation(self, value: bool) -> None:
        self._state().pending_rotation = bool(value)

    # ---------- 会话状态持久化（不再涉及会话 URL）----------
    def _read_state_file(self) -> Dict[str, Any]:
        """读取原始状态文件（解析失败或非 JSON 时返回空字典）。

        文件**存在但读不出来/解析不了**时记一条 warning：以前静默返回 ``{}``，
        结果是全部会话桶的预算与到顶标记被无声丢弃（文件损坏无从得知）。
        为免每次访问都刷屏，同一个进程只提醒一次。
        """
        facade = _facade()
        try:
            if not config.SESSION_FILE.exists():
                return {}
            raw = config.SESSION_FILE.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            if not getattr(facade, "_warned_bad_state", False):
                facade._warned_bad_state = True
                _log().warning("读取会话状态文件失败（%s）：本次按空状态继续。",
                               config.SESSION_FILE, exc_info=True)
            return {}
        if not raw.startswith("{"):
            if raw and not getattr(facade, "_warned_bad_state", False):
                facade._warned_bad_state = True
                _log().warning("会话状态文件不是 JSON（%s）：本次按空状态继续。",
                               config.SESSION_FILE)
            return {}
        try:
            data = _json().loads(raw)
            return migrate_state_document(data)
        except Exception:  # noqa: BLE001
            if not getattr(facade, "_warned_bad_state", False):
                facade._warned_bad_state = True
                _log().warning("会话状态文件解析失败或迁移失败（%s）：本次按空状态继续。",
                               config.SESSION_FILE, exc_info=True)
            return empty_state_document()

    def _load_session_state(self, key: Optional[str] = None) -> Dict[str, Any]:
        """读取某个会话桶的状态（轮数 / 体积 / 是否到顶 / 上次错误）。"""
        bucket = key or DEFAULT_SESSION_KEY
        data = self._read_state_file()
        if bucket == DEFAULT_SESSION_KEY:
            return session_payload_only(data)
        extra = data.get("sessions")
        own = extra.get(bucket) if isinstance(extra, dict) else None
        return session_payload_only(own) if isinstance(own, dict) else {}

    def _save_session_state(self, key: Optional[str] = None) -> None:
        """落盘某个会话桶的状态，供轮转决策与跨重启延续预算使用。

        整段「读改写」由 :data:`~chatgpt_web.session.lock.STATE_FILE_LOCK` 保护：
        并发落盘（多桶 / 多 Agent）时不会互相覆盖——旧实现的后写会把先写的
        那份整个覆盖掉。写入用「临时文件 + ``replace``」原子替换，中途失败不会
        损坏原文件。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        state.updated_at = int(time.time())
        payload = state.to_payload()
        schema_version = _schema_version()

        with STATE_FILE_LOCK:
            data = self._read_state_file()
            extra = data.get("sessions")
            extra = dict(extra) if isinstance(extra, dict) else {}
            if bucket == DEFAULT_SESSION_KEY:
                payload = {
                    "schema_version": schema_version,
                    **session_payload_only(payload),
                    "sessions": extra,
                }
            else:
                extra[bucket] = session_payload_only(payload)
                data = {k: v for k, v in data.items() if k != "sessions"}
                data["schema_version"] = schema_version
                data["sessions"] = extra
                payload = data
            try:
                config.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                # 原子写（临时文件 + replace）：中途挂掉也不会留下半截 JSON
                tmp = config.SESSION_FILE.with_name(config.SESSION_FILE.name + ".tmp")
                tmp.write_text(
                    _json().dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                tmp.replace(config.SESSION_FILE)
            except Exception:  # noqa: BLE001
                # 状态落盘失败不该静默：会让轮转决策与重启后的预算延续失真
                _log().warning("会话状态落盘失败（key=%s）：已忽略，下一轮会重试。",
                               bucket, exc_info=True)

    async def _remember_session(self, key: Optional[str] = None) -> None:
        """刷新落盘的会话状态（保留此名字，兼容既有调用）。

        文件 I/O 放到线程里执行（``asyncio.to_thread``）：写的是整个状态文件，
        大时同步执行会卡住事件循环，拖慢 SSE keep-alive 与其它会话桶（T3.3）。
        """
        await asyncio.to_thread(self._save_session_state, key)

    def session_keys(self) -> List[str]:
        """当前在用的会话桶（至少包含默认桶）。"""
        return sorted({DEFAULT_SESSION_KEY, *self._sessions})

    def needs_seed(self, key: Optional[str] = None) -> bool:
        """某个会话桶的网页会话里没有可用上下文时，需要把完整历史播种进去。"""
        return not self._state(key).has_history

    def session_stats(self, key: Optional[str] = None) -> Dict[str, Any]:
        """供 /healthz 观察会话增长情况。"""
        state = self._state(key)
        return {
            "has_history": state.has_history,
            "needs_seed": self.needs_seed(key),
            "turns": state.turns,
            "est_tokens": state.est_tokens,
            "cap_hit": state.cap_hit,
            "pending_rotation": state.pending_rotation,
            "last_error": state.last_error,
            # 连续“到顶”失败次数：>=2 时播种内容会被自动压缩（防死循环）
            "cap_failures": getattr(state, "cap_failures", 0),
            "buckets": self.session_keys(),
        }

    def _session_over_budget(self, key: Optional[str] = None) -> bool:
        """会话体积是否已达到轮转阈值（0 表示禁用该维度）。"""
        state = self._state(key)
        if config.SESSION_MAX_TURNS and state.turns >= config.SESSION_MAX_TURNS:
            return True
        if config.SESSION_MAX_TOKENS and state.est_tokens >= config.SESSION_MAX_TOKENS:
            return True
        return False

    def reset_session(self, key: Optional[str] = None) -> None:
        """把某个会话桶标记为“下一轮开新会话”（手动逃生口）。

        只改状态、不碰页面：下一轮的 ``send_chat`` 会先轮转，并用“播种”
        prompt 重放历史，所以不会丢上下文。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        state.pending_rotation = True
        state.cap_hit = False
        state.has_history = False
        self._save_session_state(key=bucket)
        _log().info(f"[会话] 已请求重置 key={bucket} 的会话，下一轮将开启新会话并播种上下文。")
