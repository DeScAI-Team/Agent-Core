"""Enrich triaged claims with citation resolution, OpenAlex metadata, and LLM evidence grading.

Protocol adaptation: disables self_reported / self_reported_method reclassification
since pre-results documents have no own-findings to exempt from citation checking.
Adds protocol-specific grades: design_precedent (cited reference demonstrates prior
use of the same design) and established_method (method is well-established in the
literature).

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
    EVIDENCE_AUDITOR_SYSTEM,
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

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")

_METHOD_HEADING_HINTS: frozenset[str] = frozenset([
    "method", "materials", "protocol", "procedure", "design",
    "statistical", "analysis plan", "sample size", "randomization",
    "blinding", "measurement", "outcome",
])

PROTOCOL_DESIGN_TAGS: frozenset[str] = frozenset({
    "Methodological", "Measurement", "Benchmark", "Performance",
})


def _effective_semantic_category(rec: dict[str, Any]) -> str:
    """Resolve semantic_category with protocol-specific heading hints."""
    sem = str(rec.get("semantic_category") or "").strip().lower()
    if sem and sem != "other":
        return sem
    heading = str(rec.get("section_heading") or "").strip().lower()
    if not heading:
        return sem or "other"
    if any(kw in heading for kw in _METHOD_HEADING_HINTS):
        return "method"
    return sem or "other"


def _tags_for_claim(rec: dict[str, Any]) -> set[str]:
    tags: set[str] = set()
    for key in CLASSIFICATION_KEYS:
        part = rec.get(key) or []
        if isinstance(part, list):
            for x in part:
                tags.add(str(x).strip())
    return tags


def _is_design_method_claim(rec: dict[str, Any]) -> bool:
    """True for claims that describe protocol design/methodology.

    In a protocol, these are NOT self-reported findings; they are planned
    methodology that should ideally be justified by literature precedent.
    """
    sem = _effective_semantic_category(rec)
    if sem == "method":
        return True
    tags = _tags_for_claim(rec)
    return bool(tags & PROTOCOL_DESIGN_TAGS)


def _reclassify_protocol_grade(rec: dict[str, Any], grade: str, summary: str) -> tuple[str, str]:
    """Protocol-specific post-LLM reclassification.

    Unlike the empirical pipeline, we do NOT reclassify unsupported/unreferenced
    to self_reported. Instead, for method-section claims with moderate/strong
    support, we upgrade to design_precedent or established_method when the
    evidence clearly supports a planned approach.
    """
    if grade in ("strong", "moderate") and _is_design_method_claim(rec):
        tags = _tags_for_claim(rec)
        if "Methodological" in tags:
            return "established_method", summary
        return "design_precedent", summary
    return grade, summary


def enrich_triaged_protocol(
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
    """Protocol-aware enrichment: no self-reported reclassification."""
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

        grade, summary = _reclassify_protocol_grade(rec, grade, summary)

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
        description="Enrich triaged.json with citations and evidence grades (protocol mode)."
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

    enriched = enrich_triaged_protocol(
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
