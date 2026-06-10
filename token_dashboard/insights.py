"""Per-project and per-session drill-down queries (cards detail, session header)."""
from __future__ import annotations

from .db import best_project_name, connect, real_prompt_sql

FILE_TOOLS = ("Read", "Edit", "Write", "NotebookEdit")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")


def project_files(db_path, project_slug: str, limit: int = 8) -> list:
    """Most-touched file targets (Read/Edit/Write) for one project."""
    marks = ",".join("?" * len(FILE_TOOLS))
    sql = f"""
      SELECT target AS file, COUNT(*) AS calls
        FROM tool_calls
       WHERE project_slug = ? AND tool_name IN ({marks})
         AND target IS NOT NULL AND target != ''
       GROUP BY target
       ORDER BY calls DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (project_slug, *FILE_TOOLS, limit))]


def session_overview(db_path, session_id: str) -> dict:
    """Deterministic session summary: title (first prompt) + facts from the DB."""
    with connect(db_path) as c:
        head = c.execute(
            f"""
            SELECT MIN(timestamp) AS started, MAX(timestamp) AS ended,
                   COUNT(*) AS records,
                   SUM(CASE WHEN {real_prompt_sql()} THEN 1 ELSE 0 END) AS turns,
                   COALESCE(SUM(input_tokens),0)            AS input_tokens,
                   COALESCE(SUM(output_tokens),0)           AS output_tokens,
                   COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
                   MAX(project_slug) AS project_slug
              FROM messages WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if not head or head["records"] == 0:
            return {"session_id": session_id, "records": 0}

        # real_prompt_sql also skips sidechain records: without it a
        # subagent's injected prompt (or a compaction preamble) can win the
        # ORDER BY and become the session title.
        first = c.execute(
            f"""
            SELECT prompt_text FROM messages
             WHERE session_id = ? AND {real_prompt_sql()}
             ORDER BY timestamp ASC LIMIT 1
            """,
            (session_id,),
        ).fetchone()

        models = [dict(r) for r in c.execute(
            """
            SELECT COALESCE(model,'unknown') AS model, COUNT(*) AS turns,
                   COALESCE(SUM(input_tokens),0)            AS input_tokens,
                   COALESCE(SUM(output_tokens),0)           AS output_tokens,
                   COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
                   COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
                   COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
              FROM messages
             WHERE session_id = ? AND type = 'assistant'
               AND COALESCE(model,'') != '<synthetic>'
             GROUP BY model ORDER BY output_tokens DESC
            """,
            (session_id,),
        )]

        marks = ",".join("?" * len(EDIT_TOOLS))
        files_edited = [r["target"] for r in c.execute(
            f"""
            SELECT target, COUNT(*) AS n FROM tool_calls
             WHERE session_id = ? AND tool_name IN ({marks})
               AND target IS NOT NULL AND target != ''
             GROUP BY target ORDER BY n DESC LIMIT 10
            """,
            (session_id, *EDIT_TOOLS),
        )]

        top_tools = [dict(r) for r in c.execute(
            """
            SELECT tool_name, COUNT(*) AS calls FROM tool_calls
             WHERE session_id = ? AND tool_name != '_tool_result'
             GROUP BY tool_name ORDER BY calls DESC LIMIT 6
            """,
            (session_id,),
        )]

        slug = head["project_slug"] or ""
        cwds = [r["cwd"] for r in c.execute(
            "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
            (slug,),
        )]

    return {
        "session_id": session_id,
        "project_slug": slug,
        "project_name": best_project_name(cwds, slug),
        "first_prompt": first["prompt_text"] if first else None,
        "started": head["started"],
        "ended": head["ended"],
        "records": head["records"],
        "turns": head["turns"],
        "input_tokens": head["input_tokens"],
        "output_tokens": head["output_tokens"],
        "cache_read_tokens": head["cache_read_tokens"],
        "models": models,
        "files_edited": files_edited,
        "top_tools": top_tools,
    }


