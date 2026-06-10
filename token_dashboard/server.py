"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import queue
import signal
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from datetime import datetime, timedelta, timezone

from .db import (
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    daily_token_breakdown, model_breakdown, skill_breakdown,
)
from .insights import project_files, session_overview, session_tips, turn_detail
from .meta import descriptions, set_description
from .pricing import load_pricing, cost_for, get_plan, set_plan
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .skills import cached_catalog


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"
LOGO_PATH = Path(__file__).resolve().parent.parent / "docs" / "logo.png"

# SSE broadcast: one queue per connected client, so an event reaches every
# stream instead of whichever consumer polls first.
_SUBSCRIBERS: "list[queue.Queue[dict]]" = []
_SUB_LOCK = threading.Lock()


def subscribe() -> "queue.Queue[dict]":
    q: "queue.Queue[dict]" = queue.Queue()
    with _SUB_LOCK:
        _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q: "queue.Queue[dict]") -> None:
    with _SUB_LOCK:
        try:
            _SUBSCRIBERS.remove(q)
        except ValueError:
            pass


def notify(evt: dict) -> None:
    with _SUB_LOCK:
        targets = list(_SUBSCRIBERS)
    for q in targets:
        q.put(evt)

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (plan, tip key)
MAX_LIMIT = 1000


def _send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, status: int, msg: str) -> None:
    _send_json(handler, {"error": msg}, status=status)


