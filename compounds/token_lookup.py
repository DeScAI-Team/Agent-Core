#!/usr/bin/env python3
"""Resolve pump.science ticker and output paths from compound names."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

_COMPOUNDS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _COMPOUNDS_DIR.parent

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

_COMPOUND_PREFIX_RE = re.compile(r"^compound\s+\d+:\s*(.+)$", re.IGNORECASE)


def repo_root() -> Path:
    return _REPO_ROOT


def default_tokens_path() -> Path:
    override = os.environ.get("COMPOUND_TOKENS_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _REPO_ROOT / "crawlers" / "output" / "pump.science" / "compound-tokens.json"


def normalize_compound_name(name: str) -> str:
    """NFKC unicode, collapse whitespace, lowercase for comparison."""
    text = unicodedata.normalize("NFKC", name or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def safe_compound_dir(compound: str) -> str:
    safe = re.sub(r"[^\w\-.]+", "_", compound, flags=re.UNICODE).strip("._- ")[:80] or "compound"
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe


def parse_intervention(intervention: str) -> list[str]:
    """Parse intervention field from compound-tokens.json."""
    text = (intervention or "").strip()
    if not text:
        return []
    if _COMPOUND_PREFIX_RE.search(text.split(";")[0].strip()):
        names: list[str] = []
        for part in text.split(";"):
            part = part.strip()
            m = _COMPOUND_PREFIX_RE.match(part)
            if m:
                names.append(m.group(1).strip())
            elif part:
                names.append(part)
        return names
    return [text]


def load_tokens(path: Path | None = None) -> list[dict]:
    tokens_path = path or default_tokens_path()
    if not tokens_path.is_file():
        raise FileNotFoundError(
            f"Compound tokens file not found: {tokens_path}\n"
            "Run crawlers/pump-science/fetch-compound-tokens.sh to generate it."
        )
    data = json.loads(tokens_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array in {tokens_path}")
    return data


def _match_token_entries(
    compounds: list[str],
    *,
    tokens_path: Path | None = None,
) -> list[dict]:
    names = [c.strip() for c in compounds if c.strip()]
    if not names:
        raise ValueError("At least one compound name is required")
    target = {normalize_compound_name(c) for c in names}
    tokens = load_tokens(tokens_path)
    matches: list[dict] = []
    for entry in tokens:
        if not isinstance(entry, dict):
            continue
        parsed = parse_intervention(str(entry.get("intervention") or ""))
        parsed_norm = {normalize_compound_name(p) for p in parsed if p}
        if parsed_norm == target:
            matches.append(entry)
    return matches


def resolve_token_entry(compounds: list[str], *, tokens_path: Path | None = None) -> dict[str, Any]:
    """Return pump.science token metadata for the given compound name list."""
    matches = _match_token_entries(compounds, tokens_path=tokens_path)
    if len(matches) == 1:
        entry = matches[0]
        return {
            "ticker": str(entry.get("ticker") or "").strip(),
            "mint": str(entry.get("mint") or "").strip(),
            "token_id": str(entry.get("id") or "").strip(),
            "intervention": str(entry.get("intervention") or "").strip(),
        }
    if len(matches) > 1:
        tickers = [str(e.get("ticker") or "") for e in matches]
        raise ValueError(
            f"Ambiguous ticker match for compounds {compounds!r}: {tickers}. "
            "Refine compound names or update compound-tokens.json."
        )
    names = [c.strip() for c in compounds if c.strip()]
    target = {normalize_compound_name(c) for c in names}
    hints: list[str] = []
    tokens = load_tokens(tokens_path)
    for entry in tokens:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker") or "").strip()
        parsed = parse_intervention(str(entry.get("intervention") or ""))
        parsed_norm = {normalize_compound_name(p) for p in parsed if p}
        overlap = target & parsed_norm
        if overlap:
            hints.append(f"  {ticker}: {parsed} (overlap: {sorted(overlap)})")
    hint_text = "\n".join(hints[:8]) if hints else "  (no partial overlaps found)"
    raise ValueError(
        f"No ticker found for compounds {names!r}.\n"
        f"Tokens file: {tokens_path or default_tokens_path()}\n"
        f"Partial overlaps:\n{hint_text}"
    )


def resolve_ticker(compounds: list[str], *, tokens_path: Path | None = None) -> str:
    """Return pump.science ticker for the given compound name list."""
    return resolve_token_entry(compounds, tokens_path=tokens_path)["ticker"]


def compound_run_root(ticker: str) -> Path:
    return _REPO_ROOT / "reviews" / "compounds" / ticker


def review_output_dir(ticker: str) -> Path:
    return compound_run_root(ticker) / "review"


def steps_dir(ticker: str) -> Path:
    return compound_run_root(ticker) / "steps"


def compound_steps_dir(ticker: str, compound: str) -> Path:
    return steps_dir(ticker) / safe_compound_dir(compound)


def bootstrap_run_dirs(ticker: str) -> Path:
    """Create reviews/compounds/<TICKER>/{review,steps}/ and return run root."""
    run_root = compound_run_root(ticker)
    (run_root / "steps").mkdir(parents=True, exist_ok=True)
    (run_root / "review").mkdir(parents=True, exist_ok=True)
    return run_root


def bundle_filename(compounds: list[str]) -> str:
    slug = "-".join(c[:5].lower() for c in compounds)
    return f"{slug}-bundle.json"
