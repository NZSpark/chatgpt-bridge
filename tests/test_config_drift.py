"""配置漂移检查：.env.example 必须覆盖 config.py 读取的全部配置键。

背景：曾经有 4 个配置项（FILL_RETRIES / FILL_TIMEOUT_MS / PROMPT_MAX_CHARS /
TOOL_RESULT_MAX_CHARS）只在 config.py 里被读取、.env.example 里却没有。
照模板部署的人会缺项，且旧版测试只校验一个**写死的子集**，抓不到新增项。
这里改成全量对齐：从 config.py 里抽出所有 env_xxx("KEY", ...) 的 KEY，
逐个要求在 .env.example 中出现。新增配置项而忘了补模板时，本测试会失败。
"""

import re
import unittest
from pathlib import Path

from chatgpt_web import config  # noqa: F401  （确保 config 可导入）

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = PROJECT_ROOT / "chatgpt_web" / "config.py"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# 匹配 config.py 里的 env_str("KEY", ...) / env_int("KEY", ...) 等调用。
_ENV_KEY_RE = re.compile(r"env_[a-z]+\(\s*\"([A-Z][A-Z0-9_]*)\"")


def _config_keys() -> set:
    text = CONFIG_PY.read_text(encoding="utf-8")
    return set(_ENV_KEY_RE.findall(text))


def _example_keys() -> set:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


class ConfigDriftTests(unittest.TestCase):

    def test_config_keys_are_sane(self):
        # 抽不出 key 说明正则与 config.py 的写法脱节了，宁可失败也别静默通过。
        keys = _config_keys()
        self.assertGreater(len(keys), 20, "从 config.py 抽到的配置键过少，正则可能失效")
        for known in ("HOST", "PORT", "HEADLESS", "PROMPT_MAX_CHARS"):
            self.assertIn(known, keys)

    def test_env_example_covers_all_config_keys(self):
        missing = sorted(_config_keys() - _example_keys())
        self.assertFalse(
            missing,
            f".env.example 缺失以下配置项（config.py 会读取但它们没出现在模板里）: {missing}",
        )

    def test_env_example_has_no_stale_keys(self):
        # 反向：模板里不该有 config.py 根本不读的键（拼写错误 / 废弃项）。
        # 允许少量非 config 的键（如仅文档用途），这里仅报告，不强制为空。
        stale = sorted(_example_keys() - _config_keys())
        if stale:
            self.skipTest(f".env.example 中存在 config.py 未读取的键（可能是废弃项）: {stale}")


if __name__ == "__main__":
    unittest.main()
