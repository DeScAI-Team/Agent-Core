"""Inventory dataroom files and assign multimedia processing routes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

RouteKind = Literal["pdf", "image", "video", "text", "skip"]

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

PDF_EXTENSIONS = frozenset({".pdf"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".webm", ".mkv"})
TEXT_EXTENSIONS = frozenset({".txt", ".md", ".html", ".htm", ".docx", ".json", ".csv", ".rtf"})

CONTENT_TYPE_ROUTE: dict[str, RouteKind] = {
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "image/svg+xml": "image",
    "video/mp4": "video",
    "video/quicktime": "video",
    "video/webm": "video",
    "video/x-msvideo": "video",
    "text/plain": "text",
    "text/markdown": "text",
    "text/html": "text",
    "application/json": "text",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "text",
}

METADATA_FILENAMES = frozenset({
    "profile.json",
    "manifest.json",
    "dataroom.json",
    "profiles-index.json",
    "links.json",
    "crawl-manifest.json",
    "crawl-extracted-links.json",
    "nitter-manifest.json",
})


def _json_in_metadata(ipnft_dir: Path, name: str) -> Path:
    meta = ipnft_dir / "metadata" / name
    if meta.is_file():
        return meta
    root = ipnft_dir / name
    return root if root.is_file() else meta


def _local_content_path(ipnft_dir: Path, filename: str) -> Path:
    for base in (ipnft_dir / "output", ipnft_dir / "metadata", ipnft_dir):
        p = base / filename
        if p.is_file():
            return p
    return ipnft_dir / "output" / filename


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _matches_skip_pattern(filename: str) -> bool:
    return any(p.search(filename) for p in SKIP_FILENAME_PATTERNS)


def _route_from_extension(suffix: str) -> RouteKind | None:
    lower = suffix.lower()
    if lower in PDF_EXTENSIONS:
        return "pdf"
    if lower in IMAGE_EXTENSIONS:
        return "image"
    if lower in VIDEO_EXTENSIONS:
        return "video"
    if lower in TEXT_EXTENSIONS:
        return "text"
    return None


def _route_from_content_type(content_type: str) -> RouteKind | None:
    base = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_ROUTE.get(base)


def classify_route(filename: str, content_type: str = "") -> RouteKind:
    """Determine processing route from filename and optional MIME type."""
    if filename in METADATA_FILENAMES:
        return "skip"
    if _matches_skip_pattern(filename):
        return "skip"

    by_ext = _route_from_extension(Path(filename).suffix)
    if by_ext:
        return by_ext
    if content_type:
        by_mime = _route_from_content_type(content_type)
        if by_mime:
            return by_mime
    return "skip"


def inventory_files(ipnft_dir: Path) -> list[dict[str, Any]]:
    """Return processable file entries from manifest + dataroom metadata."""
    manifest = _load_json(_json_in_metadata(ipnft_dir, "manifest.json")) or []
    dataroom = _load_json(_json_in_metadata(ipnft_dir, "dataroom.json")) or {}
    dataroom_files = dataroom.get("files", []) if isinstance(dataroom, dict) else []

    dataroom_by_path: dict[str, dict] = {}
    for f in dataroom_files:
        p = f.get("path", "")
        if p:
            dataroom_by_path[p] = f

    entries: list[dict[str, Any]] = []
    seen_filenames: set[str] = set()

    for entry in manifest:
        filename = entry.get("fileName", "")
        if not filename:
            path_val = entry.get("path", "")
            filename = Path(path_val).name if path_val else ""
        if not filename or filename in seen_filenames:
            continue
        seen_filenames.add(filename)

        entry_path = entry.get("path", "")
        dr_entry = dataroom_by_path.get(entry_path, {})
        content_type = dr_entry.get("contentType", "")
        local_path = _local_content_path(ipnft_dir, filename)

        if not local_path.is_file():
            continue

        route = classify_route(filename, content_type)
        if route == "skip":
            continue

        entries.append({
            "filename": filename,
            "path": local_path,
            "route": route,
            "content_type": content_type,
            "description": entry.get("description", ""),
            "tags": entry.get("tags", []),
            "dataroom_path": entry_path,
        })

    return entries
