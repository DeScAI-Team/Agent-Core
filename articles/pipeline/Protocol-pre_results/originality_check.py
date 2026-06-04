"""Originality check for a study protocol / pre-registration.

Thin wrapper around empirical/originality_check.py that loads prompts from
the local Protocol-pre_results/prompts/ directory instead of the empirical
prompts. All core logic (abstract extraction, term generation, OpenAlex
search, similarity scoring, statement writing) is reused.
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
PROMPTS_DIR = _BASE / "prompts"

_EMPIRICAL = _BASE.parent / "empirical"
if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

from originality_check import (  # noqa: E402
    extract_paper_abstract,
    load_kb_chunks,
    generate_search_terms,
    fetch_related_works,
    score_related_works,
    write_originality_statement,
    patch_review_json,
    _find_fullmd,
    _find_kb,
    VLLM_BASE_URL as _VLLM_BASE_URL,
    VLLM_API_KEY as _VLLM_API_KEY,
    OPENALEX_EMAIL,
)

VLLM_BASE_URL = _VLLM_BASE_URL
VLLM_API_KEY = _VLLM_API_KEY


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Originality check for protocol/pre-registration documents."
    )
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--fullmd", type=Path, default=None)
    parser.add_argument("--kb", type=Path, default=None)
    parser.add_argument("--openalex-cache", type=Path, default=None)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--terms-per-chunk", type=int, default=1)
    parser.add_argument("--max-results-per-term", type=int, default=5)
    parser.add_argument("--chunk-batch-size", type=int, default=4)
    parser.add_argument("--review", type=Path, default=None)
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()

    directory = args.directory.expanduser().resolve()
    if not directory.exists():
        print(f"originality_check.py: directory not found: {directory}", file=sys.stderr)
        sys.exit(1)

    kb_path = (args.kb or _find_kb(directory) or directory / "text_knowledge_base.jsonl").expanduser().resolve()
    fullmd_path = (args.fullmd or _find_fullmd(directory))
    if fullmd_path:
        fullmd_path = fullmd_path.expanduser().resolve()

    cache_path = (args.openalex_cache or directory / "originality_openalex_cache.json").expanduser().resolve()
    out_path = (args.output or directory / "originality.json").expanduser().resolve()

    stderr = sys.stderr
    client: OpenAI | None = None

    print("\n=== Stage 1: abstract_extractor ===", file=stderr)
    paper_abstract, doc_name = extract_paper_abstract(kb_path, fullmd_path)
    if not paper_abstract:
        print("originality_check.py: WARNING — could not extract abstract.", file=stderr)
    else:
        print(f"  Extracted abstract ({len(paper_abstract)} chars) from doc: {doc_name!r}", file=stderr)

    print("\n=== Stage 2: term_generator ===", file=stderr)
    chunks = load_kb_chunks(kb_path)
    print(f"  Loaded {len(chunks)} chunks from {kb_path}", file=stderr)

    if args.skip_llm:
        print("  --skip-llm: skipping term generation", file=stderr)
        search_terms: list[str] = []
    else:
        search_term_prompt = _load_prompt("search_term_prompt.md")
        if client is None:
            client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        search_terms = generate_search_terms(
            chunks, client, search_term_prompt,
            terms_per_chunk=args.terms_per_chunk,
            batch_size=args.chunk_batch_size,
        )
        print(f"  Generated {len(search_terms)} unique search terms", file=stderr)

    print("\n=== Stage 3: openalex_searcher ===", file=stderr)
    if search_terms:
        related_works, _ = fetch_related_works(
            search_terms, cache_path, OPENALEX_EMAIL,
            args.max_results_per_term, stderr,
        )
        print(f"  Retrieved {len(related_works)} unique related works", file=stderr)
    else:
        related_works: list[dict[str, Any]] = []

    print("\n=== Stage 4a: similarity_scorer ===", file=stderr)
    avg_similarity: float = 0.0
    originality_score: float = 1.0

    if args.skip_llm or not related_works or not paper_abstract:
        if args.skip_llm:
            print("  --skip-llm: skipping similarity scoring", file=stderr)
    else:
        if client is None:
            client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        similarity_prompt = _load_prompt("similarity_scorer_prompt.md")
        related_works, avg_similarity, originality_score = score_related_works(
            paper_abstract, related_works, client, similarity_prompt,
        )

    print("\n=== Stage 4b: originality_writer ===", file=stderr)
    if args.skip_llm or not paper_abstract:
        originality_statement = ""
    else:
        if client is None:
            client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        originality_prompt = _load_prompt("originality_statement_prompt.md")
        originality_statement = write_originality_statement(
            paper_abstract, related_works, originality_score,
            client, originality_prompt,
        )

    output = {
        "doc_name": doc_name,
        "check_date": date.today().strftime("%B %d, %Y"),
        "paper_abstract": paper_abstract,
        "search_terms": search_terms,
        "related_works_count": len(related_works),
        "avg_similarity_score": avg_similarity,
        "originality_score": originality_score,
        "related_works": related_works,
        "originality_statement": originality_statement,
    }

    text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"\nOriginality check written to {out_path}", file=stderr)

    if not args.skip_llm and originality_statement:
        review_path = (args.review or directory / "review.json").expanduser().resolve()
        print("\n=== Patching review.json ===", file=stderr)
        patch_review_json(review_path, originality_score, originality_statement, stderr)


if __name__ == "__main__":
    main()
