#!/usr/bin/env python3
"""Build a compact Markdown audit trail for the empirical review pipeline.

Runs after score.py (no LLM). Reads review.json, retrieve_compare output,
screener.json, and optionally originality.json; writes evidence_audit.md.

If the document exceeds --max-bytes, trimming is applied in order (see
``build_audit_markdown`` docstring).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
_PIPELINE = _BASE.parent
_MAPPINGS = _PIPELINE / "mappings.json"

if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))
from prep import SCORE_EXCLUDED_GRADES  # noqa: E402

# Bump when output schema or provenance fields change materially.
GENERATOR_VERSION = "1.1"

DEFAULT_MAX_BYTES = 120 * 1024
RETRIEVE_CANDIDATES = ("retrieve_compare_llm.json", "retrieve_compare_out.json")

# Trimming phases: drop screener info, then claim rows for strong/moderate (weakest last).


@dataclass
class BuildParams:
    claim_max_chars: int
    ref_rationale_max_chars: int
    quote_max_chars: int
    observation_max_chars: int
    top_originality: int
    include_self_reported: bool
    drop_screener_info: bool
    drop_ref_rationale: bool
    drop_strong_moderate_claims: bool
    provenance_compact: bool


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_retrieve_path(directory: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"retrieve_compare file not found: {p}")
        return p
    for name in RETRIEVE_CANDIDATES:
        p = directory / name
        if p.is_file():
            return p
    raise FileNotFoundError(
        f"no {RETRIEVE_CANDIDATES[0]} or {RETRIEVE_CANDIDATES[1]} in {directory}",
    )


def _short_doi(doi: object) -> str:
    if not doi:
        return "?"
    s = str(doi).strip()
    for prefix in ("https://doi.org/", "http://doi.org/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
            break
    return s if len(s) <= 80 else s[:77] + "..."


def _trunc(s: str, max_len: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _sha256_file_short(path: Path, *, nbytes: int = 16) -> str | None:
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
    """One-line summary of composite weights for auditors."""
    if not _MAPPINGS.is_file():
        return (
            "Weights are defined under `dimension_weights` in `articles/pipeline/mappings.json` "
            "(file not found at expected path relative to this script)."
        )
    try:
        raw = json.loads(_MAPPINGS.read_text(encoding="utf-8"))
        dw = raw.get("dimension_weights")
        if not isinstance(dw, dict) or not dw:
            return "See `dimension_weights` in `articles/pipeline/mappings.json`."
        parts = [f"{k}={v}" for k, v in sorted(dw.items(), key=lambda x: (-float(x[1]), x[0]))]
        return "Current `dimension_weights`: " + "; ".join(parts) + "."
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return "See `dimension_weights` in `articles/pipeline/mappings.json`."


def _build_provenance_block(
    *,
    cwd: Path,
    review_path: Path,
    retrieve_path: Path,
    screener_path: Path | None,
    originality_path: Path | None,
    output_path: Path,
    compact: bool,
) -> list[str]:
    """Markdown lines for ## Provenance (compact when trimming)."""
    lines: list[str] = []
    lines.append("## Provenance")
    lines.append("")
    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_rev = _git_revision(cwd) or _git_revision(output_path.parent)
    model = (
        os.environ.get("LLM_MODEL", "").strip()
        or os.environ.get("VALIDATOR_MODEL", "").strip()
        or "(unset)"
    )

    lines.append(f"- **Generated (UTC):** {gen_ts}")
    lines.append(f"- **Generator:** `evidence-doc.py` v{GENERATOR_VERSION} (no LLM)")
    lines.append(f"- **LLM_MODEL / VALIDATOR_MODEL:** `{model}` *(upstream retrieve_compare / review / score)*")
    lines.append(f"- **Git revision:** `{git_rev}` *(repository containing the run, best-effort)*" if git_rev else "- **Git revision:** *(unavailable — not a git checkout or git not on PATH)*")
    lines.append(f"- **Retrieve source file:** `{retrieve_path.name}`")

    if not compact:
        lines.append("")
        lines.append("| Input | SHA-256 (first 16 hex) |")
        lines.append("|-------|-------------------------|")
        rows = [
            ("review.json", review_path),
            (retrieve_path.name, retrieve_path),
            ("screener.json", screener_path),
            ("originality.json", originality_path),
        ]
        for label, pth in rows:
            if pth is None or not pth.is_file():
                lines.append(f"| {label} | — |")
            else:
                fp = _sha256_file_short(pth)
                lines.append(f"| {label} | `{fp or '?'}` |")
        lines.append("")
        lines.append(
            "*Fingerprints are of the JSON files read at generation time; use them to verify this "
            "document matches a frozen artifact bundle.*",
        )
    else:
        rv = _sha256_file_short(review_path)
        rc = _sha256_file_short(retrieve_path)
        lines.append(
            f"- **Fingerprints (short):** review=`{rv or '?'}` retrieve=`{rc or '?'}`",
        )

    lines.append("")
    lines.append("### Composite score")
    lines.append("")
    lines.append(
        "The **composite score** in `review.json` is produced by "
        "`articles/pipeline/empirical/score.py`, which combines category scores using "
        "**`dimension_weights`** in `articles/pipeline/mappings.json`. "
        "Dimensions absent from the review use weight 0 implicitly.",
    )
    lines.append("")
    lines.append(_load_dimension_weights_text())
    lines.append("")
    lines.append("### Reference support lines")
    lines.append("")
    lines.append(
        "In **References**, citations may show "
        "`(no verdict — missing abstract)` when OpenAlex had no abstract for that work, or "
        "`(no verdict — ungraded)` when the retrieve_compare step did not assign "
        "`support_verdict` (e.g. skipped LLM grading). "
        "These are **not** contradictions of the paper — they mark limits of automated checking.",
    )
    lines.append("")
    return lines


