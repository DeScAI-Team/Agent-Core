#!/usr/bin/env python3
"""Run the compound material -> filtered evidence -> review pipeline.

Artifacts under ``reviews/compounds/<TICKER>/steps/`` (intermediates) and
``reviews/compounds/<TICKER>/review/`` (published review + evidence).

Steps:
  1. discover.py (incremental) → material.json + delta-tag → longevity.json / risk.json
  2. topic_grouper.py → longevity_groups.json + risk_groups.json
  3. review.py     → review.json (default: ``review/review.json`` under run root)
  4. overview.py   → overview.json (plain-language copy of review text; same scores)
  5. evidence-doc.py → evidence_audit.md (non-LLM audit beside review)

With --skip-discover, step 1 is tag-group-filter only (no incremental discover).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_PUMP_SCIENCE_DIR = Path(__file__).resolve().parent
_COMPOUNDS_DIR = _PUMP_SCIENCE_DIR.parent.parent

if str(_COMPOUNDS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS_DIR))
from token_lookup import bootstrap_run_dirs, resolve_ticker  # noqa: E402


def _run(label: str, cmd: list[str | Path]) -> None:
    str_cmd = [str(c) for c in cmd]
    print(f"\n[{label}] {' '.join(str_cmd)}", flush=True)
    result = subprocess.run(str_cmd)
    if result.returncode != 0:
        print(f"\n[{label}] FAILED (exit code {result.returncode})", file=sys.stderr, flush=True)
        raise SystemExit(result.returncode)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the material JSONL review pipeline for a compound.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--compound", required=True, help="Compound name to review.")
    ap.add_argument("--run-root", type=Path, default=None, metavar="DIR")
    ap.add_argument(
        "--steps-dir",
        "--compound-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory with material.json and step outputs (default: reviews/compounds/<ticker>/steps).",
    )
    ap.add_argument("--model", default=None, metavar="NAME")
    ap.add_argument("--skip-risk", action="store_true")
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--review-output", type=Path, default=None, metavar="PATH")
    ap.add_argument("--skip-overview", action="store_true", help="Skip plain-language overview.json step.")
    args = ap.parse_args()

    compound = args.compound.strip()

    if args.run_root is not None:
        run_root = args.run_root.expanduser().resolve()
        (run_root / "steps").mkdir(parents=True, exist_ok=True)
        (run_root / "review").mkdir(parents=True, exist_ok=True)
    else:
        ticker = resolve_ticker([compound])
        run_root = bootstrap_run_dirs(ticker)

    steps_dir = (
        args.steps_dir.expanduser().resolve()
        if args.steps_dir is not None
        else (run_root / "steps").resolve()
    )
    steps_dir.mkdir(parents=True, exist_ok=True)
    review_dir = run_root / "review"
    review_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    _PIPELINE_DIR = _PUMP_SCIENCE_DIR.parent
    base_steps = 3 if not args.skip_discover else 4
    n_steps = base_steps + 1 if args.skip_overview else base_steps + 2

    def lab(step: int) -> str:
        return f"{step}/{n_steps}"

    step_num = 1
    longevity_path = steps_dir / "longevity.json"
    risk_path = steps_dir / "risk.json"
    tagged_path = steps_dir / "material_tagged.jsonl"

    if args.skip_discover:
        from discover_lib.material import (  # noqa: E402
            MATERIAL_FILENAME,
            ensure_material_json,
            find_discover_source,
        )

        source = find_discover_source(steps_dir)
        if source is None:
            print(f"--skip-discover: no material.json or report_*.json found in {steps_dir}", file=sys.stderr)
            return 1
        material_path = ensure_material_json(steps_dir, compound) or source
        if material_path.name == MATERIAL_FILENAME and source.name.startswith("report_"):
            print(
                f"\n[{lab(step_num)} discover] Skipped — converted {source.name} -> material.json",
                flush=True,
            )
        else:
            print(f"\n[{lab(step_num)} discover] Skipped — using existing: {material_path}", flush=True)
    else:
        material_path = steps_dir / "material.json"
        discover_cmd: list[str | Path] = [
            py,
            _PUMP_SCIENCE_DIR / "discover.py",
            "--compound",
            compound,
            "--compound-dir",
            str(steps_dir),
            "--incremental",
            "--output",
            str(material_path),
        ]
        if args.model:
            discover_cmd += ["--model", args.model]
        if not args.skip_risk:
            discover_cmd.append("--include-risk-severity")
        _run(f"{lab(step_num)} discover", discover_cmd)
    step_num += 1

    if args.skip_discover:
        tag_cmd: list[str | Path] = [
            py,
            _PUMP_SCIENCE_DIR / "tag-group-filter.py",
            str(material_path),
            "--out-dir",
            str(steps_dir),
            "--tagged-output",
            str(tagged_path),
        ]
        if not args.skip_risk:
            tag_cmd.append("--include-risk-severity")
        if args.model:
            tag_cmd += ["--model", args.model]
        _run(f"{lab(step_num)} tag-group-filter", tag_cmd)
        step_num += 1

    longevity_groups_path = steps_dir / "longevity_groups.json"
    risk_groups_path = steps_dir / "risk_groups.json"
    _run(
        f"{lab(step_num)} topic-grouper",
        [py, _PUMP_SCIENCE_DIR / "topic_grouper.py", str(steps_dir)],
    )
    step_num += 1

    review_cmd: list[str | Path] = [
        py, _PUMP_SCIENCE_DIR / "review.py", str(longevity_path),
        "--compound", compound,
        "--risk", str(risk_path),
        "--longevity-groups", str(longevity_groups_path),
        "--risk-groups", str(risk_groups_path),
        "--run-root", str(run_root),
    ]
    if args.model:
        review_cmd += ["--model", args.model]
    if args.review_output is not None:
        review_cmd += ["-o", str(args.review_output.expanduser().resolve())]
    _run(f"{lab(step_num)} review", review_cmd)
    step_num += 1

    review_path = args.review_output.expanduser().resolve() if args.review_output else review_dir / "review.json"
    overview_path = review_path.parent / "overview.json"

    if not args.skip_overview:
        overview_cmd: list[str | Path] = [
            py,
            _PIPELINE_DIR / "overview.py",
            str(review_path),
            "-o",
            str(overview_path),
        ]
        if args.model:
            overview_cmd += ["--model", args.model]
        _run(f"{lab(step_num)} overview", overview_cmd)
        step_num += 1

    evidence_out = review_path.parent / "evidence_audit.md"
    evidence_cmd: list[str | Path] = [
        py,
        _PIPELINE_DIR / "evidence-doc.py",
        "--data-dir",
        str(steps_dir),
        "--compound",
        compound,
        "--review",
        str(review_path),
        "-o",
        str(evidence_out),
    ]
    _run(f"{lab(step_num)} evidence-audit", evidence_cmd)

    print("\nPipeline complete.", flush=True)
    print(f"Review: {review_path}", flush=True)
    if not args.skip_overview:
        print(f"Overview: {overview_path}", flush=True)
    print(f"Evidence audit: {evidence_out}", flush=True)
    print(f"Steps: {steps_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
