"""Unified scoring step for the empirical review pipeline.

Runs AFTER all other pipeline steps (review.py, originality_check.py,
screener.py) and recomputes every category score in review.json using a
consistent, auditable methodology:

  - Claim-level dimensions  → evidence-grade weighted formula (from prep.py)
  - Originality             → pass-through from originality.json
  - Screener-only dims      → deterministic rubric-penalty formula

Replaces the old ``average_score`` with a weighted ``composite_score`` and
regenerates the review statement so it reflects the final scoring state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

_BASE = Path(__file__).resolve().parent
PIPELINE_DIR = _BASE.parent
PROMPTS_DIR = _BASE / "prompts"

sys.path.insert(0, str(_BASE))
from prep import compute_evidence_score, SCORE_EXCLUDED_GRADES  # noqa: E402

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[2]
_PIPELINE = _Path(__file__).resolve().parents[1]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
from publish_review import publish_categories, publish_composite  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")

MAX_RETRIES = 4
STATEMENT_MAX_TOKENS = 4096

KEEP_BUCKETS = frozenset({"empirical", "methodological", "contextual", "aspirational"})

EXCLUDED_DIMENSIONS = frozenset({"governance_accountability", "cross_cutting"})


# ---------------------------------------------------------------------------
# LLM helpers (mirrors review.py)
# ---------------------------------------------------------------------------

def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _llm_call(
    client: OpenAI,
    system_prompt: str,
    user_content: str,
    max_tokens: int = STATEMENT_MAX_TOKENS,
) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            raw = response.choices[0].message.content
            if raw is None:
                raise ValueError("LLM returned None content")
            text = raw.strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()

            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length" or (text and text[-1] not in ".!?"):
                last_period = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
                if last_period > 0:
                    text = text[: last_period + 1].strip()

            return text
        except Exception as exc:
            err_str = str(exc).lower()
            if "context length" in err_str or "maximum context" in err_str:
                raise
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}")
            if attempt == MAX_RETRIES:
                raise
    return ""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Evidence-grade scoring (scientific_rigor, evidential_strength, …)
# ---------------------------------------------------------------------------

def _iter_prepped_claims(dimension: dict) -> list[dict]:
    """Collect all non-noise-bucket claims from a prepped_evidence dimension."""
    claims: list[dict] = []
    for bname, blist in dimension.get("buckets", {}).items():
        if bname not in KEEP_BUCKETS:
            continue
        if isinstance(blist, list):
            claims.extend(blist)
    return claims


def score_evidence_dimensions(prepped: dict) -> dict[str, dict[str, Any]]:
    """Recompute scores for dimensions present in prepped_evidence.json."""
    results: dict[str, dict[str, Any]] = {}

    for dim_key, dim_data in prepped.items():
        if dim_key in EXCLUDED_DIMENSIONS:
            continue
        if not isinstance(dim_data, dict) or "buckets" not in dim_data:
            continue
        claims = _iter_prepped_claims(dim_data)
        score = compute_evidence_score(claims)
        results[dim_key] = {
            "score": round(score, 4),
            "score_method": "evidence_grade_weighted",
            "claim_count": len(claims),
        }

    return results


# ---------------------------------------------------------------------------
# Originality pass-through
# ---------------------------------------------------------------------------

def score_originality(originality: dict) -> dict[str, Any]:
    """Read the pre-computed originality score — no recomputation."""
    return {
        "score": round(float(originality.get("originality_score", 0.5)), 4),
        "score_method": "literature_similarity",
        "compared_works": int(originality.get("related_works_count", 0)),
    }


# ---------------------------------------------------------------------------
# Rubric-penalty scoring (screener-only dimensions)
# ---------------------------------------------------------------------------

def score_rubric_dimension(
    findings: list[dict[str, Any]],
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """Apply deterministic penalty formula to a single dimension's findings.

    score = max(floor, baseline + sum(penalties[finding.severity]))
    """
    baseline = float(rubric.get("baseline", 0.7))
    penalties = rubric.get("penalties", {})
    floor = float(rubric.get("floor", 0.1))

    total_penalty = sum(
        float(penalties.get(f.get("severity", "info"), 0.0))
        for f in findings
    )

    score = max(floor, baseline + total_penalty)
    return {
        "score": round(score, 4),
        "score_method": "rubric_penalty",
        "finding_count": len(findings),
    }


def score_rubric_dimensions(
    screener: dict,
    rubrics: dict,
    evidence_dims: set[str],
) -> dict[str, dict[str, Any]]:
    """Score every screener dimension that is NOT already evidence-scored."""
    findings_by_dim = screener.get("findings_by_dimension", {})
    results: dict[str, dict[str, Any]] = {}

    for dim_key, findings in findings_by_dim.items():
        if dim_key in evidence_dims or dim_key == "originality":
            continue
        rubric = rubrics.get(dim_key)
        if rubric is None:
            rubric = {
                "baseline": 0.7,
                "penalties": {"red_flag": -0.15, "concern": -0.08, "info": 0.0},
                "floor": 0.1,
            }
        results[dim_key] = score_rubric_dimension(findings, rubric)

    return results


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def compute_composite(
    category_scores: dict[str, dict[str, Any]],
    dimension_weights: dict[str, float],
) -> float:
    """Weighted average, renormalized to the dimensions actually present."""
    present = {
        k: dimension_weights.get(k, 0.0)
        for k in category_scores
        if dimension_weights.get(k, 0.0) > 0
    }
    total_weight = sum(present.values())
    if total_weight == 0:
        scores = [v["score"] for v in category_scores.values()]
        return round(sum(scores) / len(scores), 4) if scores else 0.0

    weighted_sum = sum(
        category_scores[k]["score"] * w
        for k, w in present.items()
    )
    return round(weighted_sum / total_weight, 4)


# ---------------------------------------------------------------------------
# Review statement regeneration
# ---------------------------------------------------------------------------

def regenerate_review_statement(
    review_obj: dict,
    statement_prompt: str,
    client: OpenAI,
) -> str:
    context = json.dumps(
        {
            "research_name": review_obj.get("research_name", ""),
            "composite_score": review_obj.get("composite_score", 0),
            "categories": {
                k: {"score": v["score"], "rationale": v.get("rationale", "")}
                for k, v in review_obj.get("categories", {}).items()
            },
        },
        indent=2,
    )
    print("  Generating top-level review statement ...")
    return _llm_call(client, statement_prompt, context)


OVERVIEW_MAX_TOKENS = 512


# ---------------------------------------------------------------------------
# Overview generation (simplified, layperson-readable review)
# ---------------------------------------------------------------------------

def generate_overview(
    review_obj: dict[str, Any],
    overview_prompt: str,
    client: OpenAI,
) -> dict[str, Any]:
    """Build a layperson-readable copy of the review with simplified rationales."""
    overview_categories: dict[str, Any] = {}

    for dim_key, cat_data in review_obj.get("categories", {}).items():
        rationale = cat_data.get("rationale", "")
        if not rationale:
            overview_categories[dim_key] = dict(cat_data)
            continue

        dim_label = dim_key.replace("_", " ").title()
        user_content = (
            f"Dimension: {dim_label}\n\n"
            f"Technical rationale:\n{rationale}"
        )

        print(f"  [{dim_label}] simplifying rationale ...")
        simplified = _llm_call(
            client, overview_prompt, user_content,
            max_tokens=OVERVIEW_MAX_TOKENS,
        )

        entry = dict(cat_data)
        entry["rationale"] = simplified
        overview_categories[dim_key] = entry

    overview_statement = review_obj.get("review_statement", "")
    if overview_statement:
        print("  [Review Statement] simplifying ...")
        overview_statement = _llm_call(
            client, overview_prompt,
            f"Dimension: Overall Review Statement\n\n"
            f"Technical rationale:\n{overview_statement}",
            max_tokens=OVERVIEW_MAX_TOKENS,
        )

    return {
        "research_name": review_obj.get("research_name", ""),
        "review_date": review_obj.get("review_date", ""),
        "composite_score": review_obj.get("composite_score", 0),
        "review_statement": overview_statement,
        "categories": overview_categories,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified scoring: recompute all category scores, composite "
        "score, and regenerate the review statement."
    )
    parser.add_argument(
        "--review", type=Path, default=Path("review.json"),
        help="Path to review.json (default: review.json)",
    )
    parser.add_argument(
        "--prepped-evidence", type=Path, default=Path("prepped_evidence.json"),
        help="Path to prepped_evidence.json (default: prepped_evidence.json)",
    )
    parser.add_argument(
        "--originality", type=Path, default=Path("originality.json"),
        help="Path to originality.json (default: originality.json)",
    )
    parser.add_argument(
        "--screener", type=Path, default=Path("screener.json"),
        help="Path to screener.json (default: screener.json)",
    )
    parser.add_argument(
        "--mappings", type=Path,
        default=PIPELINE_DIR / "mappings.json",
        help="Path to mappings.json",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output path for final review.json (default: overwrite --review)",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip review statement regeneration (keep existing or empty)",
    )
    args = parser.parse_args()

    review_path = args.review.expanduser().resolve()
    prepped_path = args.prepped_evidence.expanduser().resolve()
    originality_path = args.originality.expanduser().resolve()
    screener_path = args.screener.expanduser().resolve()
    mappings_path = args.mappings.expanduser().resolve()
    out_path = (args.output or args.review).expanduser().resolve()

    stderr = sys.stderr

    # ------------------------------------------------------------------
    # Load inputs
    # ------------------------------------------------------------------
    print("Loading inputs ...", file=stderr)
    review = _load_json(review_path)
    prepped = _load_json(prepped_path)
    mappings = _load_json(mappings_path)

    originality: dict[str, Any] = {}
    if originality_path.exists():
        originality = _load_json(originality_path)
    else:
        print(f"  originality.json not found at {originality_path} — skipping", file=stderr)

    screener: dict[str, Any] = {}
    if screener_path.exists():
        screener = _load_json(screener_path)
    else:
        print(f"  screener.json not found at {screener_path} — skipping", file=stderr)

    rubrics = mappings.get("rubrics", {})
    dimension_weights = mappings.get("dimension_weights", {})
    categories = review.get("categories", {})

    # ------------------------------------------------------------------
    # Stage 1 — Evidence-grade scoring
    # ------------------------------------------------------------------
    print("\n=== Stage 1: score evidence dimensions ===", file=stderr)
    evidence_scores = score_evidence_dimensions(prepped)
    for dim_key, info in evidence_scores.items():
        print(
            f"  {dim_key}: score={info['score']}  "
            f"({info['claim_count']} claims)",
            file=stderr,
        )

    # ------------------------------------------------------------------
    # Stage 2 — Originality pass-through
    # ------------------------------------------------------------------
    print("\n=== Stage 2: score originality ===", file=stderr)
    orig_score: dict[str, Any] | None = None
    if originality:
        orig_score = score_originality(originality)
        print(
            f"  originality: score={orig_score['score']}  "
            f"({orig_score['compared_works']} compared works)",
            file=stderr,
        )
    else:
        print("  No originality data — skipping", file=stderr)

    # ------------------------------------------------------------------
    # Stage 3 — Rubric-penalty scoring
    # ------------------------------------------------------------------
    print("\n=== Stage 3: score rubric dimensions ===", file=stderr)
    evidence_dim_keys = set(evidence_scores.keys())
    rubric_scores = score_rubric_dimensions(screener, rubrics, evidence_dim_keys)
    for dim_key, info in rubric_scores.items():
        print(
            f"  {dim_key}: score={info['score']}  "
            f"({info['finding_count']} findings)",
            file=stderr,
        )
    if not rubric_scores:
        print("  No screener-only dimensions to score", file=stderr)

    # ------------------------------------------------------------------
    # Stage 4 — Merge scores into review categories
    # ------------------------------------------------------------------
    print("\n=== Stage 4: merge scores ===", file=stderr)

    all_scores: dict[str, dict[str, Any]] = {}

    for dim_key, info in evidence_scores.items():
        cat = categories.get(dim_key, {})
        all_scores[dim_key] = {
            "score": info["score"],
            "score_method": info["score_method"],
            "claim_count": info["claim_count"],
            "rationale": cat.get("rationale", ""),
        }

    if orig_score and "originality" in categories:
        all_scores["originality"] = {
            "score": orig_score["score"],
            "score_method": orig_score["score_method"],
            "compared_works": orig_score["compared_works"],
            "rationale": categories["originality"].get("rationale", ""),
        }

    for dim_key, info in rubric_scores.items():
        cat = categories.get(dim_key, {})
        all_scores[dim_key] = {
            "score": info["score"],
            "score_method": info["score_method"],
            "finding_count": info["finding_count"],
            "rationale": cat.get("rationale", ""),
        }

    for dim_key in categories:
        if dim_key in EXCLUDED_DIMENSIONS:
            continue
        if dim_key not in all_scores:
            cat = categories[dim_key]
            if isinstance(cat, dict) and cat.get("rationale"):
                all_scores[dim_key] = {
                    "score": cat.get("score", 0.5),
                    "rationale": cat.get("rationale", ""),
                }

    print(f"  {len(all_scores)} categories merged (pre-publish)", file=stderr)

    # ------------------------------------------------------------------
    # Stage 5 — Composite score (published scale)
    # ------------------------------------------------------------------
    print("\n=== Stage 5: composite score ===", file=stderr)
    published_categories = publish_categories(all_scores)
    composite = publish_composite(all_scores, dimension_weights, compute_composite)
    print(
        f"  composite_score = {composite}  "
        f"({len(published_categories)} published categories)",
        file=stderr,
    )

    # ------------------------------------------------------------------
    # Build final review object
    # ------------------------------------------------------------------
    review_obj: dict[str, Any] = {
        "research_name": review.get("research_name", ""),
        "review_date": review.get("review_date", ""),
        "composite_score": composite,
        "review_statement": review.get("review_statement", ""),
        "categories": published_categories,
    }

    # ------------------------------------------------------------------
    # LLM client (shared by Stage 6 and Stage 7)
    # ------------------------------------------------------------------
    client: OpenAI | None = None
    if not args.skip_llm:
        client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    # ------------------------------------------------------------------
    # Stage 6 — Regenerate review statement
    # ------------------------------------------------------------------
    print("\n=== Stage 6: review statement ===", file=stderr)
    if args.skip_llm:
        print("  --skip-llm: keeping existing review statement", file=stderr)
    else:
        statement_prompt = _load_prompt("review_statement_prompt.md")
        review_obj["review_statement"] = regenerate_review_statement(
            review_obj, statement_prompt, client,
        )

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------
    text = json.dumps(review_obj, indent=2, ensure_ascii=False) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"\nFinal review written to {out_path}", file=stderr)

    # ------------------------------------------------------------------
    # Stage 7 — Generate simplified overview
    # ------------------------------------------------------------------
    print("\n=== Stage 7: overview generation ===", file=stderr)
    if args.skip_llm:
        print("  --skip-llm: skipping overview generation", file=stderr)
    else:
        overview_prompt = _load_prompt("overview_rationale_prompt.md")
        overview_obj = generate_overview(review_obj, overview_prompt, client)
        overview_path = out_path.parent / "overview.json"
        overview_text = json.dumps(overview_obj, indent=2, ensure_ascii=False) + "\n"
        overview_path.write_text(overview_text, encoding="utf-8")
        print(f"\nOverview written to {overview_path}", file=stderr)


if __name__ == "__main__":
    main()
