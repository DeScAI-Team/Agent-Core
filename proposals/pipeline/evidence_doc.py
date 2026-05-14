#!/usr/bin/env python3
"""Build a Markdown evidence audit trail for the proposal review pipeline.

Runs after proposal_pipe.py (no LLM). Reads review.json,
screener_findings.json, and originality.json; writes evidence_audit.md.

Usage:
  python evidence_doc.py -d ../data/4459
  python evidence_doc.py -d ../data/4459 -o custom_output.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
_MAPPINGS = _BASE / "proposal_mappings.json"

GENERATOR_VERSION = "1.0"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _trunc(s: str, max_len: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _short_doi(doi: object) -> str:
    if not doi:
        return "?"
    s = str(doi).strip()
    for prefix in ("https://doi.org/", "http://doi.org/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return s if len(s) <= 80 else s[:77] + "..."


def _sha256_short(path: Path, *, nbytes: int = 16) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:nbytes]


def _git_revision(cwd: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _load_dimension_weights_text() -> str:
    if not _MAPPINGS.is_file():
        return "Weights defined in `proposals/pipeline/proposal_mappings.json` (file not found)."
    try:
        raw = json.loads(_MAPPINGS.read_text(encoding="utf-8"))
        dw = raw.get("dimension_weights")
        if not isinstance(dw, dict) or not dw:
            return "See `dimension_weights` in `proposals/pipeline/proposal_mappings.json`."
        parts = [f"{k}={v}" for k, v in sorted(dw.items(), key=lambda x: (-float(x[1]), x[0]))]
        return "Current `dimension_weights`: " + "; ".join(parts) + "."
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return "See `dimension_weights` in `proposals/pipeline/proposal_mappings.json`."


def build_evidence_doc(
    review: dict[str, Any],
    screener: dict[str, Any] | None,
    originality: dict[str, Any] | None,
    *,
    review_path: Path,
    screener_path: Path | None,
    originality_path: Path | None,
    output_path: Path,
    top_originality: int = 25,
    quote_max_chars: int = 200,
    observation_max_chars: int = 400,
) -> str:
    lines: list[str] = []

    name = review.get("proposal_name", review.get("research_name", "?"))
    review_date = review.get("review_date", "?")
    composite = review.get("composite_score")
    comp_s = str(composite) if composite is not None else "?"

    lines.append("# Proposal evidence audit trail")
    lines.append("")
    lines.append(f"**Proposal:** {name}  ")
    lines.append(f"**Review date:** {review_date}  ")
    lines.append(f"**Composite score:** {comp_s}/100  ")
    lines.append("")
    lines.append(
        "*This file traces the screener findings, originality comparison, "
        "and funding assessment that produced the review. "
        "The review itself is in review.json.*"
    )
    lines.append("")

    # --- Provenance ---
    lines.append("## Provenance")
    lines.append("")
    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_rev = _git_revision(output_path.parent) or _git_revision(_BASE)
    model = os.environ.get("VALIDATOR_MODEL", "").strip() or "(unset)"

    lines.append(f"- **Generated (UTC):** {gen_ts}")
    lines.append(f"- **Generator:** `proposals/pipeline/evidence_doc.py` v{GENERATOR_VERSION} (no LLM)")
    lines.append(f"- **VALIDATOR_MODEL:** `{model}` *(model used by upstream proposal_pipe.py)*")
    if git_rev:
        lines.append(f"- **Git revision:** `{git_rev}`")
    lines.append("")

    lines.append("| Input | SHA-256 (first 16 hex) |")
    lines.append("|-------|-------------------------|")
    file_rows = [
        ("review.json", review_path),
        ("screener_findings.json", screener_path),
        ("originality.json", originality_path),
    ]
    for label, pth in file_rows:
        if pth is not None and pth.is_file():
            fp = _sha256_short(pth)
            lines.append(f"| {label} | `{fp or '?'}` |")
        else:
            lines.append(f"| {label} | — |")
    lines.append("")

    lines.append("### Composite score")
    lines.append("")
    lines.append(
        "The **composite score** is produced by `proposals/pipeline/proposal_pipe.py`, "
        "combining category scores using **`dimension_weights`** in "
        "`proposals/pipeline/proposal_mappings.json`."
    )
    lines.append("")
    lines.append(_load_dimension_weights_text())
    lines.append("")

    # --- Category scores table ---
    lines.append("## Category scores")
    lines.append("")
    lines.append("| Dimension | Score |")
    lines.append("|-----------|------:|")
    cats = review.get("categories")
    if isinstance(cats, dict):
        for dim, data in sorted(cats.items()):
            if not isinstance(data, dict):
                continue
            score = data.get("score")
            score_s = str(score) if score is not None else "?"
            lines.append(f"| {dim} | {score_s} |")
    lines.append("")

    # --- Category rationales ---
    lines.append("## Category rationales")
    lines.append("")
    if isinstance(cats, dict):
        for dim, data in sorted(cats.items()):
            if not isinstance(data, dict):
                continue
            label = dim.replace("_", " ").title()
            score = data.get("score", "?")
            lines.append(f"### {label} (score: {score})")
            lines.append("")
            rationale = data.get("rationale", "")
            if rationale:
                for para in rationale.split("\n\n"):
                    para = para.strip()
                    if para:
                        lines.append(para)
                        lines.append("")
            else:
                lines.append("*No rationale available.*")
                lines.append("")

    # --- Document screener ---
    lines.append("## Document screener findings")
    lines.append("")
    if screener and isinstance(screener, dict):
        total_raw = screener.get("total_findings_raw", 0)
        total_deduped = screener.get("total_findings_deduped", 0)
        windows_count = screener.get("windows_count", "?")
        lines.append(f"**Windows scanned:** {windows_count}  ")
        lines.append(f"**Raw findings:** {total_raw}  ")
        lines.append(f"**After deduplication:** {total_deduped}  ")
        lines.append("")

        by_dim = screener.get("findings_by_dimension")
        if isinstance(by_dim, dict) and by_dim:
            all_findings: list[dict[str, Any]] = []
            for items in by_dim.values():
                if isinstance(items, list):
                    all_findings.extend(f for f in items if isinstance(f, dict))
            all_findings.sort(
                key=lambda x: (
                    str(x.get("dimension", "")),
                    {"red_flag": 0, "concern": 1, "info": 2}.get(
                        str(x.get("severity", "info")), 3
                    ),
                ),
            )

            for f in all_findings:
                dim = f.get("dimension", "?")
                sev = f.get("severity", "info")
                sec = f.get("section", "?")
                quote = _trunc(str(f.get("quote", "")), quote_max_chars)
                obs = _trunc(str(f.get("observation", "")), observation_max_chars)
                tags = f.get("tags", [])
                tag_str = f"  Tags: {', '.join(tags)}" if tags else ""

                lines.append(f"- **{dim}** ({sev}) · *{sec}*{tag_str}")
                if quote:
                    lines.append(f"  - Quote: {quote}")
                if obs:
                    lines.append(f"  - Observation: {obs}")
                lines.append("")
        else:
            lines.append(
                "*No screener findings were generated. The proposal text may not "
                "have contained enough detail for the screener to surface "
                "dimension-specific observations.*"
            )
            lines.append("")
    else:
        lines.append("*screener_findings.json not loaded.*")
        lines.append("")

    # --- Originality ---
    lines.append("## Originality (literature overlap)")
    lines.append("")
    if originality and isinstance(originality, dict):
        o_score = originality.get("originality_score")
        avg_sim = originality.get("avg_similarity_score")
        rw_count = originality.get("related_works_count")
        lines.append(
            f"**Originality score:** {o_score} · "
            f"**Related works retrieved:** {rw_count} · "
            f"**Avg similarity:** {avg_sim}"
        )
        lines.append("")

        rw = originality.get("related_works")
        if isinstance(rw, list) and rw:
            ranked = sorted(
                [x for x in rw if isinstance(x, dict)],
                key=lambda x: float(x.get("similarity_score", 0)),
                reverse=True,
            )
            top = ranked[:max(0, top_originality)]

            lines.append("| Rank | Similarity | Year | Title | DOI |")
            lines.append("|-----:|-----------:|------|-------|-----|")
            for i, w in enumerate(top, start=1):
                sim = w.get("similarity_score")
                year = w.get("year") or "?"
                title = _trunc(str(w.get("title", "")), 72).replace("|", "\\|")
                doi = _short_doi(w.get("doi")).replace("|", "\\|")
                sim_s = f"{float(sim):.4f}" if isinstance(sim, (int, float)) else str(sim)
                lines.append(f"| {i} | {sim_s} | {year} | {title} | {doi} |")
            lines.append("")

            remaining = len(ranked) - len(top)
            if remaining > 0:
                lines.append(f"*… and {remaining} additional related work(s) not shown.*")
                lines.append("")
        else:
            lines.append("*No related works scored.*")
            lines.append("")
    else:
        lines.append("*originality.json not present — run the pipeline with OpenAlex enabled.*")
        lines.append("")

    # --- Funding snapshot ---
    lines.append("## Funding snapshot")
    lines.append("")
    if isinstance(cats, dict) and "funding_realism" in cats:
        fr = cats["funding_realism"]
        rationale = fr.get("rationale", "")
        if rationale:
            lines.append(rationale)
            lines.append("")
        else:
            lines.append("*No funding realism rationale available.*")
            lines.append("")
    else:
        lines.append("*Funding realism not assessed.*")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Markdown evidence audit for proposal review (no LLM).",
    )
    parser.add_argument(
        "-d", "--directory", type=Path, default=None,
        help="Directory containing review.json and intermediates",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output Markdown path (default: <directory>/evidence_audit.md)",
    )
    parser.add_argument(
        "--top-originality", type=int, default=25,
        help="Max related works rows (default: 25)",
    )
    args = parser.parse_args()

    directory = (args.directory or Path.cwd()).expanduser().resolve()

    review_path = directory / "review.json"
    screener_path = directory / "screener_findings.json"
    originality_path = directory / "originality.json"
    out_path = (args.output or directory / "evidence_audit.md").expanduser().resolve()

    if not review_path.is_file():
        print(f"error: missing {review_path}", file=sys.stderr)
        sys.exit(1)

    review = _load_json(review_path)
    screener = _load_json(screener_path) if screener_path.is_file() else None
    originality = _load_json(originality_path) if originality_path.is_file() else None

    text = build_evidence_doc(
        review,
        screener,
        originality,
        review_path=review_path,
        screener_path=screener_path if screener_path.is_file() else None,
        originality_path=originality_path if originality_path.is_file() else None,
        output_path=out_path,
        top_originality=args.top_originality,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    nbytes = len(text.encode("utf-8"))
    print(f"  Wrote {out_path} ({nbytes} bytes)")


if __name__ == "__main__":
    main()
