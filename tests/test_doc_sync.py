"""防止 README 配置默认值表与 config.py 漂移。"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


ROOT = Path(__file__).resolve().parent.parent



def _env_values():
    values = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class PromptDesignDocSyncTests(unittest.TestCase):
    """doc/prompt_design.md §2 的标题表必须与代码里的常量一致。

    标题常量是「生成 / 去重 / 泄漏检测」的唯一键（design §2、§6 I1）。
    文档漂移会让读者以为某个标题存在，而代码里其实是别的字符串。
    """

    # design §2 表格里声明的「常量名 → 期望值」；改标题时必须两边一起改。
    EXPECTED = {
        "TOOLCALL_HEADER": "[Tool Calling Instructions]",
        "EMPHASIS_HEADER": "[Output Format Emphasis]",
        "EDIT_MD_HEADER": "[edit_markdown notes]",
        "RETRY_HEADER": "[Tool Call Correction]",
        "JSON_FORMAT_HEADER": "[Tool Call JSON Format Error]",
        "REPEAT_HEADER": "[Repeated Empty Tool Call]",
        "SECTION_USER": "[User Request]",
        "SECTION_TOOL_RESULTS": "[Tool Results]",
        "CONTEXT_REBUILD_HEADER": "[上下文重建]",
        "ENV_NOTE_HEADER": "[环境说明]",
    }

    def test_doc_table_matches_code_constants(self):
        from chatgpt_web import toolcalls

        doc = (ROOT / "doc" / "prompt_design.md").read_text(encoding="utf-8")
        for name, value in self.EXPECTED.items():
            self.assertTrue(
                hasattr(toolcalls, name), "代码里缺少标题常量 %s" % name
            )
            self.assertEqual(
                getattr(toolcalls, name), value,
                "%s 的代码值与文档不一致" % name,
            )
            self.assertIn(
                value, doc, "doc/prompt_design.md 未记录 %s 的值 %s" % (name, value)
            )

    def test_titles_defined_once_in_toolcalls(self):
        """标题常量必须单点定义（design §2 / §9 实现位置表）。"""
        prompting = (ROOT / "chatgpt_web" / "prompting.py").read_text(encoding="utf-8")
        for name, value in self.EXPECTED.items():
            self.assertNotIn(
                '%s = "%s"' % (name, value), prompting,
                "%s 不得在 prompting.py 里重复定义" % name,
            )


class DesignDocSyncTests(unittest.TestCase):
    def test_env_example_lists_every_config_key(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
        # 至少覆盖这些新增/关键键，避免模板悄悄落后
        for key in (
            "SAVE_FILES", "OUTPUT_MAX_FILES", "OUTPUT_MAX_AGE_DAYS",
            "MAX_SESSION_STATE_CACHE", "SESSION_MAX_TURNS", "RESPONSES_TOOL_BUFFER",
        ):
            self.assertIn(key, documented)

    def test_readme_table_defaults_match_config_literals(self):
        """README 变量表里的「默认值」必须等于 config.py 里 env_* 的字面量默认值。

        背景：README 表格曾与 config.py 脱节（README 写 1000000/120/10000000，
        config 却是 100000/60/60000），因为旧测试只校验 design.md 的 4 个键，
        没覆盖 README。这里把 README 表格整体纳入校验。

        注意：README 明确写「列出的是 config.py 的**内置默认值**」，因此这里
        对照的是 config.py 源码里的字面量默认值（而非被 .env 覆盖后的运行值）。
        """
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        # PI-906 之后 config 是包：合并包内全部 *.py 再抽取字面量默认值。
        config_dir = ROOT / "chatgpt_web" / "config"
        config_src = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(config_dir.glob("*.py"))
        )

        # README 表格行：| `KEY` | `VALUE` | 说明 |
        rows = re.findall(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*`([^`]*)`\s*\|", readme, re.MULTILINE)
        self.assertTrue(rows, "README 未解析到任何变量表行")

        def builtin_default(key: str):
            """从 config 包取 env_*("KEY", <default>) 的字面量默认值；取不到返回 None。"""
            match = re.search(
                rf'env_(?:int|float|bool|str)\(\s*"{re.escape(key)}"\s*,\s*([^)\n]+?)\s*\)',
                config_src,
            )
            if not match:
                return None
            return match.group(1).strip().strip('"').strip("'")

        checked = 0
        for key, documented in rows:
            actual = builtin_default(key)
            if actual is None:
                # README 里可能记录的是组合/派生项（config 无直接 env_* 字面量），跳过
                continue
            checked += 1
            try:
                same = float(actual) == float(documented)
            except ValueError:
                same = str(actual).lower() == str(documented).lower()
            self.assertTrue(
                same,
                f"{key}: README 写 {documented}，config 内置默认 {actual}",
            )
        self.assertGreater(checked, 0, "没有校验到任何 README 变量表默认值")


if __name__ == "__main__":
    unittest.main()
