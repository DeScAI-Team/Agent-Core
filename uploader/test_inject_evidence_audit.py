"""Tests for evidence_audit field injection in upload temp copies."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uploader.core import _insert_after_key, inject_evidence_audit


class TestInsertAfterKey(unittest.TestCase):
    def test_inserts_after_review_date(self) -> None:
        data = {
            "research_name": "Foo",
            "review_date": "June 2, 2026",
            "composite_score": 80,
            "review_statement": "Original text.",
        }
        result = _insert_after_key(data, "review_date", "evidence_audit", "https://arweave.net/abc")
        self.assertEqual(
            list(result.keys()),
            ["research_name", "review_date", "evidence_audit", "composite_score", "review_statement"],
        )
        self.assertEqual(result["evidence_audit"], "https://arweave.net/abc")
        self.assertEqual(result["review_statement"], "Original text.")

    def test_replaces_existing_evidence_audit(self) -> None:
        data = {"review_date": "June 2, 2026", "evidence_audit": "old", "score": 1}
        result = _insert_after_key(data, "review_date", "evidence_audit", "https://arweave.net/new")
        self.assertEqual(result["evidence_audit"], "https://arweave.net/new")
        self.assertEqual(list(result.keys()), ["review_date", "evidence_audit", "score"])


class TestInjectEvidenceAudit(unittest.TestCase):
    def _write_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_injects_after_review_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "review.json"
            self._write_json(
                src,
                {
                    "research_name": "Test",
                    "review_date": "June 2, 2026",
                    "review_statement": "Unchanged.",
                },
            )
            out = inject_evidence_audit(src, "tx123")
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            finally:
                out.unlink()

            self.assertEqual(
                list(data.keys())[:3],
                ["research_name", "review_date", "evidence_audit"],
            )
            self.assertEqual(data["evidence_audit"], "https://arweave.net/tx123")
            self.assertEqual(data["review_statement"], "Unchanged.")

    def test_fallback_after_research_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "review.json"
            self._write_json(src, {"research_name": "Test", "review_statement": "Body."})
            out = inject_evidence_audit(src, "tx456")
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            finally:
                out.unlink()

            self.assertEqual(
                list(data.keys())[:2],
                ["research_name", "evidence_audit"],
            )
            self.assertEqual(data["evidence_audit"], "https://arweave.net/tx456")


class TestRecipes(unittest.TestCase):
    def test_all_review_recipes_are_three_step_with_overview(self) -> None:
        from uploader.recipes import RECIPES

        for name in ("article", "proposal", "dao", "compounds"):
            recipe = RECIPES[name]
            self.assertEqual(len(recipe.steps), 3, name)
            self.assertIn("overview", recipe.file_map, name)
            self.assertEqual(recipe.steps[-1].metadata_key, "overview", name)
            self.assertTrue(recipe.steps[1].inject_evidence_audit, name)
            self.assertTrue(recipe.steps[2].inject_evidence_audit, name)


if __name__ == "__main__":
    unittest.main()
