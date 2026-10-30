"""edit_markdown 路径沙箱与写入门槛的单测（doc/tasks.md T1.3 / T4.5）。

背景：`execute_edit_markdown` 过去只校验 path 是「非空字符串」，而模型可以被
网页内容 / 工具结果注入——等于把「以服务进程权限覆盖本机任意文件」的能力交给
了模型。这里锁定修复后的契约：

* 绝对路径、含 `..` 的路径、解析后越出 `EDIT_MARKDOWN_ROOT` 的路径一律拒绝；
* 默认 dry-run；即便模型传 `write=true`，也要 `EDIT_MARKDOWN_WRITE=true` 才落盘；
* `run_local_edit_markdown` 是 server / responses 共用的唯一本地执行入口，
  非 edit_markdown 调用原样透传。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_web import config
from chatgpt_web.toolcalls import (
    EDIT_MARKDOWN_TOOL_NAME,
    execute_edit_markdown,
    resolve_edit_path,
    run_local_edit_markdown,
)

SAMPLE = "# 标题\n\n第一段。\n\n第二段。\n"


class SandboxCase(unittest.TestCase):
    """把沙箱根指向临时目录，避免碰真实仓库文件。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="edit-md-root-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.doc = self.root / "README.md"
        self.doc.write_text(SAMPLE, encoding="utf-8")
        for name, value in (
            ("EDIT_MARKDOWN_ROOT", str(self.root)),
            ("EDIT_MARKDOWN_WRITE", False),
            ("EDIT_MARKDOWN_BACKUP_DIR", str(self.root / "backups")),
        ):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def _edit(self, **overrides):
        args = {
            "path": "README.md",
            "start": 3,
            "end": 3,
            "new_text": "改过的第一段。",
        }
        args.update(overrides)
        return execute_edit_markdown(args, backup_dir=config.EDIT_MARKDOWN_BACKUP_DIR)


class ResolveEditPathTests(SandboxCase):
    def test_relative_path_inside_root_is_accepted(self) -> None:
        resolved, error = resolve_edit_path("README.md")
        self.assertIsNone(error)
        self.assertEqual(resolved, (self.root / "README.md").resolve())

    def test_parent_traversal_is_rejected(self) -> None:
        resolved, error = resolve_edit_path("../../etc/passwd")
        self.assertIsNone(resolved)
        self.assertIn("path rejected", error or "")

    def test_absolute_path_is_rejected(self) -> None:
        resolved, error = resolve_edit_path("/tmp/x.md")
        self.assertIsNone(resolved)
        self.assertIn("absolute paths are not allowed", error or "")

    def test_absolute_path_inside_root_is_still_rejected(self) -> None:
        """即便绝对路径正好在沙箱内也拒绝——模型没有理由给出绝对路径。"""
        resolved, error = resolve_edit_path(str(self.doc))
        self.assertIsNone(resolved)
        self.assertIn("absolute paths are not allowed", error or "")

    def test_symlink_escape_is_rejected(self) -> None:
        outside = self.root.parent / "edit-md-symlink-target.md"
        outside.write_text("外部文件\n", encoding="utf-8")
        self.addCleanup(lambda: outside.exists() and outside.unlink())
        link = self.root / "linked.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        resolved, error = resolve_edit_path("linked.md")
        self.assertIsNone(resolved)
        self.assertIn("path rejected", error or "")

    def test_empty_path_is_rejected(self) -> None:
        resolved, error = resolve_edit_path("   ")
        self.assertIsNone(resolved)
        self.assertIn("requires a non-empty path", error or "")

    def test_non_string_path_is_rejected(self) -> None:
        resolved, error = resolve_edit_path({"a": 1})  # type: ignore[arg-type]
        self.assertIsNone(resolved)
        self.assertIn("requires a non-empty path", error or "")


class ExecuteSandboxTests(SandboxCase):
    def test_outside_paths_are_rejected_without_touching_disk(self) -> None:
        outside = self.root.parent / "outside-should-not-exist.md"
        outside.write_text("原始内容\n", encoding="utf-8")
        self.addCleanup(lambda: outside.exists() and outside.unlink())
        for bad in ("../outside-should-not-exist.md", str(outside)):
            with self.subTest(path=bad):
                result = self._edit(path=bad)
                self.assertFalse(result["ok"], result)
                self.assertIn("path rejected", result["error"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "原始内容\n")

    def test_dry_run_does_not_write(self) -> None:
        result = self._edit()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["written"])
        self.assertIn("-第一段。", result["diff"])
        self.assertEqual(self.doc.read_text(encoding="utf-8"), SAMPLE)

    def test_write_true_is_refused_while_write_gate_is_off(self) -> None:
        result = self._edit(write=True)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["written"])
        self.assertTrue(result["write_requested"])
        self.assertIn("EDIT_MARKDOWN_WRITE=false", result["note"])
        self.assertEqual(self.doc.read_text(encoding="utf-8"), SAMPLE, "文件被意外改写")

    def test_write_true_writes_when_gate_is_on(self) -> None:
        with mock.patch.object(config, "EDIT_MARKDOWN_WRITE", True):
            result = self._edit(write=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["written"])
        self.assertIsNotNone(result["backup"])
        text = self.doc.read_text(encoding="utf-8")
        self.assertIn("改过的第一段。", text)
        self.assertNotIn("第一段。", text.replace("改过的第一段。", ""))

    def test_line_range_still_validated(self) -> None:
        result = self._edit(start=1, end=999)
        self.assertFalse(result["ok"])
        self.assertIn("line range out of bounds", result["error"])

    def test_backup_failure_never_writes(self) -> None:
        original = self.doc.read_text(encoding="utf-8")
        with mock.patch.object(config, "EDIT_MARKDOWN_WRITE", True), mock.patch(
            "chatgpt_web.markdown_io.backup_md", side_effect=OSError("backup disk full")
        ):
            result = self._edit(write=True)
        self.assertFalse(result["ok"], result)
        self.assertIn("backup failed", result["error"])
        self.assertEqual(self.doc.read_text(encoding="utf-8"), original)

    def test_oversized_file_is_rejected_before_parse(self) -> None:
        self.doc.write_text("x" * 100, encoding="utf-8")
        with mock.patch.object(config, "EDIT_MARKDOWN_MAX_FILE_BYTES", 16), mock.patch(
            "chatgpt_web.markdown_io.read_md"
        ) as read_md:
            result = self._edit()
        self.assertFalse(result["ok"], result)
        self.assertIn("file too large", result["error"])
        read_md.assert_not_called()


