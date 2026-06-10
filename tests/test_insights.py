import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import expensive_prompts, init_db, model_breakdown, recent_sessions
from token_dashboard.insights import project_files, session_overview, session_tips, turn_detail


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

    def test_recent_sessions_project_filter(self):
        rows = recent_sessions(self.db, project_slug="p2")
        self.assertEqual([r["session_id"] for r in rows], ["s2"])

    def test_expensive_prompts_session_filter(self):
        rows = expensive_prompts(self.db, session_id="s2")
        self.assertEqual({r["session_id"] for r in rows}, {"s2"})
        self.assertIn("prompt_id", rows[0])


class TurnAndTipsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            # Mirrors real transcripts: prompt_id lives ONLY on user records;
            # assistant rows never carry it. Turn = time window to next prompt.
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, prompt_id, prompt_text, prompt_chars) VALUES ('u1','s1','p1','user','2026-06-01T00:00:00Z','pr1','do it',5)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens) VALUES ('a1','u1','s1','p1','assistant','2026-06-01T00:00:01Z','claude-opus-4-8',100,50)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens) VALUES ('a2','a1','s1','p1','assistant','2026-06-01T00:00:02Z','claude-opus-4-8',200,80)")
            # next prompt closes the window — its assistant work must NOT leak into pr1
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, prompt_id, prompt_text, prompt_chars) VALUES ('u2','s1','p1','user','2026-06-01T00:01:00Z','pr2','more',4)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens) VALUES ('a3','u2','s1','p1','assistant','2026-06-01T00:01:01Z','claude-opus-4-8',999,999)")
            for i in range(5):  # 5 reads of the same file → rereads tip
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, is_error, timestamp) VALUES ('a1','s1','p1','Read','/p/big.py',0,'2026-06-01T00:00:01Z')")
            c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp) VALUES ('a2','s1','p1','_tool_result',NULL,25000,0,'2026-06-01T00:00:02Z')")
            c.commit()

    def test_turn_detail_aggregates_whole_turn(self):
        d = turn_detail(self.db, "s1", "pr1")
        self.assertEqual(d["models"][0]["input_tokens"], 300)  # both snapshots
        self.assertEqual(d["models"][0]["turns"], 2)
        self.assertEqual(d["tool_calls"][0], {"tool_name": "Read", "target": "/p/big.py", "calls": 5})
        self.assertEqual(d["result_tokens_max"], 25000)

    def test_turn_detail_unknown_prompt_is_empty(self):
        d = turn_detail(self.db, "s1", "nope")
        self.assertEqual(d["models"], [])
        self.assertEqual(d["result_count"], 0)

    def test_session_tips_rules_fire(self):
        tips = {t["key"] for t in session_tips(self.db, "s1")}
        self.assertIn("rereads", tips)
        self.assertIn("big-results", tips)
        self.assertNotIn("marathon", tips)  # only 1 turn

    def test_session_tips_quiet_session(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, prompt_text, prompt_chars) VALUES ('u9','s9','p1','user','2026-06-01T01:00:00Z','hi',2)")
            c.commit()
        self.assertEqual(session_tips(self.db, "s9"), [])


if __name__ == "__main__":
    unittest.main()
