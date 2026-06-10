import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db, model_breakdown, recent_sessions
from token_dashboard.insights import project_files, session_overview


def _msg(c, uuid, sid, slug, typ, ts, model=None, out=0, prompt=None, parent=None):
    c.execute(
        """INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, cwd, type,
           timestamp, model, input_tokens, output_tokens, cache_read_tokens,
           cache_create_5m_tokens, cache_create_1h_tokens, prompt_text, prompt_chars)
           VALUES (?,?,?,?,'/tmp/p',?,?,?,10,?,0,0,0,?,?)""",
        (uuid, parent, sid, slug, typ, ts, model, out, prompt, len(prompt) if prompt else None),
    )


def _tool(c, sid, slug, tool, target, ts="2026-06-01T00:00:02Z"):
    c.execute(
        "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp) VALUES ('x',?,?,?,?,1,0,?)",
        (sid, slug, tool, target, ts),
    )


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            # session s1, project p1: 2 user turns, 2 assistant turns + 1 synthetic
            _msg(c, "u1", "s1", "p1", "user", "2026-06-01T00:00:00Z", prompt="fix the bug")
            _msg(c, "a1", "s1", "p1", "assistant", "2026-06-01T00:00:01Z", model="claude-opus-4-8", out=100, parent="u1")
            _msg(c, "u2", "s1", "p1", "user", "2026-06-01T00:10:00Z", prompt="now add tests")
            _msg(c, "a2", "s1", "p1", "assistant", "2026-06-01T00:10:01Z", model="claude-sonnet-4-6", out=50, parent="u2")
            _msg(c, "a3", "s1", "p1", "assistant", "2026-06-01T00:10:02Z", model="<synthetic>", out=0)
            # session s2, project p2: 1 turn
            _msg(c, "u3", "s2", "p2", "user", "2026-06-02T00:00:00Z", prompt="hello world")
            _msg(c, "a4", "s2", "p2", "assistant", "2026-06-02T00:00:01Z", model="claude-opus-4-8", out=10, parent="u3")
            _tool(c, "s1", "p1", "Edit", "/tmp/p/main.py")
            _tool(c, "s1", "p1", "Edit", "/tmp/p/main.py")
            _tool(c, "s1", "p1", "Read", "/tmp/p/other.py")
            _tool(c, "s1", "p1", "Bash", "ls")
            c.commit()

    def test_session_overview_facts(self):
        ov = session_overview(self.db, "s1")
        self.assertEqual(ov["first_prompt"], "fix the bug")
        self.assertEqual(ov["turns"], 2)
        self.assertEqual(ov["files_edited"], ["/tmp/p/main.py"])
        models = {m["model"] for m in ov["models"]}
        self.assertEqual(models, {"claude-opus-4-8", "claude-sonnet-4-6"})
        tools = {t["tool_name"]: t["calls"] for t in ov["top_tools"]}
        self.assertEqual(tools["Edit"], 2)

    def test_session_overview_missing_session(self):
        self.assertEqual(session_overview(self.db, "nope")["records"], 0)

    def test_project_files_counts_file_tools_only(self):
        files = project_files(self.db, "p1")
        self.assertEqual(files[0], {"file": "/tmp/p/main.py", "calls": 2})
        self.assertNotIn("ls", [f["file"] for f in files])

    def test_recent_sessions_sort_and_first_prompt(self):
        by_recent = recent_sessions(self.db, sort="recent")
        self.assertEqual(by_recent[0]["session_id"], "s2")
        self.assertEqual(by_recent[0]["first_prompt"], "hello world")
        by_turns = recent_sessions(self.db, sort="turns")
        self.assertEqual(by_turns[0]["session_id"], "s1")

    def test_model_breakdown_excludes_synthetic_and_filters_project(self):
        models = [m["model"] for m in model_breakdown(self.db)]
        self.assertNotIn("<synthetic>", models)
        p2 = model_breakdown(self.db, project_slug="p2")
        self.assertEqual(len(p2), 1)
        self.assertEqual(p2[0]["model"], "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
