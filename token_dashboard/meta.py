"""Project descriptions: auto-extracted from CLAUDE.md/README.md, manual override in DB.

Auto descriptions are computed on request from the project's own docs on disk
(local reads only) and never stored; only manual overrides persist, in the
`project_meta` table. Clearing an override falls back to the auto extraction.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional, Union

from .db import connect, project_root_path

DOC_CANDIDATES = ("CLAUDE.md", "README.md")
MAX_DESCRIPTION_CHARS = 280
_MAX_DOC_BYTES = 256 * 1024

_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"[*_`]{1,3}")
_WS = re.compile(r"\s+")


def _clean_paragraph(lines: list) -> str:
    text = " ".join(lines)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_MARKS.sub("", text)
    text = _WS.sub(" ", text).strip()
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return text


_SETEXT_UNDERLINE = re.compile(r"=+|-{2,}")


def _strip_frontmatter(lines: list) -> list:
    """Drop a leading YAML frontmatter block (--- … --- / ...), keys included."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return lines
    for j in range(i + 1, len(lines)):
        if lines[j].strip() in ("---", "..."):
            return lines[j + 1:]
    return lines  # unterminated — treat as content


def extract_description(doc_text: str) -> Optional[str]:
    """First meaningful paragraph of a markdown doc: skips YAML frontmatter,
    headings (ATX and setext), badges, blockquotes, HTML comments, tables,
    and fenced code."""
    in_fence = False
    paragraph: list = []
    for raw in _strip_frontmatter(doc_text.splitlines()):
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if len(paragraph) == 1 and _SETEXT_UNDERLINE.fullmatch(line):
            # the buffered line was a setext heading ("Title\n====="), not prose
            paragraph = []
            continue
        skip = (
            not line
            or line.startswith(("#", ">", "<!--", "|", "---", "[![", "!["))
            or _SETEXT_UNDERLINE.fullmatch(line)
        )
        if skip:
            if paragraph:
                break  # paragraph ended
            continue
        paragraph.append(line)
    if not paragraph:
        return None
    return _clean_paragraph(paragraph) or None


def auto_description(root_path: Optional[str]) -> Optional[str]:
    if not root_path:
        return None
    root = Path(root_path)
    for name in DOC_CANDIDATES:
        p = root / name
        try:
            if not p.is_file():
                continue
            desc = extract_description(p.read_text(encoding="utf-8", errors="replace")[:_MAX_DOC_BYTES])
        except OSError:
            continue
        if desc:
            return desc
    return None


def descriptions(db_path: Union[str, Path], slugs: list) -> dict:
    """slug -> {description, source} where source is 'manual' | 'auto' | None."""
    out = {}
    with connect(db_path) as c:
        overrides = {
            r["project_slug"]: r["description"]
            for r in c.execute("SELECT project_slug, description FROM project_meta")
        }
        for slug in slugs:
            if slug in overrides:
                out[slug] = {"description": overrides[slug], "source": "manual"}
                continue
            cwds = [r["cwd"] for r in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                (slug,),
            )]
            desc = auto_description(project_root_path(cwds, slug))
            out[slug] = {"description": desc, "source": "auto" if desc else None}
    return out


def archived_slugs(db_path: Union[str, Path]) -> set:
    with connect(db_path) as c:
        return {r["project_slug"] for r in c.execute("SELECT project_slug FROM archived_projects")}


def set_archived(db_path: Union[str, Path], slug: str, archived: bool) -> bool:
    with connect(db_path) as c:
        if archived:
            c.execute(
                "INSERT OR REPLACE INTO archived_projects (project_slug, archived_at) VALUES (?, ?)",
                (slug, time.time()),
            )
        else:
            c.execute("DELETE FROM archived_projects WHERE project_slug=?", (slug,))
        c.commit()
    return archived


def set_description(db_path: Union[str, Path], slug: str, text: str) -> dict:
    """Store a manual override; empty text clears it (back to auto).
    Returns the resolved {description, source} after the change."""
    text = (text or "").strip()
    with connect(db_path) as c:
        if text:
            c.execute(
                "INSERT OR REPLACE INTO project_meta (project_slug, description, updated_at) VALUES (?, ?, ?)",
                (slug, text[:MAX_DESCRIPTION_CHARS], time.time()),
            )
        else:
            c.execute("DELETE FROM project_meta WHERE project_slug=?", (slug,))
        c.commit()
    return descriptions(db_path, [slug])[slug]
