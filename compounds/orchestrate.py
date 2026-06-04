#!/usr/bin/env python3
"""Pump-science review orchestrator.

Single compound  →  run_review.py (discover → … → review → overview)

Multiple compounds → run_review.py per compound under ``steps/<compound>/`` (unless
``--skip-individual``), then ``interactions.py``, ``review-multiple.py``, ``overview.py``, and
combination ``evidence-doc.py``. All outputs under ``reviews/compounds/<TICKER>/{review,steps}/``.

Usage:
  python orchestrate.py --compounds Doxycycline
  python orchestrate.py --compounds Omipalisib "Ginsenoside Rh2" "Urolithin A"
  python orchestrate.py --compounds Omipalisib "Ginsenoside Rh2" --skip-individual
  python orchestrate.py --compounds Omipalisib --skip-discover
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent

if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
from token_lookup import (  # noqa: E402
    bootstrap_run_dirs,
    bundle_filename,
    compound_steps_dir,
    resolve_ticker,
    review_output_dir,
    steps_dir as token_steps_dir,
)


def _run(label: str, cmd: list, *, cwd: Path | None = None) -> None:
    print(f"\n[{label}]", flush=True)
    kwargs: dict = {}
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    result = subprocess.run([str(c) for c in cmd], **kwargs)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compounds", nargs="+", required=True, metavar="COMPOUND")
    ap.add_argument("--model", default=None)
    ap.add_argument("--skip-risk", action="store_true")
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--skip-individual", action="store_true", help="Multi only: skip per-compound pipelines.")
    ap.add_argument("--skip-overview", action="store_true", help="Skip plain-language overview.json step.")
    ap.add_argument(
        "--tokens-file",
        type=Path,
        default=None,
        help="Override compound-tokens.json path.",
    )
    args = ap.parse_args()

    py = sys.executable
    compounds = [c.strip() for c in args.compounds if c.strip()]
    tokens_path = args.tokens_file.expanduser().resolve() if args.tokens_file else None

    ticker = resolve_ticker(compounds, tokens_path=tokens_path)
    run_root = bootstrap_run_dirs(ticker)
    steps_root = token_steps_dir(ticker)
    review_dir = review_output_dir(ticker)

    print(f"Ticker: {ticker}", flush=True)
    print(f"Run root: {run_root}", flush=True)

    def review_flags() -> list:
        f = ["--run-root", str(run_root)]
        if args.model:
            f += ["--model", args.model]
        if args.skip_risk:
            f.append("--skip-risk")
        if args.skip_discover:
            f.append("--skip-discover")
        if args.skip_overview:
            f.append("--skip-overview")
        return f

    if len(compounds) == 1:
        _run(
            f"review: {compounds[0]}",
            [
                py,
                _DIR / "pipeline" / "single" / "run_review.py",
                "--compound",
                compounds[0],
                "--steps-dir",
                str(steps_root),
            ]
            + review_flags(),
        )
    else:
        if not args.skip_individual:
            for c in compounds:
                compound_steps = compound_steps_dir(ticker, c)
                compound_steps.mkdir(parents=True, exist_ok=True)
                review_out = compound_steps / "review" / "review.json"
                (compound_steps / "review").mkdir(parents=True, exist_ok=True)
                _run(
                    f"review: {c}",
                    [
                        py,
                        _DIR / "pipeline" / "single" / "run_review.py",
                        "--compound",
                        c,
                        "--run-root",
                        str(run_root),
                        "--steps-dir",
                        str(compound_steps),
                        "--review-output",
                        str(review_out),
                    ]
                    + review_flags(),
                )

        bundle = steps_root / bundle_filename(compounds)
        int_cmd = [
            py,
            _DIR / "pipeline" / "multi" / "interactions.py",
            "--compounds",
            *compounds,
            "--data-root",
            str(steps_root),
            "-o",
            str(bundle),
        ]
        if tokens_path is not None:
            int_cmd += ["--tokens-file", str(tokens_path)]
        _run("interactions", int_cmd)

        combo_cmd = [
            py,
            _DIR / "pipeline" / "multi" / "review-multiple.py",
            str(bundle),
            "--run-root",
            str(run_root),
        ]
        if args.model:
            combo_cmd += ["--model", args.model]
        _run("review-multiple", combo_cmd)

        combo_review = review_dir / "review.json"
        if not args.skip_overview:
            overview_cmd = [
                py,
                _DIR / "pipeline" / "overview.py",
                str(combo_review),
                "-o",
                str(review_dir / "overview.json"),
            ]
            if args.model:
                overview_cmd += ["--model", args.model]
            _run("overview", overview_cmd)

        evidence_script = _DIR / "pipeline" / "evidence-doc.py"
        _run(
            "evidence audit (combination)",
            [
                py,
                evidence_script,
                "--combination-bundle",
                str(bundle),
                "--combo-review",
                str(review_dir / "review.json"),
                "-o",
                str(review_dir / "evidence_audit.md"),
            ],
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
