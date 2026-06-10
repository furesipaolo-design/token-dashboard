"""Per-project and per-session drill-down queries (cards detail, session header)."""
from __future__ import annotations

from .db import best_project_name, connect

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
            """
            SELECT MIN(timestamp) AS started, MAX(timestamp) AS ended,
                   COUNT(*) AS records,
                   SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
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

        # NOT LIKE '<%' skips harness-injected user records
        # (<local-command-caveat>, <ide_opened_file>, <system-reminder>, …)
        first = c.execute(
            """
            SELECT prompt_text FROM messages
             WHERE session_id = ? AND type = 'user'
               AND prompt_text IS NOT NULL AND prompt_text != ''
               AND prompt_text NOT LIKE '<%'
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
