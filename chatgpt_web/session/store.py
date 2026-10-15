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
            self._last_speeds.pop(bucket, None)
            self._last_speed_request_ids.pop(bucket, None)
            for request_id, sample_ref in list(self._speed_samples_by_request_id.items()):
                if sample_ref[0] == bucket:
                    self._speed_samples_by_request_id.pop(request_id, None)

    def _page_for(self, key: Optional[str] = None):
        """取出某个会话桶的页面；默认桶就是 ``self.page``。"""
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY:
            return self.page
        return self._pages.get(bucket)

    def sent_prompt(self, key: Optional[str] = None) -> Optional[str]:
        """某个会话桶最近一次真正发给网页版的 prompt（可能因轮转由增量改选播种版）。"""
        return self._last_prompts.get(key or DEFAULT_SESSION_KEY)

    def last_speed(self, key: Optional[str] = None):
        """某个会话桶最近一次成功生成的基础 TokenSpeed（仅内存）。"""
        return self._last_speeds.get(key or DEFAULT_SESSION_KEY)

    def speed_for_request(self, request_id: str):
        """取指定流式请求自己的基础速度样本，避免并发请求串样本。"""
        record = self._speed_samples_by_request_id.get(request_id)
        return record[1] if record is not None else None

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

    # ---------- 会话状态持久化 ----------
    # 注：这里不再保存「自动探测到的」会话 URL（恢复一律靠新开会话 + 播种）；
    # 唯一被持久化的地址是用户用 ``/link`` **显式绑定**的那条（``linked_url``）。
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
            "linked_url": state.linked_url,
            # 聊天命令（/bridge session、/bridge settings）排队的 / 持久化的状态
            "pending_reseed": state.pending_reseed,
            "think_mode": state.think_mode,
            "save_files": state.save_files,
            "last_error": state.last_error,
            # 连续“到顶”失败次数：>=2 时播种内容会被自动压缩（防死循环）
            "cap_failures": getattr(state, "cap_failures", 0),
            # /bridge token：当前桶的滚动速度统计与最近一次样本。
            "speed_samples": state.speed_samples,
            "speed_recent_samples": [dict(sample) for sample in state.speed_recent_samples],
            "speed_prefill_tps_sum": state.speed_prefill_tps_sum,
            "speed_decode_tps_sum": state.speed_decode_tps_sum,
            "speed_ttft_s_sum": state.speed_ttft_s_sum,
            "speed_decode_s_sum": state.speed_decode_s_sum,
            "speed_prompt_tokens_sum": state.speed_prompt_tokens_sum,
            "speed_reply_tokens_sum": state.speed_reply_tokens_sum,
            "speed_last_prompt_tokens": state.speed_last_prompt_tokens,
            "speed_last_reply_tokens": state.speed_last_reply_tokens,
            "speed_last_ttft_s": state.speed_last_ttft_s,
            "speed_last_decode_s": state.speed_last_decode_s,
            "speed_last_total_s": state.speed_last_total_s,
            "speed_last_ttft_adj_s": state.speed_last_ttft_adj_s,
            "speed_last_decode_adj_s": state.speed_last_decode_adj_s,
            "speed_last_poll_credit_s": state.speed_last_poll_credit_s,
            "speed_last_settle_credit_s": state.speed_last_settle_credit_s,
            "speed_last_prefill_tps": state.speed_last_prefill_tps,
            "speed_last_decode_tps": state.speed_last_decode_tps,
            "speed_last_e2e_ttft_s": state.speed_last_e2e_ttft_s,
            "speed_last_e2e_decode_tps": state.speed_last_e2e_decode_tps,
            "speed_last_bridge_overhead": state.speed_last_bridge_overhead,
            "speed_last_e2e_valid": state.speed_last_e2e_valid,
            "speed_last_saw_generating": state.speed_last_saw_generating,
            "speed_last_streamed": state.speed_last_streamed,
            "speed_last_rewritten": state.speed_last_rewritten,
            "buckets": self.session_keys(),
        }

    @staticmethod
    def _clean_e2e_value(value: Optional[float], *, maximum: Optional[float] = None) -> float:
        if value is None:
            return 0.0
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not (result >= 0.0 and result < float("inf")):
            return 0.0
        return min(maximum, result) if maximum is not None else result

    def _apply_streaming_e2e(
        self,
        bucket: str,
        sample: Dict[str, float],
        *,
        e2e_ttft_s: Optional[float],
        e2e_decode_tps: Optional[float],
        bridge_overhead_ratio: Optional[float],
    ) -> None:
        """更新指定样本；仅当它仍是最新样本时同步更新 speed_last_*。"""
        ttft = self._clean_e2e_value(e2e_ttft_s)
        decode = self._clean_e2e_value(e2e_decode_tps)
        overhead = self._clean_e2e_value(bridge_overhead_ratio, maximum=1.0)
        valid = e2e_ttft_s is not None and e2e_decode_tps is not None
        if any(row is sample for row in self._state(bucket).speed_recent_samples):
            sample.update({
                "e2e_ttft_s": ttft,
                "e2e_decode_tps": decode,
                "bridge_overhead_ratio": overhead,
                "e2e_valid": float(valid),
            })

        state = self._state(bucket)
        latest_request_id = self._last_speed_request_ids.get(bucket)
        # Legacy callers (or older fake drivers) don't pass request IDs; they target
        # the current sample. Request-scoped callers are guarded by the caller below.
        if latest_request_id is None or latest_request_id == getattr(self, "_e2e_update_request_id", None):
            state.speed_last_e2e_ttft_s = ttft
            state.speed_last_e2e_decode_tps = decode
            state.speed_last_bridge_overhead = overhead
            state.speed_last_e2e_valid = valid
            state.speed_last_streamed = True
        self._save_session_state(bucket)

    def record_streaming_e2e_for_request(
        self,
        request_id: str,
        *,
        e2e_ttft_s: Optional[float],
        e2e_decode_tps: Optional[float],
        bridge_overhead_ratio: Optional[float],
    ) -> None:
        """按请求 ID 补写样本；不会覆盖同一桶里更新的另一个请求。"""
        record = self._speed_samples_by_request_id.pop(request_id, None)
        if record is None:
            return
        bucket, _speed, sample = record
        state = self._state(bucket)
        ttft = self._clean_e2e_value(e2e_ttft_s)
        decode = self._clean_e2e_value(e2e_decode_tps)
        overhead = self._clean_e2e_value(bridge_overhead_ratio, maximum=1.0)
        valid = e2e_ttft_s is not None and e2e_decode_tps is not None
        if any(row is sample for row in state.speed_recent_samples):
            sample.update({
                "e2e_ttft_s": ttft,
                "e2e_decode_tps": decode,
                "bridge_overhead_ratio": overhead,
                "e2e_valid": float(valid),
            })
        if self._last_speed_request_ids.get(bucket) == request_id:
            state.speed_last_e2e_ttft_s = ttft
            state.speed_last_e2e_decode_tps = decode
            state.speed_last_bridge_overhead = overhead
            state.speed_last_e2e_valid = valid
            state.speed_last_streamed = True
        self._save_session_state(bucket)

    def record_streaming_e2e(
        self,
        key: Optional[str],
        *,
        e2e_ttft_s: Optional[float],
        e2e_decode_tps: Optional[float],
        bridge_overhead_ratio: Optional[float],
    ) -> None:
        """兼容旧调用：把端到端指标补写到该桶最近一次样本。"""
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        sample = state.speed_recent_samples[-1] if state.speed_recent_samples else {}
        ttft = self._clean_e2e_value(e2e_ttft_s)
        decode = self._clean_e2e_value(e2e_decode_tps)
        overhead = self._clean_e2e_value(bridge_overhead_ratio, maximum=1.0)
        valid = e2e_ttft_s is not None and e2e_decode_tps is not None
        if sample:
            sample.update({
                "e2e_ttft_s": ttft,
                "e2e_decode_tps": decode,
                "bridge_overhead_ratio": overhead,
                "e2e_valid": float(valid),
            })
        state.speed_last_e2e_ttft_s = ttft
        state.speed_last_e2e_decode_tps = decode
        state.speed_last_bridge_overhead = overhead
        state.speed_last_e2e_valid = valid
        state.speed_last_streamed = True
        self._save_session_state(bucket)

    def _session_over_budget(self, key: Optional[str] = None) -> bool:
        """会话体积是否已达到轮转阈值（0 表示禁用该维度）。"""
        state = self._state(key)
        if config.SESSION_MAX_TURNS and state.turns >= config.SESSION_MAX_TURNS:
            return True
        if config.SESSION_MAX_TOKENS and state.est_tokens >= config.SESSION_MAX_TOKENS:
            return True
        return False

    # ---------- 会话绑定（/link，见 chatgpt_web.linking）----------
    def linked_url(self, key: Optional[str] = None) -> Optional[str]:
        """该桶当前绑定的网页会话 URL（``None`` = 未绑定）。"""
        return self._state(key).linked_url

    def link_session(self, url: str, key: Optional[str] = None, seed: bool = False) -> str:
        """把某个会话桶绑定到指定的网页会话 URL，返回实际记录下来的地址。

        ``seed=False``（默认）：该会话被视为**已有此前上下文** → 之后只发增量
        （这是「ChatGPT 自己开了新会话、用户把会话地址丢过来」的常见情形）；
        ``seed=True``：下一次发送把完整历史**播种**进去——那条会话里其实没有
        此前记录时用（代价是历史在网页端会重复一遍）。

        只改状态、**不碰页面**：真正的导航由发送前的
        ``PagePoolMixin._ensure_linked_target`` 完成（页面漂移 / 句柄重建 /
        启动恢复都会回到这条会话），所以浏览器没起、句柄已经失效时这条命令照样有效。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        state.linked_url = url
        state.has_history = not seed
        state.cap_hit = False
        state.pending_rotation = False
        state.last_error = None
        self._save_session_state(key=bucket)
        _log().info(
            "[绑定] key=%s 已绑定网页会话 %s（播种=%s，之后每轮都会确保页面停在该会话上）。",
            bucket, url, seed,
        )
        return url

    def unlink_session(self, key: Optional[str] = None) -> bool:
        """解除绑定（返回是否真的解除过）。

        恢复默认行为：下一轮先轮转到一个新会话、再播种完整历史——否则会把下一轮
        的增量发进那条已经不归我们管的会话里。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        had = bool(state.linked_url)
        state.linked_url = None
        state.has_history = False
        state.pending_rotation = True
        self._save_session_state(key=bucket)
        if had:
            _log().info("[绑定] key=%s 已解除会话绑定，下一轮将开启新会话并播种上下文。", bucket)
        return had

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

    # ---------- 聊天命令的会话状态（/bridge session、/bridge settings）----------
    def reseed_session(self, key: Optional[str] = None) -> None:
        """排队「下一轮把完整历史重新播种一遍」（``/bridge session reseed``）。

        标记写在会话状态里并**立即落盘**：一次性语义必须能跨进程重启、并发请求与
        重试仍然成立；真正的消费在 ``chat_io._send_chat_with_ladder``（成功发送后清除）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        self._state(bucket).pending_reseed = True
        self._save_session_state(key=bucket)
        _log().info("[命令] key=%s 已排队下一轮重新播种完整历史。", bucket)

    def think_mode_preference(self, key: Optional[str] = None) -> Optional[bool]:
        """该桶的思考模式偏好（``None`` = 未设置，跟随 ``config.THINK_MODE_DEFAULT``）。"""
        return self._state(key).think_mode

    def set_think_mode_preference(self, value: Optional[bool], key: Optional[str] = None) -> None:
        """写入该桶的思考模式偏好（``None`` = 恢复“跟随全局配置”）。

        只改本桶状态、**不碰全局 config**：否则一条来自单个客户端的聊天命令会改变
        其它所有客户端的行头。
        """
        bucket = key or DEFAULT_SESSION_KEY
        self._state(bucket).think_mode = None if value is None else bool(value)
        self._save_session_state(key=bucket)
        _log().info("[命令] key=%s 的思考模式偏好已改为 %s。", bucket, value)

    def save_files_preference(self, key: Optional[str] = None) -> Optional[bool]:
        """该桶的代码块落盘偏好（``None`` = 未设置，跟随 ``config.SAVE_FILES``）。"""
        return self._state(key).save_files

    def set_save_files_preference(self, value: Optional[bool], key: Optional[str] = None) -> None:
        """写入该桶的代码块落盘偏好（``None`` = 恢复“跟随全局配置”）。"""
        bucket = key or DEFAULT_SESSION_KEY
        self._state(bucket).save_files = None if value is None else bool(value)
        self._save_session_state(key=bucket)
        _log().info("[命令] key=%s 的代码块落盘偏好已改为 %s。", bucket, value)
