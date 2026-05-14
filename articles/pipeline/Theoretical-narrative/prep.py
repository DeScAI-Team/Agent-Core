"""Build evidence-aware narrative sentences for each claim.

Reads retrieve_compare output (triaged + evidence-graded JSON) and adds a
``claim_narrative`` to each claim using the evidence narrative template.
Strips noise-bucket claims and computes per-dimension grade distributions
for downstream rationale generation.  Scoring is handled by ``score.py``.

Theoretical-narrative adaptation:
- KEEP_BUCKETS includes thesis_argument and synthesis (not empirical)
- No grades are excluded from scoring (no self_reported exemption)
- Adds overclaim grade weight

Output: prepped_evidence.json (same top-level shape, claims enriched with
``claim_narrative`` and ``evidence_grade_distribution``).
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

RELEVANCY_TIERS: tuple[tuple[float, str], ...] = (
    (0.2, "low relevancy"),
    (0.4, "slightly relevant"),
    (0.6, "moderately relevant"),
    (0.8, "very relevant"),
    (1.0, "extremely relevant"),
)

EVIDENCE_WEIGHTS: dict[str, float] = {
    "strong": 1.0,
    "moderate": 0.8,
    "weak": 0.5,
    "unverifiable": 0.4,
    "unreferenced": 0.35,
    "unsupported": 0.25,
    "overclaim": 0.2,
    "pending": 0.3,
}

SCORE_EXCLUDED_GRADES: frozenset[str] = frozenset()

KEEP_BUCKETS = frozenset({
    "thesis_argument", "synthesis", "methodological", "contextual", "aspirational",
})


def load_sentence_template(template_path: Path) -> str:
    text = template_path.read_text(encoding="utf-8")
    if "## Sentence template" not in text:
        raise ValueError(f"No '## Sentence template' section in {template_path}")
    after = text.split("## Sentence template", 1)[1]
    for line in after.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("|"):
            return line
    raise ValueError(f"No template line after '## Sentence template' in {template_path}")


def relevancy_label(score: object) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "relevancy unknown"
    s = max(0.0, min(1.0, s))
    for upper, label in RELEVANCY_TIERS:
        if s < upper or upper == 1.0 and s <= 1.0:
            return label
    return "relevancy unknown"


def verdict_phrase(verdict: object) -> str:
    if verdict is None:
        return "unknown"
    return str(verdict).strip().replace("_", " ")


def normalize_rationale(raw: str) -> str:
    s = raw.strip()
    if not s:
        s = "no rationale recorded"
    return s if s.endswith(".") else s + "."


def format_claim_narrative(template: str, rec: dict) -> str:
    doc = str(rec.get("doc_name") or "This document").strip().replace("_", " ")
    claim = str(rec.get("claim") or "")
    raw_section = rec.get("section_heading")
    section = (
        str(raw_section).strip().replace("_", " ")
        if raw_section is not None and str(raw_section).strip()
        else "unspecified"
    )
    verdict = verdict_phrase(rec.get("verdict"))
    rationale = normalize_rationale(str(rec.get("rationale") or ""))
    rel = relevancy_label(rec.get("relevancy_score"))
    eg = str(rec.get("evidence_grade") or "unknown").replace("_", " ")
    es = normalize_rationale(str(rec.get("evidence_summary") or ""))

    out = template
    for field, value in [
        ("doc_name", doc),
        ("claim", claim),
        ("section_heading", section),
        ("verdict", verdict),
        ("rationale", rationale),
        ("relevancy_label", rel),
        ("evidence_grade", eg),
        ("evidence_summary", es),
    ]:
        out = out.replace("{" + field + "}", value)

    bad = re.findall(r"\{([^}]+)\}", out)
    if bad:
        raise ValueError(f"Unresolved placeholders in template: {bad}")
    return out


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

    Theoretical papers have no self-reported exemptions -- all claims
    contribute to the evidence score.
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


def evidence_grade_counts(claims: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in claims:
        grade = str(c.get("evidence_grade") or "unknown").strip().lower()
        counts[grade] = counts.get(grade, 0) + 1
    return counts


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
        description="Build evidence-aware narratives and scores from retrieve_compare output."
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
