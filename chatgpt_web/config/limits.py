"""LimitConfig domain module (PI-906).

Groups the completion/timeout and storage limits that bound a single request.
``LimitConfig`` is an alias for the completion limits snapshot
(:class:`chatgpt_web.config._core.CompletionConfig`); ``StorageConfig`` covers the
on-disk retention limits. Both are re-exported here so limit-related code has a
single import location.
"""

from ._core import CompletionConfig, StorageConfig

#: Documented PI-906 name for the per-request completion/timeout limits.
LimitConfig = CompletionConfig

__all__ = ["LimitConfig", "CompletionConfig", "StorageConfig"]
