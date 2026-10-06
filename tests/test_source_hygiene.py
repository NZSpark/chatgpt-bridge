"""源码级卫生检查（小型的“静态断言”，对照 doc/tasks.md T4.6）。

放在这里而不是靠 code review：这类退化（协程里用 `get_event_loop()`）不会
在单测里报错，但会在 Python 3.12+ / 无当前事件循环的场景下抛异常或拿错 loop。
"""

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBRARY = PROJECT_ROOT / "chatgpt_web"


class GetRunningLoopTests(unittest.TestCase):
    def test_library_does_not_use_get_event_loop(self) -> None:
        offenders = []
        for path in sorted(LIBRARY.glob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"asyncio\.get_event_loop\s*\(", line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertFalse(
            offenders,
            "协程内请用 asyncio.get_running_loop()（get_event_loop 在 3.12+ 已不推荐）：\n"
            + "\n".join(offenders),
        )

    def test_no_legacy_loop_creation_helpers(self) -> None:
        """同时禁止 asyncio.new_event_loop / set_event_loop 之类的自建 loop 用法。"""
        offenders = []
        for path in sorted(LIBRARY.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in ("asyncio.new_event_loop", "asyncio.set_event_loop"):
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        self.assertFalse(offenders, offenders)


if __name__ == "__main__":
    unittest.main(verbosity=2)