def _iter_retrieve_claims(retrieve: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Yield (dimension, bucket, claim_dict)."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for dim, body in retrieve.items():
        if not isinstance(body, dict):
            continue
        buckets = body.get("buckets")
        if not isinstance(buckets, dict):
            continue
        for bname, blist in buckets.items():
            if not isinstance(blist, list):
                continue
            for cl in blist:
                if isinstance(cl, dict):
                    out.append((str(dim), str(bname), cl))
    return out


def _grade_counts(retrieve: dict[str, Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for dim, _b, cl in _iter_retrieve_claims(retrieve):
        g = str(cl.get("evidence_grade") or "unknown").strip() or "unknown"
        counts.setdefault(dim, {})
        counts[dim][g] = counts[dim].get(g, 0) + 1
    return counts


def _claim_detail_allowed(
    grade: str | None,
    params: BuildParams,
) -> bool:
    g = str(grade or "unknown").strip() or "unknown"
    if params.include_self_reported:
        return True
    if g in SCORE_EXCLUDED_GRADES:
        return False
    if params.drop_strong_moderate_claims and g in ("strong", "moderate"):
        return False
    return True


def _support_verdict_label(cit: dict[str, Any]) -> str:
    ver = cit.get("support_verdict")
    if ver is not None and str(ver).strip():
        return str(ver).replace("_", " ")
    abst = cit.get("abstract")
    if abst is None or (isinstance(abst, str) and not abst.strip()):
        return "(no verdict — missing abstract)"
    return "(no verdict — ungraded)"


def _format_ref_line(
    c: dict[str, Any],
    params: BuildParams,
) -> str:
    refn = c.get("ref_number")
    doi = _short_doi(c.get("doi"))
    ver_s = _support_verdict_label(c)
    line = f"- [{refn}] {doi} → {ver_s}"
    if not params.drop_ref_rationale and params.ref_rationale_max_chars > 0:
        r = c.get("support_rationale")
        if r:
            line += f" — {_trunc(str(r), params.ref_rationale_max_chars)}"
    return line


def _screener_findings(screener: dict[str, Any]) -> list[dict[str, Any]]:
    by_dim = screener.get("findings_by_dimension")
    if not isinstance(by_dim, dict):
        return []
    out: list[dict[str, Any]] = []
    for _dim_key, items in by_dim.items():
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    out.append(it)
    out.sort(
        key=lambda x: (
            str(x.get("dimension") or ""),
            str(x.get("severity") or ""),
            str(x.get("section") or ""),
        ),
    )
    return out


def build_audit_markdown(
    review: dict[str, Any],
    retrieve: dict[str, Any],
    screener: dict[str, Any] | None,
    originality: dict[str, Any] | None,
    params: BuildParams,
    *,
    provenance_lines: list[str] | None = None,
) -> str:
    """Assemble Markdown. Trimming is done by the caller re-calling with tighter params.

    Trimming order when over budget: lower top_originality, shorter strings,
    drop_ref_rationale, drop_screener_info, drop_strong_moderate_claims;
    then reduce claim_max_chars / other limits in the outer loop.
    """
    lines: list[str] = []
    name = review.get("research_name", "?")
    date = review.get("review_date", "?")
    comp = review.get("composite_score")
    comp_s = f"{float(comp):.4f}" if isinstance(comp, (int, float)) else str(comp)

    lines.append("# Evidence audit trail")
    lines.append("")
    lines.append(f"**Document:** {name}  ")
    lines.append(f"**Review date:** {date}  ")
    lines.append(f"**Composite score:** {comp_s}  ")
    lines.append("")
    lines.append(
        "*Full narrative review and plain-language overview are published separately "
        "(review.json / overview.json). This file traces citation grades, screener "
        "findings, and originality context.*",
    )
    lines.append("")
    if provenance_lines:
        lines.extend(provenance_lines)

    # Category table
    lines.append("## Category scores")
    lines.append("")
    lines.append("| Dimension | Score | Method | Notes |")
    lines.append("|-----------|------:|--------|-------|")
    cats = review.get("categories")
    if isinstance(cats, dict):
        for dim, data in sorted(cats.items()):
            if not isinstance(data, dict):
                continue
            score = data.get("score")
            sm = data.get("score_method", "")
            score_s = f"{float(score):.4f}" if isinstance(score, (int, float)) else str(score)
            notes_parts: list[str] = []
            for key in ("claim_count", "finding_count", "compared_works"):
                if key in data and data[key] is not None:
                    notes_parts.append(f"{key}={data[key]}")
            notes = "; ".join(notes_parts)
            lines.append(f"| {dim} | {score_s} | {sm} | {notes} |")
    lines.append("")

    # Grade summaries
    lines.append("## Evidence grade counts (retrieve_compare)")
    lines.append("")
    gcounts = _grade_counts(retrieve)
    for dim in sorted(gcounts.keys()):
        parts = [f"{g}: {n}" for g, n in sorted(gcounts[dim].items(), key=lambda x: (-x[1], x[0]))]
        lines.append(f"- **{dim}:** " + "; ".join(parts))
    lines.append("")

    # Claim-level detail
    lines.append("## Claim-level trace (non-self-reported)")
    lines.append("")
    if not params.include_self_reported:
        lines.append(
            "*Listing excludes grades counted only internally (self_reported, "
            "self_reported_method). Use `--include-self-reported` for all claims.*",
        )
        lines.append("")
    shown = 0
    for dim, bucket, cl in _iter_retrieve_claims(retrieve):
        grade = cl.get("evidence_grade")
        if not _claim_detail_allowed(grade, params):
            continue
        shown += 1
        chunk = cl.get("chunk_id", "?")
        sec = cl.get("section_heading") or cl.get("section") or "?"
        claim_text = _trunc(str(cl.get("claim") or ""), params.claim_max_chars)
        gstr = str(grade or "?")
        lines.append(f"### {dim} / {bucket} · chunk {chunk} · {sec}")
        lines.append("")
        lines.append(f"**Grade:** `{gstr}`  ")
        lines.append("")
        lines.append(f"> {claim_text}")
        lines.append("")
        cites = cl.get("citations")
        if isinstance(cites, list) and cites:
            lines.append("**References:**")
            lines.append("")
            for cit in cites:
                if isinstance(cit, dict):
                    lines.append(_format_ref_line(cit, params))
            lines.append("")
        elif cl.get("citation_numbers"):
            lines.append(f"*Inline citation numbers:* {cl.get('citation_numbers')}")
            lines.append("")
    if shown == 0:
        lines.append("*No claims in this section under current filters.*")
        lines.append("")

    # Screener
    lines.append("## Document screener")
    lines.append("")
    if screener and isinstance(screener, dict):
        findings = _screener_findings(screener)
        n = 0
        for f in findings:
            sev = str(f.get("severity") or "")
            if params.drop_screener_info and sev == "info":
                continue
            n += 1
            dim = f.get("dimension", "?")
            quote = _trunc(str(f.get("quote") or ""), params.quote_max_chars)
            obs = _trunc(str(f.get("observation") or ""), params.observation_max_chars)
            sec = f.get("section") or "?"
            lines.append(f"- **{dim}** ({sev}) · *{sec}*")
            lines.append(f"  - Quote: {quote}")
            lines.append(f"  - Observation: {obs}")
            lines.append("")
        if n == 0:
            lines.append("*No screener findings (or all filtered out).*")
            lines.append("")
    else:
        lines.append("*screener.json not loaded.*")
        lines.append("")

    # Originality
    lines.append("## Originality (literature overlap)")
    lines.append("")
    if originality and isinstance(originality, dict):
        ow = originality.get("related_works_count")
        av = originality.get("avg_similarity_score")
        os_ = originality.get("originality_score")
        lines.append(
            f"**Originality score:** {os_} · **Related works retrieved:** {ow} · "
            f"**Avg similarity:** {av}",
        )
        lines.append("")
        rw = originality.get("related_works")
        if isinstance(rw, list) and rw:
            ranked = sorted(
                [x for x in rw if isinstance(x, dict)],
                key=lambda x: float(x.get("similarity_score") or 0.0),
                reverse=True,
            )
            top = ranked[: max(0, params.top_originality)]
            lines.append("| Rank | Similarity | Year | Title | DOI |")
            lines.append("|-----:|-----------:|------|-------|-----|")
            for i, w in enumerate(top, start=1):
                sim = w.get("similarity_score")
                year = w.get("year") or "?"
                title = _trunc(str(w.get("title") or ""), 72).replace("|", "\\|")
                doi = _short_doi(w.get("doi")).replace("|", "\\|")
                sim_s = f"{float(sim):.4f}" if isinstance(sim, (int, float)) else str(sim)
                lines.append(f"| {i} | {sim_s} | {year} | {title} | {doi} |")
            lines.append("")
        else:
            lines.append("*No related_works list.*")
            lines.append("")
    else:
        lines.append("*originality.json not present.*")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def shrink_params(p: BuildParams) -> BuildParams:
    """One step tighter for binary search / iterative trim."""
    return BuildParams(
        claim_max_chars=max(80, p.claim_max_chars - 40),
        ref_rationale_max_chars=max(0, p.ref_rationale_max_chars - 40),
        quote_max_chars=max(60, p.quote_max_chars - 30),
        observation_max_chars=max(80, p.observation_max_chars - 40),
        top_originality=max(3, p.top_originality - 2),
        include_self_reported=p.include_self_reported,
        drop_screener_info=True,
        drop_ref_rationale=True,
        drop_strong_moderate_claims=p.drop_strong_moderate_claims,
        provenance_compact=True,
    )


def build_under_budget(
    review: dict[str, Any],
    retrieve: dict[str, Any],
    screener: dict[str, Any] | None,
    originality: dict[str, Any] | None,
    max_bytes: int,
    initial: BuildParams,
    *,
    provenance_full_lines: list[str],
    provenance_compact_lines: list[str],
) -> tuple[str, BuildParams]:
    """Produce Markdown within max_bytes UTF-8 length."""

    def _prov(p: BuildParams) -> list[str] | None:
        if not provenance_full_lines:
            return None
        return provenance_compact_lines if p.provenance_compact else provenance_full_lines

    p = initial
    for phase in range(12):
        text = build_audit_markdown(
            review,
            retrieve,
            screener,
            originality,
            p,
            provenance_lines=_prov(p),
        )
        if len(text.encode("utf-8")) <= max_bytes:
            return text, p
        if phase == 0:
            p = BuildParams(
                claim_max_chars=p.claim_max_chars,
                ref_rationale_max_chars=p.ref_rationale_max_chars,
                quote_max_chars=p.quote_max_chars,
                observation_max_chars=p.observation_max_chars,
                top_originality=p.top_originality,
                include_self_reported=p.include_self_reported,
                drop_screener_info=p.drop_screener_info,
                drop_ref_rationale=True,
                drop_strong_moderate_claims=p.drop_strong_moderate_claims,
                provenance_compact=p.provenance_compact,
            )
            continue
        if phase == 1:
            p = BuildParams(
                claim_max_chars=p.claim_max_chars,
                ref_rationale_max_chars=p.ref_rationale_max_chars,
                quote_max_chars=p.quote_max_chars,
                observation_max_chars=p.observation_max_chars,
                top_originality=p.top_originality,
                include_self_reported=p.include_self_reported,
                drop_screener_info=True,
                drop_ref_rationale=True,
                drop_strong_moderate_claims=p.drop_strong_moderate_claims,
                provenance_compact=p.provenance_compact,
            )
            continue
        if phase == 2:
            p = BuildParams(
                claim_max_chars=p.claim_max_chars,
                ref_rationale_max_chars=p.ref_rationale_max_chars,
                quote_max_chars=p.quote_max_chars,
                observation_max_chars=p.observation_max_chars,
                top_originality=p.top_originality,
                include_self_reported=p.include_self_reported,
                drop_screener_info=True,
                drop_ref_rationale=True,
                drop_strong_moderate_claims=True,
                provenance_compact=p.provenance_compact,
            )
            continue
        p = shrink_params(p)

    text = build_audit_markdown(
        review,
        retrieve,
        screener,
        originality,
        p,
        provenance_lines=_prov(p),
    )
    return text, p


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write compact Markdown evidence audit (no LLM).",
    )
    parser.add_argument(
        "--directory",
        "-d",
        type=Path,
        default=None,
        help="Run directory containing review.json and intermediates",
    )
    parser.add_argument(
        "--review",
        type=Path,
        default=None,
        help="Path to review.json (default: <directory>/review.json)",
    )
    parser.add_argument(
        "--retrieve",
        type=Path,
        default=None,
        help="Path to retrieve_compare JSON (default: auto-detect in directory)",
    )
    parser.add_argument(
        "--screener",
        type=Path,
        default=None,
        help="Path to screener.json (default: <directory>/screener.json)",
    )
    parser.add_argument(
        "--originality",
        type=Path,
        default=None,
        help="Path to originality.json (default: <directory>/originality.json if exists)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output Markdown path (default: <directory>/evidence_audit.md)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Maximum UTF-8 size (default: {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--claim-max-chars",
        type=int,
        default=280,
        help="Max characters per claim text (default: 280)",
    )
    parser.add_argument(
        "--ref-rationale-max-chars",
        type=int,
        default=160,
        help="Max characters per reference support rationale (default: 160)",
    )
    parser.add_argument(
        "--quote-max-chars",
        type=int,
        default=200,
        help="Max characters per screener quote (default: 200)",
    )
    parser.add_argument(
        "--observation-max-chars",
        type=int,
        default=280,
        help="Max characters per screener observation (default: 280)",
    )
    parser.add_argument(
        "--top-originality",
        type=int,
        default=15,
        help="Max related works rows in table (default: 15)",
    )
    parser.add_argument(
        "--include-self-reported",
        action="store_true",
        help="Include self_reported / self_reported_method claims in detail (large)",
    )
    parser.add_argument(
        "--skip-provenance",
        action="store_true",
        help="Omit provenance / fingerprint section (smaller output)",
    )
    args = parser.parse_args()

    directory = args.directory
    if directory is None:
        directory = Path.cwd()
    directory = directory.expanduser().resolve()

    review_path = (args.review or directory / "review.json").expanduser().resolve()
    if not review_path.is_file():
        print(f"error: missing {review_path}", file=sys.stderr)
        sys.exit(1)

    retrieve_path = resolve_retrieve_path(directory, args.retrieve)
    screener_path = (args.screener or directory / "screener.json").expanduser().resolve()
    originality_path = (args.originality or directory / "originality.json").expanduser().resolve()
    out_path = (args.output or directory / "evidence_audit.md").expanduser().resolve()

    review = _load_json(review_path)
    retrieve = _load_json(retrieve_path)
    screener = _load_json(screener_path) if screener_path.is_file() else None
    originality = _load_json(originality_path) if originality_path.is_file() else None

    initial = BuildParams(
        claim_max_chars=args.claim_max_chars,
        ref_rationale_max_chars=args.ref_rationale_max_chars,
        quote_max_chars=args.quote_max_chars,
        observation_max_chars=args.observation_max_chars,
        top_originality=args.top_originality,
        include_self_reported=args.include_self_reported,
        drop_screener_info=False,
        drop_ref_rationale=False,
        drop_strong_moderate_claims=False,
        provenance_compact=False,
    )

    prov_full: list[str] = []
    prov_compact: list[str] = []
    if not args.skip_provenance:
        prov_full = _build_provenance_block(
            cwd=directory,
            review_path=review_path,
            retrieve_path=retrieve_path,
            screener_path=screener_path if screener_path.is_file() else None,
            originality_path=originality_path if originality_path.is_file() else None,
            output_path=out_path,
            compact=False,
        )
        prov_compact = _build_provenance_block(
            cwd=directory,
            review_path=review_path,
            retrieve_path=retrieve_path,
            screener_path=screener_path if screener_path.is_file() else None,
            originality_path=originality_path if originality_path.is_file() else None,
            output_path=out_path,
            compact=True,
        )

    text, used = build_under_budget(
        review,
        retrieve,
        screener,
        originality,
        args.max_bytes,
        initial,
        provenance_full_lines=prov_full,
        provenance_compact_lines=prov_compact,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    nbytes = len(text.encode("utf-8"))
    print(f"  Wrote {out_path} ({nbytes} bytes, max {args.max_bytes})")
    if used.drop_strong_moderate_claims or used.drop_screener_info or used.drop_ref_rationale:
        print(
            "  Note: budget trimming applied "
            f"(drop_ref_rationale={used.drop_ref_rationale}, "
            f"drop_screener_info={used.drop_screener_info}, "
            f"drop_strong_moderate_claims={used.drop_strong_moderate_claims}, "
            f"provenance_compact={used.provenance_compact})",
        )


if __name__ == "__main__":
    main()
