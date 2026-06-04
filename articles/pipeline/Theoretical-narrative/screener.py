"""Sliding-window document screener for theoretical/narrative papers.

Reads the full paper (full.md) through overlapping windows and screens each
window against every mapping dimension and cross-cutting tag. Findings are
deduplicated, grouped by dimension, and an LLM writes rationales for each
dimension that surfaced relevant observations. Finally, review.json is
patched with new or enriched categories.

Reuses all infrastructure from the empirical screener module; only the
prompts are swapped to focus on argumentation quality, cherry-picking,
logical coherence, and balanced representation.

Stages:
  1. window_builder     — split full.md into overlapping ~2500-token windows
  2. window_screener    — LLM screens each window for argumentation signals
  3. dedup_aggregate    — deduplicate findings, group by dimension
  4. category_writer    — LLM writes a rationale per dimension with findings
  5. patch_review       — merge screener results into review.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from openai import OpenAI

_BASE = Path(__file__).resolve().parent
PIPELINE_DIR = _BASE.parent
PROMPTS_DIR = _BASE / "prompts"
MAPPINGS_PATH = PIPELINE_DIR / "mappings.json"

_EMPIRICAL = _BASE.parent / "empirical"
if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

from screener import (  # noqa: E402
    window_builder,
    window_screener,
    dedup_aggregate,
    category_writer,
    patch_review,
    parse_reference_index,
    _estimate_tokens,
)

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sliding-window document screener for theoretical/narrative papers."
    )
    parser.add_argument("--fullmd", required=True, type=Path)
    parser.add_argument("--openalex-cache", type=Path, default=None)
    parser.add_argument("--mappings", type=Path, default=MAPPINGS_PATH)
    parser.add_argument("--review", type=Path, default=None)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()

    stderr = sys.stderr
    fullmd_path = args.fullmd.expanduser().resolve()
    mappings_path = args.mappings.expanduser().resolve()

    print("Loading inputs ...", file=stderr)
    full_md_text = fullmd_path.read_text(encoding="utf-8")
    mappings = json.loads(mappings_path.read_text(encoding="utf-8"))

    openalex_cache: dict[str, Any] = {}
    if args.openalex_cache:
        cache_path = args.openalex_cache.expanduser().resolve()
        if cache_path.exists():
            openalex_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"  Loaded OpenAlex cache ({len(openalex_cache)} entries)", file=stderr)

    review: dict[str, Any] = {}
    review_path: Path | None = None
    if args.review:
        review_path = args.review.expanduser().resolve()
        if review_path.exists():
            review = json.loads(review_path.read_text(encoding="utf-8"))
            cats = review.get("categories", {})
            print(f"  Loaded review.json ({len(cats)} existing categories)", file=stderr)

    # Stage 1
    print("\n=== Stage 1: window_builder ===", file=stderr)
    ref_index = parse_reference_index(full_md_text, stderr)
    windows = window_builder(full_md_text, ref_index, openalex_cache, stderr)

    if args.skip_llm:
        print("  --skip-llm: stopping after window builder", file=stderr)
        output = {
            "doc_name": review.get("research_name", ""),
            "check_date": date.today().strftime("%B %d, %Y"),
            "windows_count": len(windows),
            "windows": [
                {
                    "window_idx": w["window_idx"],
                    "token_estimate": w["token_estimate"],
                    "citation_numbers": w["citation_numbers"],
                    "cited_abstracts_count": len(w["cited_abstracts"]),
                }
                for w in windows
            ],
            "findings": [],
            "grouped_findings": {},
            "writer_results": {},
        }
        text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            out_path = args.output.expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"\nScreener debug output written to {out_path}", file=stderr)
        else:
            print(text, end="")
        return

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    # Stage 2
    print("\n=== Stage 2: window_screener ===", file=stderr)
    screener_prompt = _load_prompt("screener_system_prompt.md")
    findings = window_screener(
        windows, mappings, review, screener_prompt, client, stderr,
    )
    print(f"  Total raw findings: {len(findings)}", file=stderr)

    # Stage 3
    print("\n=== Stage 3: dedup_aggregate ===", file=stderr)
    grouped = dedup_aggregate(findings, stderr)

    writer_results: dict[str, dict[str, Any]] = {}

    if not grouped:
        print("  No findings to process. Skipping stages 4-5.", file=stderr)
    else:
        # Stage 4
        print("\n=== Stage 4: category_writer ===", file=stderr)
        writer_prompt = _load_prompt("screener_category_writer_prompt.md")
        writer_results = category_writer(
            grouped, mappings, review, writer_prompt, client, stderr,
        )

        # Stage 5
        if writer_results and review_path:
            print("\n=== Stage 5: patch_review ===", file=stderr)
            patch_review(review_path, writer_results, stderr)
        elif writer_results and not review_path:
            print(
                "\n  No --review path provided; skipping review.json patch.",
                file=stderr,
            )

    # Write diagnostic output
    output = {
        "doc_name": review.get("research_name", ""),
        "check_date": date.today().strftime("%B %d, %Y"),
        "windows_count": len(windows),
        "total_findings_raw": len(findings),
        "total_findings_deduped": sum(len(v) for v in grouped.values()),
        "dimensions_with_findings": sorted(grouped.keys()),
        "findings_by_dimension": {
            dim: items for dim, items in sorted(grouped.items())
        },
        "writer_results": {
            dim: {"score": r["score"], "rationale": r["rationale"]}
            for dim, r in writer_results.items()
        },
    }

    text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        out_path = args.output.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"\nScreener output written to {out_path}", file=stderr)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
