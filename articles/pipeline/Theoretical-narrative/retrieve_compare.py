"""Enrich triaged claims with citation resolution, OpenAlex metadata, and LLM evidence grading.

Theoretical-narrative adaptation: disables self_reported / self_reported_method
reclassification entirely -- theoretical papers have no own experimental data
to exempt from citation checking. Every substantive claim should trace to cited
literature. Adds overclaim grade for claims that draw stronger conclusions than
the cited references warrant.

Reuses all citation resolution, OpenAlex caching, and LLM evidence auditing from
the empirical retrieve_compare module.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator

_BASE = Path(__file__).resolve().parent
_EMPIRICAL = _BASE.parent / "empirical"
if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

from retrieve_compare import (  # noqa: E402
    CLASSIFICATION_KEYS,
    extract_citation_numbers,
    ensure_openalex_cache,
    load_kb_indices,
    parse_reference_index,
    reconstruct_abstract,
    _build_citation_rows,
    _citation_numbers_for_claim,
    _collect_doc_names,
    _collect_dois_from_triaged,
    _default_fullmd_for_doc,
    _default_kb_path,
    _iter_claim_dicts,
    _llm_json_call,
    _normalize_doi_key,
    _refs_with_usable_abstracts,
    _run_evidence_llm,
    _unreferenced_summary,
)

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("VALIDATOR_MODEL", "/model")


EVIDENCE_AUDITOR_SYSTEM_THEORETICAL = """You are an evidence auditor for theoretical, review, and narrative papers. Given a claim from a paper that synthesizes existing literature or argues a thesis, and the abstracts of its cited references, determine whether the cited evidence actually supports the specific argumentative claim, synthesis, or interpretation as stated.

These papers do NOT generate new experimental data. Every substantive claim should be traceable to cited literature. Pay special attention to whether the author's interpretation or synthesis goes beyond what the cited references actually demonstrate.

For EACH cited reference, output a JSON object with:
- "ref_number": int
- "support_verdict": one of "direct_support", "partial_support", "tangential", "overclaim", "not_relevant"
- "support_rationale": 1-2 sentences explaining why

"overclaim" means the paper draws a stronger or broader conclusion than the cited reference warrants — the reference is relevant but does not support the specific strength of the claim.

Then provide an overall assessment:
- "evidence_grade": one of "strong", "moderate", "weak", "unsupported", "overclaim", "unverifiable"
- "evidence_summary": 1-2 sentences summarizing the overall evidence picture for this claim

Use "overclaim" as the overall grade when the paper's thesis or synthesis goes materially beyond what the cited references demonstrate, even though the references are topically relevant.

