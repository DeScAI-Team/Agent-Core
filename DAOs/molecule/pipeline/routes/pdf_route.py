"""PDF routing: 2-page OCR, LLM classification, copy to bundle/pdf/."""

from __future__ import annotations

import os
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

from ._utils import copy_file_unique, parse_json_response

_PROMPTS = _PIPELINE / "prompts"
VALID_TYPES = frozenset({"article", "proposal", "other"})


def _classify_pdf_text(
    text: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    skip_llm: bool,
) -> str:
    if skip_llm:
        return "other"
    prompt_path = _PROMPTS / "pdf_document_classifier.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    client = OpenAI(base_url=base_url, api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,
        max_tokens=300,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Classify this document:\n\n{text[:12000]}"},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = parse_json_response(raw)
    doc_type = str(data.get("document_type", "other")).lower().strip()
    return doc_type if doc_type in VALID_TYPES else "other"


def process_pdf(
    source: Path,
    bundle_dir: Path,
    *,
    vision_client: OpenAI,
    vision_model: str,
    text_model: str,
    text_base_url: str,
    text_api_key: str,
    skip_llm: bool = False,
    max_preview_pages: int = 2,
) -> dict[str, Any]:
    """OCR first pages, classify, copy raw PDF to bundle/pdf/{articles|proposals|other}/."""
    pages = rasterize_pdf_pages(source, max_pages=max_preview_pages)
    if not pages:
        raise ValueError("PDF has no pages")

    ocr_chunks: list[str] = []
    for i, img in enumerate(pages, start=1):
        text = vision_describe(vision_client, vision_model, img, PDF_OCR_PROMPT)
        ocr_chunks.append(f"<!-- page {i} -->\n\n{text}")
    preview_text = "\n\n---\n\n".join(ocr_chunks)

    doc_type = _classify_pdf_text(
        preview_text,
        model=text_model,
        base_url=text_base_url,
        api_key=text_api_key,
        skip_llm=skip_llm,
    )

    dest_subdir = bundle_dir / "pdf" / doc_type
    dest_path = copy_file_unique(source, dest_subdir)

    return {
        "route": "pdf",
        "document_type": doc_type,
        "output_path": str(dest_path),
    }
