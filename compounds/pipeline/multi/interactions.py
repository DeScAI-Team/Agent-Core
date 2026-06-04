#!/usr/bin/env python3
"""Collect structured interaction evidence for a compound combination (data extraction only — no LLM).

Per compound, reads only artifacts produced by ``run_review.py`` / ``review.py``:
  - longevity.json, risk.json (filtered tag exports)
  - longevity_topic_summaries.json, risk_topic_summaries.json
  - review.json (scores and rationales)

Cross-references SPL / kept evidence text and KEGG flags across compounds.

Usage:
  python interactions.py --compounds A B --data-root steps/ -o bundle.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MULTI_DIR = Path(__file__).resolve().parent
_COMPOUNDS_DIR = _MULTI_DIR.parent.parent

if str(_COMPOUNDS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS_DIR))
if str(_MULTI_DIR) not in sys.path:
    sys.path.insert(0, str(_MULTI_DIR))

from evidence_sources import build_compound_pipeline_evidence  # noqa: E402
from token_lookup import (  # noqa: E402
    default_tokens_path,
    resolve_token_entry,
    safe_compound_dir,
)

_SNIPPET_CONTEXT = 120


def _name_tokens(compound: str) -> frozenset[str]:
    return frozenset(t.lower() for t in re.split(r"[\s\-/,]+", compound) if len(t) >= 4)


def _cross_reference(compounds_evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    compound_names = list(compounds_evidence.keys())

    pathway_presence: dict[str, list[str]] = {}
    for name, ev in compounds_evidence.items():
        for flag, val in (ev.get("kegg") or {}).get("pathway_flags", {}).items():
            if val:
                pathway_presence.setdefault(flag, []).append(name)

    shared_pathways = sorted(flag for flag, names in pathway_presence.items() if len(names) >= 2)

    explicit_mentions: list[dict[str, Any]] = []
    for source_name, source_ev in compounds_evidence.items():
        text_blocks: list[tuple[str, str, str]] = []

        for i, excerpt in enumerate((source_ev.get("spl") or {}).get("interaction_excerpts", [])):
            text_blocks.append((f"spl_interaction_{i}", "spl_drug_interaction", excerpt))
        mech = (source_ev.get("spl") or {}).get("mechanism_excerpt")
        if mech:
            text_blocks.append(("spl_mechanism", "spl_mechanism_pharmacology", mech))

        for u in source_ev.get("longevity_evidence") or []:
            snippet = (u.get("snippet") or "") if isinstance(u, dict) else ""
            if snippet:
                text_blocks.append((
                    str(u.get("unit_id") or "longevity"),
                    str(u.get("source_type") or "longevity"),
                    snippet,
                ))

        for summary in source_ev.get("longevity_topic_summaries") or []:
            if not isinstance(summary, dict):
                continue
            tid = str(summary.get("topic_id") or "topic")
            for bi, bullet in enumerate(summary.get("bullets") or []):
                if isinstance(bullet, str) and bullet.strip():
                    text_blocks.append((f"{tid}_bullet_{bi}", "topic_summary", bullet))

        for summary in source_ev.get("risk_topic_summaries") or []:
            if not isinstance(summary, dict):
                continue
            tid = str(summary.get("topic_id") or "risk_topic")
            for bi, bullet in enumerate(summary.get("bullets") or []):
                if isinstance(bullet, str) and bullet.strip():
                    text_blocks.append((f"{tid}_bullet_{bi}", "risk_topic_summary", bullet))

        for u in source_ev.get("risk_evidence") or []:
            snippet = (u.get("snippet") or "") if isinstance(u, dict) else ""
            if snippet:
                text_blocks.append((
                    str(u.get("unit_id") or "risk"),
                    str(u.get("source_type") or "risk"),
                    snippet,
                ))

        for idx, iu in enumerate(source_ev.get("interaction_evidence") or []):
            snippet = (iu.get("snippet") or "") if isinstance(iu, dict) else ""
            if snippet:
                text_blocks.append((
                    f"interaction_{idx}",
                    str(iu.get("source_type") or "interaction_or_combination_risk"),
                    snippet,
                ))

        for target_name in compound_names:
            if target_name == source_name:
                continue
            tokens = _name_tokens(target_name)
            if not tokens:
                continue
            for unit_id, source_type, text in text_blocks:
                text_lower = text.lower()
                matched_tokens = [t for t in tokens if t in text_lower]
                if matched_tokens:
                    first = matched_tokens[0]
                    idx = text_lower.find(first)
                    start = max(0, idx - 60)
                    end = min(len(text), idx + _SNIPPET_CONTEXT)
                    snippet = (
                        ("…" if start > 0 else "")
                        + text[start:end]
                        + ("…" if end < len(text) else "")
                    )
                    explicit_mentions.append({
                        "mentioned_compound": target_name,
                        "found_in_compound": source_name,
                        "source_unit_id": unit_id,
                        "source_type": source_type,
                        "matched_tokens": matched_tokens,
                        "snippet": snippet,
                    })
                    break

    spl_notes: list[str] = []
    for name, ev in compounds_evidence.items():
        spl = ev.get("spl") or {}
        if spl.get("label_matched"):
            spl_notes.append(f"{name}: SPL matched ({spl.get('match_note', '')})")
        else:
            spl_notes.append(f"{name}: no SPL match — {spl.get('match_note', 'unknown')}")

    return {
        "pathway_presence": pathway_presence,
        "shared_pathways": shared_pathways,
        "explicit_mentions": explicit_mentions,
        "spl_coverage_summary": " | ".join(spl_notes),
        "note": (
            "explicit_mentions are text-token matches in kept review-pipeline evidence only. "
            "shared_pathways are KEGG flags from longevity.json/risk.json rows."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect interaction evidence for a compound combination (no LLM).")
    ap.add_argument("--compounds", nargs="+", required=True, metavar="COMPOUND")
    ap.add_argument("--output", "-o", default=None, metavar="PATH")
    ap.add_argument("--data-root", required=True, metavar="DIR")
    ap.add_argument(
        "--tokens-file",
        type=Path,
        default=None,
        help=f"pump.science tokens JSON (default: {default_tokens_path()})",
    )
    args = ap.parse_args()

    if len(args.compounds) < 2:
        print("ERROR: --compounds requires at least two compound names.", file=sys.stderr)
        return 1

    compounds = [c.strip() for c in args.compounds]
    data_root = Path(args.data_root).resolve()
    tokens_path = args.tokens_file.expanduser().resolve() if args.tokens_file else None

    token_meta: dict[str, str] = {}
    try:
        token_meta = resolve_token_entry(compounds, tokens_path=tokens_path)
        intervention = token_meta.get("intervention", "")
        preview = intervention[:80] + ("…" if len(intervention) > 80 else "")
        print(f"Token: {token_meta.get('ticker')} ({preview})", file=sys.stderr)
    except ValueError as exc:
        print(f"  [WARN] Token lookup: {exc}", file=sys.stderr)

    print(f"Compounds: {', '.join(compounds)}", file=sys.stderr)

    compounds_evidence: dict[str, dict[str, Any]] = {}
    for compound in compounds:
        data_dir = data_root / safe_compound_dir(compound)
        print(f"  [{compound}] {data_dir}", file=sys.stderr)
        compounds_evidence[compound] = build_compound_pipeline_evidence(compound, data_dir)
        counts = compounds_evidence[compound].get("pipeline_counts") or {}
        print(
            f"    longevity={counts.get('longevity_rows', 0)} "
            f"risk={counts.get('risk_rows', 0)} "
            f"topic_summaries={len(compounds_evidence[compound].get('longevity_topic_summaries') or [])}",
            file=sys.stderr,
        )
        for w in compounds_evidence[compound].get("warnings", []):
            print(f"    WARN: {w}", file=sys.stderr)

    print("Cross-referencing...", file=sys.stderr)
    cross_ref = _cross_reference(compounds_evidence)

    if cross_ref["shared_pathways"]:
        print(f"  Shared KEGG pathways: {cross_ref['shared_pathways']}", file=sys.stderr)
    else:
        print("  No shared KEGG pathway flags found.", file=sys.stderr)

    if cross_ref["explicit_mentions"]:
        for m in cross_ref["explicit_mentions"]:
            print(
                f"  Mention: '{m['mentioned_compound']}' in '{m['found_in_compound']}' "
                f"→ {m['source_unit_id']}",
                file=sys.stderr,
            )
    else:
        print("  No explicit compound name mentions found.", file=sys.stderr)

    bundle: dict[str, Any] = {
        "$schema_hint": "pump-science.interactions_evidence.v3",
        "combination_name": " + ".join(compounds),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compound_count": len(compounds),
        "compounds": compounds_evidence,
        "cross_reference": cross_ref,
    }
    if token_meta:
        bundle["ticker"] = token_meta.get("ticker")
        bundle["mint"] = token_meta.get("mint")
        bundle["token_id"] = token_meta.get("token_id")
        bundle["intervention"] = token_meta.get("intervention")

    out_text = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text, encoding="utf-8")
        print(f"Wrote: {out_path}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(out_text.encode("utf-8"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
