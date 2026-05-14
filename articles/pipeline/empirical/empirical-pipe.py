#!/usr/bin/env python3
"""
Run the full article through evidence review and unified scoring.

Upstream (claim extraction, same as run_pipe2 steps 1-5):

  1. spaCy tagging
  2. LLM claim extraction
  3. LLM validation
  4. classify_claims
  5. group -> grouped.json

Empirical folder (steps 6-13):

  6. triage, 7. retrieve_compare, 8. prep, 9. review,
  10. originality_check, 11. screener, 12. score, 13. evidence-doc

Input directory (--input-dir) must contain:

  - full.md

The knowledge base JSONL may live in the same folder **or** one level up (e.g.
``articles/data/text_knowledge_base.jsonl`` with ``document (10)/full.md``).
Use ``--kb`` to point at ``text_knowledge_base.jsonl`` explicitly.

Optional for resume (--from-step >= 6): grouped.json in the input dir (copied if
the run folder does not already have it under steps/).

All pipeline intermediates (KB copy, claims, triaged, retrieve_compare, etc.)
live under:

  <output-dir>/<research-folder>/steps/

Published artifacts (after scoring and audit export):

  <output-dir>/<research-folder>/output/review.json
  <output-dir>/<research-folder>/output/overview.json   (from score.py when LLM enabled)
  <output-dir>/<research-folder>/output/evidence_audit.md

The folder name uses the paper title from the KB (profile_read_paper heuristic),
not the PDF filename.

Environment: VLLM_BASE_URL, VLLM_API_KEY; --model sets VALIDATOR_MODEL.

Example:
  python empirical-pipe.py --input-dir "../../../articles/data/document (10)"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_EMPIRICAL = Path(__file__).resolve().parent
_PIPELINE = _EMPIRICAL.parent
_CLAIM_EXTRACT = _PIPELINE / "claim-extract"
_REPO_ROOT = _PIPELINE.parent.parent
DEFAULT_OUTPUT_DIR = _REPO_ROOT / "articles" / "data"
MAPPINGS = _PIPELINE / "mappings.json"
PY = sys.executable

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _safe_research_name(name: str) -> str:
    """Filesystem-safe folder name (Windows-friendly)."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" ._-") or "research"
    if len(safe) > 120:
        safe = safe[:120]
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe


def _import_profile_helpers():
    """Load extract_title / load_chunks from profile_read_paper (same dir as empirical/)."""
    if str(_PIPELINE) not in sys.path:
        sys.path.insert(0, str(_PIPELINE))
    from profile_read_paper import extract_title, load_chunks  # noqa: PLC0415

    return extract_title, load_chunks


def _first_doc_name_from_kb(kb_path: Path) -> str:
    with kb_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            dn = rec.get("doc_name")
            if dn:
                return str(dn).strip()
    return ""


def _first_doc_name_from_grouped(grouped_path: Path) -> str:
    data = json.loads(grouped_path.read_text(encoding="utf-8"))
    for dim_data in data.values():
        if not isinstance(dim_data, dict):
            continue
        for m in dim_data.get("members") or []:
            if not isinstance(m, dict):
                continue
            dn = m.get("doc_name")
            if dn:
                return str(dn).strip()
    return ""


def _title_from_fullmd(fullmd_path: Path) -> str | None:
    text = fullmd_path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# ") and len(line) > 3:
            title = line[2:].strip()
            if title:
                return title
    return None


def resolve_run_folder_name(
    input_dir: Path,
    kb_path: Path,
    grouped_path: Path | None,
    fullmd_path: Path,
) -> str:
    """Paper title from KB chunks (profile heuristic), else full.md H1, else folder name."""
    extract_title, load_chunks = _import_profile_helpers()
    if grouped_path and grouped_path.is_file():
        doc_name = _first_doc_name_from_grouped(grouped_path) or _first_doc_name_from_kb(kb_path)
    else:
        doc_name = _first_doc_name_from_kb(kb_path) or input_dir.name
    chunks = load_chunks(kb_path, doc_name)
    title = extract_title(chunks) if chunks else None
    if title and str(title).strip():
        raw = str(title).strip()
    else:
        raw = _title_from_fullmd(fullmd_path) or input_dir.name
    return _safe_research_name(raw)


def _unique_run_dir(base: Path) -> Path:
    if not base.exists():
        return base
    stem = base.name
    parent = base.parent
    for n in range(2, 1000):
        cand = parent / f"{stem}_{n}"
        if not cand.exists():
            return cand
    raise FileExistsError(f"could not allocate unique folder under {parent}")


