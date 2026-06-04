#!/usr/bin/env python3
"""Extract reviewable lines from each chunk and tag them.

Two LLM calls per chunk (both with thinking disabled):

  1. Extract — picky boilerplate prompt selected by source_kind. Returns up to
     MAX_LINES_PER_CHUNK lines classified as claim|feature|mission|fact.
  2. Tag — single tagger prompt that assigns category + subgroup +
     needs_scientific_support + polarity_unknown to each extracted line.

Reads:  steps/chunks.jsonl and ipnft_dir/profile.json (for metadata header)
Writes: steps/extracted.jsonl with one row per extracted line
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from llm_client import (  # noqa: E402
    call,
    discover_model,
    load_prompt,
    make_client,
    parse_json_object,
)

MAX_LINES_PER_CHUNK = 6
EXTRACT_PROMPT_BY_KIND = {
    "pdf":             "dao-extract-pdf.md",
    "crawl_md":        "dao-extract-crawl.md",
    "text_doc":        "dao-extract-crawl.md",
    "image_caption":   "dao-extract-image.md",
    "video_transcript": "dao-extract-video.md",
    "video_frame":     "dao-extract-video.md",
    "onchain_fact":    "dao-extract-onchain.md",
}


def _load_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _profile_metadata(ipnft_dir: Path) -> dict[str, str]:
    profile_path = ipnft_dir / "profile.json"
    if not profile_path.exists():
        profile_path = ipnft_dir / "metadata" / "profile.json"
    if not profile_path.exists():
        return {
            "ipnft_symbol": "",
            "ipnft_name": "",
            "organization": "",
            "research_lead": "",
            "topic": "",
        }
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    ipnft = profile.get("ipnft", {}) or {}
    lead = ipnft.get("researchLead", {}) or {}
    return {
        "ipnft_symbol": str(profile.get("symbol") or ipnft.get("initialSymbol") or ""),
        "ipnft_name": str(ipnft.get("name") or ""),
        "organization": str(ipnft.get("organization") or ""),
        "research_lead": str(lead.get("name") or "Anonymous"),
        "topic": str(ipnft.get("topic") or ""),
    }


def _format_extract_system(template: str, meta: dict[str, str], chunk: dict[str, Any]) -> str:
    return template.format(
        ipnft_symbol=meta["ipnft_symbol"],
        ipnft_name=meta["ipnft_name"],
        organization=meta["organization"],
        research_lead=meta["research_lead"],
        topic=meta["topic"],
        doc_title=chunk.get("doc_title", ""),
        domain=chunk.get("domain", ""),
        section=chunk.get("section", ""),
        page=chunk.get("page") if chunk.get("page") is not None else "n/a",
        max_lines=MAX_LINES_PER_CHUNK,
    )


def _coerce_line_objects(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        for key in ("lines", "items", "extracted", "results"):
            v = parsed.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _extract_lines(
    client,
    *,
    model: str,
    chunk: dict[str, Any],
    meta: dict[str, str],
    prompt_cache: dict[str, str],
) -> list[dict[str, Any]]:
    prompt_name = EXTRACT_PROMPT_BY_KIND.get(chunk["source_kind"], "dao-extract-crawl.md")
    template = prompt_cache.setdefault(prompt_name, load_prompt(prompt_name))
    system = _format_extract_system(template, meta, chunk)
    user = json.dumps(
        {
            "chunk_id": chunk["chunk_id"],
            "source_kind": chunk["source_kind"],
            "doc_title": chunk.get("doc_title"),
            "domain": chunk.get("domain"),
            "section": chunk.get("section"),
            "page": chunk.get("page"),
            "text": chunk["text"],
        },
        ensure_ascii=False,
    )
    raw = call(client, model=model, system=system, user=user, max_tokens=1500)
    parsed = parse_json_object(raw)
    lines = _coerce_line_objects(parsed)
    cleaned: list[dict[str, Any]] = []
    for item in lines[:MAX_LINES_PER_CHUNK]:
        line_type = str(item.get("line_type", "")).strip().lower()
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if line_type not in {"claim", "feature", "mission", "fact"}:
            line_type = "feature"
        cleaned.append({
            "line_type": line_type,
            "text": text[:500],
            "verbatim_quote": str(item.get("verbatim_quote", ""))[:400],
        })
    return cleaned


def _tag_line(
    client,
    *,
    model: str,
    line: dict[str, Any],
    chunk: dict[str, Any],
    tagging_prompt: str,
) -> dict[str, Any]:
    user = json.dumps(
        {
            "line_type": line["line_type"],
            "text": line["text"],
            "verbatim_quote": line["verbatim_quote"],
            "source_kind": chunk["source_kind"],
            "doc_title": chunk.get("doc_title"),
            "domain": chunk.get("domain"),
            "section": chunk.get("section"),
        },
        ensure_ascii=False,
    )
    raw = call(client, model=model, system=tagging_prompt, user=user, max_tokens=400)
    parsed = parse_json_object(raw)
    if not isinstance(parsed, dict):
        parsed = {}

    valid_categories = {
        "research_output_quality",
        "scientific_grounding",
        "execution_competence",
        "team_credibility",
        "mission_clarity",
        "governance_tokenomics",
    }
    category = str(parsed.get("category", "")).strip()
    if category not in valid_categories:
        category = _fallback_category(line, chunk)
    return {
        "category": category,
        "subgroup": str(parsed.get("subgroup", ""))[:60].strip() or "uncategorized",
        "needs_scientific_support": bool(parsed.get("needs_scientific_support", False)),
        "polarity_unknown": bool(parsed.get("polarity_unknown", line["line_type"] != "claim")),
    }


def _fallback_category(line: dict[str, Any], chunk: dict[str, Any]) -> str:
    """Pick a reasonable category when the tagger fails."""
    if chunk["source_kind"] == "onchain_fact":
        section = (chunk.get("section") or "").lower()
        if "tokenomics" in section or "agreements" in section or "funding" in section:
            return "governance_tokenomics"
        if "research_lead" in section or "identity" in section:
            return "team_credibility"
        if "description" in section:
            return "mission_clarity"
        return "governance_tokenomics"
    if line["line_type"] == "mission":
        return "mission_clarity"
    if line["line_type"] == "claim":
        return "scientific_grounding"
    if line["line_type"] == "feature":
        return "research_output_quality"
    return "execution_competence"


def run(
    *,
    chunks_path: Path,
    ipnft_dir: Path,
    output_path: Path,
    extract_model: str | None = None,
    tagger_model: str | None = None,
) -> dict[str, int]:
    extract_client = make_client()
    tagger_client = make_client(tagger=True)

    em = extract_model or discover_model(
        extract_client,
        env_var="LLM_MODEL",
        fallback_envs=("VALIDATOR_MODEL",),
    )
    tm = tagger_model or discover_model(
        tagger_client,
        env_var="TAGGER_MODEL",
        fallback_envs=("LLM_MODEL", "VALIDATOR_MODEL"),
    )
    print(f"[extract] extractor model: {em}")
    print(f"[extract] tagger model:    {tm}")

    meta = _profile_metadata(ipnft_dir)
    tagging_prompt = load_prompt("dao-tagging.md")
    prompt_cache: dict[str, str] = {}

    chunks = list(_load_jsonl(chunks_path))
    print(f"[extract] {len(chunks)} chunks to process")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    by_category: dict[str, int] = {}
    line_counter = 0

    with output_path.open("w", encoding="utf-8") as fh:
        for i, chunk in enumerate(chunks, 1):
            try:
                lines = _extract_lines(
                    extract_client,
                    model=em,
                    chunk=chunk,
                    meta=meta,
                    prompt_cache=prompt_cache,
                )
            except Exception as exc:
                print(f"  [{i}/{len(chunks)}] extract FAILED on {chunk['chunk_id']}: {exc}", file=sys.stderr)
                lines = []
            for line in lines:
                try:
                    tags = _tag_line(
                        tagger_client,
                        model=tm,
                        line=line,
                        chunk=chunk,
                        tagging_prompt=tagging_prompt,
                    )
                except Exception as exc:
                    print(
                        f"  [{i}/{len(chunks)}] tag FAILED on {chunk['chunk_id']}: {exc}",
                        file=sys.stderr,
                    )
                    tags = {
                        "category": _fallback_category(line, chunk),
                        "subgroup": "tagger_failed",
                        "needs_scientific_support": line["line_type"] == "claim",
                        "polarity_unknown": line["line_type"] != "claim",
                    }
                line_counter += 1
                row = {
                    "line_id": f"L{line_counter}",
                    "chunk_id": chunk["chunk_id"],
                    "source_kind": chunk["source_kind"],
                    "source_path": chunk.get("source_path"),
                    "bundle_path": chunk.get("bundle_path"),
                    "doc_title": chunk.get("doc_title"),
                    "domain": chunk.get("domain"),
                    "section": chunk.get("section"),
                    "page": chunk.get("page"),
                    "line_type": line["line_type"],
                    "text": line["text"],
                    "verbatim_quote": line["verbatim_quote"],
                    "category": tags["category"],
                    "subgroup": tags["subgroup"],
                    "needs_scientific_support": tags["needs_scientific_support"],
                    "polarity_unknown": tags["polarity_unknown"],
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                by_category[tags["category"]] = by_category.get(tags["category"], 0) + 1
            if i % 10 == 0:
                print(f"  [{i}/{len(chunks)}] extracted {written} lines so far")

    print(f"[extract] wrote {written} lines to {output_path}")
    for c, n in sorted(by_category.items()):
        print(f"  {c}: {n}")
    return {"lines": written, **by_category}


def main() -> int:
    parser = argparse.ArgumentParser(description="DAO chunk → extract + tag → JSONL")
    parser.add_argument("--chunks", type=Path, required=True)
    parser.add_argument("--ipnft-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extract-model", type=str, default=None)
    parser.add_argument("--tagger-model", type=str, default=None)
    args = parser.parse_args()

    run(
        chunks_path=args.chunks.resolve(),
        ipnft_dir=args.ipnft_dir.resolve(),
        output_path=args.output.resolve(),
        extract_model=args.extract_model,
        tagger_model=args.tagger_model,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
