#!/usr/bin/env python3
"""Generate the DAO review.json from grouped+scored evidence.

For each category in dao_mappings.json:
  - Build a citation-ready evidence block (one entry per validated line, with
    `[#L42]` refs and OpenAlex abstracts for scientific lines).
  - Call the per-category review prompt to produce a rationale (LLM, no thinking).
  - The score is the deterministic score_pct from group_scores.json.
  - If a category has no usable lines, fall back to a "no evidence" rationale and
    a null score.

After all category rationales are produced, generate the top-level review_statement
with one final LLM call. Composite score is the weighted mean of per-category
scores (ceil to int 0-100). Categories with null scores are excluded from the
composite weighting (their weight is redistributed to others).

Reads:
  steps/groups/<category>.json
  steps/group_scores.json
  ipnft_dir/profile.json
  dao_mappings.json
Writes:
  review/review.json with categories carrying ONLY score + rationale.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_PIPELINE_DIR = Path(__file__).resolve().parent
_DEFAULT_MAPPINGS = _PIPELINE_DIR / "dao_mappings.json"
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from llm_client import call, discover_model, load_prompt, make_client  # noqa: E402

GENERATOR_VERSION = "1.0.0"
EVIDENCE_DOC_NOTE = "Check evidence document for citations and links to original sources."


def with_evidence_note(text: str) -> str:
    """Append the standard evidence-audit pointer if not already present."""
    body = (text or "").strip()
    if not body:
        return body
    if EVIDENCE_DOC_NOTE in body:
        return body
    return f"{body} {EVIDENCE_DOC_NOTE}"


def format_review_date(when: datetime | None = None) -> str:
    """Human-readable date matching compounds review.json (e.g. 'June 2, 2026')."""
    dt = when or datetime.now()
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def _profile_metadata(ipnft_dir: Path) -> dict[str, Any]:
    profile_path = ipnft_dir / "profile.json"
    if not profile_path.exists():
        profile_path = ipnft_dir / "metadata" / "profile.json"
    if not profile_path.exists():
        return {}
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    ipnft = profile.get("ipnft", {}) or {}
    lead = ipnft.get("researchLead", {}) or {}
    ipt = ipnft.get("ipt") or {}
    markets = ipt.get("markets") or [] if isinstance(ipt, dict) else []
    return {
        "ipnft_id": ipnft.get("id"),
        "ipnft_symbol": profile.get("symbol") or ipnft.get("initialSymbol"),
        "ipnft_name": ipnft.get("name"),
        "organization": ipnft.get("organization"),
        "research_lead": lead.get("name"),
        "research_lead_email": lead.get("email"),
        "topic": ipnft.get("topic"),
        "trl": ipnft.get("trlValue"),
        "funding": {
            "amount_value": ipnft.get("fundingAmountValue"),
            "amount_decimals": ipnft.get("fundingAmountDecimals"),
            "currency": ipnft.get("fundingAmountCurrency"),
        },
        "ipt": {
            "symbol": ipt.get("symbol") if isinstance(ipt, dict) else None,
            "holder_count": ipt.get("holderCount") if isinstance(ipt, dict) else None,
            "market_count": len(markets),
            "primary_liquidity_usd": markets[0].get("liquidityUsd") if markets else None,
            "primary_market_cap_usd": markets[0].get("marketCapUsd") if markets else None,
        },
        "agreements": [a.get("type") for a in (ipnft.get("agreements") or [])],
        "timeline": {
            "created_at": ipnft.get("createdAt"),
            "minted_at": ipnft.get("mintedAt"),
            "updated_at": ipnft.get("updatedAt"),
        },
    }


def _format_evidence_block(lines: list[dict[str, Any]], *, max_lines: int = 60) -> str:
    """Render every validated line with [#Ln] refs + supporting evidence."""
    parts: list[str] = []
    for row in lines[:max_lines]:
        line_id = row.get("line_id", "L?")
        ref = f"[#{line_id}]"
        verdict = row.get("verdict", "neutral")
        kind = row.get("verdict_kind", "")
        provenance = (
            f"source: {row.get('source_kind', '?')} | doc: {row.get('doc_title', '')} "
            f"[{row.get('domain', '')}] | section: {row.get('section', '')}"
        )
        text = row.get("text", "")
        quote = row.get("verbatim_quote", "")
        rationale = row.get("rationale", "")
        block = [
            f"{ref} (verdict: {verdict} / {kind})",
            f"  {provenance}",
            f"  line: {text}",
            f"  quote: {quote}" if quote else None,
            f"  rationale: {rationale}" if rationale else None,
        ]
        works = row.get("retrieved_works") or []
        cited_ids = set(row.get("citations") or [])
        if works and (cited_ids or verdict in {"valid", "invalid", "inconclusive"}):
            block.append("  OpenAlex evidence:")
            shown = 0
            for w in works:
                oid = w.get("openalex_id")
                if cited_ids and oid not in cited_ids:
                    continue
                title = (w.get("title") or "").strip()[:160]
                year = w.get("year")
                cites = w.get("cited_by_count")
                abstract = (w.get("abstract") or "").strip().replace("\n", " ")[:600]
                block.append(f"    - [{oid}] {title} (year={year}, cited_by={cites})")
                if abstract:
                    block.append(f"      abstract: {abstract}")
                shown += 1
                if shown >= 4:
                    break
        parts.append("\n".join(p for p in block if p is not None))
    if len(lines) > max_lines:
        parts.append(f"... ({len(lines) - max_lines} additional lines omitted) ...")
    return "\n\n".join(parts) if parts else "(no validated lines for this category)"


def _format_category_system(template: str, meta: dict[str, Any], agg: dict[str, Any]) -> str:
    return template.format(
        ipnft_symbol=meta.get("ipnft_symbol") or "",
        ipnft_name=meta.get("ipnft_name") or "",
        organization=meta.get("organization") or "",
        research_lead=meta.get("research_lead") or "Anonymous",
        topic=meta.get("topic") or "",
        score_pct=agg.get("score_pct") if agg.get("score_pct") is not None else "n/a",
        numerator=agg.get("numerator", 0),
        denominator=agg.get("denominator", 0),
    )


def _generate_category_rationale(
    client,
    *,
    model: str,
    category: str,
    group: dict[str, Any],
    agg: dict[str, Any],
    meta: dict[str, Any],
) -> str:
    if not group.get("lines"):
        return (
            f"No reviewable lines were captured for {category} in the available evidence. "
            "(no citation)"
        )
    template = load_prompt(f"dao-category-review-{category}.md")
    system = _format_category_system(template, meta, agg)
    user = _format_evidence_block(group["lines"])
    raw = call(client, model=model, system=system, user=user, max_tokens=900)
    return raw.strip()


def _format_review_statement_user(
    rationales: dict[str, dict[str, Any]],
) -> str:
    parts: list[str] = []
    for cat, payload in rationales.items():
        score = payload.get("score")
        score_str = "n/a" if score is None else str(score)
        parts.append(f"## {cat} (score={score_str})\n{payload.get('rationale', '')}")
    return "\n\n".join(parts)


def _composite(scores: dict[str, int | None], weights: dict[str, float]) -> int | None:
    total_w = 0.0
    weighted = 0.0
    for cat, score in scores.items():
        if score is None:
            continue
        w = float(weights.get(cat, 0.0))
        weighted += score * w
        total_w += w
    if total_w <= 0:
        return None
    return int(math.ceil(weighted / total_w))


def run(
    *,
    groups_dir: Path,
    scores_path: Path,
    ipnft_dir: Path,
    output_path: Path,
    mappings_path: Path = _DEFAULT_MAPPINGS,
    model: str | None = None,
    extracted_path: Path | None = None,
    chunks_path: Path | None = None,
) -> dict[str, Any]:
    mappings = json.loads(mappings_path.read_text(encoding="utf-8"))
    categories: list[str] = mappings["categories"]
    weights: dict[str, float] = mappings["weights"]
    aggregates = json.loads(scores_path.read_text(encoding="utf-8"))

    client = make_client()
    m = model or discover_model(
        client,
        env_var="LLM_MODEL",
        fallback_envs=("VALIDATOR_MODEL",),
    )
    print(f"[review] model: {m}")

    meta = _profile_metadata(ipnft_dir)

    rationales: dict[str, dict[str, Any]] = {}
    scores: dict[str, int | None] = {}
    for cat in categories:
        agg = aggregates.get(cat) or {"score_pct": None, "numerator": 0, "denominator": 0}
        group_path = groups_dir / f"{cat}.json"
        group = json.loads(group_path.read_text(encoding="utf-8")) if group_path.exists() else {"lines": []}
        print(f"  [review] {cat} ({len(group.get('lines') or [])} lines, score={agg.get('score_pct')})")
        rationale = _generate_category_rationale(
            client,
            model=m,
            category=cat,
            group=group,
            agg=agg,
            meta=meta,
        )
        rationale = with_evidence_note(rationale)
        score = agg.get("score_pct")
        rationales[cat] = {"score": score, "rationale": rationale}
        scores[cat] = score

    composite = _composite(scores, weights)
    print(f"[review] composite score: {composite}")

    statement_template = load_prompt("dao-review-statement.md")
    statement_system = statement_template.format(
        ipnft_symbol=meta.get("ipnft_symbol") or "",
        ipnft_name=meta.get("ipnft_name") or "",
        organization=meta.get("organization") or "",
        research_lead=meta.get("research_lead") or "Anonymous",
        topic=meta.get("topic") or "",
        composite_score=composite if composite is not None else "n/a",
    )
    statement_user = _format_review_statement_user(rationales)
    review_statement = call(
        client,
        model=m,
        system=statement_system,
        user=statement_user,
        max_tokens=900,
    ).strip()
    review_statement = with_evidence_note(review_statement)

    now = datetime.now()
    output = {
        "research_dao": ipnft_dir.name,
        "review_date": format_review_date(now),
        "composite_score": composite,
        "review_statement": review_statement,
        "categories": {
            cat: {"score": rationales[cat]["score"], "rationale": rationales[cat]["rationale"]}
            for cat in categories
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[review] wrote {output_path}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build review.json from grouped DAO evidence")
    parser.add_argument("--groups-dir", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--ipnft-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mappings", type=Path, default=_DEFAULT_MAPPINGS)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--extracted", type=Path, default=None, help="optional, used for input_summary counts")
    parser.add_argument("--chunks", type=Path, default=None, help="optional, used for input_summary counts")
    args = parser.parse_args()

    run(
        groups_dir=args.groups_dir.resolve(),
        scores_path=args.scores.resolve(),
        ipnft_dir=args.ipnft_dir.resolve(),
        output_path=args.output.resolve(),
        mappings_path=args.mappings.resolve(),
        model=args.model,
        extracted_path=args.extracted.resolve() if args.extracted else None,
        chunks_path=args.chunks.resolve() if args.chunks else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
