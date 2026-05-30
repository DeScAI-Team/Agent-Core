"""Text routing: extract plaintext → bundle/text/{stem}.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ._utils import safe_stem


def _extract_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    return "\n\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())


def _extract_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(data, indent=2, ensure_ascii=False)


def _extract_html(path: Path) -> str:
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _extract_with_docling(path: Path) -> str | None:
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        return None
    try:
        result = DocumentConverter().convert(str(path))
        return result.document.export_to_markdown()
    except Exception:
        return None


def extract_plaintext(source: Path, content_type: str = "") -> str:
    suffix = source.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".rtf"}:
        return source.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        return _extract_html(source)
    if suffix == ".docx":
        return _extract_docx(source)
    if suffix == ".json":
        return _extract_json(source)

    docling_text = _extract_with_docling(source)
    if docling_text:
        return docling_text
    raise ValueError(f"No extractor for {source.name} ({content_type or 'unknown type'})")


def process_text(
    source: Path,
    bundle_dir: Path,
    *,
    content_type: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    out_dir = bundle_dir / "text"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_stem(source.name)}.md"

    if out_path.exists() and not overwrite:
        return {"route": "text", "output_path": str(out_path), "skipped": True}

    body = extract_plaintext(source, content_type)
    header = (
        f"---\nsource: {source.name}\ncontent_type: {content_type or 'unknown'}\n---\n\n"
    )
    out_path.write_text(header + body + "\n", encoding="utf-8")
    return {"route": "text", "output_path": str(out_path)}
