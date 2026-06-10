"""Real transcripts write one JSONL line per content block: same message.id,
distinct uuids, parentUuid chained line-to-line (NOT pointing at the user
record), identical usage on every line, and each line carrying its own tool
blocks. The scanner must collapse the group to one message row without
orphaning the prompt→response join or losing tool rows.
"""
import json
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import expensive_prompts, init_db
from token_dashboard.scanner import scan_dir

USAGE = {
    "input_tokens": 100, "output_tokens": 300,
    "cache_read_input_tokens": 500,
    "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 0},
}


def _user(uuid, ts, text="fix the login bug"):
    return {"type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
            "isSidechain": False, "promptId": f"pid-{uuid}",
            "message": {"role": "user", "content": text}}


def _block_line(uuid, parent, ts, content):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent,
            "sessionId": "s1", "timestamp": ts, "isSidechain": False,
            "message": {"id": "msg_BL", "model": "claude-opus-4-8",
                        "content": content, "usage": USAGE}}


class BlockPerLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.proj_root = os.path.join(self.tmp, "projects")
        self.proj_dir = os.path.join(self.proj_root, "C--work-sample")
        os.makedirs(self.proj_dir)
        init_db(self.db)
        self.path = os.path.join(self.proj_dir, "s1.jsonl")

    def _write(self, recs, mode="w"):
        with open(self.path, mode, encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def _three_block_response(self):
        """user → text line → tool line → tool line, chained parents."""
        return [
            _user("u1", "2026-04-10T00:00:00Z"),
            _block_line("r1", "u1", "2026-04-10T00:00:01Z",
                        [{"type": "text", "text": "Looking…"}]),
            _block_line("r2", "r1", "2026-04-10T00:00:02Z",
                        [{"type": "tool_use", "id": "tuA", "name": "Read",
                          "input": {"file_path": "a.py"}}]),
            _block_line("r3", "r2", "2026-04-10T00:00:03Z",
                        [{"type": "tool_use", "id": "tuB", "name": "Grep",
                          "input": {"pattern": "login"}}]),
        ]

    def _assert_collapsed(self):
        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT uuid, parent_uuid, input_tokens, output_tokens "
                "FROM messages WHERE type='assistant'").fetchall()
            tools = c.execute(
                "SELECT tool_name FROM tool_calls WHERE tool_name != '_tool_result' "
                "ORDER BY tool_name").fetchall()
        self.assertEqual(len(rows), 1, "block lines must collapse to one row")
        self.assertEqual(rows[0]["input_tokens"], 100, "usage must not be summed")
        self.assertEqual(rows[0]["output_tokens"], 300)
        self.assertEqual(rows[0]["parent_uuid"], "u1",
                         "survivor must adopt the group's external parent")
        self.assertEqual([t["tool_name"] for t in tools], ["Grep", "Read"],
                         "tool rows from evicted lines must survive")
        prompts = expensive_prompts(self.db)
        self.assertEqual(len(prompts), 1,
                         "prompt→first-response join must still work")
        self.assertEqual(prompts[0]["billable_tokens"], 100 + 300 + 40)

    def test_single_scan(self):
        self._write(self._three_block_response())
        scan_dir(self.proj_root, self.db)
        self._assert_collapsed()

    def test_incremental_scans(self):
        recs = self._three_block_response()
        self._write(recs[:2])
        scan_dir(self.proj_root, self.db)
        self._write(recs[2:3], mode="a")
        scan_dir(self.proj_root, self.db)
        self._write(recs[3:], mode="a")
        scan_dir(self.proj_root, self.db)
        self._assert_collapsed()

    def test_full_rescan_is_idempotent(self):
        self._write(self._three_block_response())
        scan_dir(self.proj_root, self.db)
        # shrink-then-regrow forces a from-zero rescan of the same content
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE files SET bytes_read = 999999, mtime = 0")
            c.commit()
        scan_dir(self.proj_root, self.db)
        self._assert_collapsed()

    def test_malformed_record_does_not_kill_scan(self):
        recs = self._three_block_response()
        bad = {"type": "assistant", "uuid": "bad1", "sessionId": "s1",
               "timestamp": "2026-04-10T00:00:02Z", "message": "not a dict"}
        self._write(recs[:2] + [bad] + recs[2:])
        totals = scan_dir(self.proj_root, self.db)  # must not raise
        self.assertEqual(totals["files"], 1)
        self._assert_collapsed()


if __name__ == "__main__":
    unittest.main()
