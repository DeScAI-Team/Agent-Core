#!/usr/bin/env python3
"""Build review/evidence_audit.md — a public-facing audit document.

Every entry links back to the original publicly-accessible source (DAO website,
GitHub repo, IPNFT mint page, OpenAlex paper, etc.). Internal pipeline paths are
not surfaced. The audit lists, per source, the actual lines the agent extracted
and how each was scored, so anyone can click through and verify the work.

Reads:
  steps/chunks.jsonl
  steps/extracted.jsonl
  steps/validated.jsonl
  steps/group_scores.json
  review/review.json
  ipnft_dir/profile.json
  ipnft_dir/links.json   (optional — used for richer source labelling)
  ipnft_dir/dataroom.json (optional — used to label dataroom files)
Writes:
  review/evidence_audit.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "1.2.0"
MAX_LINES_PER_SOURCE = 12

_PIPELINE_DIR = Path(__file__).resolve().parent
_DEFAULT_MAPPINGS = _PIPELINE_DIR / "dao_mappings.json"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _ipnft_id(profile: dict[str, Any]) -> str | None:
    ipnft = profile.get("ipnft", {}) or {}
    return ipnft.get("id") or profile.get("tokenId")


def _public_url_for(row: dict[str, Any], *, ipnft_id: str | None, dataroom_files: set[str]) -> tuple[str, str]:
    """Return (label, url) for a chunk/line — never an absolute filesystem path.

    Mapping:
      - crawl_md  → https://<domain>/  (the page the crawler scraped)
      - pdf / image_caption / video_*  → IPNFT dataroom page on Molecule
      - onchain_fact  → IPNFT mint page on Molecule
      - text_doc  → IPNFT dataroom page on Molecule (best we can do)
    """
    kind = row.get("source_kind") or ""
    domain = (row.get("domain") or "").strip()
    doc_title = (row.get("doc_title") or "").strip()
    mint_url = f"https://mint.molecule.to/ipnft/{ipnft_id}" if ipnft_id else "https://mint.molecule.to/"

    if kind == "crawl_md" and domain:
        return (f"{domain}", f"https://{domain}/")
    if kind == "onchain_fact":
        return ("IPNFT on-chain profile", mint_url)
    if kind == "pdf":
        # surface the original filename so a human can locate it in the dataroom
        suffix = doc_title or "PDF"
        return (f"Dataroom PDF · {suffix}", mint_url)
    if kind == "image_caption":
        suffix = doc_title or "image"
        return (f"Dataroom image · {suffix}", mint_url)
    if kind in {"video_transcript", "video_frame"}:
        suffix = doc_title or "video"
        return (f"Dataroom video · {suffix}", mint_url)
    if kind == "text_doc":
        return (f"Dataroom file · {doc_title}", mint_url)
    if domain:
        return (domain, f"https://{domain}/")
    return (doc_title or kind or "source", mint_url)


def _source_key(row: dict[str, Any]) -> str:
    """Bucket chunks/lines under a stable per-source label that does NOT leak filesystem paths."""
    kind = row.get("source_kind") or ""
    if kind == "onchain_fact":
        return "onchain"
    if kind == "crawl_md" and row.get("domain"):
        return f"crawl::{row['domain']}"
    if kind == "pdf":
        return f"pdf::{row.get('doc_title') or ''}"
    if kind == "image_caption":
        return f"image::{row.get('doc_title') or ''}"
    if kind in {"video_transcript", "video_frame"}:
        return f"video::{row.get('doc_title') or ''}"
    if kind == "text_doc":
        return f"text::{row.get('doc_title') or ''}"
    return f"{kind}::{row.get('doc_title') or ''}"


def _verdict_emoji(v: str | None) -> str:
    return {
        "valid": "✅",
        "positive": "✅",
        "invalid": "❌",
        "negative": "❌",
        "inconclusive": "❓",
        "neutral": "·",
    }.get(v or "", "·")


def _format_quote(line: dict[str, Any]) -> str:
    quote = (line.get("verbatim_quote") or "").strip()
    text = (line.get("text") or "").strip()
    if quote and quote.lower() != text.lower():
        return f"> {quote}"
    return ""


def _format_citation(cid: str) -> str:
    return f"[{cid}](https://openalex.org/{cid})"


def _line_weight(
    line: dict[str, Any],
    *,
    source_weights: dict[str, float],
    line_type_weights: dict[str, dict[str, float]],
    default_source: float,
    default_line_type: float,
) -> float:
    cat = line.get("category") or ""
    sw = float(source_weights.get(line.get("source_kind") or "", default_source))
    cat_map = line_type_weights.get(cat) or {}
    lw = float(cat_map.get(line.get("line_type") or "", default_line_type))
    return round(sw * lw, 4)


def build(
    *,
    ipnft_dir: Path,
    steps_dir: Path,
    review_dir: Path,
    output_path: Path,
) -> None:
    chunks = _load_jsonl(steps_dir / "chunks.jsonl")
    extracted = _load_jsonl(steps_dir / "extracted.jsonl")
    validated = _load_jsonl(steps_dir / "validated.jsonl")
    group_scores = _load_json(steps_dir / "group_scores.json") or {}
    review = _load_json(review_dir / "review.json") or {}

    # Pull weight / effective_weight / cluster_size off the per-category group
    # files, indexed by line_id, so the per-line display reflects the same
    # numbers that drove the score.
    groups_dir = steps_dir / "groups"
    group_line_meta: dict[str, dict[str, Any]] = {}
    if groups_dir.exists():
        for gp in sorted(groups_dir.glob("*.json")):
            gdata = _load_json(gp) or {}
            for ln in gdata.get("lines") or []:
                lid = ln.get("line_id")
                if not lid:
                    continue
                group_line_meta[lid] = {
                    "weight": ln.get("weight"),
                    "effective_weight": ln.get("effective_weight"),
                    "cluster_id": ln.get("cluster_id"),
                    "cluster_size": ln.get("cluster_size"),
                }

    mappings = _load_json(_DEFAULT_MAPPINGS) or {}
    source_weights = mappings.get("source_weights") or {}
    line_type_weights = mappings.get("line_type_weights") or {}
    default_source = float(mappings.get("default_source_weight", 1.0))
    default_line_type = float(mappings.get("default_line_type_weight", 1.0))

    profile_path = ipnft_dir / "profile.json"
    if not profile_path.exists():
        profile_path = ipnft_dir / "metadata" / "profile.json"
    profile = _load_json(profile_path) or {}
    ipnft = profile.get("ipnft", {}) or {}

    dataroom_path = ipnft_dir / "dataroom.json"
    dataroom = _load_json(dataroom_path) or {}
    dataroom_files: set[str] = {
        (f.get("path") or "").lstrip("/")
        for f in (dataroom.get("files") or [])
        if isinstance(f, dict)
    }

    name = review.get("research_dao") or ipnft_dir.name or "Unknown"
    org = ipnft.get("organization") or "Unknown"
    review_date = review.get("review_date") or datetime.now(timezone.utc).isoformat()
    composite = review.get("composite_score")
    ipnft_id = _ipnft_id(profile)
    mint_url = f"https://mint.molecule.to/ipnft/{ipnft_id}" if ipnft_id else None

    valid_lines = [r for r in validated if r.get("verdict") in {"valid", "positive"}]
    negative_lines = [r for r in validated if r.get("verdict") in {"invalid", "negative"}]
    inconclusive_lines = [r for r in validated if r.get("verdict") == "inconclusive"]

    md: list[str] = []
    md.append(f"# Evidence audit · {name}")
    md.append("")
    md.append(f"**Organization:** {org}  ")
    if mint_url:
        md.append(f"**IPNFT page:** [{mint_url}]({mint_url})  ")
    md.append(f"**Review date:** {review_date}  ")
    md.append(f"**Composite score:** {'(none)' if composite is None else composite} / 100  ")
    md.append("")
    md.append(
        "This document is a public audit of the AI agent's review. Every link below "
        "points to a publicly-accessible source. For each source, you can see the lines "
        "the agent surfaced, how each line was scored, and which OpenAlex papers were "
        "used to validate scientific claims."
    )
    md.append("")
    md.append("---")
    md.append("")

    md.append("## At a glance")
    md.append("")
    md.append(f"- Sources reviewed: **{len({_source_key(c) for c in chunks})}**")
    md.append(f"- Lines extracted from those sources: **{len(extracted)}**")
    md.append(f"- Lines that produced a clear verdict: **{len(valid_lines) + len(negative_lines)}** "
              f"({len(valid_lines)} favorable, {len(negative_lines)} unfavorable)")
    md.append(f"- Lines whose science could not be confirmed: **{len(inconclusive_lines)}**")
    md.append("")
    md.append("---")
    md.append("")

    md.append("## Category scores")
    md.append("")
    md.append("| Category | Score | Weighted favorable / scored | Raw favorable / scored | Lines | Clusters |")
    md.append("|----------|-------|-----------------------------|------------------------|-------|----------|")
    for cat in (review.get("categories") or {}):
        agg = group_scores.get(cat) or {}
        score = agg.get("score_pct")
        score_str = "n/a" if score is None else f"{score}"
        w_num = agg.get("numerator", 0)
        w_den = agg.get("denominator", 0)
        r_num = agg.get("raw_numerator", 0)
        r_den = agg.get("raw_denominator", 0)
        md.append(
            f"| {cat} | {score_str} | {w_num:.2f} / {w_den:.2f} | {r_num} / {r_den} "
            f"| {agg.get('line_count', 0)} | {agg.get('cluster_count', 0)} |"
        )
    md.append("")
    md.append(
        "**How the score is computed.** Three layers sit between raw verdicts and the score:"
    )
    md.append("")
    md.append(
        "1. *Category-scoped validation* — each line is judged by a validator that sees "
        "ONLY the criteria for that line's category. A vague-mission line cannot be "
        "penalised by `team_credibility`'s anonymity rule, and a missing-agreement "
        "line cannot be penalised for `mission_clarity`. Categories are evaluated "
        "independently."
    )
    md.append(
        "2. *Tag-based weighting* — each line carries "
        "`weight = source_weight × line_type_weight[category]`. Source weights reflect "
        "trust (on-chain facts highest, vision-extracted slide text lowest). Line-type "
        "weights reflect what each category cares about — *features* drive "
        "`research_output_quality`, *mission* lines drive `mission_clarity`, *facts* "
        "drive `team_credibility` and `governance_tokenomics`."
    )
    md.append(
        "3. *Near-duplicate clustering* — within a category, lines that say essentially "
        "the same thing are merged into a cluster. Each line's effective contribution is "
        "`weight ÷ √cluster_size`, so a DAO that repeats one mission across six docs no "
        "longer eats six penalties (or six rewards). The `Clusters` column shows how "
        "many distinct ideas, not lines, the score is built from."
    )
    md.append("")
    md.append(
        "Score = `round(100 × Σ effective_weight(favorable) / Σ effective_weight(favorable + unfavorable))`. "
        "Neutral / inconclusive lines do not count toward the denominator."
    )
    md.append("")
    md.append("---")
    md.append("")

    md.append("## Sources reviewed")
    md.append("")
    md.append(
        "Every source the agent looked at, in order. The lines listed under each source "
        "are the *actual extracts* the agent fed to the validator — click any link to "
        "verify them."
    )
    md.append("")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunk_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in chunks:
        chunk_groups[_source_key(c)].append(c)
    for r in validated:
        grouped[_source_key(r)].append(r)

    seen_keys: set[str] = set()
    sample_rows: dict[str, dict[str, Any]] = {}
    for c in chunks:
        k = _source_key(c)
        sample_rows.setdefault(k, c)

    sorted_keys = sorted(
        sample_rows.keys(),
        key=lambda k: (
            0 if k.startswith("crawl::") else
            1 if k == "onchain" else
            2 if k.startswith("pdf::") else
            3 if k.startswith("video::") else
            4 if k.startswith("image::") else
            5,
            k,
        ),
    )

    for key in sorted_keys:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        sample = sample_rows[key]
        label, url = _public_url_for(sample, ipnft_id=ipnft_id, dataroom_files=dataroom_files)
        lines_for_source = grouped.get(key) or []
        chunks_for_source = chunk_groups.get(key) or []
        verdict_counts = Counter(
            (r.get("verdict") or "neutral") for r in lines_for_source
        )

        md.append(f"### {label}")
        md.append("")
        if url.startswith("http"):
            md.append(f"**Link:** [{url}]({url})  ")
        else:
            md.append(f"**Reference:** {url}  ")
        md.append(
            f"**Chunks read:** {len(chunks_for_source)} · "
            f"**Lines extracted:** {len(lines_for_source)} · "
            f"**Verdicts:** "
            + (", ".join(f"{k}={v}" for k, v in sorted(verdict_counts.items())) if verdict_counts else "—")
        )
        md.append("")

        meaningful = [
            r for r in lines_for_source
            if r.get("verdict") in {"valid", "positive", "invalid", "negative", "inconclusive"}
        ]
        if not meaningful:
            md.append("*No meaningful verdicts produced from this source.*")
            md.append("")
            continue

        meaningful.sort(
            key=lambda r: {
                "valid": 0, "positive": 1, "negative": 2, "invalid": 3, "inconclusive": 4
            }.get(r.get("verdict"), 5)
        )
        shown = meaningful[:MAX_LINES_PER_SOURCE]

        # Index every group file once so we can read the per-line
        # effective_weight + cluster_size that group_score.py stamped in.
        for r in shown:
            verdict = r.get("verdict") or "neutral"
            line_id = r.get("line_id") or "?"
            text = (r.get("text") or "").strip()
            quote_block = _format_quote(r)
            cites = r.get("citations") or []
            line_id_str = r.get("line_id") or ""
            group_meta = group_line_meta.get(line_id_str, {})
            weight = group_meta.get("weight")
            if weight is None:
                weight = _line_weight(
                    r,
                    source_weights=source_weights,
                    line_type_weights=line_type_weights,
                    default_source=default_source,
                    default_line_type=default_line_type,
                )
            effective = group_meta.get("effective_weight", weight)
            csize = group_meta.get("cluster_size", 1)
            cat_short = (r.get("category") or "").replace("_", " ")
            ltype = r.get("line_type") or "?"
            cluster_note = f" · cluster ×{csize}" if csize and csize > 1 else ""
            md.append(
                f"- {_verdict_emoji(verdict)} **{verdict}** · `#{line_id}` "
                f"· _{cat_short} · {ltype} · weight {weight:.2f} → eff {effective:.2f}{cluster_note}_ — {text}"
            )
            if quote_block:
                md.append(f"    {quote_block}")
            if cites:
                md.append("    Supporting literature: " + ", ".join(_format_citation(c) for c in cites))
            rat = (r.get("rationale") or "").strip()
            if rat:
                md.append(f"    *Validator note:* {rat}")
        if len(meaningful) > MAX_LINES_PER_SOURCE:
            md.append(f"- _… {len(meaningful) - MAX_LINES_PER_SOURCE} additional scored line(s) omitted._")
        md.append("")

    md.append("---")
    md.append("")

    citation_titles: dict[str, dict[str, Any]] = {}
    citation_to_lines: dict[str, list[str]] = defaultdict(list)
    for r in validated:
        for cid in r.get("citations") or []:
            citation_to_lines[cid].append(r.get("line_id", "?"))
        for w in r.get("retrieved_works") or []:
            cid = w.get("openalex_id")
            if cid and cid not in citation_titles:
                citation_titles[cid] = w

    md.append("## Scientific literature consulted")
    md.append("")
    if citation_to_lines:
        md.append("Each row is an OpenAlex paper the agent retrieved and cited when validating a scientific claim. Click through to read the abstract.")
        md.append("")
        md.append("| Paper | Year | Cited by | Used for lines |")
        md.append("|-------|------|----------|----------------|")
        for cid in sorted(citation_to_lines.keys()):
            w = citation_titles.get(cid, {})
            title = (w.get("title") or "(unknown)")[:160]
            year = w.get("year") or "—"
            cites = w.get("cited_by_count")
            cites_str = "—" if cites is None else str(cites)
            refs = ", ".join(f"#{lid}" for lid in citation_to_lines[cid])
            md.append(
                f"| [{title}](https://openalex.org/{cid}) "
                f"| {year} | {cites_str} | {refs} |"
            )
    else:
        md.append("*No OpenAlex citations were anchored during validation.*")
    md.append("")
    md.append("---")
    md.append("")

    md.append("## What's missing")
    md.append("")
    missing: list[str] = []
    if not (ipnft.get("agreements") or []):
        missing.append("No on-chain legal agreements present.")
    elif "Development Agreement" not in {a.get("type") for a in (ipnft.get("agreements") or []) if isinstance(a, dict)}:
        missing.append("On-chain Assignment Agreement is present, but no Development Agreement was found.")
    if not ipnft.get("ipt"):
        missing.append("No IPT (IP Token) issued for this IPNFT.")
    else:
        markets = (ipnft.get("ipt") or {}).get("markets") or []
        if not markets:
            missing.append("IPT exists but no markets are registered.")
    lead = ipnft.get("researchLead") or {}
    lead_name = (lead.get("name") or "").lower()
    if not lead_name or lead_name in {"anon", "anonymous", "pseudonym", "n/a"}:
        missing.append("Research lead is anonymous or pseudonymous on-chain.")
    if not any(c.get("source_kind") == "pdf" for c in chunks):
        missing.append("No PDF text was processed — the dataroom had no PDFs or vision was skipped.")
    if not any(c.get("source_kind") == "crawl_md" for c in chunks):
        missing.append("No crawled web pages were ingested.")
    if not any(c.get("source_kind") == "video_transcript" for c in chunks):
        missing.append("No video audio was transcribed (whisper.cpp may not be set up, or the dataroom had no videos).")
    if missing:
        for s in missing:
            md.append(f"- {s}")
    else:
        md.append("*No structural gaps detected.*")
    md.append("")
    md.append("---")
    md.append("")
    md.append(f"*Generated by DAO Review Pipeline v{GENERATOR_VERSION}.*")
    md.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(md), encoding="utf-8")
    print(f"[evidence-audit] wrote {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the DAO public-facing evidence audit")
    parser.add_argument("--ipnft-dir", type=Path, required=True)
    parser.add_argument("--steps-dir", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    build(
        ipnft_dir=args.ipnft_dir.resolve(),
        steps_dir=args.steps_dir.resolve(),
        review_dir=args.review_dir.resolve(),
        output_path=args.output.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
