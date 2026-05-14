"""Deterministic triage of grouped.json claims into protocol review buckets.

Buckets (first match wins: design_specification → methodological →
background_rationale → aspirational → contextual):

- **design_specification**: Core protocol design — hypotheses, endpoints,
  sample size, randomization, blinding, inclusion/exclusion criteria.
  Fact or Assertion claims with empirical-design tags describing *planned*
  measurements or outcomes.
- **methodological**: Planned analytical methods, statistical analysis plan,
  measurement procedures — Fact or Assertion with Methodological, Benchmark,
  or Measurement paired with Methodological tags.
- **background_rationale**: Literature-grounded justification, gap
  identification — Fact or Assertion with Background, Synthesis, or
  SourceAttribution tags, plus Roadmap claims with GapStatement.
- **aspirational**: Expected outcomes, anticipated impact, feasibility —
  Roadmap claims (except GapStatement), Fact/Assertion with
  NoveltyAssertion/FutureWork/Feasibility/ImpactPotential tags.
- **contextual**: Definitions, framing, interpretive context — Fact/Assertion
  with Definitional/Interpretive/Prescriptive/Hedge tags.

Noise: low relevancy, missing primary tags, claim_type None, figure/table
captions, or no bucket match.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO

CLASSIFICATION_KEYS = (
    "claim_classification_1",
    "claim_classification_2",
    "claim_classification_3",
)

DOMINANCE_WARN_MIN_TRIAD_TOTAL = 5

DESIGN_SPEC_FACT_TAGS: frozenset[str] = frozenset(
    {
        "Causal",
        "Correlational",
        "Comparative",
        "Mechanistic",
        "Performance",
        "Benchmark",
        "Measurement",
        "Observational",
        "NullFinding",
        "Replication",
    }
)

DESIGN_SPEC_ASSERTION_TAGS: frozenset[str] = frozenset(
    {
        "Causal",
        "Correlational",
        "Comparative",
        "Mechanistic",
        "Performance",
        "Observational",
    }
)

BACKGROUND_RATIONALE_TAGS: frozenset[str] = frozenset(
    {
        "Background",
        "Synthesis",
        "SourceAttribution",
    }
)

ASPIRATIONAL_TAGS: frozenset[str] = frozenset(
    {
        "Hypothesis",
        "NoveltyAssertion",
        "FutureWork",
        "Roadmap",
        "Feasibility",
        "ImpactPotential",
    }
)

CONTEXTUAL_FACT_ASSERTION_TAGS: frozenset[str] = frozenset(
    {
        "Definitional",
    }
)

CONTEXTUAL_ASSERTION_ONLY_TAGS: frozenset[str] = frozenset(
    {
        "Interpretive",
        "Prescriptive",
        "Hedge",
    }
)

FIGURE_TABLE_SECTION_KEYWORDS: tuple[str, ...] = ("figure", "table")
FIGURE_TABLE_CLAIM_PREFIXES: tuple[str, ...] = ("figure", "table", "in all ")


def load_known_tags(mappings_path: Path) -> frozenset[str]:
    """All tag strings declared in mappings.json dimensions and cross_cutting."""
    data = json.loads(mappings_path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for dim in (data.get("dimensions") or {}).values():
        if isinstance(dim, dict):
            for t in dim.get("tags") or []:
                out.add(str(t))
    cc = data.get("cross_cutting")
    if isinstance(cc, dict):
        for t in cc.get("tags") or []:
            out.add(str(t))
    return frozenset(out)


def _tags_for_member(rec: dict) -> frozenset[str]:
    tags: set[str] = set()
    for key in CLASSIFICATION_KEYS:
        part = rec.get(key) or []
        if isinstance(part, list):
            for x in part:
                tags.add(str(x).strip())
    return frozenset(tags)


def _claim_classification_1_empty(rec: dict) -> bool:
    c1 = rec.get("claim_classification_1")
    return not isinstance(c1, list) or len(c1) == 0


def _relevancy_below_threshold(rec: dict, threshold: float = 0.3) -> bool:
    r = rec.get("relevancy_score")
    try:
        v = float(r)
    except (TypeError, ValueError):
        return False
    return v < threshold


def _is_figure_table_caption(rec: dict) -> bool:
    heading = str(rec.get("section_heading") or "").lower()
    if any(kw in heading for kw in FIGURE_TABLE_SECTION_KEYWORDS):
        return True
    claim_text = str(rec.get("claim") or "").lower().strip()
    return any(claim_text.startswith(prefix) for prefix in FIGURE_TABLE_CLAIM_PREFIXES)


def _quality_gate_reason(rec: dict) -> str | None:
    if _is_figure_table_caption(rec):
        return "figure_table_caption"
    return None


def assign_bucket(claim_type: str, tags: frozenset[str]) -> str | None:
    """Return bucket name or None if no bucket matches (caller sends to noise).

    Protocol-specific: no empirical or boilerplate_method buckets. All
    data-verifiable–style claims become design_specification (planned
    measurements, endpoints, hypotheses with empirical tags).
    """
    ct = str(claim_type).strip()

    # Design specification: planned empirical claims
    if ct == "Fact" and (tags & DESIGN_SPEC_FACT_TAGS):
        return "design_specification"
    if ct == "Assertion" and (tags & DESIGN_SPEC_ASSERTION_TAGS):
        return "design_specification"

    # Methodological: how the planned work will be done
    if ct in ("Fact", "Assertion"):
        if "Methodological" in tags:
            return "methodological"
        if "Measurement" in tags and "Methodological" in tags:
            return "methodological"
        if len(tags) == 1 and "Benchmark" in tags:
            return "methodological"

    # Background rationale: literature justification and gap identification
    if ct == "Roadmap" and "GapStatement" in tags:
        return "background_rationale"
    if ct in ("Fact", "Assertion") and (tags & BACKGROUND_RATIONALE_TAGS):
        return "background_rationale"

    # Aspirational: expected outcomes, future work, novelty
    if ct == "Roadmap":
        return "aspirational"
    if ct in ("Fact", "Assertion") and (tags & ASPIRATIONAL_TAGS):
        return "aspirational"

    # Contextual: definitions, framing
    if ct in ("Fact", "Assertion") and (tags & CONTEXTUAL_FACT_ASSERTION_TAGS):
        return "contextual"
    if ct == "Assertion" and (tags & CONTEXTUAL_ASSERTION_ONLY_TAGS):
        return "contextual"

    return None


def _noise_gate_reason(rec: dict) -> str | None:
    """If this record is forced to noise before bucketing, return a short reason."""
    if str(rec.get("claim_type") or "").strip() == "None":
        return "claim_type_none"
    if _claim_classification_1_empty(rec):
        return "empty_claim_classification_1"
    if _relevancy_below_threshold(rec):
        return "low_relevancy"
    return None


def triage_grouped(
    grouped: dict[str, Any],
    *,
    known_tags: frozenset[str],
    stderr: TextIO,
) -> dict[str, Any]:
    """Build triaged.json structure; emit unknown-tag and no-bucket lines to stderr."""
    warned_unknown: set[str] = set()
    out: dict[str, Any] = {}

    for dim_key, dim_val in grouped.items():
        if not isinstance(dim_val, dict):
            continue
        score = dim_val.get("score")
        members_raw = dim_val.get("members") or []
        if not isinstance(members_raw, list):
            members_raw = []

        buckets: dict[str, list[dict]] = {
            "design_specification": [],
            "methodological": [],
            "background_rationale": [],
            "contextual": [],
            "aspirational": [],
        }
        noise: list[dict] = []

        for rec in members_raw:
            if not isinstance(rec, dict):
                continue

            tags = _tags_for_member(rec)
            for t in tags:
                if t and t not in known_tags and t not in warned_unknown:
                    warned_unknown.add(t)
                    print(
                        f'triage.py: unknown tag "{t}" (not in mappings.json)',
                        file=stderr,
                    )

            gate = _noise_gate_reason(rec)
            if gate is not None:
                noise.append(dict(rec))
                continue

            quality_gate = _quality_gate_reason(rec)
            if quality_gate is not None:
                noise.append(dict(rec))
                continue

            ct = str(rec.get("claim_type") or "").strip()
            bucket = assign_bucket(ct, tags)
            if bucket is None:
                cid = rec.get("chunk_id", "?")
                print(
                    f"triage.py: no bucket for dimension={dim_key} "
                    f"claim_type={ct!r} tags={sorted(tags)} chunk_id={cid}",
                    file=stderr,
                )
                noise.append(dict(rec))
                continue

            placed = dict(rec)
            placed["triage_bucket"] = bucket
            buckets[bucket].append(placed)

        stats = {
            "total": sum(len(buckets[k]) for k in buckets) + len(noise),
            "design_specification": len(buckets["design_specification"]),
            "methodological": len(buckets["methodological"]),
            "background_rationale": len(buckets["background_rationale"]),
            "contextual": len(buckets["contextual"]),
            "aspirational": len(buckets["aspirational"]),
            "noise": len(noise),
        }

        out[dim_key] = {
            "score": score,
            "buckets": buckets,
            "noise": noise,
            "stats": stats,
        }

    return out


def _print_stats_summary(triaged: dict[str, Any], stderr: TextIO) -> None:
    totals = {
        "design_specification": 0,
        "methodological": 0,
        "background_rationale": 0,
        "contextual": 0,
        "aspirational": 0,
        "noise": 0,
        "total": 0,
    }
    for dim_key, dim_val in triaged.items():
        if not isinstance(dim_val, dict):
            continue
        st = dim_val.get("stats") or {}
        if not isinstance(st, dict):
            continue
        print(f"triage.py: {dim_key} - stats: {st}", file=stderr)
        for k in totals:
            if k in st and isinstance(st[k], int):
                totals[k] += st[k]
    print(f"triage.py: ALL - aggregate stats: {totals}", file=stderr)


def _maybe_dominance_warning(triaged: dict[str, Any], stderr: TextIO) -> None:
    design = background = aspirational = 0
    for dim_val in triaged.values():
        if not isinstance(dim_val, dict):
            continue
        st = dim_val.get("stats") or {}
        if not isinstance(st, dict):
            continue
        design += int(st.get("design_specification") or 0)
        background += int(st.get("background_rationale") or 0)
        aspirational += int(st.get("aspirational") or 0)
    triad = design + background + aspirational
    if triad >= DOMINANCE_WARN_MIN_TRIAD_TOTAL and (background + aspirational) > design:
        print(
            "triage.py: note: background_rationale+aspirational bucket counts exceed "
            "design_specification across all dimensions. For protocol documents the "
            "background_rationale bucket is often the largest — this is expected.",
            file=stderr,
        )


def main() -> None:
    default_mappings = Path(__file__).resolve().parent.parent / "mappings.json"
    p = argparse.ArgumentParser(
        description="Triage grouped.json claims into protocol review buckets (deterministic)."
    )
    p.add_argument("grouped_json", type=Path, help="Input grouped.json")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write JSON here (default: stdout)",
    )
    p.add_argument(
        "--mappings",
        type=Path,
        default=default_mappings,
        help="Path to mappings.json (known tags for warnings)",
    )
    args = p.parse_args()

    known_tags = load_known_tags(args.mappings)
    grouped = json.loads(args.grouped_json.read_text(encoding="utf-8"))
    if not isinstance(grouped, dict):
        print("triage.py: error: grouped JSON must be an object", file=sys.stderr)
        raise SystemExit(1)

    triaged = triage_grouped(grouped, known_tags=known_tags, stderr=sys.stderr)
    _print_stats_summary(triaged, sys.stderr)
    _maybe_dominance_warning(triaged, sys.stderr)

    text = json.dumps(triaged, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
