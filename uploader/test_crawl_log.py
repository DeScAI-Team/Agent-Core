"""Tests for crawl-log v2 mark/review resolution."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.crawl_log import (
    CrawlLogMark,
    mark_reviewed,
    resolve_crawl_log_mark,
    _normalize_entries,
)
from uploader.recipes import get_recipe


class TestMarkReviewed(unittest.TestCase):
    def test_marks_existing_entry(self) -> None:
        data = {
            "version": 2,
            "researchhub": {
                "files": [{"path": "papers/PaperRecord_1.json"}],
            },
        }
        self.assertTrue(mark_reviewed(data, section="researchhub", key="papers/PaperRecord_1.json"))
        self.assertEqual(data["researchhub"]["files"][0]["reviewed"], "reviewed")

    def test_appends_missing_entry(self) -> None:
        data = {"version": 2, "molecule": {"folders": []}}
        mark_reviewed(data, section="molecule", key="VITA")
        self.assertEqual(len(data["molecule"]["folders"]), 1)
        self.assertEqual(data["molecule"]["folders"][0]["name"], "VITA")
        self.assertEqual(data["molecule"]["folders"][0]["reviewed"], "reviewed")

    def test_normalize_preserves_reviewed(self) -> None:
        entries = _normalize_entries(
            "pumpScience",
            [{"ticker": "RIF"}, {"ticker": "URO", "reviewed": "reviewed"}],
        )
        self.assertEqual(entries[0]["ticker"], "RIF")
        self.assertNotIn("reviewed", entries[0])
        self.assertEqual(entries[1]["reviewed"], "reviewed")


class TestResolveCrawlLogMark(unittest.TestCase):
    def test_proposal(self) -> None:
        recipe = get_recipe("proposal")
        mark = resolve_crawl_log_mark(
            recipe,
            Path("/reviews/proposals/proposal_99/review"),
            {},
        )
        self.assertEqual(mark, CrawlLogMark("researchhub", "proposals/proposal_99.json"))

    def test_compounds(self) -> None:
        recipe = get_recipe("compounds")
        mark = resolve_crawl_log_mark(
            recipe,
            Path("/reviews/compounds/RIF/review"),
            {"compound_name": "RIF"},
        )
        self.assertEqual(mark, CrawlLogMark("pumpScience", "RIF"))

    def test_article_from_papers_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers = Path(tmp) / "papers"
            papers.mkdir()
            paper = papers / "PaperRecord_42.json"
            paper.write_text(
                json.dumps({"pdf_url": "https://example.com/my-paper-file.pdf"}),
                encoding="utf-8",
            )
            recipe = get_recipe("article")
            with patch("uploader.crawl_log.PAPERS_DIR", papers):
                mark = resolve_crawl_log_mark(
                    recipe,
                    Path("/reviews/articles/my-paper-file/review"),
                    {},
                )
            self.assertEqual(mark, CrawlLogMark("researchhub", "papers/PaperRecord_42.json"))


if __name__ == "__main__":
    unittest.main()
