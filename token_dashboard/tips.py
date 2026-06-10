"""Rule-based tips engine — produces actionable suggestions from SQLite."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .db import connect
from .pricing import load_pricing

_PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"


def _tier_rates(tier: str, fallback: dict) -> dict:
    """Full $/MTok rate card for a tier from pricing.json, with a safe fallback."""
    try:
        rates = load_pricing(_PRICING_JSON)["tier_fallback"][tier]
        if all(k in rates for k in ("input", "output", "cache_read",
                                    "cache_create_5m", "cache_create_1h")):
            return rates
    except (OSError, KeyError, ValueError):
        pass
    return fallback


def _usage_cost(rates: dict, row) -> float:
    """Dollar cost of an aggregated usage row at a tier's rates."""
    return (
        (row["in_tok"] or 0) * rates["input"]
        + (row["cc5"] or 0) * rates["cache_create_5m"]
        + (row["cc1"] or 0) * rates["cache_create_1h"]
        + (row["cr"] or 0) * rates["cache_read"]
        + (row["out_tok"] or 0) * rates["output"]
    ) / 1_000_000


def _utcnow_iso() -> str:
    # naive-UTC, matching the shape of stored transcript timestamps
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _iso_days_ago(today_iso: str, n: int) -> str:
    d = datetime.fromisoformat(today_iso.replace("Z", ""))
    return (d - timedelta(days=n)).isoformat()


def _key(category: str, scope: str) -> str:
    return f"{category}:{scope}"


def _is_dismissed(db_path, key: str) -> bool:
    with connect(db_path) as c:
        r = c.execute("SELECT dismissed_at FROM dismissed_tips WHERE tip_key=?", (key,)).fetchone()
    if not r:
        return False
    return (time.time() - r["dismissed_at"]) < 14 * 86400


def dismiss_tip(db_path, key: str) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO dismissed_tips (tip_key, dismissed_at) VALUES (?, ?)",
            (key, time.time()),
        )
        c.commit()


def cache_discipline_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or _utcnow_iso()
    since = _iso_days_ago(today_iso, 7)
    # Main-chain only: subagents and auto-compact runs start with cold caches
    # by design — their rebuild isn't a habit the user can change.
    sql = """
      SELECT project_slug,
             SUM(cache_read_tokens) AS cr,
             SUM(input_tokens + cache_create_5m_tokens + cache_create_1h_tokens) AS rebuild
        FROM messages
       WHERE type='assistant' AND is_sidechain=0 AND timestamp >= ?
       GROUP BY project_slug
       HAVING (cr + rebuild) > 100000
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since,)):
            total = (row["cr"] or 0) + (row["rebuild"] or 0)
            hit = (row["cr"] or 0) / total if total else 0
            if hit < 0.40:
                key = _key("cache", row["project_slug"])
                if _is_dismissed(db_path, key):
                    continue
                out.append({
                    "key": key,
                    "category": "cache",
                    "title": f"Low cache hit rate in {row['project_slug']}",
                    "body": f"Cache hit rate is {hit*100:.0f}% over the last 7 days. Sessions that restart context frequently rebuild cache. Consider longer-lived sessions or fewer context resets.",
                    "scope": row["project_slug"],
                })
    return out


def repeated_target_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or _utcnow_iso()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        # Read only: counting Edit/Write here flagged every actively-developed
        # file, and the advice (summarize once, read once per session) can't
        # reduce edit counts anyway.
        for row in c.execute("""
          SELECT target, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions
            FROM tool_calls
           WHERE tool_name = 'Read' AND timestamp >= ?
           GROUP BY target HAVING n > 10
           ORDER BY n DESC LIMIT 10
        """, (since,)):
            key = _key("repeat-file", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            out.append({
                "key": key, "category": "repeat-file",
                "title": f"{row['target']} read {row['n']} times",
                "body": f"This file was opened {row['n']} times across {row['sessions']} sessions in the past 7 days. A summary in CLAUDE.md or one read per session would avoid repeats.",
                "scope": row["target"],
            })
        for row in c.execute("""
          SELECT target, COUNT(*) AS n
            FROM tool_calls
           WHERE tool_name='Bash' AND timestamp >= ?
           GROUP BY target HAVING n > 15
           ORDER BY n DESC LIMIT 10
        """, (since,)):
            key = _key("repeat-bash", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            out.append({
                "key": key, "category": "repeat-bash",
                "title": f"`{row['target']}` ran {row['n']} times",
                "body": f"This bash command ran {row['n']} times in the past 7 days. Consider a watch flag or shell alias.",
                "scope": row["target"],
            })
    return out


_OPUS_FALLBACK   = {"input": 5.0, "output": 25.0, "cache_read": 0.50,
                    "cache_create_5m": 6.25, "cache_create_1h": 10.0}
_SONNET_FALLBACK = {"input": 3.0, "output": 15.0, "cache_read": 0.30,
                    "cache_create_5m": 3.75, "cache_create_1h": 6.0}


def right_size_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or _utcnow_iso()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT COUNT(*) AS n,
             SUM(input_tokens) AS in_tok,
             SUM(cache_create_5m_tokens) AS cc5,
             SUM(cache_create_1h_tokens) AS cc1,
             SUM(cache_read_tokens) AS cr,
             SUM(output_tokens) AS out_tok
        FROM messages
       WHERE type='assistant' AND model LIKE '%opus%'
         AND output_tokens < 500 AND is_sidechain = 0
         AND timestamp >= ?
    """
    with connect(db_path) as c:
        row = c.execute(sql, (since,)).fetchone()
    if not row or (row["n"] or 0) < 10:
        return []
    api_opus   = _usage_cost(_tier_rates("opus", _OPUS_FALLBACK), row)
    api_sonnet = _usage_cost(_tier_rates("sonnet", _SONNET_FALLBACK), row)
    savings = api_opus - api_sonnet
    if savings < 1.0:
        return []
    key = _key("right-size", "opus-short-turns-7d")
    if _is_dismissed(db_path, key):
        return []
    return [{
        "key": key, "category": "right-size",
        "title": f"{row['n']} short Opus calls might fit on Sonnet",
        "body": f"Opus API calls with under 500 output tokens (including the tool-call steps of "
                f"longer turns) cost ~${api_opus:.2f} in cache and tokens over the last 7 days. "
                f"The same traffic on Sonnet would be ~${api_sonnet:.2f} (~${savings:.2f} less). "
                f"For short, mechanical tasks consider starting the session on Sonnet.",
        "scope": "opus-short-turns-7d",
    }]


