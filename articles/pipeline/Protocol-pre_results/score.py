"""Unified scoring step for the protocol review pipeline.

Protocol adaptation of empirical/score.py:
- Imports compute_evidence_score and SCORE_EXCLUDED_GRADES from the local prep.py
- Uses protocol-specific KEEP_BUCKETS
- Loads prompts from the local prompts/ directory
- Same scoring framework: evidence-grade weighted, rubric-penalty, composite
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

if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))
from prep import compute_evidence_score, SCORE_EXCLUDED_GRADES, KEEP_BUCKETS  # noqa: E402

_EMPIRICAL = PIPELINE_DIR / "empirical"
if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

from score import (  # noqa: E402
    score_originality,
    score_rubric_dimension,
    score_rubric_dimensions,
    compute_composite,
    regenerate_review_statement,
    generate_overview,
    EXCLUDED_DIMENSIONS,
    MAX_RETRIES,
    STATEMENT_MAX_TOKENS,
)

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


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified scoring for protocol review pipeline."
    )
    parser.add_argument("--review", type=Path, default=Path("review.json"))
    parser.add_argument("--prepped-evidence", type=Path, default=Path("prepped_evidence.json"))
    parser.add_argument("--originality", type=Path, default=Path("originality.json"))
    parser.add_argument("--screener", type=Path, default=Path("screener.json"))
    parser.add_argument("--mappings", type=Path, default=PIPELINE_DIR / "mappings.json")
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()

    review_path = args.review.expanduser().resolve()
    prepped_path = args.prepped_evidence.expanduser().resolve()
    originality_path = args.originality.expanduser().resolve()
    screener_path = args.screener.expanduser().resolve()
    mappings_path = args.mappings.expanduser().resolve()
    out_path = (args.output or args.review).expanduser().resolve()

    stderr = sys.stderr

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

    print("\n=== Stage 1: score evidence dimensions ===", file=stderr)
    evidence_scores = score_evidence_dimensions(prepped)
    for dim_key, info in evidence_scores.items():
        print(f"  {dim_key}: score={info['score']} ({info['claim_count']} claims)", file=stderr)

    print("\n=== Stage 2: score originality ===", file=stderr)
    orig_score: dict[str, Any] | None = None
    if originality:
        orig_score = score_originality(originality)
        print(
            f"  originality: score={orig_score['score']} ({orig_score['compared_works']} works)",
            file=stderr,
        )
    else:
        print("  No originality data — skipping", file=stderr)

    print("\n=== Stage 3: score rubric dimensions ===", file=stderr)
    evidence_dim_keys = set(evidence_scores.keys())
    rubric_scores = score_rubric_dimensions(screener, rubrics, evidence_dim_keys)
    for dim_key, info in rubric_scores.items():
        print(f"  {dim_key}: score={info['score']} ({info['finding_count']} findings)", file=stderr)
    if not rubric_scores:
        print("  No screener-only dimensions to score", file=stderr)

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

    print("\n=== Stage 5: composite score ===", file=stderr)
    published_categories = publish_categories(all_scores)
    composite = publish_composite(all_scores, dimension_weights, compute_composite)
    print(
        f"  composite_score = {composite}  "
        f"({len(published_categories)} published categories)",
        file=stderr,
    )

    review_obj: dict[str, Any] = {
        "research_name": review.get("research_name", ""),
        "review_date": review.get("review_date", ""),
        "composite_score": composite,
        "review_statement": review.get("review_statement", ""),
        "categories": published_categories,
    }

    client: OpenAI | None = None
    if not args.skip_llm:
        client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    print("\n=== Stage 6: review statement ===", file=stderr)
    if args.skip_llm:
        print("  --skip-llm: keeping existing review statement", file=stderr)
    else:
        statement_prompt = _load_prompt("review_statement_prompt.md")
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
        review_obj["review_statement"] = _llm_call(client, statement_prompt, context)

    text = json.dumps(review_obj, indent=2, ensure_ascii=False) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"\nFinal review written to {out_path}", file=stderr)

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
