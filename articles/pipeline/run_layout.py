"""Shared per-paper directory layout for article review runs."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

_WIN_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def safe_stem(stem: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._-") or "document"
    if len(s) > 120:
        s = s[:120]
    if s.upper() in _WIN_RESERVED:
        s = f"_{s}_"
    return s


def run_dir_for_stem(base: Path, pdf_stem: str) -> Path:
    return base / safe_stem(pdf_stem)


def find_run_dir(base: Path, pdf_stem: str) -> Path | None:
    """Locate reviews/articles/<stem>/ (new or legacy layout)."""
    safe = safe_stem(pdf_stem)
    candidates = [base / safe]
    if base.is_dir():
        candidates.extend(
            d for d in sorted(base.iterdir())
            if d.is_dir() and d.name.startswith(safe)
        )
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen or not cand.is_dir():
            continue
        seen.add(cand)
        if (cand / "steps" / "full.md").is_file():
            return cand
        if (cand / "full.md").is_file():
            return cand
    return None


def work_dir_for_run(run_dir: Path) -> Path:
    """Intermediates directory (steps/ in new layout)."""
    steps = run_dir / "steps"
    if (steps / "full.md").is_file():
        return steps
    if (run_dir / "full.md").is_file():
        return run_dir
    return steps


def review_dir_for_run(run_dir: Path, *, legacy: bool = False) -> Path:
    if legacy:
        return run_dir / "output"
    return run_dir / "review"


def copy_into_steps(src: Path, dest: Path, *, refresh: bool = False) -> None:
    """Copy artifact into steps/ unless source and destination are already the same file."""
    src_r = src.expanduser().resolve()
    dest_r = dest.expanduser().resolve()
    if src_r == dest_r:
        return
    if refresh or not dest_r.is_file():
        dest_r.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_r, dest_r)
