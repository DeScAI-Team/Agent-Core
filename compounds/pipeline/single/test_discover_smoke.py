#!/usr/bin/env python3
"""Smoke tests for dedupe, material append, and review grounding default (no LLM)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_SINGLE = Path(__file__).resolve().parent
if str(_SINGLE) not in sys.path:
    sys.path.insert(0, str(_SINGLE))

from discover_lib.dedupe import DiscoverRegistry, dedupe_key_for_row  # noqa: E402
from discover_lib.material import (  # noqa: E402
    append_material_rows,
    ensure_material_json,
    init_material_file,
    load_material_records,
)


def test_dedupe_key_stable() -> None:
    row = {
        "source_type": "europe_pmc",
        "content": {"pmid": "12345", "title": "Test Article", "doi": "10.1/example"},
    }
    assert dedupe_key_for_row(row) == "pmid:12345"
    reg = DiscoverRegistry()
    assert reg.add_row(row) is True
    assert reg.add_row(row) is False


def test_material_append_dedupe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "material.json"
        init_material_file(path, "Test Compound", fresh=True)
        row = {"source_type": "europe_pmc", "content": {"pmid": "999", "title": "A"}}
        reg = DiscoverRegistry()
        assert reg.add_row(row)
        append_material_rows(path, [row])
        dup = {"source_type": "europe_pmc", "content": {"pmid": "999", "title": "A again"}}
        if reg.add_row(dup):
            append_material_rows(path, [dup])
        rows = load_material_records(path)
        pmids = [
            r["content"]["pmid"]
            for r in rows
            if r.get("source_type") == "europe_pmc"
        ]
        assert pmids.count("999") == 1


def test_report_converts_to_material_jsonl() -> None:
    report = {
        "compound_name": "Test Compound",
        "kegg": {
            "kegg_drug_ids": ["D00001"],
            "longevity_pathway_flags": {"mTOR signaling": True},
            "pathways": [],
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        compound_dir = Path(tmp)
        report_path = compound_dir / "report_20260101_000000Z.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        material_path = ensure_material_json(compound_dir, "Test Compound")
        assert material_path is not None
        assert material_path.name == "material.json"
        rows = load_material_records(material_path)
        assert any(r.get("source_type") == "kegg_summary" for r in rows)


def test_grounding_default_50() -> None:
    from review import (  # noqa: E402
        DEFAULT_GROUNDING_SCORE_PCT,
        _fraction_to_percent,
        scientific_grounding_score,
    )

    assert DEFAULT_GROUNDING_SCORE_PCT == 50.0
    assert scientific_grounding_score([]) is None
    assert _fraction_to_percent(scientific_grounding_score([])) is None


def main() -> int:
    test_dedupe_key_stable()
    test_material_append_dedupe()
    test_report_converts_to_material_jsonl()
    test_grounding_default_50()
    print("All smoke tests passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
