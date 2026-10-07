"""BrowserConfig domain module (PI-906).

Re-exports the browser-facing typed snapshot from :mod:`chatgpt_web.config._core`.
"""

from ._core import BrowserConfig

__all__ = ["BrowserConfig"]
