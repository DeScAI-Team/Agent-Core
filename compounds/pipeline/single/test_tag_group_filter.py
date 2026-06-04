#!/usr/bin/env python3
"""Unit tests for longevity/risk review filters (no LLM)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SINGLE = Path(__file__).resolve().parent
if str(_SINGLE) not in sys.path:
    sys.path.insert(0, str(_SINGLE))

_TAG_FILTER_PATH = _SINGLE / "tag-group-filter.py"
_spec = importlib.util.spec_from_file_location("tag_group_filter", _TAG_FILTER_PATH)
assert _spec and _spec.loader
_tgf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tgf)


def test_oncology_indirect_excluded_from_longevity_export() -> None:
    row = {
        "source_type": "europe_pmc",
        "content": {
            "title": "Rh2 induces autophagy in HepG2 cells",
            "abstract": "Cytotoxic effects on hepatoma cell lines via mTOR inhibition.",
        },
        "longevity_relevance": "indirect_longevity_mechanism",
        "risk_relevance": "not_relevant",
    }
    assert not _tgf.passes_longevity_review(row)


def test_aging_context_indirect_included() -> None:
    row = {
        "source_type": "europe_pmc",
        "content": {
            "title": "Rh2 and cellular senescence in aging",
            "abstract": "Effects on senescence markers in aged mice and healthspan.",
        },
        "longevity_relevance": "indirect_longevity_mechanism",
        "risk_relevance": "not_relevant",
    }
    assert _tgf.passes_longevity_review(row)


def test_assay_cytotox_excluded_from_risk_export() -> None:
    row = {
        "source_type": "pubchem_bioassay",
        "content": {"description": "Cytotoxicity assay, GI50 on cancer cell line"},
        "longevity_relevance": "not_relevant",
        "risk_relevance": "toxicity_or_adverse_signal",
    }
    assert not _tgf.passes_risk_review(row)


def test_faers_included_in_risk_export() -> None:
    row = {
        "source_type": "openfda_faers",
        "content": {"reactions": ["nausea"]},
        "longevity_relevance": "not_relevant",
        "risk_relevance": "toxicity_or_adverse_signal",
    }
    assert _tgf.passes_risk_review(row)


def test_apply_rules_demotes_assay_tox() -> None:
    row = {
        "source_type": "chembl_activity",
        "content": {"description": "Cytotoxicity in A549, IC50 2 uM"},
    }
    _, risk = _tgf.apply_rule_based_relevance_overrides(row, None, "toxicity_or_adverse_signal")
    assert risk == "no_risk_signal"


def test_apply_rules_demotes_oncology_indirect() -> None:
    row = {
        "source_type": "europe_pmc",
        "content": {
            "title": "Antitumor activity of ginsenoside Rh2",
            "abstract": "mTOR pathway in breast cancer xenografts.",
        },
    }
    longevity, _ = _tgf.apply_rule_based_relevance_overrides(
        row, "indirect_longevity_mechanism", "not_relevant"
    )
    assert longevity == "general_bioactivity"


def main() -> int:
    test_oncology_indirect_excluded_from_longevity_export()
    test_aging_context_indirect_included()
    test_assay_cytotox_excluded_from_risk_export()
    test_faers_included_in_risk_export()
    test_apply_rules_demotes_assay_tox()
    test_apply_rules_demotes_oncology_indirect()
    print("All tag-group-filter tests passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
