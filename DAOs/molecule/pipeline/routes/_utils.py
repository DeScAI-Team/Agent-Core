"""Shared helpers for multimedia route modules."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

_FENCE_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.I)


def parse_json_response(raw: str) -> dict:
    """Extract JSON object from LLM response."""
    text = raw.strip()
    m = _FENCE_BLOCK.search(text)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def safe_stem(name: str) -> str:
    stem = Path(name).stem
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._-") or "document"
    return s[:120]


def copy_file_unique(src: Path, dest_dir: Path) -> Path:
    """Copy src into dest_dir, avoiding overwrite by suffixing."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
        return dest
    stem, suffix = src.stem, src.suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem}__{n}{suffix}"
        if not candidate.exists():
            shutil.copy2(src, candidate)
            return candidate
        n += 1
