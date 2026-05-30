"""Filter and categorize IPNFT dataroom documents for pipeline processing.

DEPRECATED: Use multimedia_processor.py for routing all dataroom file types.
This module remains for standalone PDF inspection/debugging.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


SCIENCE_CATEGORIES = frozenset({"science", "research", "clinical"})

SCIENCE_FILENAME_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"report",
        r"whitepaper",
        r"white.paper",
        r"proposal",
        r"update",
        r"study",
        r"trial",
        r"preclinical",
        r"hypothesis",
        r"irb",
        r"protocol",
        r"experiment",
        r"vdp.?\d+",
    ]
]

SKIP_FILENAME_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"brand.?guide",
        r"guidelines",
        r"logo",
        r"icon",
        r"dummy",
        r"jwt.?test",
        r"jwt.?script",
    ]
]

SKIP_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".avi", ".webm",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".mp3", ".wav",
})

PDF_CONTENT_TYPES = frozenset({
    "application/pdf",
    "application/x-pdf",
})


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _is_pdf_by_name(filename: str) -> bool:
    return filename.lower().endswith(".pdf")


def _has_skip_extension(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    return suffix in SKIP_EXTENSIONS


def _matches_skip_pattern(filename: str) -> bool:
    return any(p.search(filename) for p in SKIP_FILENAME_PATTERNS)


def _matches_science_pattern(filename: str) -> bool:
    return any(p.search(filename) for p in SCIENCE_FILENAME_PATTERNS)


def _has_science_category(entry: dict) -> bool:
    categories = entry.get("categories", [])
    if isinstance(categories, str):
        categories = [categories]
    return bool(SCIENCE_CATEGORIES & {c.lower() for c in categories})


def filter_docs(ipnft_dir: Path) -> list[dict[str, Any]]:
    """Return a list of processable PDF entries with metadata.

    Each entry: {"filename": str, "path": Path, "reason": str}
    """
    manifest = _load_json(ipnft_dir / "manifest.json") or []
    dataroom = _load_json(ipnft_dir / "dataroom.json") or {}
    dataroom_files = dataroom.get("files", []) if isinstance(dataroom, dict) else []

    dataroom_by_path: dict[str, dict] = {}
    for f in dataroom_files:
        p = f.get("path", "")
        dataroom_by_path[p] = f

    candidates: list[dict[str, Any]] = []
    seen_filenames: set[str] = set()

    for entry in manifest:
        filename = entry.get("fileName", "")
        if not filename:
            path_val = entry.get("path", "")
            filename = Path(path_val).name if path_val else ""
        if not filename:
            continue

        if filename in seen_filenames:
            continue
        seen_filenames.add(filename)

        local_path = ipnft_dir / filename
        entry_path = entry.get("path", "")
        dr_entry = dataroom_by_path.get(entry_path, {})

        content_type = dr_entry.get("contentType", "")

        if _has_skip_extension(filename):
            continue
        if not _is_pdf_by_name(filename) and content_type not in PDF_CONTENT_TYPES:
            continue
        if _matches_skip_pattern(filename):
            continue
        if not local_path.exists():
            continue

        reason = "filename_match"
        if _has_science_category(dr_entry):
            reason = "science_category"
        elif _matches_science_pattern(filename):
            reason = "filename_match"
        elif entry.get("description", ""):
            reason = "has_description"
        else:
            reason = "pdf_default"

        candidates.append({
            "filename": filename,
            "path": local_path,
            "reason": reason,
            "description": entry.get("description", ""),
            "tags": entry.get("tags", []),
        })

    candidates.sort(key=lambda c: (
        0 if c["reason"] == "science_category" else
        1 if c["reason"] == "filename_match" else
        2 if c["reason"] == "has_description" else 3
    ))

    return candidates


def filter_docs_summary(ipnft_dir: Path) -> dict[str, Any]:
    """Return a summary of filtering decisions for logging."""
    manifest = _load_json(ipnft_dir / "manifest.json") or []
    candidates = filter_docs(ipnft_dir)
    return {
        "ipnft_dir": str(ipnft_dir),
        "total_manifest_entries": len(manifest),
        "processable_pdfs": len(candidates),
        "files": [
            {"filename": c["filename"], "reason": c["reason"]}
            for c in candidates
        ],
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Filter IPNFT docs for pipeline processing")
    parser.add_argument("ipnft_dir", type=Path, help="Path to IPNFT directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    ipnft_dir = args.ipnft_dir.resolve()
    if not ipnft_dir.is_dir():
        print(f"Error: {ipnft_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    candidates = filter_docs(ipnft_dir)

    if args.json:
        out = [{"filename": c["filename"], "reason": c["reason"], "path": str(c["path"])} for c in candidates]
        print(json.dumps(out, indent=2))
    else:
        summary = filter_docs_summary(ipnft_dir)
        print(f"IPNFT: {ipnft_dir.name}")
        print(f"Total manifest entries: {summary['total_manifest_entries']}")
        print(f"Processable PDFs: {summary['processable_pdfs']}")
        for f in summary["files"]:
            print(f"  [{f['reason']}] {f['filename']}")


if __name__ == "__main__":
    main()
