"""Portable and collision-resistant task snapshot filenames."""

import json
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_web import config, tasks


class TaskSnapshotPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-task-paths-")
        self.addCleanup(self.tmp.cleanup)
        self.dir_patch = patch.object(config, "TASK_FILE_DIR", self.tmp.name)
        self.namespace_patch = patch.object(config, "TASK_NAMESPACE", "chatgpt")
        self.dir_patch.start()
        self.namespace_patch.start()
        self.addCleanup(self.dir_patch.stop)
        self.addCleanup(self.namespace_patch.stop)

    def test_ordinary_bucket_filename_remains_backward_compatible(self):
        self.assertEqual(tasks._file("project-42").name, "project-42.json")

    def test_generated_namespace_keys_are_portable(self):
        for bucket in ("ua:cursor", "cwd:repo-0123456789ab"):
            with self.subTest(bucket=bucket):
                path = tasks._file(bucket)
                self.assertNotIn(":", path.name)
                self.assertEqual(path.parent, tasks._dir())
                self.assertLessEqual(len(path.name), 80)

    def test_sanitization_does_not_merge_distinct_bucket_names(self):
        unsafe = tasks._file("ua:cursor")
        sanitized_lookalike = tasks._file("ua_cursor")
        self.assertNotEqual(unsafe, sanitized_lookalike)

    def test_windows_reserved_names_and_traversal_are_not_used_as_paths(self):
        reserved = tasks._file("CON")
        traversal = tasks._file("../../outside")
        self.assertNotEqual(reserved.name, "CON.json")
        self.assertNotIn("/", traversal.name)
        self.assertNotIn("\\\\", traversal.name)
        self.assertEqual(traversal.parent, tasks._dir())

    def test_legacy_colon_filename_is_read_when_new_path_is_absent(self):
        legacy = tasks._legacy_file("ua:cursor")
        self.assertIsNotNone(legacy)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        expected = {"namespace": "chatgpt", "bucket": "ua:cursor", "goal": "legacy"}
        legacy.write_text(json.dumps(expected), encoding="utf-8")
        self.assertEqual(tasks.load("ua:cursor"), expected)

    def test_legacy_filename_collision_cannot_load_another_bucket(self):
        # The old sanitizer maps both values to the same filename.
        self.assertEqual(tasks._legacy_file("repo/a"), tasks._legacy_file("repo_a"))
        legacy = tasks._legacy_file("repo/a")
        self.assertIsNotNone(legacy)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({
            "namespace": "chatgpt", "bucket": "repo_a", "goal": "other task",
        }), encoding="utf-8")
        self.assertEqual(tasks.load("repo/a"), {})


if __name__ == "__main__":
    unittest.main()
