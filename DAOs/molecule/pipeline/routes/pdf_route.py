"""PDF routing: OCR every page → sectioned JSON at bundle/pdf/{stem}.json."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

_PIPELINE = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PIPELINE.parent.parent.parent
_ARTICLE_PIPELINE = _REPO_ROOT / "articles" / "pipeline"
if str(_ARTICLE_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_ARTICLE_PIPELINE))

from vision_client import PDF_OCR_PROMPT, rasterize_pdf_pages, vision_describe  # noqa: E402

from ._utils import safe_stem

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _extract_title(page_text: str, fallback: str) -> str:
    """Pick the first heading or first non-empty line as title."""
    m = _HEADING_RE.search(page_text)
    if m:
        return m.group(2).strip()[:200]
    for line in page_text.splitlines():
        line = line.strip()
        if line and not line.startswith("<!--"):
            return line[:200]
    return fallback


def _split_sections(page_texts: list[str]) -> list[dict[str, Any]]:
    """Split OCR'd pages into sections delimited by markdown headings.

    Each page contributes its text; headings detected anywhere in the OCR text start
    a new section. When a page contains no heading, its text is appended to the
    current section under the same heading. Pages with no preceding heading land in
    a synthetic "Body" section.
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for page_idx, page_text in enumerate(page_texts, start=1):
        cursor = 0
        matches = list(_HEADING_RE.finditer(page_text))
        if not matches:
            chunk = page_text.strip()
            if chunk:
                if current is None:
                    current = {"heading": "Body", "page_start": page_idx, "text_parts": []}
                current["text_parts"].append(chunk)
                current.setdefault("page_end", page_idx)
                current["page_end"] = page_idx
            continue

        first_start = matches[0].start()
        if first_start > 0:
            preface = page_text[:first_start].strip()
            if preface:
                if current is None:
                    current = {"heading": "Body", "page_start": page_idx, "text_parts": []}
                current["text_parts"].append(preface)
                current["page_end"] = page_idx

        for i, m in enumerate(matches):
            heading = m.group(2).strip()
            section_start = m.end()
            section_end = matches[i + 1].start() if i + 1 < len(matches) else len(page_text)
            body = page_text[section_start:section_end].strip()

            if current is not None:
                sections.append({
                    "heading": current["heading"],
                    "page_start": current["page_start"],
                    "page_end": current.get("page_end", current["page_start"]),
                    "text": "\n\n".join(current["text_parts"]).strip(),
                })
            current = {"heading": heading, "page_start": page_idx, "text_parts": [body] if body else []}
            current["page_end"] = page_idx

    if current is not None:
        sections.append({
            "heading": current["heading"],
            "page_start": current["page_start"],
            "page_end": current.get("page_end", current["page_start"]),
            "text": "\n\n".join(current["text_parts"]).strip(),
        })

    return [s for s in sections if s["text"]]


def process_pdf(
    source: Path,
    bundle_dir: Path,
    *,
    vision_client: OpenAI,
    vision_model: str,
    max_pages: int | None = None,
    overwrite: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """OCR every page, split by markdown headings, write sectioned JSON."""
    out_dir = bundle_dir / "pdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(source.name)
    out_path = out_dir / f"{stem}.json"

    if out_path.exists() and not overwrite:
        return {"route": "pdf", "output_path": str(out_path), "skipped": True}

    pages = rasterize_pdf_pages(source, max_pages=max_pages)
    if not pages:
        raise ValueError("PDF has no pages")

    page_texts: list[str] = []
    for img in pages:
        text = vision_describe(vision_client, vision_model, img, PDF_OCR_PROMPT)
        page_texts.append(text or "")

    full_text = "\n\n".join(
        f"<!-- page {i} -->\n\n{txt}" for i, txt in enumerate(page_texts, start=1)
    ).strip()

    sections = _split_sections(page_texts)
    title = _extract_title(page_texts[0] if page_texts else "", source.stem)

    payload: dict[str, Any] = {
        "source_file": source.name,
        "source_path": str(source),
        "sha256": _sha256(source),
        "page_count": len(pages),
        "title": title,
        "sections": sections,
        "full_text": full_text,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "route": "pdf",
        "output_path": str(out_path),
        "page_count": len(pages),
        "section_count": len(sections),
    }