Return ONLY valid JSON with keys: "per_reference" (array), "evidence_grade" (string), "evidence_summary" (string).
"""


def _reclassify_theoretical_grade(
    rec: dict[str, Any], grade: str, summary: str
) -> tuple[str, str]:
    """Theoretical-narrative post-LLM reclassification.

    Unlike the empirical pipeline, we NEVER reclassify to self_reported.
    Theoretical papers have no own-data to exempt. All grades pass through
    unchanged.
    """
    return grade, summary


def enrich_triaged_theoretical(
    triaged: dict[str, Any],
    *,
    chunk_index: dict[int, str],
    kb_by_doc: dict[str, list[tuple[int, str]]],
    ref_index: dict[int, dict[str, Any]],
    openalex_cache: dict[str, Any],
    client: Any,
    skip_llm: bool,
    debug_llm: bool,
    use_json_response_format: bool,
    stderr: Any,
) -> dict[str, Any]:
    """Theoretical-narrative enrichment: no self-reported reclassification."""
    max_ref = max(ref_index.keys()) if ref_index else None
    out = copy.deepcopy(triaged)
    borrowed = 0

    for _, _, rec in _iter_claim_dicts(out):
        try:
            ck = int(rec["chunk_id"]) if rec.get("chunk_id") is not None else None
        except (TypeError, ValueError):
            ck = None
        if ck is not None and ck not in chunk_index:
            print(
                f"retrieve_compare.py: warning: chunk_id={ck} not in KB",
                file=stderr,
            )

        cites, cite_sources, did_borrow = _citation_numbers_for_claim(
            rec, chunk_index, kb_by_doc, max_ref
        )
        rec["citation_numbers"] = cites
        rec["citation_source_chunk_ids"] = cite_sources
        if did_borrow:
            borrowed += 1

        if skip_llm:
            rec["citations"] = _build_citation_rows(
                cites, ref_index, openalex_cache,
                truncate_abstract=500, verdicts=None,
            )
            for c in rec["citations"]:
                c["support_verdict"] = None
                c["support_rationale"] = None
            if not cites:
                rec["evidence_grade"] = "unreferenced"
                rec["evidence_summary"] = _unreferenced_summary(rec)
            else:
                rec["evidence_grade"] = "pending"
                rec["evidence_summary"] = "LLM check skipped."
            continue

        if not cites:
            rec["citations"] = []
            rec["evidence_grade"] = "unreferenced"
            rec["evidence_summary"] = _unreferenced_summary(rec)
            continue

        usable = _refs_with_usable_abstracts(cites, ref_index, openalex_cache)
        if not usable:
            rec["citations"] = _build_citation_rows(
                cites, ref_index, openalex_cache,
                truncate_abstract=500, verdicts=None,
            )
            rec["evidence_grade"] = "unverifiable"
            rec["evidence_summary"] = (
                "All cited references could not be retrieved from OpenAlex."
            )
            continue

        if client is None:
            rec["citations"] = _build_citation_rows(
                cites, ref_index, openalex_cache,
                truncate_abstract=500, verdicts=None,
            )
            rec["evidence_grade"] = "unverifiable"
            rec["evidence_summary"] = "LLM client not configured."
            continue

        verdicts, grade, summary = _run_evidence_llm(
            client, rec, usable,
            debug_llm=debug_llm,
            use_json_response_format=use_json_response_format,
        )
        rec["citations"] = _build_citation_rows(
            cites, ref_index, openalex_cache,
            truncate_abstract=500, verdicts=verdicts,
        )

        grade, summary = _reclassify_theoretical_grade(rec, grade, summary)

        rec["evidence_grade"] = grade
        rec["evidence_summary"] = summary

    print(
        f"retrieve_compare.py: cross-chunk citation borrow used for {borrowed} claim(s)",
        file=stderr,
    )
    return out


def main() -> None:
    default_kb = _default_kb_path()
    p = argparse.ArgumentParser(
        description="Enrich triaged.json with citations and evidence grades (theoretical-narrative mode)."
    )
    p.add_argument("triaged_json", type=Path, help="Input triaged.json")
    p.add_argument(
        "--kb", type=Path, default=default_kb,
        help="text_knowledge_base.jsonl",
    )
    p.add_argument(
        "--fullmd", type=Path, default=None,
        help="full.md path",
    )
    p.add_argument(
        "--openalex-cache", type=Path, default=None,
        help="OpenAlex JSON cache path",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write JSON here (default: stdout)",
    )
    p.add_argument("--skip-llm", action="store_true")
    p.add_argument("--debug-llm", action="store_true")
    args = p.parse_args()

    triaged = json.loads(args.triaged_json.read_text(encoding="utf-8"))
    if not isinstance(triaged, dict):
        print("retrieve_compare.py: error: triaged JSON must be an object", file=sys.stderr)
        raise SystemExit(1)

    doc_names = _collect_doc_names(triaged)
    fullmd_path = args.fullmd
    if fullmd_path is None:
        if len(doc_names) == 1:
            fullmd_path = _default_fullmd_for_doc(next(iter(doc_names)))
            print(f"retrieve_compare.py: using --fullmd {fullmd_path}", file=sys.stderr)
        elif len(doc_names) == 0:
            print("retrieve_compare.py: error: no doc_name; pass --fullmd", file=sys.stderr)
            raise SystemExit(1)
        else:
            print(
                f"retrieve_compare.py: error: multiple doc_name values {sorted(doc_names)!r}; pass --fullmd",
                file=sys.stderr,
            )
            raise SystemExit(1)

    cache_path = args.openalex_cache
    if cache_path is None:
        if args.output is not None:
            cache_path = args.output.parent / "openalex_cache.json"
        else:
            cache_path = args.triaged_json.parent / "openalex_cache.json"
        print(f"retrieve_compare.py: using --openalex-cache {cache_path}", file=sys.stderr)

    full_text = fullmd_path.read_text(encoding="utf-8")
    ref_index = parse_reference_index(full_text, sys.stderr)
    chunk_index, kb_by_doc = load_kb_indices(args.kb, sys.stderr)

    debug_llm = args.debug_llm or os.environ.get(
        "RETRIEVE_COMPARE_DEBUG_LLM", ""
    ).strip().lower() in ("1", "true", "yes")
    use_json_response_format = os.environ.get(
        "RETRIEVE_COMPARE_JSON_RESPONSE_FORMAT", ""
    ).strip().lower() in ("1", "true", "yes")

    dois_needed = _collect_dois_from_triaged(
        triaged, chunk_index, kb_by_doc, ref_index
    )
    from retrieve_compare import OPENALEX_EMAIL  # noqa: E402
    openalex_cache = ensure_openalex_cache(
        dois_needed, cache_path, OPENALEX_EMAIL, sys.stderr
    )

    client: Any = None
    if not args.skip_llm:
        from openai import OpenAI as _OpenAI
        client = _OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    enriched = enrich_triaged_theoretical(
        triaged,
        chunk_index=chunk_index,
        kb_by_doc=kb_by_doc,
        ref_index=ref_index,
        openalex_cache=openalex_cache,
        client=client,
        skip_llm=args.skip_llm,
        debug_llm=debug_llm,
        use_json_response_format=use_json_response_format,
        stderr=sys.stderr,
    )

    text = json.dumps(enriched, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
