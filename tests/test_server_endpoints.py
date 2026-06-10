import http.server
import json
import os
import queue
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

from token_dashboard.db import init_db
from token_dashboard.server import build_handler, subscribe, unsubscribe, watch_tick

USER_LINE = '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:00Z","isSidechain":false,"message":{"role":"user","content":"ciao"}}\n'
ASSISTANT_LINE = '{"type":"assistant","uuid":"a1","parentUuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:01Z","isSidechain":false,"message":{"model":"claude-opus-4-8","usage":{"input_tokens":5,"output_tokens":7}}}\n'


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp, prompt_text, prompt_chars) VALUES ('u1','s1','p','/tmp/p','user','2026-06-01T00:00:00Z','fix login',9)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, cwd, type, timestamp, model, input_tokens, output_tokens, cache_create_5m_tokens) VALUES ('a1','u1','s1','p','/tmp/p','assistant','2026-06-01T00:00:01Z','claude-opus-4-8',100,200,50)")
            c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp) VALUES ('a1','s1','p','Edit','/tmp/p/x.py',10,0,'2026-06-01T00:00:01Z')")
            c.commit()
        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read())

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        return json.loads(urllib.request.urlopen(req).read())

    def test_projects_cards_shape(self):
        rows = self._get("/api/projects/cards")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIn("description", r)
        self.assertIn("daily", r)
        self.assertIsNotNone(r["cost_usd"])

    def test_projects_detail(self):
        d = self._get("/api/projects/detail?slug=p")
        self.assertEqual(d["models"][0]["model"], "claude-opus-4-8")
        self.assertEqual(d["top_files"][0]["file"], "/tmp/p/x.py")
        self.assertEqual(d["top_tools"][0]["tool_name"], "Edit")

    def test_projects_detail_requires_slug(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/projects/detail")
        self.assertEqual(ctx.exception.code, 400)

    def test_description_override_roundtrip(self):
        out = self._post("/api/projects/description", {"slug": "p", "description": "La mia app"})
        self.assertEqual(out["source"], "manual")
        self.assertEqual(self._get("/api/projects/cards")[0]["description"], "La mia app")
        out = self._post("/api/projects/description", {"slug": "p", "description": ""})
        self.assertIsNone(out["description"])  # no docs on disk for /tmp/p

    def test_description_validates_input(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/projects/description", {"description": "x"})
        self.assertEqual(ctx.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/projects/description", {"slug": "p", "description": 42})
        self.assertEqual(ctx.exception.code, 400)

    def test_session_meta_endpoint(self):
        ov = self._get("/api/sessions/s1/meta")
        self.assertEqual(ov["first_prompt"], "fix login")
        self.assertEqual(ov["files_edited"], ["/tmp/p/x.py"])
        self.assertIsNotNone(ov["cost_usd"])

    def test_sessions_sort_param(self):
        rows = self._get("/api/sessions?sort=turns")
        self.assertEqual(rows[0]["first_prompt"], "fix login")

    def test_plan_includes_pricing_mtime(self):
        plan = self._get("/api/plan")
        self.assertIn("pricing_mtime", plan)
        self.assertRegex(plan["pricing_mtime"], r"^\d{4}-\d{2}-\d{2}T")


class WatchTickTests(unittest.TestCase):
    def test_tick_scans_and_notifies(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        init_db(db)
        proj = os.path.join(tmp, "projects", "demo")
        os.makedirs(proj)
        with open(os.path.join(proj, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(USER_LINE)
            f.write(ASSISTANT_LINE)
        q = subscribe()
        try:
            n = watch_tick(os.path.join(tmp, "projects"), db)
            self.assertEqual(n["files"], 1)
            self.assertEqual(q.get(timeout=1)["type"], "scan")
            # idle tick: nothing new, no event
            n = watch_tick(os.path.join(tmp, "projects"), db)
            self.assertEqual(n["files"], 0)
            with self.assertRaises(queue.Empty):
                q.get(timeout=0.2)
        finally:
            unsubscribe(q)


if __name__ == "__main__":
    unittest.main()
