"""ServerConfig domain module (PI-906).

Re-exports the server-facing typed snapshot from :mod:`chatgpt_web.config._core`,
so callers can ``from chatgpt_web.config.server import ServerConfig``.
"""

from ._core import ServerConfig

__all__ = ["ServerConfig"]