def outlier_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or _utcnow_iso()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        big = c.execute("""
          SELECT COUNT(*) AS n, AVG(result_tokens) AS avg_t
            FROM tool_calls
           WHERE tool_name='_tool_result' AND result_tokens > 50000 AND timestamp >= ?
        """, (since,)).fetchone()
        if big and (big["n"] or 0) >= 5:
            key = _key("tool-bloat", "result-50k+")
            if not _is_dismissed(db_path, key):
                out.append({
                    "key": key, "category": "tool-bloat",
                    "title": f"{big['n']} tool results over 50k tokens this week",
                    "body": f"Average size is {int(big['avg_t']):,} tokens. Pipe long Bash output to head/tail and ask for narrower file reads.",
                    "scope": "result-50k+",
                })
        # agent_id is unique per subagent *run*, so compare whole-run totals
        # across runs (grouping per agent_id and comparing rows within one
        # run only ever described single API calls and never fired).
        runs = [r["total"] or 0 for r in c.execute("""
          SELECT agent_id, SUM(input_tokens+output_tokens) AS total
            FROM messages
           WHERE is_sidechain=1 AND agent_id IS NOT NULL AND timestamp >= ?
           GROUP BY agent_id
        """, (since,))]
        if len(runs) >= 5:
            max_t = max(runs)
            others = list(runs)
            others.remove(max_t)
            # Compare against the mean of the *other* runs: a mean that
            # includes the outlier can never be exceeded 6× with few runs
            # (max/mean ≤ n), so the rule would silently never fire.
            mean_t = sum(others) / len(others)
            if max_t > 6 * mean_t and max_t > 50_000:
                key = _key("subagent-outlier", "7d")
                if not _is_dismissed(db_path, key):
                    out.append({
                        "key": key, "category": "subagent-outlier",
                        "title": "One subagent run was a token outlier",
                        "body": f"The largest of {len(runs)} subagent runs this week used "
                                f"{int(max_t):,} tokens vs a {int(mean_t):,} average. Worth "
                                f"checking what that run did differently (over-broad prompt, "
                                f"missing scope limits).",
                        "scope": "7d",
                    })
    return out


def all_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    return [
        *cache_discipline_tips(db_path, today_iso),
        *repeated_target_tips(db_path, today_iso),
        *right_size_tips(db_path, today_iso),
        *outlier_tips(db_path, today_iso),
    ]