def session_tips(db_path, session_id: str) -> list:
    """Per-session optimization hints. Same spirit as tips.py but scoped to one
    session and ephemeral (no dismissal — they describe what already happened)."""
    out = []
    with connect(db_path) as c:
        rereads = [dict(r) for r in c.execute(
            """
            SELECT target, COUNT(*) AS n FROM tool_calls
             WHERE session_id = ? AND tool_name = 'Read'
               AND target IS NOT NULL AND target != ''
             GROUP BY target HAVING n >= 4 ORDER BY n DESC LIMIT 3
            """,
            (session_id,),
        )]
        big = c.execute(
            """
            SELECT COUNT(*) AS n, MAX(result_tokens) AS mx
              FROM tool_calls
             WHERE session_id = ? AND tool_name = '_tool_result'
               AND result_tokens >= 20000
            """,
            (session_id,),
        ).fetchone()
        # Cache sums are main-chain only: subagents and auto-compact runs
        # start cold by design, and their rebuild isn't a habit the user can
        # change — blaming it on "context resets" would be misleading.
        usage = c.execute(
            f"""
            SELECT SUM(CASE WHEN {real_prompt_sql()} THEN 1 ELSE 0 END) AS turns,
                   COALESCE(SUM(CASE WHEN type='assistant' AND is_sidechain=0
                     THEN cache_read_tokens END),0) AS cr,
                   COALESCE(SUM(CASE WHEN type='assistant' AND is_sidechain=0 THEN
                     input_tokens + cache_create_5m_tokens + cache_create_1h_tokens END),0) AS rebuild
              FROM messages WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

    if rereads:
        worst = rereads[0]
        out.append({
            "key": "rereads",
            "title": f"{worst['target'].rsplit('/', 1)[-1]} was read {worst['n']} times",
            "body": "Re-reading the same file rebuilds input tokens every time. Ask Claude to take notes "
                    "in the conversation (or summarize the file once) instead of re-opening it.",
        })
    if big and (big["n"] or 0) >= 1:
        out.append({
            "key": "big-results",
            "title": f"{big['n']} tool result(s) over 20k tokens (max {int(big['mx']):,})",
            "body": "Huge tool outputs dominate input cost. Prefer Grep over full-file Reads, "
                    "pipe long Bash output through head/tail, and ask for narrower reads.",
        })
    total_ctx = (usage["cr"] or 0) + (usage["rebuild"] or 0)
    if (usage["turns"] or 0) >= 5 and total_ctx > 100_000:
        hit = (usage["cr"] or 0) / total_ctx
        if hit < 0.40:
            out.append({
                "key": "cache-miss",
                "title": f"Low cache hit rate ({hit * 100:.0f}%)",
                "body": "The prompt cache expires after ~5 minutes of inactivity. Long pauses between "
                        "turns (or restarting context) force a full rebuild — batch related questions "
                        "while the session is warm.",
            })
    if (usage["turns"] or 0) > 40:
        out.append({
            "key": "marathon",
            "title": f"{usage['turns']} turns in one session",
            "body": "Long sessions drag the whole history into every turn. Splitting unrelated tasks "
                    "into separate sessions (or /clear between tasks) keeps input tokens down.",
        })
    return out


def turn_detail(db_path, session_id: str, prompt_id: str) -> dict:
    """Everything we know about one prompt's full turn: per-model usage, tool
    calls, and tool-result sizes.

    promptId only exists on user records in the transcripts — assistant rows
    never carry it — so the turn is reconstructed as the time window from this
    prompt to the session's next *typed* prompt (sidechain work included).
    The window must close only on main-chain text prompts: sidechain user
    records carry their own promptIds (a subagent's injected prompt would
    close the window the moment the Task was dispatched), and tool results
    from a queued concurrent prompt can carry a different promptId mid-turn.
    """
    empty = {
        "session_id": session_id, "prompt_id": prompt_id, "models": [],
        "tool_calls": [], "result_tokens_total": 0, "result_tokens_max": 0,
        "result_count": 0,
    }
    with connect(db_path) as c:
        anchor = c.execute(
            "SELECT MIN(timestamp) AS t0 FROM messages WHERE session_id = ? AND prompt_id = ?",
            (session_id, prompt_id),
        ).fetchone()
        if not anchor or not anchor["t0"]:
            return empty
        t0 = anchor["t0"]
        nxt = c.execute(
            """
            SELECT MIN(timestamp) AS t1 FROM messages
             WHERE session_id = ? AND type = 'user' AND is_sidechain = 0
               AND prompt_text IS NOT NULL AND prompt_id IS NOT NULL
               AND prompt_id != ? AND timestamp > ?
            """,
            (session_id, prompt_id, t0),
        ).fetchone()
        t1 = (nxt and nxt["t1"]) or "9999"

        models = [dict(r) for r in c.execute(
            """
            SELECT COALESCE(model,'unknown') AS model, COUNT(*) AS turns,
                   COALESCE(SUM(input_tokens),0)            AS input_tokens,
                   COALESCE(SUM(output_tokens),0)           AS output_tokens,
                   COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
                   COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
                   COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
              FROM messages
             WHERE session_id = ? AND type = 'assistant'
               AND timestamp >= ? AND timestamp < ?
               AND COALESCE(model,'') != '<synthetic>'
             GROUP BY model
            """,
            (session_id, t0, t1),
        )]
        tool_calls = [dict(r) for r in c.execute(
            """
            SELECT tool_name, target, COUNT(*) AS calls
              FROM tool_calls
             WHERE session_id = ? AND timestamp >= ? AND timestamp < ?
               AND tool_name != '_tool_result'
             GROUP BY tool_name, target
             ORDER BY calls DESC, tool_name
             LIMIT 20
            """,
            (session_id, t0, t1),
        )]
        results = c.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(result_tokens),0) AS total,
                   COALESCE(MAX(result_tokens),0) AS max
              FROM tool_calls
             WHERE session_id = ? AND timestamp >= ? AND timestamp < ?
               AND tool_name = '_tool_result'
            """,
            (session_id, t0, t1),
        ).fetchone()
    return {
        **empty,
        "models": models,
        "tool_calls": tool_calls,
        "result_tokens_total": results["total"],
        "result_tokens_max": results["max"],
        "result_count": results["n"],
    }
