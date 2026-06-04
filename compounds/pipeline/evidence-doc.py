#!/usr/bin/env python3
"""Build Markdown evidence audits for compound reviews (no LLM).

**Combination mode:** ``--combination-bundle`` (``interactions.py`` output) plus optional
``--combo-review``. Writes ``evidence_audit.md`` beside the combo review.

**Single mode:** ``--data-dir`` + ``--compound`` plus optional ``--review``. Writes
``evidence_audit.md`` beside the single-compound review.
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

_PIPELINE = Path(__file__).resolve().parent
_COMPOUNDS = _PIPELINE.parent
_REPO_ROOT = _COMPOUNDS.parent
_MULTI = _PIPELINE / "multi"

if str(_COMPOUNDS) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS))
if str(_MULTI) not in sys.path:
    sys.path.insert(0, str(_MULTI))

from evidence_sources import (  # noqa: E402
    build_compound_pipeline_evidence,
    review_json_candidates,
)

GENERATOR_VERSION = "1.1"
DEFAULT_MAX_BYTES = 120 * 1024

@dataclass
class ComboParams:
    snippet_max_chars: int
    spl_excerpt_max_chars: int
    rationale_max_chars: int
    omit_mechanism_units: bool
    provenance_compact: bool


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def default_combo_review_path(bundle_path: Path) -> Path:
    steps = bundle_path.parent
    return steps.parent / "review" / "review.json"


def default_combo_evidence_audit_path(bundle_path: Path) -> Path:
    return default_combo_review_path(bundle_path).parent / "evidence_audit.md"


def default_single_review_path(data_dir: Path) -> Path:
    for path in review_json_candidates(data_dir):
        if path.is_file():
            return path
    return data_dir / "review" / "review.json"


def default_single_evidence_audit_path(review_path: Path) -> Path:
    return review_path.parent / "evidence_audit.md"


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


def _trunc(s: str, max_len: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _compound_evidence_artifacts(data_dir: Path) -> list[tuple[str, Path]]:
    """Modern per-compound artifacts for combo provenance (replaces grouped_by_stance)."""
    out: list[tuple[str, Path]] = []
    for filename, label in (
        ("material_tagged.jsonl", "material_tagged"),
        ("longevity.json", "longevity"),
        ("risk.json", "risk"),
    ):
        path = data_dir / filename
        if path.is_file():
            out.append((label, path))
    return out


def _build_combo_provenance_block(
    *,
    cwd: Path,
    bundle_path: Path,
    combo_review_path: Path | None,
    compound_artifacts: list[tuple[str, str, Path]],
    output_path: Path,
    compact: bool,
) -> list[str]:
    lines: list[str] = []
    lines.append("## Provenance")
    lines.append("")
    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_rev = _git_revision(cwd) or _git_revision(output_path.parent)
    model = (
        os.environ.get("REVIEWER_MODEL", "").strip()
        or os.environ.get("TAGGER_MODEL", "").strip()
        or "(unset)"
    )
    lines.append(f"- **Generated (UTC):** {gen_ts}")
    lines.append(f"- **Generator:** `compounds/pipeline/evidence-doc.py` v{GENERATOR_VERSION} (no LLM)")
    lines.append(f"- **Mode:** combination (interactions bundle + combo review)")
    lines.append(
        f"- **REVIEWER_MODEL / TAGGER_MODEL:** `{model}` "
        "*(models used by upstream review-multiple.py, when set)*",
    )
    lines.append(
        f"- **Git revision:** `{git_rev}` *(best-effort)*"
        if git_rev
        else "- **Git revision:** *(unavailable)*",
    )
    if not compact:
        lines.append("")
        lines.append("| Input | SHA-256 (first 16 hex) |")
        lines.append("|-------|-------------------------|")
        rows: list[tuple[str, Path | None]] = [
            ("interactions bundle", bundle_path),
            ("combo-review.json", combo_review_path),
        ]
        for compound_label, artifact_label, ap in compound_artifacts:
            rows.append((f"{artifact_label} ({compound_label})", ap))
        for label, pth in rows:
            if pth is None or not pth.is_file():
                lines.append(f"| {label} | — |")
            else:
                fp = _sha256_file_short(pth)
                lines.append(f"| {label} | `{fp or '?'}` |")
        lines.append("")
        lines.append(
            "*Fingerprints are of the files read at generation time; use them to verify this "
            "document matches a frozen artifact bundle.*",
        )
    else:
        b = _sha256_file_short(bundle_path)
        c = _sha256_file_short(combo_review_path) if combo_review_path else None
        lines.append(f"- **Fingerprints (short):** bundle=`{b or '?'}` combo_review=`{c or '?'}`")
    lines.append("")
    lines.append("### What this audit contains")
    lines.append("")
    lines.append(
        "Structured fields come from ``interactions.py`` (no LLM). Narrative paragraphs come "
        "from ``review-multiple.py`` (LLM). Per-compound stance/unit detail remains in each "
        "compound’s ``reviews/compounds/<TICKER>/steps/<compound>/`` artifacts when present.",
    )
    lines.append("")
    return lines


def _build_single_provenance_block(
    *,
    cwd: Path,
    data_dir: Path,
    compound: str,
    review_path: Path | None,
    output_path: Path,
    compact: bool,
) -> list[str]:
    lines: list[str] = []
    lines.append("## Provenance")
    lines.append("")
    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_rev = _git_revision(cwd) or _git_revision(output_path.parent)
    model = (
        os.environ.get("REVIEWER_MODEL", "").strip()
        or os.environ.get("TAGGER_MODEL", "").strip()
        or "(unset)"
    )
    lines.append(f"- **Generated (UTC):** {gen_ts}")
    lines.append(f"- **Generator:** `compounds/pipeline/evidence-doc.py` v{GENERATOR_VERSION} (no LLM)")
    lines.append(f"- **Mode:** single compound (`{compound}`)")
    lines.append(
        f"- **REVIEWER_MODEL / TAGGER_MODEL:** `{model}` "
        "*(models used by upstream run_review.py, when set)*",
    )
    lines.append(
        f"- **Git revision:** `{git_rev}` *(best-effort)*"
        if git_rev
        else "- **Git revision:** *(unavailable)*",
    )
    artifact_rows = [(label, path) for label, path in _compound_evidence_artifacts(data_dir)]
    if not compact:
        lines.append("")
        lines.append("| Input | SHA-256 (first 16 hex) |")
        lines.append("|-------|-------------------------|")
        rows: list[tuple[str, Path | None]] = [
            ("review.json", review_path),
        ]
        for artifact_label, ap in artifact_rows:
            rows.append((artifact_label, ap))
        for label, pth in rows:
            if pth is None or not pth.is_file():
                lines.append(f"| {label} | — |")
            else:
                fp = _sha256_file_short(pth)
                lines.append(f"| {label} | `{fp or '?'}` |")
        lines.append("")
        lines.append(
            "*Fingerprints are of the files read at generation time; use them to verify this "
            "document matches a frozen artifact bundle.*",
        )
    else:
        r = _sha256_file_short(review_path) if review_path else None
        lines.append(f"- **Fingerprints (short):** review=`{r or '?'}`")
    lines.append("")
    lines.append("### What this audit contains")
    lines.append("")
    lines.append(
        "Structured fields come from ``run_review.py`` pipeline artifacts (no LLM). "
        "Narrative paragraphs come from ``review.py`` (LLM).",
    )
    lines.append("")
    return lines


def _coverage_rows(coverage: Any) -> list[str]:
    if not isinstance(coverage, dict):
        return ["*No coverage object.*"]
    rows: list[str] = []
    for key in sorted(coverage.keys()):
        info = coverage[key]
        if not isinstance(info, dict):
            continue
        present = info.get("present")
        reason = info.get("reason")
        rows.append(f"- **{key}:** present={present}" + (f"; reason={reason}" if reason else ""))
    return rows or ["*Empty coverage.*"]


def _render_compound_evidence_section(
    ev: dict[str, Any],
    compound_name: str,
    params: ComboParams,
    *,
    heading_level: str = "###",
) -> list[str]:
    lines: list[str] = []
    lines.append(f"{heading_level} {compound_name}")
    lines.append("")
    lines.append(f"- **data_dir:** `{ev.get('data_dir', '?')}`")
    lines.append(f"- **found:** {ev.get('found')}")
    counts = ev.get("pipeline_counts")
    if isinstance(counts, dict):
        lines.append(
            f"- **pipeline rows:** longevity={counts.get('longevity_rows', '?')} · "
            f"risk={counts.get('risk_rows', '?')}",
        )
    warns = ev.get("warnings")
    if isinstance(warns, list) and warns:
        lines.append("- **warnings:**")
        for w in warns:
            lines.append(f"  - {w}")
    lines.append(
        f"- **scores (from review.json):** "
        f"scientific_grounding={ev.get('scientific_grounding_score')!s} · "
        f"risk={ev.get('risk_score')!s}",
    )
    lines.append("")
    lines.append("#### Coverage")
    lines.append("")
    lines.extend(_coverage_rows(ev.get("coverage")))
    lines.append("")

    kegg = ev.get("kegg")
    if isinstance(kegg, dict):
        flags = kegg.get("flags_present")
        if isinstance(flags, list) and flags:
            lines.append("**KEGG longevity flags present:** " + ", ".join(str(x) for x in flags))
        else:
            lines.append("**KEGG longevity flags present:** *(none)*")
        lines.append("")
    spl = ev.get("spl")
    if isinstance(spl, dict):
        lines.append("#### SPL (extracted text)")
        lines.append("")
        lines.append(f"- label_matched: `{spl.get('label_matched')}` — {spl.get('match_note', '')}")
        excerpts = spl.get("interaction_excerpts")
        if isinstance(excerpts, list) and excerpts:
            lines.append("- **interaction_excerpts** (truncated):")
            lines.append("")
            for i, ex in enumerate(excerpts[:5], start=1):
                lines.append(f"  {i}. {_trunc(str(ex), params.spl_excerpt_max_chars)}")
            if len(excerpts) > 5:
                lines.append(f"  *… {len(excerpts) - 5} more excerpt(s) omitted here.*")
            lines.append("")
        mech = spl.get("mechanism_excerpt")
        if mech:
            lines.append("- **mechanism_excerpt** (truncated):")
            lines.append("")
            lines.append(_trunc(str(mech), params.spl_excerpt_max_chars))
            lines.append("")

    if not params.omit_mechanism_units:
        for axis, key in (("longevity", "longevity_topic_summaries"), ("risk", "risk_topic_summaries")):
            summaries = ev.get(key)
            if isinstance(summaries, list) and summaries:
                lines.append(f"#### {axis} topic summaries (review stage 1)")
                lines.append("")
                for s in summaries[:6]:
                    if not isinstance(s, dict):
                        continue
                    label = s.get("topic_label") or s.get("topic_id") or "?"
                    lines.append(f"- **{label}**")
                    for b in (s.get("bullets") or [])[:4]:
                        lines.append(f"  - {_trunc(str(b), params.snippet_max_chars)}")
                lines.append("")

        longevity = ev.get("longevity_evidence") or ev.get("mechanism_units")
        if isinstance(longevity, list) and longevity:
            lines.append("#### Longevity evidence rows (longevity.json)")
            lines.append("")
            for u in longevity[:10]:
                if not isinstance(u, dict):
                    continue
                uid = u.get("unit_id", "?")
                ut = u.get("source_type") or "?"
                lr = u.get("longevity_relevance") or "?"
                sn_s = _trunc(str(u.get("snippet")), params.snippet_max_chars) if u.get("snippet") else "*(no snippet)*"
                lines.append(f"- **{uid}** ({ut}) · `{lr}` — {sn_s}")
            lines.append("")

        risk_ev = ev.get("risk_evidence")
        if isinstance(risk_ev, list) and risk_ev:
            lines.append("#### Risk evidence rows (risk.json)")
            lines.append("")
            for u in risk_ev[:8]:
                if not isinstance(u, dict):
                    continue
                uid = u.get("unit_id", "?")
                rr = u.get("risk_relevance") or "?"
                sn_s = _trunc(str(u.get("snippet")), params.snippet_max_chars) if u.get("snippet") else "*(no snippet)*"
                lines.append(f"- **{uid}** · `{rr}` — {sn_s}")
            lines.append("")

        iu_list = ev.get("interaction_evidence")
        if isinstance(iu_list, list) and iu_list:
            lines.append("#### Interaction-tagged risk rows (risk.json)")
            lines.append("")
            for i, iu in enumerate(iu_list, start=1):
                if not isinstance(iu, dict):
                    continue
                st = iu.get("source_type", "?")
                sn = iu.get("snippet")
                sn_s = _trunc(str(sn), params.snippet_max_chars) if sn else "*(no snippet)*"
                lines.append(f"- **interaction_{i}** ({st}) — {sn_s}")
            lines.append("")
    return lines


def _render_review_excerpts(
    review: dict[str, Any],
    params: ComboParams,
    *,
    section_title: str,
    category_keys: tuple[str, ...],
) -> list[str]:
    lines: list[str] = []
    lines.append(section_title)
    lines.append("")
    cats = review.get("categories")
    if isinstance(cats, dict):
        lines.append("| Category | Score / metric |")
        lines.append("|----------|------------------|")
        for cat_key in category_keys:
            block = cats.get(cat_key)
            if not isinstance(block, dict):
                continue
            sc = block.get("score")
            if isinstance(sc, (int, float)):
                metric = f"{float(sc):.2f}"
            else:
                metric = str(sc) if sc is not None else "—"
            lines.append(f"| {cat_key} | {metric} |")
        lines.append("")
        for key in category_keys:
            block = cats.get(key)
            if not isinstance(block, dict):
                continue
            rat = block.get("rationale")
            if not rat:
                continue
            lines.append(f"### {key}")
            lines.append("")
            lines.append(_trunc(str(rat), params.rationale_max_chars))
            lines.append("")
    stmt = review.get("review_statement")
    if stmt:
        lines.append("### review_statement")
        lines.append("")
        lines.append(_trunc(str(stmt), params.rationale_max_chars))
        lines.append("")
    return lines


def build_combo_audit_markdown(
    bundle: dict[str, Any],
    combo_review: dict[str, Any] | None,
    params: ComboParams,
    *,
    provenance_lines: list[str] | None = None,
) -> str:
    lines: list[str] = []
    name = str(bundle.get("combination_name") or "?")
    gen_at = bundle.get("generated_at", "?")
    review_date = combo_review.get("review_date", "?") if combo_review else "?"

    lines.append("# Evidence audit trail (combination)")
    lines.append("")
    lines.append(f"**Combination:** {name}  ")
    if bundle.get("ticker"):
        lines.append(f"**Ticker:** {bundle.get('ticker')}  ")
    if bundle.get("intervention"):
        lines.append(f"**Intervention:** {bundle.get('intervention')}  ")
    lines.append(f"**Bundle generated:** {gen_at}  ")
    lines.append(f"**Combo review date:** {review_date}  ")
    lines.append(f"**Compounds:** {bundle.get('compound_count', '?')}  ")
    lines.append("")
    lines.append(
        "*This file is the non-LLM audit for the **pairing** (bundle + cross-references). "
        "Evidence traces match the single-compound review pipeline: "
        "``longevity.json``, ``risk.json``, topic summaries, and ``review.json`` per compound.*",
    )
    lines.append("")
    if provenance_lines:
        lines.extend(provenance_lines)

    xr = bundle.get("cross_reference")
    lines.append("## Cross-reference (interactions.py)")
    lines.append("")
    if isinstance(xr, dict):
        note = xr.get("note")
        if note:
            lines.append(f"*{note}*")
            lines.append("")
        sp = xr.get("shared_pathways")
        if isinstance(sp, list) and sp:
            lines.append("**Shared KEGG longevity flags:** " + ", ".join(str(x) for x in sp))
        else:
            lines.append("**Shared KEGG longevity flags:** *(none)*")
        lines.append("")
        em = xr.get("explicit_mentions")
        n_em = len(em) if isinstance(em, list) else 0
        lines.append(f"**Explicit cross-mentions (token match):** {n_em}")
        lines.append("")
        sm = xr.get("spl_coverage_summary")
        if sm:
            lines.append(f"**SPL coverage:** {sm}")
            lines.append("")
    else:
        lines.append("*No cross_reference in bundle.*")
        lines.append("")

    compounds_map = bundle.get("compounds")
    if not isinstance(compounds_map, dict):
        compounds_map = {}

    lines.append("## Per-compound bundle trace")
    lines.append("")
    for cname in sorted(compounds_map.keys()):
        ev = compounds_map[cname]
        if not isinstance(ev, dict):
            continue
        lines.extend(_render_compound_evidence_section(ev, cname, params))
        lines.append("---")
        lines.append("")

    if combo_review:
        lines.extend(
            _render_review_excerpts(
                combo_review,
                params,
                section_title="## Combo review (verbatim excerpts from combo-review.json)",
                category_keys=("scientific_grounding", "risk_assessment", "compatibility"),
            ),
        )
    else:
        lines.append("## Combo review")
        lines.append("")
        lines.append("*combo-review.json not found — run review-multiple.py or pass ``--combo-review``.*")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_single_audit_markdown(
    ev: dict[str, Any],
    review: dict[str, Any] | None,
    compound: str,
    params: ComboParams,
    *,
    provenance_lines: list[str] | None = None,
) -> str:
    lines: list[str] = []
    review_date = review.get("review_date", "?") if review else "?"

    lines.append("# Evidence audit trail")
    lines.append("")
    lines.append(f"**Compound:** {compound}  ")
    lines.append(f"**Review date:** {review_date}  ")
    lines.append(f"**data_dir:** `{ev.get('data_dir', '?')}`  ")
    lines.append("")
    lines.append(
        "*Non-LLM audit of pipeline artifacts and review excerpts. "
        "Evidence traces: ``longevity.json``, ``risk.json``, topic summaries, and ``review.json``.*",
    )
    lines.append("")
    if provenance_lines:
        lines.extend(provenance_lines)

    lines.append("## Pipeline evidence trace")
    lines.append("")
    lines.extend(_render_compound_evidence_section(ev, compound, params, heading_level="###"))
    lines.append("")

    if review:
        lines.extend(
            _render_review_excerpts(
                review,
                params,
                section_title="## Review (verbatim excerpts from review.json)",
                category_keys=("scientific_grounding", "risk_assessment"),
            ),
        )
    else:
        lines.append("## Review")
        lines.append("")
        lines.append("*review.json not found — run run_review.py or pass ``--review``.*")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def shrink_combo_params(p: ComboParams) -> ComboParams:
    return ComboParams(
        snippet_max_chars=max(80, p.snippet_max_chars - 60),
        spl_excerpt_max_chars=max(200, p.spl_excerpt_max_chars - 150),
        rationale_max_chars=max(800, p.rationale_max_chars - 800),
        omit_mechanism_units=True,
        provenance_compact=True,
    )


def build_combo_under_budget(
    bundle: dict[str, Any],
    combo_review: dict[str, Any] | None,
    max_bytes: int,
    initial: ComboParams,
    *,
    provenance_full_lines: list[str],
    provenance_compact_lines: list[str],
) -> tuple[str, ComboParams]:
    def _prov(p: ComboParams) -> list[str] | None:
        if not provenance_full_lines:
            return None
        return provenance_compact_lines if p.provenance_compact else provenance_full_lines

    p = initial
    for _ in range(18):
        text = build_combo_audit_markdown(bundle, combo_review, p, provenance_lines=_prov(p))
        if len(text.encode("utf-8")) <= max_bytes:
            return text, p
        p = shrink_combo_params(p)
    text = build_combo_audit_markdown(bundle, combo_review, p, provenance_lines=_prov(p))
    return text, p


def build_single_under_budget(
    ev: dict[str, Any],
    review: dict[str, Any] | None,
    compound: str,
    max_bytes: int,
    initial: ComboParams,
    *,
    provenance_full_lines: list[str],
    provenance_compact_lines: list[str],
) -> tuple[str, ComboParams]:
    def _prov(p: ComboParams) -> list[str] | None:
        if not provenance_full_lines:
            return None
        return provenance_compact_lines if p.provenance_compact else provenance_full_lines

    p = initial
    for _ in range(18):
        text = build_single_audit_markdown(
            ev, review, compound, p, provenance_lines=_prov(p),
        )
        if len(text.encode("utf-8")) <= max_bytes:
            return text, p
        p = shrink_combo_params(p)
    text = build_single_audit_markdown(ev, review, compound, p, provenance_lines=_prov(p))
    return text, p


def main_combination(args: argparse.Namespace) -> int:
    bundle_path = args.combination_bundle.expanduser().resolve()
    if not bundle_path.is_file():
        print(f"error: missing bundle {bundle_path}", file=sys.stderr)
        return 1

    bundle = _load_json(bundle_path)
    combo_name = str(bundle.get("combination_name") or "?")

    combo_review_path = (
        args.combo_review.expanduser().resolve()
        if args.combo_review
        else default_combo_review_path(bundle_path)
    )
    combo_review: dict[str, Any] | None = (
        _load_json(combo_review_path) if combo_review_path.is_file() else None
    )

    out_path = (
        args.output.expanduser().resolve()
        if args.output
        else default_combo_evidence_audit_path(bundle_path)
    )

    compounds_map = bundle.get("compounds")
    compound_artifacts: list[tuple[str, str, Path]] = []
    if isinstance(compounds_map, dict):
        for cname in sorted(compounds_map.keys()):
            ev = compounds_map[cname]
            if not isinstance(ev, dict):
                continue
            dd = ev.get("data_dir")
            if isinstance(dd, str):
                data_dir = Path(dd).resolve() if Path(dd).is_absolute() else Path(dd).resolve()
                for artifact_label, path in _compound_evidence_artifacts(data_dir):
                    compound_artifacts.append((cname, artifact_label, path))

    cwd = bundle_path.parent
    prov_full: list[str] = []
    prov_compact: list[str] = []
    if not args.skip_provenance:
        prov_full = _build_combo_provenance_block(
            cwd=cwd,
            bundle_path=bundle_path,
            combo_review_path=combo_review_path if combo_review_path.is_file() else None,
            compound_artifacts=compound_artifacts,
            output_path=out_path,
            compact=False,
        )
        prov_compact = _build_combo_provenance_block(
            cwd=cwd,
            bundle_path=bundle_path,
            combo_review_path=combo_review_path if combo_review_path.is_file() else None,
            compound_artifacts=compound_artifacts,
            output_path=out_path,
            compact=True,
        )

    initial = ComboParams(
        snippet_max_chars=args.excerpt_max_chars,
        spl_excerpt_max_chars=min(2000, max(400, args.excerpt_max_chars * 3)),
        rationale_max_chars=args.rationale_max_chars,
        omit_mechanism_units=False,
        provenance_compact=False,
    )

    text, used = build_combo_under_budget(
        bundle,
        combo_review,
        args.max_bytes,
        initial,
        provenance_full_lines=prov_full,
        provenance_compact_lines=prov_compact,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    nbytes = len(text.encode("utf-8"))
    print(f"  Wrote {out_path} ({nbytes} bytes, max {args.max_bytes})")
    if used.omit_mechanism_units or used.provenance_compact:
        print(
            "  Note: budget trimming applied "
            f"(omit_mechanism_units={used.omit_mechanism_units}, "
            f"provenance_compact={used.provenance_compact})",
        )
    if combo_review is None:
        print(
            f"  Note: combo review not found at {combo_review_path}; audit is bundle-only.",
            file=sys.stderr,
        )
    return 0


def main_single(args: argparse.Namespace) -> int:
    data_dir = args.data_dir.expanduser().resolve()
    if not data_dir.is_dir():
        print(f"error: missing data directory {data_dir}", file=sys.stderr)
        return 1

    compound = args.compound.strip()
    review_path = (
        args.review.expanduser().resolve()
        if args.review
        else default_single_review_path(data_dir)
    )
    review: dict[str, Any] | None = (
        _load_json(review_path) if review_path.is_file() else None
    )

    out_path = (
        args.output.expanduser().resolve()
        if args.output
        else default_single_evidence_audit_path(review_path)
    )

    ev = build_compound_pipeline_evidence(compound, data_dir)

    cwd = data_dir
    prov_full: list[str] = []
    prov_compact: list[str] = []
    if not args.skip_provenance:
        prov_full = _build_single_provenance_block(
            cwd=cwd,
            data_dir=data_dir,
            compound=compound,
            review_path=review_path if review_path.is_file() else None,
            output_path=out_path,
            compact=False,
        )
        prov_compact = _build_single_provenance_block(
            cwd=cwd,
            data_dir=data_dir,
            compound=compound,
            review_path=review_path if review_path.is_file() else None,
            output_path=out_path,
            compact=True,
        )

    initial = ComboParams(
        snippet_max_chars=args.excerpt_max_chars,
        spl_excerpt_max_chars=min(2000, max(400, args.excerpt_max_chars * 3)),
        rationale_max_chars=args.rationale_max_chars,
        omit_mechanism_units=False,
        provenance_compact=False,
    )

    text, used = build_single_under_budget(
        ev,
        review,
        compound,
        args.max_bytes,
        initial,
        provenance_full_lines=prov_full,
        provenance_compact_lines=prov_compact,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    nbytes = len(text.encode("utf-8"))
    print(f"  Wrote {out_path} ({nbytes} bytes, max {args.max_bytes})")
    if used.omit_mechanism_units or used.provenance_compact:
        print(
            "  Note: budget trimming applied "
            f"(omit_mechanism_units={used.omit_mechanism_units}, "
            f"provenance_compact={used.provenance_compact})",
        )
    if review is None:
        print(
            f"  Note: review not found at {review_path}; audit is pipeline-only.",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--combination-bundle",
        type=Path,
        metavar="BUNDLE.json",
        help="Interactions evidence bundle (combination mode).",
    )
    mode.add_argument(
        "--data-dir",
        type=Path,
        metavar="DIR",
        help="Steps directory with longevity.json etc. (single-compound mode).",
    )
    ap.add_argument(
        "--compound",
        help="Compound name (required with --data-dir).",
    )
    ap.add_argument(
        "--combo-review",
        type=Path,
        default=None,
        help="Path to combo review.json (combination mode).",
    )
    ap.add_argument(
        "--review",
        type=Path,
        default=None,
        help="Path to single review.json (single mode; default: beside --data-dir).",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output Markdown (default: evidence_audit.md beside review).",
    )
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--excerpt-max-chars", type=int, default=400)
    ap.add_argument("--rationale-max-chars", type=int, default=8000)
    ap.add_argument("--skip-provenance", action="store_true")
    args = ap.parse_args()

    if args.combination_bundle is not None:
        return main_combination(args)
    if not args.compound or not args.compound.strip():
        print("error: --compound is required with --data-dir", file=sys.stderr)
        return 1
    return main_single(args)


if __name__ == "__main__":
    raise SystemExit(main())