def resolve_text_knowledge_base(input_dir: Path, kb_arg: Path | None) -> Path:
    """Locate text_knowledge_base.jsonl: --kb, then input-dir, then parent of input-dir."""
    if kb_arg is not None:
        p = kb_arg.expanduser().resolve()
        if not p.is_file():
            print(f"error: --kb not a file: {p}", file=sys.stderr)
            sys.exit(1)
        return p
    candidates = [
        input_dir / "text_knowledge_base.jsonl",
        input_dir.parent / "text_knowledge_base.jsonl",
    ]
    for c in candidates:
        c = c.resolve()
        if c.is_file():
            return c
    print("error: text_knowledge_base.jsonl not found. Tried:", file=sys.stderr)
    for c in candidates:
        print(f"  - {c}", file=sys.stderr)
    print("  Pass --kb PATH to your JSONL.", file=sys.stderr)
    sys.exit(1)


def run_step(label: str, cmd: list[str], *, env: dict | None = None) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  cmd: {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"  FAILED (exit {result.returncode})")
        sys.exit(result.returncode)
    print("  OK")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full pipeline: spaCy to claims, classify/group, empirical review, score.",
    )
    parser.add_argument(
        "--input-dir",
        "--input-pdf-dir",
        type=Path,
        required=True,
        metavar="DIR",
        help=(
            "Folder with full.md (and optionally grouped.json). "
            "text_knowledge_base.jsonl can be here or in the parent directory; "
            "override with --kb."
        ),
    )
    parser.add_argument(
        "--kb",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to text_knowledge_base.jsonl (skips auto-discovery in input-dir / parent)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Parent directory for the run folder (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--from-step",
        type=int,
        default=1,
        choices=range(1, 14),
        help=(
            "Start here (1=spacy ... 5=group, 6=triage, 7=retrieve_compare, 8=prep, "
            "9=review, 10=originality, 11=screener, 12=score, 13=evidence-doc)"
        ),
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.environ.get("VALIDATOR_MODEL", "/model"),
        help="LLM model id for vLLM-compatible API (sets VALIDATOR_MODEL; default: env or /model)",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip LLM in retrieve_compare, originality_check, screener, and score",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Use output path even if it already exists (may mix runs)",
    )
    args = parser.parse_args()

    input_dir = args.input_dir.expanduser().resolve()
    output_parent = args.output_dir.expanduser().resolve()
    start = args.from_step

    kb_src = resolve_text_knowledge_base(input_dir, args.kb)
    fullmd_src = input_dir / "full.md"
    grouped_src = input_dir / "grouped.json"

    if not fullmd_src.is_file():
        print(f"error: missing full.md: {fullmd_src}", file=sys.stderr)
        sys.exit(1)

    grouped_for_title = grouped_src if grouped_src.is_file() else None
    folder_key = resolve_run_folder_name(input_dir, kb_src, grouped_for_title, fullmd_src)
    run_dir = output_parent / folder_key
    if not args.overwrite:
        run_dir = _unique_run_dir(run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    steps_dir = run_dir / "steps"
    output_dir = run_dir / "output"
    steps_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    kb_dest = steps_dir / "text_knowledge_base.jsonl"
    full_md = steps_dir / "full.md"
    grouped = steps_dir / "grouped.json"

    if not kb_dest.is_file() or start == 1:
        shutil.copy2(kb_src, kb_dest)
    if not full_md.is_file() or start == 1:
        shutil.copy2(fullmd_src, full_md)

    if start >= 6 and not grouped.is_file():
        if grouped_src.is_file():
            shutil.copy2(grouped_src, grouped)
        else:
            print(
                "error: --from-step >= 6 requires grouped.json in steps/ or under --input-dir",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Research folder name (from pipeline title heuristic): {folder_key}")
    print(f"Run directory: {run_dir}")
    print(f"Using knowledge base: {kb_src}")

    base_env = {**os.environ, "VALIDATOR_MODEL": args.model}
    ce_env = {**base_env, "CLAIM_EXTRACT_DATA_DIR": str(steps_dir.resolve())}
    use_llm = not args.skip_llm

    # --- Steps 1–5 (claim-extract + classify + group) ---
    if start <= 1:
        _spacy_in = _CLAIM_EXTRACT / "text_knowledge_base.jsonl"
        _spacy_out = _CLAIM_EXTRACT / "test_output_tagged.jsonl"
        shutil.copy2(kb_dest, _spacy_in)
        run_step(
            "Step 1/13 — spaCy tagging",
            [PY, str(_CLAIM_EXTRACT / "spacy_test.py")],
            env=base_env,
        )
        shutil.move(str(_spacy_out), str(steps_dir / "test_output_tagged.jsonl"))
        _spacy_in.unlink(missing_ok=True)

    if start <= 2:
        run_step(
            "Step 2/13 — LLM claim extraction",
            [PY, str(_CLAIM_EXTRACT / "LLM_extract.py")],
            env=ce_env,
        )

    if start <= 3:
        run_step(
            "Step 3/13 — LLM validation",
            [PY, str(_CLAIM_EXTRACT / "claim_validator.py")],
            env=ce_env,
        )

    validated = steps_dir / "validated_claims.jsonl"
    classified = steps_dir / "classified_claims.jsonl"
    if start <= 4:
        run_step(
            "Step 4/13 — Classify claims",
            [
                PY,
                str(_PIPELINE / "classify_claims.py"),
                "-i",
                str(validated),
                "-o",
                str(classified),
            ],
            env=base_env,
        )

    if start <= 5:
        run_step(
            "Step 5/13 — Group by dimension",
            [
                PY,
                str(_PIPELINE / "group.py"),
                str(classified),
                "-o",
                str(grouped),
                "--mappings",
                str(MAPPINGS),
            ],
            env=base_env,
        )

    # --- Steps 6–13 (empirical/) ---
    triaged = steps_dir / "triaged.json"
    if start <= 6:
        run_step(
            "Step 6/13 — Triage",
            [
                PY,
                str(_EMPIRICAL / "triage.py"),
                str(grouped),
                "-o",
                str(triaged),
                "--mappings",
                str(MAPPINGS),
            ],
            env=base_env,
        )

    rc_out = steps_dir / ("retrieve_compare_llm.json" if use_llm else "retrieve_compare_out.json")
    if start <= 7:
        rc_cmd = [
            PY,
            str(_EMPIRICAL / "retrieve_compare.py"),
            str(triaged),
            "--kb",
            str(kb_dest),
            "--fullmd",
            str(full_md),
            "--openalex-cache",
            str(steps_dir / "openalex_cache.json"),
            "-o",
            str(rc_out),
        ]
        if not use_llm:
            rc_cmd.append("--skip-llm")
        run_step(
            f"Step 7/13 — Retrieve & compare ({'LLM' if use_llm else 'skip-llm'})",
            rc_cmd,
            env=base_env,
        )

    prepped_evidence = steps_dir / "prepped_evidence.json"
    if start <= 8:
        run_step(
            "Step 8/13 — Prep evidence narratives",
            [PY, str(_EMPIRICAL / "prep.py"), str(rc_out), "-o", str(prepped_evidence)],
            env=base_env,
        )

    review_out = output_dir / "review.json"
    if start <= 9:
        run_step(
            "Step 9/13 — Review (rationales)",
            [
                PY,
                str(_EMPIRICAL / "review.py"),
                str(prepped_evidence),
                "--mappings",
                str(MAPPINGS),
                "-o",
                str(review_out),
                "--pre-condensed-dump",
                str(steps_dir / "pre_condensed_rationales.json"),
            ],
            env=base_env,
        )

    originality_out = steps_dir / "originality.json"
    if start <= 10:
        originality_cmd = [
            PY,
            str(_EMPIRICAL / "originality_check.py"),
            "--directory",
            str(steps_dir),
            "--fullmd",
            str(full_md),
            "--kb",
            str(kb_dest),
            "--openalex-cache",
            str(steps_dir / "originality_openalex_cache.json"),
            "-o",
            str(originality_out),
            "--review",
            str(review_out),
        ]
        if args.skip_llm:
            originality_cmd.append("--skip-llm")
        run_step(
            f"Step 10/13 — Originality ({'LLM' if use_llm else 'skip-llm'})",
            originality_cmd,
            env=base_env,
        )

    screener_out = steps_dir / "screener.json"
    if start <= 11:
        screener_cmd = [
            PY,
            str(_EMPIRICAL / "screener.py"),
            "--fullmd",
            str(full_md),
            "--openalex-cache",
            str(steps_dir / "openalex_cache.json"),
            "--mappings",
            str(MAPPINGS),
            "--review",
            str(review_out),
            "-o",
            str(screener_out),
        ]
        if args.skip_llm:
            screener_cmd.append("--skip-llm")
        run_step(
            f"Step 11/13 — Screener ({'LLM' if use_llm else 'skip-llm'})",
            screener_cmd,
            env=base_env,
        )

    if start <= 12:
        score_cmd = [
            PY,
            str(_EMPIRICAL / "score.py"),
            "--review",
            str(review_out),
            "--prepped-evidence",
            str(prepped_evidence),
            "--originality",
            str(originality_out),
            "--screener",
            str(screener_out),
            "--mappings",
            str(MAPPINGS),
            "-o",
            str(review_out),
        ]
        if args.skip_llm:
            score_cmd.append("--skip-llm")
        run_step(
            f"Step 12/13 — Unified score ({'LLM' if use_llm else 'skip-llm'})",
            score_cmd,
            env=base_env,
        )

    if start <= 13:
        run_step(
            "Step 13/13 — Evidence audit document",
            [
                PY,
                str(_EMPIRICAL / "evidence-doc.py"),
                "--directory",
                str(steps_dir),
                "--review",
                str(review_out),
                "--retrieve",
                str(rc_out),
                "--screener",
                str(screener_out),
                "--originality",
                str(originality_out),
                "-o",
                str(output_dir / "evidence_audit.md"),
            ],
            env=base_env,
        )

    print(f"\n{'='*60}")
    print("  PIPELINE COMPLETE")
    print(f"  Run directory: {run_dir}")
    print(f"  Intermediates: {steps_dir}")
    print(f"  Published:     {output_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
