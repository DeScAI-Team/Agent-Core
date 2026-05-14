"""Sliding-window document screener for protocol/pre-registration documents.

Thin wrapper around empirical/screener.py that loads protocol-specific
screening prompts from the local prompts/ directory. The core window
building, deduplication, and review patching logic is reused entirely.
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

_EMPIRICAL = PIPELINE_DIR / "empirical"
if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

from screener import (  # noqa: E402
    window_builder,
    window_screener,
    dedup_aggregate,
    category_writer,
    patch_review,
    parse_reference_index,
    EXCLUDED_DIMENSIONS,
    MAX_REVIEW_CATEGORIES,
)

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("VALIDATOR_MODEL", "/model")


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sliding-window screener for protocol/pre-registration documents."
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

    print("\n=== Stage 1: window_builder ===", file=stderr)
    ref_index = parse_reference_index(full_md_text, stderr)
    windows = window_builder(full_md_text, ref_index, openalex_cache, stderr)

    if args.skip_llm:
        print("  --skip-llm: stopping after window builder", file=stderr)
        output = {
            "doc_name": review.get("research_name", ""),
            "check_date": date.today().strftime("%B %d, %Y"),
            "windows_count": len(windows),
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

    print("\n=== Stage 2: window_screener ===", file=stderr)
    screener_prompt = _load_prompt("screener_system_prompt.md")
    findings = window_screener(
        windows, mappings, review, screener_prompt, client, stderr,
    )
    print(f"  Total raw findings: {len(findings)}", file=stderr)

    print("\n=== Stage 3: dedup_aggregate ===", file=stderr)
    grouped = dedup_aggregate(findings, stderr)

    writer_results: dict[str, dict[str, Any]] = {}

    if not grouped:
        print("  No findings to process. Skipping stages 4-5.", file=stderr)
    else:
        print("\n=== Stage 4: category_writer ===", file=stderr)
        writer_prompt = _load_prompt("screener_category_writer_prompt.md")
        writer_results = category_writer(
            grouped, mappings, review, writer_prompt, client, stderr,
        )

        if writer_results and review_path:
            print("\n=== Stage 5: patch_review ===", file=stderr)
            patch_review(review_path, writer_results, stderr)
        elif writer_results and not review_path:
            print("\n  No --review path provided; skipping patch.", file=stderr)

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
