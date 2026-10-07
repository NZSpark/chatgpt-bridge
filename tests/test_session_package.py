"""PI-905：session 包拆分后的结构与兼容契约回归。"""

import unittest


class SessionPackageStructureTests(unittest.TestCase):
    def test_submodules_importable(self):
        from chatgpt_web.session import lock, migration, schema, store  # noqa: F401

    def test_schema_owns_model_and_version(self):
        from chatgpt_web.session import schema

        self.assertEqual(schema.STATE_SCHEMA_VERSION, 2)
        state = schema.SessionState(cap_failures=3, turns=5)
        payload = state.to_payload()
        self.assertEqual(payload["cap_failures"], 3)
        self.assertEqual(schema.SessionState.from_payload(payload).turns, 5)

    def test_migration_upgrades_legacy_document(self):
        from chatgpt_web.session.migration import migrate_state_document

        legacy = {"has_history": True, "turns": 4, "version": 1, "sessions": {}}
        migrated = migrate_state_document(legacy)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertNotIn("version", migrated)

    def test_migration_rejects_unknown_schema(self):
        from chatgpt_web.session.migration import migrate_state_document

        with self.assertRaises(ValueError):
            migrate_state_document({"schema_version": 99})

    def test_lock_exposes_threading_lock(self):
        from chatgpt_web.session import lock

        # 具备 acquire/release 的上下文协议即可（threading.Lock 实例）。
        self.assertTrue(hasattr(lock.STATE_FILE_LOCK, "acquire"))
        self.assertTrue(hasattr(lock.STATE_FILE_LOCK, "release"))

    def test_facade_reexports_legacy_names(self):
        from chatgpt_web import session_store
        from chatgpt_web.session import schema, store

        self.assertIs(session_store.SessionStoreMixin, store.SessionStoreMixin)
        self.assertIs(session_store.SessionState, schema.SessionState)
        self.assertEqual(session_store.STATE_SCHEMA_VERSION, schema.STATE_SCHEMA_VERSION)
        # patch 契约：facade 必须暴露这些旋钮。
        for name in ("json", "logger", "_warned_bad_state", "_STATE_FILE_LOCK"):
            self.assertTrue(hasattr(session_store, name), name)


if __name__ == "__main__":
    unittest.main()
