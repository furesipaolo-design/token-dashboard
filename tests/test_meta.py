import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import _encode_slug, init_db, project_root_path
from token_dashboard.meta import (
    MAX_DESCRIPTION_CHARS, auto_description, descriptions,
    extract_description, set_description,
)


class ExtractTests(unittest.TestCase):
    def test_skips_headings_and_badges(self):
        doc = "# Title\n\n[![badge](x)](y)\n\nA local dashboard for tracking tokens.\n\nSecond paragraph."
        self.assertEqual(extract_description(doc), "A local dashboard for tracking tokens.")

    def test_skips_code_fences(self):
        doc = "# T\n\n```bash\nnot a description\n```\n\nReal description here."
        self.assertEqual(extract_description(doc), "Real description here.")

    def test_joins_multiline_paragraph_and_cleans_markdown(self):
        doc = "**Token Dashboard** — reads [transcripts](https://x.y)\nand turns them into `analytics`."
        self.assertEqual(
            extract_description(doc),
            "Token Dashboard — reads transcripts and turns them into analytics.",
        )

    def test_clamps_long_paragraphs(self):
        doc = "word " * 200
        out = extract_description(doc)
        self.assertLessEqual(len(out), MAX_DESCRIPTION_CHARS)
        self.assertTrue(out.endswith("…"))

    def test_empty_doc_returns_none(self):
        self.assertIsNone(extract_description("# Only a heading\n\n## And another\n"))

    def test_yaml_frontmatter_keys_are_not_the_description(self):
        doc = "---\ntitle: Foo\nlayout: home\n---\n\n# Project\n\nReal description here."
        self.assertEqual(extract_description(doc), "Real description here.")

    def test_setext_heading_is_not_the_description(self):
        doc = "My Project\n==========\n\nThe actual intro paragraph."
        self.assertEqual(extract_description(doc), "The actual intro paragraph.")

    def test_unterminated_frontmatter_is_treated_as_content(self):
        # no closing fence → not frontmatter; the key line is just text
        self.assertEqual(extract_description("---\ntitle: Foo\n"), "title: Foo")


class AutoDescriptionTests(unittest.TestCase):
    def test_prefers_claude_md_over_readme(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "CLAUDE.md"), "w") as f:
            f.write("# X\n\nFrom CLAUDE.\n")
        with open(os.path.join(tmp, "README.md"), "w") as f:
            f.write("From README.\n")
        self.assertEqual(auto_description(tmp), "From CLAUDE.")

    def test_falls_back_to_readme(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "README.md"), "w") as f:
            f.write("From README.\n")
        self.assertEqual(auto_description(tmp), "From README.")

    def test_missing_dir_or_docs(self):
        self.assertIsNone(auto_description(None))
        self.assertIsNone(auto_description(tempfile.mkdtemp()))


class RootPathTests(unittest.TestCase):
    def test_finds_root_from_subdir_cwd(self):
        self.assertEqual(
            project_root_path(["/Users/x/proj/sub dir"], "-Users-x-proj"),
            "/Users/x/proj",
        )

    def test_none_when_no_match(self):
        self.assertIsNone(project_root_path(["/elsewhere"], "-Users-x-proj"))


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        # one project whose root dir holds a README
        self.proj_dir = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj_dir)
        with open(os.path.join(self.proj_dir, "README.md"), "w") as f:
            f.write("Auto text.\n")
        # use the real encoder: mkdtemp() suffixes may contain `_`, which
        # encodes to `-` — a hand-rolled replace() made this test flaky
        self.slug = _encode_slug(self.proj_dir)
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp) VALUES ('u1','s1',?,?,'user','2026-06-01T00:00:00Z')",
                (self.slug, self.proj_dir),
            )
            c.commit()

    def test_auto_then_override_then_clear(self):
        d = descriptions(self.db, [self.slug])[self.slug]
        self.assertEqual(d, {"description": "Auto text.", "source": "auto"})

        d = set_description(self.db, self.slug, "Manual text")
        self.assertEqual(d, {"description": "Manual text", "source": "manual"})
        self.assertEqual(descriptions(self.db, [self.slug])[self.slug]["source"], "manual")

        d = set_description(self.db, self.slug, "")
        self.assertEqual(d, {"description": "Auto text.", "source": "auto"})

    def test_unknown_project_has_no_description(self):
        d = descriptions(self.db, ["-nope"])["-nope"]
        self.assertEqual(d, {"description": None, "source": None})


if __name__ == "__main__":
    unittest.main()
