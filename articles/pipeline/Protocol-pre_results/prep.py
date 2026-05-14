"""Build protocol-aware narrative sentences for each claim.

Adapted from empirical/prep.py for protocol/pre-registration documents:
- No self_reported / self_reported_method exclusions (protocols have no own-findings)
- Adds design_precedent and established_method to evidence weights
- Uses protocol-specific triage bucket names

Output: prepped_evidence.json (same top-level shape as empirical).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
PROMPTS_DIR = _BASE / "prompts"
DEFAULT_TEMPLATE = PROMPTS_DIR / "evidence_narrative_template.md"

_EMPIRICAL = _BASE.parent / "empirical"

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("empirical_prep", _EMPIRICAL / "prep.py")
_emp_prep = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_emp_prep)

load_sentence_template = _emp_prep.load_sentence_template
relevancy_label = _emp_prep.relevancy_label
verdict_phrase = _emp_prep.verdict_phrase
normalize_rationale = _emp_prep.normalize_rationale
format_claim_narrative = _emp_prep.format_claim_narrative
evidence_grade_counts = _emp_prep.evidence_grade_counts
RELEVANCY_TIERS = _emp_prep.RELEVANCY_TIERS

EVIDENCE_WEIGHTS: dict[str, float] = {
    "strong": 1.0,
    "moderate": 0.8,
    "design_precedent": 0.85,
    "established_method": 0.9,
    "weak": 0.5,
    "unverifiable": 0.4,
    "unreferenced": 0.3,
    "unsupported": 0.25,
    "pending": 0.3,
}

SCORE_EXCLUDED_GRADES: frozenset[str] = frozenset()

KEEP_BUCKETS = frozenset({
    "design_specification", "methodological", "background_rationale",
    "contextual", "aspirational",
})


def _iter_claims(dimension: dict):
    """Yield all claims from non-noise buckets."""
    buckets = dimension.get("buckets", {})
    for bname, claims in buckets.items():
        if bname not in KEEP_BUCKETS:
            continue
        if not isinstance(claims, list):
            continue
        yield from claims


def compute_evidence_score(claims: list[dict]) -> float:
    """Weighted score from all graded claims.

    Protocol mode: no exclusions — all grades contribute since there are
    no self-reported findings to set aside.
    """
    if not claims:
        return 0.5
    total_weight = 0.0
    total_relevancy_weight = 0.0
    for c in claims:
        grade = str(c.get("evidence_grade") or "pending").strip().lower()
        if grade in SCORE_EXCLUDED_GRADES:
            continue
        ew = EVIDENCE_WEIGHTS.get(grade, 0.3)
        try:
            rel = float(c.get("relevancy_score") or 0.5)
        except (TypeError, ValueError):
            rel = 0.5
        rel = max(0.0, min(1.0, rel))
        total_weight += ew * rel
        total_relevancy_weight += rel
    if total_relevancy_weight == 0:
        return 0.5
    return round(total_weight / total_relevancy_weight, 4)


def enrich_evidence(data: dict, sentence_template: str) -> dict:
    out: dict = {}
    for dim_key, dim_data in data.items():
        if not isinstance(dim_data, dict) or "buckets" not in dim_data:
            out[dim_key] = dim_data
            continue

        claims = list(_iter_claims(dim_data))

        for rec in claims:
            rec["claim_narrative"] = format_claim_narrative(sentence_template, rec)

        grade_dist = evidence_grade_counts(claims)

        new_buckets = {}
        for bname, blist in dim_data.get("buckets", {}).items():
            if bname not in KEEP_BUCKETS:
                continue
            new_buckets[bname] = blist

        out[dim_key] = {
            "score": dim_data.get("score"),
            "evidence_grade_distribution": grade_dist,
            "buckets": new_buckets,
            "members": claims,
            "stats": dim_data.get("stats"),
        }

    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description="Build protocol-aware narratives and scores from retrieve_compare output."
    )
    p.add_argument(
        "input_json",
        help="Path to retrieve_compare_llm.json (or retrieve_compare_out.json)",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write enriched JSON here (default: stdout)",
    )
    p.add_argument(
        "--template", type=Path, default=DEFAULT_TEMPLATE,
        help="Markdown file with ## Sentence template section",
    )
    args = p.parse_args()

    sentence = load_sentence_template(args.template)
    data = json.loads(
        Path(args.input_json).expanduser().resolve().read_text(encoding="utf-8")
    )
    if not isinstance(data, dict):
        raise ValueError("Root JSON must be an object keyed by dimension id.")

    enriched = enrich_evidence(data, sentence)
    text = json.dumps(enriched, indent=2, ensure_ascii=False) + "\n"

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote prepped evidence to {args.output}")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
