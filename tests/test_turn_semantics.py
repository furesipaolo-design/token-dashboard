"""A "turn" is one prompt the human typed. Real transcripts also write tool
results, subagent-injected prompts, and harness records as type='user' — none
of those may count as turns, title sessions, or close turn windows.
"""
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db, overview_totals, project_summary, recent_sessions
from token_dashboard.insights import session_overview, turn_detail


def _ins(c, uuid, type_, ts, *, sidechain=0, prompt_id=None, prompt_text=None,
         agent_id=None, output_tokens=0, model=None):
    c.execute(
        """INSERT INTO messages (uuid, session_id, project_slug, type, timestamp,
           is_sidechain, agent_id, prompt_id, prompt_text, model, output_tokens)
           VALUES (?, 's1', 'p', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uuid, type_, ts, sidechain, agent_id, prompt_id, prompt_text, model, output_tokens),
    )


class TurnCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            # one real prompt…
            _ins(c, "u1", "user", "2026-06-01T10:00:00Z",
                 prompt_id="pA", prompt_text="fix the login bug")
            # …and the records that must NOT count as turns:
            _ins(c, "tr1", "user", "2026-06-01T10:00:02Z", prompt_id="pA")  # tool result
            _ins(c, "tr2", "user", "2026-06-01T10:00:03Z", prompt_id="pA")  # tool result
            _ins(c, "sc1", "user", "2026-06-01T10:00:04Z", sidechain=1,
                 prompt_id="pSub", prompt_text="Read these files thoroughly",
                 agent_id="a1")  # subagent-injected prompt
            _ins(c, "h1", "user", "2026-06-01T10:00:05Z",
                 prompt_id="pH", prompt_text="<local-command-caveat>…</local-command-caveat>")
            _ins(c, "a1", "assistant", "2026-06-01T10:00:01Z",
                 model="claude-opus-4-8", output_tokens=10)
            c.commit()

    def test_overview_counts_one_turn(self):
        self.assertEqual(overview_totals(self.db)["turns"], 1)

    def test_project_summary_counts_one_turn(self):
        self.assertEqual(project_summary(self.db)[0]["turns"], 1)

    def test_recent_sessions_counts_one_turn_and_real_title(self):
        row = recent_sessions(self.db)[0]
        self.assertEqual(row["turns"], 1)
        self.assertEqual(row["first_prompt"], "fix the login bug")

    def test_session_overview_counts_one_turn(self):
        ov = session_overview(self.db, "s1")
        self.assertEqual(ov["turns"], 1)
        self.assertEqual(ov["first_prompt"], "fix the login bug")


class TitleNotSidechainTests(unittest.TestCase):
    def test_earlier_sidechain_prompt_does_not_become_title(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        init_db(db)
        with sqlite3.connect(db) as c:
            _ins(c, "sc1", "user", "2026-06-01T09:59:59Z", sidechain=1,
                 prompt_id="pSub", prompt_text="Analyze these files", agent_id="a1")
            _ins(c, "u1", "user", "2026-06-01T10:00:00Z",
                 prompt_id="pA", prompt_text="the real question")
            c.commit()
        self.assertEqual(session_overview(db, "s1")["first_prompt"], "the real question")
        self.assertEqual(recent_sessions(db)[0]["first_prompt"], "the real question")


class TurnWindowTests(unittest.TestCase):
    """The expensive turns are the subagent-dispatching ones — exactly the
    turns whose window used to close at dispatch time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            _ins(c, "u1", "user", "2026-06-01T10:00:00Z",
                 prompt_id="pA", prompt_text="audit the project")
            # subagent dispatched mid-turn: its injected prompt has its OWN
            # prompt_id and must not close pA's window
            _ins(c, "sc1", "user", "2026-06-01T10:00:10Z", sidechain=1,
                 prompt_id="pSub", prompt_text="Audit area X", agent_id="a1")
            _ins(c, "sca", "assistant", "2026-06-01T10:00:20Z", sidechain=1,
                 agent_id="a1", model="claude-sonnet-4-6", output_tokens=500)
            # parent's continuation after the subagent returns
            _ins(c, "a2", "assistant", "2026-06-01T10:01:00Z",
                 model="claude-opus-4-8", output_tokens=700)
            # next real prompt — THIS closes the window
            _ins(c, "u2", "user", "2026-06-01T10:02:00Z",
                 prompt_id="pB", prompt_text="now fix what you found")
            _ins(c, "a3", "assistant", "2026-06-01T10:02:30Z",
                 model="claude-opus-4-8", output_tokens=900)
            c.commit()

    def test_window_includes_sidechain_and_continuation(self):
        d = turn_detail(self.db, "s1", "pA")
        out_by_model = {m["model"]: m["output_tokens"] for m in d["models"]}
        self.assertEqual(out_by_model.get("claude-sonnet-4-6"), 500,
                         "subagent work belongs to the dispatching turn")
        self.assertEqual(out_by_model.get("claude-opus-4-8"), 700,
                         "parent continuation must stay inside the window")

    def test_window_closes_at_next_real_prompt(self):
        d = turn_detail(self.db, "s1", "pB")
        out_by_model = {m["model"]: m["output_tokens"] for m in d["models"]}
        self.assertEqual(out_by_model.get("claude-opus-4-8"), 900)


if __name__ == "__main__":
    unittest.main()
