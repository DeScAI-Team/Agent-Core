#!/usr/bin/env python3
"""Smoke tests for multi-pipeline evidence extraction (no LLM)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_MULTI = Path(__file__).resolve().parent
_COMPOUNDS = _MULTI.parent.parent
if str(_COMPOUNDS) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS))
if str(_MULTI) not in sys.path:
    sys.path.insert(0, str(_MULTI))

from evidence_sources import (  # noqa: E402
    build_compound_pipeline_evidence,
    coverage_from_rows,
    interaction_units_from_risk,
    kegg_from_rows,
    load_topic_summaries,
    snippet_from_row,
    spl_from_risk_rows,
)
from token_lookup import resolve_token_entry  # noqa: E402


def test_kegg_from_filtered_rows() -> None:
    rows = [
        {
            "source_type": "kegg_summary",
            "content": {
                "kegg_drug_ids": ["D00001"],
                "longevity_pathway_flags": {"mTOR signaling": True},
            },
        },
    ]
    kegg = kegg_from_rows(rows)
    assert kegg["kegg_available"] is True
    assert "mTOR signaling" in kegg["flags_present"]


def test_interaction_units_from_risk() -> None:
    rows = [
        {
            "source_type": "europe_pmc",
            "content": {"title": "Rh2 co-administered with cisplatin", "abstract": "CYP3A4 interaction."},
            "risk_relevance": "interaction_or_combination_risk",
        },
    ]
    units = interaction_units_from_risk(rows)
    assert len(units) == 1
    assert "cisplatin" in (units[0].get("snippet") or "")


def test_spl_from_risk_label_rows() -> None:
    rows = [
        {
            "source_type": "openfda_drug_label",
            "content": {
                "openfda": {"brand_name": ["Metformin"]},
                "drug_interactions": "Metformin: avoid CYP3A4 inhibitors when co-administered.",
            },
        },
    ]
    spl = spl_from_risk_rows(rows, "Metformin")
    assert spl["label_matched"] is True
    assert spl["interaction_excerpts"]


def test_build_compound_pipeline_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "longevity.json").write_text(
            json.dumps({
                "source_type": "chembl_mechanism",
                "content": {"mechanism_of_action": "activates AMPK"},
                "longevity_relevance": "indirect_longevity_mechanism",
            })
            + "\n",
            encoding="utf-8",
        )
        (data_dir / "risk.json").write_text(
            json.dumps({
                "source_type": "europe_pmc",
                "content": {"title": "Metformin combined with rapamycin"},
                "risk_relevance": "interaction_or_combination_risk",
            })
            + "\n",
            encoding="utf-8",
        )
        (data_dir / "longevity_topic_summaries.json").write_text(
            json.dumps({
                "axis": "longevity",
                "summaries": [{
                    "topic_id": "longevity_001",
                    "topic_label": "ampk",
                    "bullets": ["Metformin activates AMPK in preclinical models."],
                    "unit_ids": ["longevity_001"],
                }],
            }),
            encoding="utf-8",
        )
        (data_dir / "review.json").write_text(
            json.dumps({
                "categories": {
                    "scientific_grounding": {"score": 70, "rationale": "Grounding text."},
                    "risk_assessment": {"score": 40, "rationale": "Risk text."},
                },
            }),
            encoding="utf-8",
        )

        ev = build_compound_pipeline_evidence("Metformin", data_dir)
        assert ev["pipeline_counts"]["longevity_rows"] == 1
        assert ev["longevity_evidence"]
        assert ev["interaction_evidence"]
        assert ev["longevity_topic_summaries"]
        assert ev["scientific_grounding_rationale"] == "Grounding text."
        assert not any("material.json" in w for w in ev.get("warnings", []))


def test_resolve_token_entry_omigu() -> None:
    tokens_path = _COMPOUNDS.parent / "crawlers" / "output" / "pump.science" / "compound-tokens.json"
    if not tokens_path.is_file():
        return
    entry = resolve_token_entry(
        ["Omipalisib", "Ginsenoside Rh2", "Urolithin A"],
        tokens_path=tokens_path,
    )
    assert entry["ticker"] == "OMIGU"


def main() -> int:
    test_kegg_from_filtered_rows()
    test_interaction_units_from_risk()
    test_spl_from_risk_label_rows()
    test_build_compound_pipeline_evidence()
    test_resolve_token_entry_omigu()
    print("All multi interactions tests passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
