"""SessionConfig domain module (PI-906).

Re-exports the session-facing typed snapshot from :mod:`chatgpt_web.config._core`.
"""

from ._core import SessionConfig

__all__ = ["SessionConfig"]
