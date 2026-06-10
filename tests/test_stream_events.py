import http.server
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

from token_dashboard import server as srv
from token_dashboard.db import init_db
from token_dashboard.server import build_handler, notify, subscribe, unsubscribe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

USER_LINE = '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:00Z","isSidechain":false,"message":{"role":"user","content":"hi"}}\n'
ASSISTANT_LINE = '{"type":"assistant","uuid":"a1","parentUuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:01Z","isSidechain":false,"message":{"model":"claude-haiku-4-5","usage":{"input_tokens":1,"output_tokens":1}}}\n'


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_session(projects_dir):
    os.makedirs(os.path.join(projects_dir, "demo"), exist_ok=True)
    with open(os.path.join(projects_dir, "demo", "s.jsonl"), "w", encoding="utf-8") as f:
        f.write(USER_LINE)
        f.write(ASSISTANT_LINE)


class BroadcastTests(unittest.TestCase):
    def test_notify_reaches_every_subscriber(self):
        q1, q2 = subscribe(), subscribe()
        try:
            notify({"type": "scan", "files": 1})
            self.assertEqual(q1.get(timeout=1)["type"], "scan")
            self.assertEqual(q2.get(timeout=1)["type"], "scan")
        finally:
            unsubscribe(q1)
            unsubscribe(q2)

    def test_unsubscribed_queue_gets_nothing(self):
        q = subscribe()
        unsubscribe(q)
        notify({"type": "scan", "files": 1})
        with self.assertRaises(queue.Empty):
            q.get(timeout=0.2)

    def test_unsubscribe_unknown_queue_is_noop(self):
        unsubscribe(queue.Queue())


class ScanEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        self.proj = os.path.join(self.tmp, "projects")
        _write_session(self.proj)
        self.port = _free_port()
        H = build_handler(self.db, projects_dir=self.proj)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_api_scan_notifies_subscribers_on_changes(self):
        q = subscribe()
        try:
            body = json.loads(self._get("/api/scan"))
            self.assertEqual(body["files"], 1)
            evt = q.get(timeout=2)
            self.assertEqual(evt["type"], "scan")
            self.assertEqual(evt["files"], 1)
        finally:
            unsubscribe(q)

    def test_api_scan_silent_when_nothing_new(self):
        self._get("/api/scan")  # first scan ingests the file
        q = subscribe()
        try:
            self._get("/api/scan")  # nothing changed since
            with self.assertRaises(queue.Empty):
                q.get(timeout=0.3)
        finally:
            unsubscribe(q)

    def test_sse_stream_delivers_notify(self):
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/stream", timeout=5
        )
        deadline = time.time() + 2
        while not srv._SUBSCRIBERS and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(srv._SUBSCRIBERS, "SSE handler never subscribed")
        notify({"type": "scan", "files": 3})
        line = resp.readline().decode()
        self.assertTrue(line.startswith("data: "), line)
        evt = json.loads(line[len("data: "):])
        self.assertEqual(evt, {"type": "scan", "files": 3})
        resp.close()


class ScanAsyncCliTests(unittest.TestCase):
    def test_dashboard_scan_async_serves_then_ingests(self):
        tmp = tempfile.mkdtemp()
        proj = os.path.join(tmp, "projects")
        _write_session(proj)
        db = os.path.join(tmp, "t.db")
        port = _free_port()
        env = {**os.environ, "TOKEN_DASHBOARD_DB": db, "PORT": str(port), "HOST": "127.0.0.1"}
        p = subprocess.Popen(
            [sys.executable, "cli.py", "dashboard", "--no-open", "--scan-async",
             "--projects-dir", proj],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 15
            sessions = None
            while time.time() < deadline:
                try:
                    body = json.loads(urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/overview", timeout=1).read())
                    sessions = body["sessions"]
                    if sessions == 1:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            self.assertEqual(sessions, 1, "background scan never ingested the session")
        finally:
            p.terminate()
            p.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