class RunLocalEditMarkdownTests(SandboxCase):
    def test_disabled_returns_input_unchanged(self) -> None:
        calls = [{"name": "bash", "arguments": {"command": "ls"}}]
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", False):
            out = run_local_edit_markdown(calls)
        self.assertIs(out, calls)

    def test_non_edit_calls_pass_through(self) -> None:
        calls = [
            {"name": "bash", "arguments": {"command": "ls"}},
            {"name": EDIT_MARKDOWN_TOOL_NAME, "arguments": {
                "path": "README.md", "start": 1, "end": 1, "new_text": "# 新标题",
            }},
            {"name": "read_file", "arguments": {"path": "x"}},
        ]
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", True):
            out = run_local_edit_markdown(calls, backup_dir=str(self.root / "backups"))
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0], calls[0])
        self.assertEqual(out[2], calls[2])
        self.assertEqual(out[1]["name"], EDIT_MARKDOWN_TOOL_NAME)
        self.assertTrue(out[1]["result"]["ok"], out[1])
        # 本地执行默认走 sandbox 校验 + dry-run
        self.assertFalse(out[1]["result"]["written"])
        # 注意：macOS 的 /var 是 /private/var 的符号链接，断言要用 resolve() 后的根
        self.assertTrue(
            str(out[1]["result"]["resolved_path"]).startswith(str(self.root.resolve()))
        )

    def test_same_file_three_edits_front_to_back_do_not_drift(self) -> None:
        """同批同文件三个 edit_markdown：模型按行号从前往后给出，也不能改错位置。

        三处编辑都会改变行数（第 1 处插入 2 行），若按模型顺序执行，后两处的
        ``start`` 会指向偏移后的内容；实现改为同文件组内按 start 降序执行，
        返回列表仍保持模型给出的原始顺序。
        """
        doc = self.root / "multi.md"
        lines = [f"line-{i:02d}" for i in range(1, 21)]
        doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
        calls = [
            # 从前往后：第 2 行处插入两行，后面的行号会整体下移
            {"id": "c1", "name": EDIT_MARKDOWN_TOOL_NAME, "arguments": {
                "path": "multi.md", "start": 2, "end": 2,
                "new_text": "inserted-a\ninserted-b", "write": True,
            }},
            {"id": "c2", "name": EDIT_MARKDOWN_TOOL_NAME, "arguments": {
                "path": "multi.md", "start": 10, "end": 10,
                "new_text": "TEN", "write": True,
            }},
            {"id": "c3", "name": EDIT_MARKDOWN_TOOL_NAME, "arguments": {
                "path": "multi.md", "start": 18, "end": 18,
                "new_text": "EIGHTEEN", "write": True,
            }},
        ]
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", True), \
                mock.patch.object(config, "EDIT_MARKDOWN_WRITE", True):
            out = run_local_edit_markdown(
                calls, backup_dir=str(self.root / "backups")
            )
        # 返回顺序 == 入参顺序
        self.assertEqual([c["id"] for c in out], ["c1", "c2", "c3"])
        for call in out:
            self.assertTrue(call["result"]["ok"], call)
            self.assertTrue(call["result"]["written"], call)
        final = doc.read_text(encoding="utf-8").split("\n")
        # 原 line-02 被替换为两行，原 line-10 / line-18 仍被精确替换
        self.assertEqual(final[0], "line-01")
        self.assertEqual(final[1], "inserted-a")
        self.assertEqual(final[2], "inserted-b")
        self.assertIn("TEN", final)
        self.assertIn("EIGHTEEN", final)
        self.assertNotIn("line-10", final)
        self.assertNotIn("line-18", final)
        # 漂移会改掉邻近行；确认它们没被动
        self.assertIn("line-11", final)
        self.assertIn("line-19", final)
        # 20 行：第 2 行 1→2（+1），第 10/18 行各 1→1，故净增 1 行
        self.assertEqual(len(final) - 1, 21)

    def test_disabled_keeps_calls_intact(self) -> None:
        calls = [{"name": EDIT_MARKDOWN_TOOL_NAME, "arguments": {}}]
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", False):
            out = run_local_edit_markdown(calls, backup_dir=str(self.root / "backups"))
        self.assertEqual(out, calls)
        self.assertNotIn("result", json.dumps(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