def _clamp_limit(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _apply_costs(model_rows: list, pricing: dict):
    """Annotate per-model rows with cost; return the priced total (or None)."""
    total, priced = 0.0, False
    for m in model_rows:
        c = cost_for(m["model"], m, pricing)
        m["cost_usd"] = c["usd"]
        m["cost_estimated"] = c["estimated"]
        if c["usd"] is not None:
            total += c["usd"]
            priced = True
    return round(total, 4) if priced else None


def _serve_static(handler, rel: str) -> None:
    rel = rel.lstrip("/")
    p = (WEB_ROOT / rel).resolve()
    if not str(p).startswith(str(WEB_ROOT.resolve())) or not p.is_file():
        handler.send_response(404)
        handler.end_headers()
        return
    return _serve_file(handler, p)


def _serve_file(handler, p: Path) -> None:
    if not p.is_file():
        handler.send_response(404)
        handler.end_headers()
        return
    body = p.read_bytes()
    ctype, _ = mimetypes.guess_type(str(p))
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def build_handler(db_path: str, projects_dir: str):
    pricing = load_pricing(PRICING_JSON)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query or "")
            path = url.path
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path == "/logo.png":
                return _serve_file(self, LOGO_PATH)
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview":
                totals = overview_totals(db_path, since, until)
                cost_usd = 0.0
                for m in model_breakdown(db_path, since, until):
                    c = cost_for(m["model"], m, pricing)
                    if c["usd"] is not None:
                        cost_usd += c["usd"]
                totals["cost_usd"] = round(cost_usd, 4)
                return _send_json(self, totals)
            if path == "/api/prompts":
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                sort = qs.get("sort", ["tokens"])[0]
                rows = expensive_prompts(db_path, limit=limit, sort=sort,
                                         session_id=qs.get("session", [None])[0])
                for r in rows:
                    c = cost_for(r["model"], {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": r["cache_read_tokens"],
                        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                    }, pricing)
                    r["estimated_cost_usd"] = c["usd"]
                return _send_json(self, rows)
            if path == "/api/prompts/turn":
                sid = qs.get("session", [""])[0]
                pid = qs.get("prompt", [""])[0]
                if not sid or not pid:
                    return _send_error(self, 400, "missing session or prompt")
                d = turn_detail(db_path, sid, pid)
                d["cost_usd"] = _apply_costs(d["models"], pricing)
                return _send_json(self, d)
            if path == "/api/projects":
                return _send_json(self, project_summary(db_path, since, until))
            if path == "/api/projects/cards":
                rows = project_summary(db_path, since, until)
                descs = descriptions(db_path, [r["project_slug"] for r in rows])
                spark_since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                for r in rows:
                    slug = r["project_slug"]
                    d = descs.get(slug) or {}
                    r["description"] = d.get("description")
                    r["description_source"] = d.get("source")
                    r["cost_usd"] = _apply_costs(
                        model_breakdown(db_path, since, until, project_slug=slug), pricing)
                    r["daily"] = [
                        {"day": x["day"],
                         "tokens": x["input_tokens"] + x["output_tokens"] + x["cache_create_tokens"]}
                        for x in daily_token_breakdown(db_path, spark_since, None, project_slug=slug)
                    ]
                return _send_json(self, rows)
            if path == "/api/projects/detail":
                slug = qs.get("slug", [""])[0]
                if not slug:
                    return _send_error(self, 400, "missing slug")
                models = model_breakdown(db_path, since, until, project_slug=slug)
                cost = _apply_costs(models, pricing)
                spark_since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                return _send_json(self, {
                    "project_slug": slug,
                    "cost_usd": cost,
                    "models": models,
                    "top_tools": tool_token_breakdown(db_path, since, until, project_slug=slug)[:8],
                    "top_files": project_files(db_path, slug, limit=8),
                    "daily": daily_token_breakdown(db_path, spark_since, None, project_slug=slug),
                    "top_sessions": recent_sessions(db_path, limit=3, sort="tokens", project_slug=slug),
                })
            if path == "/api/tools":
                return _send_json(self, tool_token_breakdown(db_path, since, until))
            if path == "/api/sessions":
                return _send_json(self, recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                    sort=qs.get("sort", ["recent"])[0],
                    project_slug=qs.get("project", [None])[0],
                ))
            if path == "/api/daily":
                return _send_json(self, daily_token_breakdown(db_path, since, until))
            if path == "/api/skills":
                rows = skill_breakdown(db_path, since, until)
                catalog = cached_catalog()
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
                return _send_json(self, rows)
            if path == "/api/by-model":
                rows = model_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                rest = path[len("/api/sessions/"):]
                if rest.endswith("/meta"):
                    sid = rest[: -len("/meta")]
                    ov = session_overview(db_path, sid)
                    if ov.get("models") is not None:
                        ov["cost_usd"] = _apply_costs(ov["models"], pricing)
                        ov["tips"] = session_tips(db_path, sid)
                    return _send_json(self, ov)
                return _send_json(self, session_turns(db_path, rest))
            if path == "/api/tips":
                return _send_json(self, all_tips(db_path))
            if path == "/api/plan":
                try:
                    mtime = datetime.fromtimestamp(
                        PRICING_JSON.stat().st_mtime, tz=timezone.utc).isoformat()
                except OSError:
                    mtime = None
                return _send_json(self, {
                    "plan": get_plan(db_path), "pricing": pricing,
                    "pricing_mtime": mtime,
                })
            if path == "/api/scan":
                n = scan_dir(projects_dir, db_path)
                if n["files"]:
                    notify({"type": "scan", **n})
                return _send_json(self, n)
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = subscribe()
                try:
                    while True:
                        try:
                            evt = q.get(timeout=15)
                            chunk = f"data: {json.dumps(evt, default=str)}\n\n".encode()
                        except queue.Empty:
                            chunk = b": ping\n\n"
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                finally:
                    unsubscribe(q)
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_error(self, 400, "invalid Content-Length")
            if length < 0 or length > MAX_POST_BYTES:
                return _send_error(self, 413, f"body too large (max {MAX_POST_BYTES} bytes)")
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return _send_error(self, 400, "invalid JSON")
            if not isinstance(body, dict):
                return _send_error(self, 400, "body must be a JSON object")
            if url.path == "/api/plan":
                set_plan(db_path, body.get("plan", "api"))
                return _send_json(self, {"ok": True})
            if url.path == "/api/tips/dismiss":
                dismiss_tip(db_path, body.get("key", ""))
                return _send_json(self, {"ok": True})
            if url.path == "/api/projects/description":
                slug = body.get("slug", "")
                if not isinstance(slug, str) or not slug:
                    return _send_error(self, 400, "missing slug")
                desc = body.get("description", "")
                if not isinstance(desc, str):
                    return _send_error(self, 400, "description must be a string")
                return _send_json(self, {"ok": True, **set_description(db_path, slug, desc)})
            self.send_response(404)
            self.end_headers()

    return H

def watch_tick(projects_dir: str, db_path: str) -> dict:
    """One watcher pass: incremental scan; broadcast when anything changed."""
    n = scan_dir(projects_dir, db_path)
    if n["files"]:
        notify({"type": "scan", **n})
    return n


def _watch_loop(projects_dir: str, db_path: str, interval: float) -> None:
    while True:
        time.sleep(interval)
        try:
            watch_tick(projects_dir, db_path)
        except Exception:
            pass  # transient FS/DB hiccups must not kill the watcher


def run(host: str, port: int, db_path: str, projects_dir: str):
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)

    # Live updates: rescan transcripts every few seconds and push an SSE
    # event when new data lands. scan_dir is incremental, so an idle tick
    # costs one stat() per transcript file. Set the env var to 0 to disable.
    interval = float(os.environ.get("TOKEN_DASHBOARD_WATCH_INTERVAL", "10"))
    if interval > 0:
        threading.Thread(
            target=_watch_loop, args=(projects_dir, db_path, interval), daemon=True
        ).start()

    def _handle_term(_signum, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_term)

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
