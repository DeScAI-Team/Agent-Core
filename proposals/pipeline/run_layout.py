"""Per-proposal directory layout: run root, steps/, review/."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict


class ProposalArtifacts(TypedDict):
    run_dir: Path
    steps_dir: Path
    review_dir: Path
    review_path: Path
    screener_path: Path
    originality_path: Path
    audit_out_path: Path


def steps_dir_for_run(run_dir: Path) -> Path:
    return run_dir / "steps"


def review_dir_for_run(run_dir: Path) -> Path:
    return run_dir / "review"


def resolve_proposal_artifacts(base: Path) -> ProposalArtifacts:
    """Resolve paths for run root, review/, steps/, or legacy flat dir."""
    base = base.expanduser().resolve()

    if (base / "review" / "review.json").is_file():
        run_dir = base
        review_dir = base / "review"
        steps_dir = base / "steps"
    elif base.name == "review" and (base / "review.json").is_file():
        review_dir = base
        run_dir = base.parent
        steps_dir = run_dir / "steps"
    elif (base / "review.json").is_file():
        run_dir = base
        review_dir = base
        steps_dir = base
    else:
        run_dir = base
        review_dir = base / "review"
        steps_dir = base / "steps"

    screener = steps_dir / "screener_findings.json"
    if not screener.is_file() and (run_dir / "screener_findings.json").is_file():
        screener = run_dir / "screener_findings.json"

    originality = steps_dir / "originality.json"
    if not originality.is_file() and (run_dir / "originality.json").is_file():
        originality = run_dir / "originality.json"

    review_path = review_dir / "review.json"
    if not review_path.is_file() and (run_dir / "review.json").is_file():
        review_path = run_dir / "review.json"

    audit_out = review_dir / "evidence_audit.md"
    if not audit_out.parent.is_dir():
        audit_out = run_dir / "evidence_audit.md"

    return {
        "run_dir": run_dir,
        "steps_dir": steps_dir,
        "review_dir": review_dir,
        "review_path": review_path,
        "screener_path": screener,
        "originality_path": originality,
        "audit_out_path": audit_out,
    }
