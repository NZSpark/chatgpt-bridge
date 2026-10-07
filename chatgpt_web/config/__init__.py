"""配置包（PI-906）——同时是历史 ``config`` 模块的兼容 facade。

原先单文件 ``chatgpt_web/config.py`` 现拆为包：

    _core.py     环境加载（.env）+ 全部可调参数 + typed Config 快照 + load_config
    server.py    ServerConfig
    browser.py   BrowserConfig
    session.py   SessionConfig
    tools.py     ToolConfig（PI-906 新增）
    limits.py    LimitConfig（PI-906 新增）
    debug.py     DebugConfig（PI-906 新增）

**兼容契约**：所有 ``config.<NAME>`` 访问（历史代码与 ``patch.object(config, ...)``）
不变——``__init__`` 把 ``_core`` 的全部公开名字再导出；各 domain 模块只是把
对应 typed 快照再导出一次，便于按领域 import。

注意：``chatgpt_web.config`` 现在是**包**，故 ``import chatgpt_web.config`` /
``from chatgpt_web import config`` 解析到本 ``__init__``。
"""

from ._core import *  # noqa: F401,F403
from ._core import (  # noqa: F401  (显式再导出关键名字，便于静态分析)
    PROJECT_ROOT,
    ENV_FILE,
    env_bool,
    env_float,
    env_int,
    env_str,
    load_config,
    _load_env_file,
)
from .browser import BrowserConfig
from .debug import DebugConfig
from .limits import LimitConfig
from .server import ServerConfig
from .session import SessionConfig
from .tools import ToolConfig

__all__ = [
    "PROJECT_ROOT",
    "ENV_FILE",
    "env_str",
    "env_int",
    "env_float",
    "env_bool",
    "load_config",
    "ServerConfig",
    "BrowserConfig",
    "SessionConfig",
    "ToolConfig",
    "LimitConfig",
    "DebugConfig",
]
