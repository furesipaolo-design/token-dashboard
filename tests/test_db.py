import os
import sqlite3
import tempfile
import unittest
from token_dashboard.db import init_db, connect


class InitDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")

    def test_init_creates_expected_tables(self):
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"files", "messages", "tool_calls", "plan", "dismissed_tips"}
        self.assertTrue(expected.issubset(tables), f"Missing: {expected - tables}")

    def test_init_is_idempotent(self):
        init_db(self.db_path)
        init_db(self.db_path)

    def test_connect_returns_row_factory(self):
        init_db(self.db_path)
        with connect(self.db_path) as c:
            r = c.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(r["one"], 1)

    def test_destructive_migration_backs_up_the_db_first(self):
        # Build a pre-tool_use_id DB holding a row whose transcript no longer
        # exists on disk — the DB is the only copy, so clearing must snapshot.
        with sqlite3.connect(self.db_path) as c:
            c.execute("""CREATE TABLE files (path TEXT PRIMARY KEY, mtime REAL NOT NULL,
                bytes_read INTEGER NOT NULL, scanned_at REAL NOT NULL)""")
            c.execute("""CREATE TABLE messages (uuid TEXT PRIMARY KEY,
                session_id TEXT NOT NULL, project_slug TEXT NOT NULL,
                type TEXT NOT NULL, timestamp TEXT NOT NULL, model TEXT,
                message_id TEXT)""")
            c.execute("""CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_uuid TEXT NOT NULL, session_id TEXT NOT NULL,
                project_slug TEXT NOT NULL, tool_name TEXT NOT NULL, target TEXT,
                result_tokens INTEGER, is_error INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL)""")
            c.execute("""INSERT INTO messages (uuid, session_id, project_slug, type,
                timestamp, message_id) VALUES ('u-old', 's', 'p', 'user', 't', 'm-old')""")
            c.commit()
        init_db(self.db_path)
        bak = self.db_path + ".pre-migration.bak"
        self.assertTrue(os.path.exists(bak), "destructive migration must leave a backup")
        with sqlite3.connect(bak) as c:
            rows = c.execute("SELECT uuid FROM messages").fetchall()
        self.assertEqual(rows, [("u-old",)])

    def test_no_backup_when_schema_is_current(self):
        init_db(self.db_path)
        init_db(self.db_path)
        self.assertFalse(os.path.exists(self.db_path + ".pre-migration.bak"))


if __name__ == "__main__":
    unittest.main()
