"""会话状态落盘的并发控制（PI-905）。

状态文件是「整个文件读改写」的共享资源；跨线程（``asyncio.to_thread``）并发
落盘时必须串行化，否则后写会把先写的整个覆盖掉（T3.3 验收用例）。
"""

import threading

#: 保护状态文件「读改写」的进程内锁。
STATE_FILE_LOCK = threading.Lock()

#: 状态文件损坏时只提醒一次的标记（每轮都会读它，反复 warning 会刷屏）。
#:
#: 说明：兼容契约要求 ``patch.object(session_store, "_warned_bad_state", ...)``
#: 生效，因此真正的读写在 :mod:`chatgpt_web.session.store` 里通过 facade 模块
#: 访问该名字，而不是直接 import 这个变量。
WARNED_BAD_STATE = False

__all__ = ["STATE_FILE_LOCK", "WARNED_BAD_STATE"]
