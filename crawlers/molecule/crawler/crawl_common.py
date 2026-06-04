"""Shared helpers for molecule IPNFT crawlers (nitter, crawl-links)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

METADATA_DIR = "metadata"
OUTPUT_DIR = "output"
PROFILE_FILENAME = "profile.json"
LINKS_FILENAME = "links.json"

SKIP_JSON_SOURCES = frozenset({
    "crawl-manifest.json",
    "nitter-manifest.json",
    "ipfs-manifest.json",
    "crawl-extracted-links.json",
})

URL_PREFIX_RE = re.compile(r"^(https?://|ipfs://)", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def metadata_dir(ipnft_dir: Path) -> Path:
    return ipnft_dir / METADATA_DIR


def output_dir(ipnft_dir: Path) -> Path:
    return ipnft_dir / OUTPUT_DIR


def ensure_layout(ipnft_dir: Path) -> tuple[Path, Path]:
    meta = metadata_dir(ipnft_dir)
    out = output_dir(ipnft_dir)
    meta.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    return meta, out


def links_json_path(ipnft_dir: Path) -> Path:
    meta_path = metadata_dir(ipnft_dir) / LINKS_FILENAME
    if meta_path.is_file():
        return meta_path
    legacy = ipnft_dir / LINKS_FILENAME
    return legacy if legacy.is_file() else meta_path


def load_crawl_skip_folders(skip_file: Path | None) -> set[str]:
    if skip_file is None or not skip_file.is_file():
        return set()
    try:
        data = json.loads(skip_file.read_text(encoding="utf-8"))
        folders = data.get("moleculeFolders")
        if isinstance(folders, list):
            return {str(x) for x in folders}
    except Exception:
        pass
    return set()


def load_manifest(meta_dir: Path, filename: str) -> dict[str, Any] | None:
    """Load a JSON manifest from metadata/ (legacy: output/ or IPNFT root)."""
    path = meta_dir / filename
    if not path.is_file():
        parent = meta_dir.parent
        if meta_dir.name == METADATA_DIR:
            legacy_out = parent / OUTPUT_DIR / filename
            if legacy_out.is_file():
                path = legacy_out
        if not path.is_file():
            legacy_root = parent / filename
            if legacy_root.is_file():
                path = legacy_root
            else:
                return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_manifest(meta_dir: Path, filename: str, data: dict[str, Any]) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_dir / filename
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def iter_ipnft_folders(
    ipnfts_dir: Path,
    *,
    folder: str | None,
    max_folders: int | None,
    skip_folders: set[str],
    require_links: bool = True,
) -> list[Path]:
    if folder:
        target = ipnfts_dir / folder
        if not target.is_dir():
            raise FileNotFoundError(f"folder not found: {target}")
        folders = [target]
    else:
        folders = sorted(p for p in ipnfts_dir.iterdir() if p.is_dir())

    folders = [f for f in folders if f.name not in skip_folders]
    if require_links:
        folders = [f for f in folders if links_json_path(f).is_file()]
    else:
        folders = [f for f in folders if _has_project_layout(f)]
    if max_folders is not None:
        folders = folders[:max_folders]
    return folders


def _has_project_layout(ipnft_dir: Path) -> bool:
    if links_json_path(ipnft_dir).is_file():
        return True
    meta = metadata_dir(ipnft_dir)
    if meta.is_dir() and any(meta.glob("*.json")):
        return True
    return any(
        p.suffix == ".json" and p.name not in SKIP_JSON_SOURCES
        for p in ipnft_dir.iterdir()
        if p.is_file()
    )


def is_url_like(value: str) -> bool:
    return bool(URL_PREFIX_RE.match(value.strip()))


def is_nitter_url(url: str) -> bool:
    try:
        host = urlparse(url.strip()).hostname or ""
        return host.lower().removeprefix("www.") == "nitter.net"
    except Exception:
        return False


def collect_strings_from_json(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if is_url_like(value):
            yield value.strip()
        return
    if isinstance(value, list):
        for item in value:
            yield from collect_strings_from_json(item)
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from collect_strings_from_json(nested)


def collect_urls_from_metadata(
    ipnft_dir: Path,
    predicate: Callable[[str], bool],
) -> list[dict[str, str]]:
    """Collect unique URLs from JSON under metadata/ (legacy: project root)."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []

    scan_dirs: list[Path] = []
    meta = metadata_dir(ipnft_dir)
    if meta.is_dir():
        scan_dirs.append(meta)
    else:
        scan_dirs.append(ipnft_dir)

    for scan in scan_dirs:
        json_files = sorted(
            p for p in scan.iterdir()
            if p.is_file() and p.suffix == ".json" and p.name not in SKIP_JSON_SOURCES
        )
        for path in json_files:
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for url in collect_strings_from_json(parsed):
                if not predicate(url) or url in seen:
                    continue
                seen.add(url)
                out.append({"url": url, "doc": path.name})
    return out


def rel_output_path(ipnft_dir: Path, out_file: Path) -> str:
    """Path relative to IPNFT root for manifests (e.g. output/beeard.ai.md)."""
    try:
        return out_file.relative_to(ipnft_dir).as_posix()
    except ValueError:
        return out_file.name


# Back-compat alias used by crawl_nitter
collect_urls_from_folder = collect_urls_from_metadata


def add_base_crawl_args(parser: Any) -> None:
    import argparse

    assert isinstance(parser, argparse.ArgumentParser)
    parser.add_argument("--ipnfts-dir", type=Path, required=True)
    parser.add_argument("--folder", type=str, default=None)
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--crawl-skip-file", type=Path, default=None)
