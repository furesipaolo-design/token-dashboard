"""JSONL transcript walker + parser."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .db import connect

# Scans can be triggered concurrently (startup --scan-async thread, the
# server's filesystem watcher, and /api/scan requests); serialize them so
# they don't contend on SQLite write locks.
_SCAN_LOCK = threading.Lock()


INSERT_MSG = """
INSERT OR REPLACE INTO messages (
  uuid, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, entrypoint,
  type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, message_id,
  input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
  prompt_text, prompt_chars, tool_calls_json
) VALUES (
  :uuid, :parent_uuid, :session_id, :project_slug, :cwd, :git_branch, :cc_version, :entrypoint,
  :type, :is_sidechain, :agent_id, :timestamp, :model, :stop_reason, :prompt_id, :message_id,
  :input_tokens, :output_tokens, :cache_read_tokens, :cache_create_5m_tokens, :cache_create_1h_tokens,
  :prompt_text, :prompt_chars, :tool_calls_json
)
"""

INSERT_TOOL = """
INSERT OR IGNORE INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp, tool_use_id)
VALUES (:message_uuid, :session_id, :project_slug, :tool_name, :target, :result_tokens, :is_error, :timestamp, :tool_use_id)
"""


_TARGET_FIELDS = {
    "Read":      "file_path",
    "Edit":      "file_path",
    "Write":     "file_path",
    "Glob":      "pattern",
    "Grep":      "pattern",
    "Bash":      "command",
    "WebFetch":  "url",
    "WebSearch": "query",
    "Task":      "subagent_type",
    "Skill":     "skill",
}


def _usage(rec: dict) -> dict:
    u = (rec.get("message") or {}).get("usage") or {}
    cc = u.get("cache_creation") or {}
    return {
        "input_tokens":           int(u.get("input_tokens") or 0),
        "output_tokens":          int(u.get("output_tokens") or 0),
        "cache_read_tokens":      int(u.get("cache_read_input_tokens") or 0),
        "cache_create_5m_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
        "cache_create_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
    }


def _prompt_text(rec: dict) -> Tuple[Optional[str], Optional[int]]:
    if rec.get("type") != "user":
        return None, None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content, len(content)
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(parts) if parts else None
        return text, (len(text) if text else None)
    return None, None


def _target(name: str, inp: dict) -> Optional[str]:
    field = _TARGET_FIELDS.get(name)
    if field and isinstance(inp, dict):
        v = inp.get(field)
        if isinstance(v, str):
            return v[:500]
    return None


def _extract_tools(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "unknown"
        target = _target(name, block.get("input") or {})
        out.append({
            "tool_name":     name,
            "target":        target,
            "result_tokens": None,
            "is_error":      0,
            "timestamp":     rec.get("timestamp"),
            "tool_use_id":   block.get("id"),
        })
    return out


def _extract_results(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, str):
            chars = len(body)
        elif isinstance(body, list):
            chars = sum(len(p.get("text", "")) for p in body if isinstance(p, dict))
        else:
            chars = 0
        out.append({
            "tool_name":     "_tool_result",
            "target":        block.get("tool_use_id"),
            "result_tokens": chars // 4,
            "is_error":      1 if block.get("is_error") else 0,
            "timestamp":     rec.get("timestamp"),
            "tool_use_id":   block.get("tool_use_id"),
        })
    return out


def parse_record(rec: dict, project_slug: str) -> Tuple[dict, List[dict]]:
    """Return (message_row, [tool_call_rows])."""
    msg_obj = rec.get("message") or {}
    text, chars = _prompt_text(rec)
    msg = {
        "uuid":         rec.get("uuid"),
        "parent_uuid":  rec.get("parentUuid"),
        "session_id":   rec.get("sessionId"),
        "project_slug": project_slug,
        "cwd":          rec.get("cwd"),
        "git_branch":   rec.get("gitBranch"),
        "cc_version":   rec.get("version"),
        "entrypoint":   rec.get("entrypoint"),
        "type":         rec.get("type"),
        "is_sidechain": 1 if rec.get("isSidechain") else 0,
        "agent_id":     rec.get("agentId"),
        "timestamp":    rec.get("timestamp"),
        "model":        msg_obj.get("model"),
        "stop_reason":  msg_obj.get("stop_reason"),
        "prompt_id":    rec.get("promptId"),
        "message_id":   msg_obj.get("id"),
        "prompt_text":  text,
        "prompt_chars": chars,
        "tool_calls_json": None,
        **_usage(rec),
    }
    tools = _extract_tools(rec)
    tools.extend(_extract_results(rec))
    if tools:
        msg["tool_calls_json"] = json.dumps(
            [{"name": t["tool_name"], "target": t["target"]} for t in tools if t["tool_name"] != "_tool_result"]
        )
    for t in tools:
        t["message_uuid"] = msg["uuid"]
        t["session_id"]   = msg["session_id"]
        t["project_slug"] = project_slug
    return msg, tools


def _project_slug(file_path: Path, projects_root: Path) -> str:
    rel = file_path.relative_to(projects_root)
    return rel.parts[0]


def _evict_prior_snapshots(conn, session_id: str, message_id: str,
                           keep_uuid: str, keep_parent: Optional[str]) -> Optional[str]:
    """Remove older lines of the same (session_id, message_id) response.

    Claude Code writes one JSONL line per content block (and occasionally
    re-flushed streaming snapshots) with identical message.id, distinct
    top-level uuids, and identical usage — so only one line may survive or
    tokens would be summed N times.

    Two things must outlive the evicted lines:
    - the parent chain: each line's parentUuid points at the *previous line*
      of the same group, not at the user prompt. The survivor adopts the
      group's external parent (the one parent_uuid that is not itself in the
      group) so the prompt→first-response join keeps working. Returns the
      parent_uuid the caller should store on the surviving row.
    - tool rows: each line carries its own tool_use blocks; deleting rows
      with their line erased ~20% of tool analytics. Rows stay attached to
      their (now evicted) message_uuid — every tool_calls query filters by
      session/project, none joins back to messages — and the unique
      (session_id, tool_use_id, tool_name) index dedups snapshot re-flushes.
    """
    old = conn.execute(
        "SELECT uuid, parent_uuid FROM messages WHERE session_id=? AND message_id=? AND uuid!=?",
        (session_id, message_id, keep_uuid),
    ).fetchall()
    if not old:
        return keep_parent
    old_uuids = {r["uuid"] for r in old}
    if keep_parent in old_uuids:
        keep_parent = next(
            (r["parent_uuid"] for r in old if r["parent_uuid"] not in old_uuids),
            keep_parent,
        )
    placeholders = ",".join("?" * len(old_uuids))
    conn.execute(f"DELETE FROM messages WHERE uuid IN ({placeholders})", list(old_uuids))
    return keep_parent


def scan_file(path: Path, project_slug: str, conn, start_byte: int = 0) -> dict:
    """Ingest new lines from a JSONL file starting at ``start_byte``.

    Returns message/tool counts plus ``end_offset`` — the byte offset just
    past the last fully-parsed line. Callers persist ``end_offset`` as the
    file's high-water mark so a line partially flushed at EOF gets re-read
    once it completes.
    """
    msgs = tools = 0
    end_offset = start_byte
    with open(path, "rb") as fb:
        if start_byte:
            fb.seek(start_byte)
        while True:
            raw = fb.readline()
            if not raw:
                break  # EOF
            if not raw.endswith(b"\n"):
                # Partial line — Claude Code is mid-flush. Leave the
                # high-water mark behind the line start so we re-read it
                # once the write completes.
                break
            line_end = fb.tell()
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                end_offset = line_end
                continue
            if not line:
                end_offset = line_end
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                end_offset = line_end
                continue
            if not isinstance(rec, dict) or "uuid" not in rec or "type" not in rec:
                end_offset = line_end
                continue
            try:
                msg, tlist = parse_record(rec, project_slug)
            except Exception:
                # JSON-valid but shape-violating record (e.g. message is a
                # string, usage value non-numeric). One bad line must not
                # poison the whole scan pass.
                end_offset = line_end
                continue
            if not msg["session_id"] or not msg["timestamp"]:
                end_offset = line_end
                continue
            if msg["message_id"]:
                msg["parent_uuid"] = _evict_prior_snapshots(
                    conn, msg["session_id"], msg["message_id"], msg["uuid"], msg["parent_uuid"])
            conn.execute(INSERT_MSG, msg)
            # Clear this uuid's own prior rows so full rescans stay
            # idempotent for rows without a tool_use_id; rows with one are
            # deduped by the unique index + INSERT OR IGNORE.
            conn.execute("DELETE FROM tool_calls WHERE message_uuid=?", (msg["uuid"],))
            for t in tlist:
                tools += conn.execute(INSERT_TOOL, t).rowcount
            msgs += 1
            end_offset = line_end
    return {"messages": msgs, "tools": tools, "end_offset": end_offset}


def scan_dir(projects_root: Union[str, Path], db_path: Union[str, Path]) -> dict:
    with _SCAN_LOCK:
        return _scan_dir_locked(projects_root, db_path)


def _scan_dir_locked(projects_root: Union[str, Path], db_path: Union[str, Path]) -> dict:
    root = Path(projects_root)
    totals = {"messages": 0, "tools": 0, "files": 0}
    if not root.is_dir():
        return totals
    with connect(db_path) as conn:
        for p in root.rglob("*.jsonl"):
            try:
                stat = p.stat()
            except OSError:
                continue
            row = conn.execute(
                "SELECT mtime, bytes_read FROM files WHERE path=?", (str(p),)
            ).fetchone()
            offset = 0
            if row and row["mtime"] == stat.st_mtime and row["bytes_read"] == stat.st_size:
                continue
            if row and stat.st_size > row["bytes_read"]:
                offset = row["bytes_read"]
            slug = _project_slug(p, root)
            try:
                sub = scan_file(p, slug, conn, start_byte=offset)
            except OSError:
                # File vanished or turned unreadable mid-scan; roll back its
                # partial rows and move on — the next pass retries it.
                conn.rollback()
                continue
            # Persist the byte offset of the last fully-parsed line (not
            # st_size) so a partial line mid-flush is retried on the next
            # scan instead of being skipped over.
            conn.execute(
                "INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at) VALUES (?, ?, ?, ?)",
                (str(p), stat.st_mtime, sub["end_offset"], time.time()),
            )
            # Commit per file: a first scan of a large history would
            # otherwise hold the write lock for the whole pass, stalling
            # API reads/writes, and one bad file would roll back everything.
            conn.commit()
            totals["messages"] += sub["messages"]
            totals["tools"]    += sub["tools"]
            totals["files"]    += 1
    return totals
