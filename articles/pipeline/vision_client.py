"""Shared vLLM vision helpers for PDF rasterization and image description."""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path

from openai import OpenAI
from PIL import Image

try:
    import pypdfium2 as pdfium
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "pip install pypdfium2 (or docling, which depends on it)."
    ) from e

PDF_OCR_PROMPT = (
    "Extract the text from the above document as if you were reading it naturally. "
    "Return the tables in html format. Return the equations in LaTeX representation. "
    "If there is an image in the document and image caption is not present, add a small "
    "description of the image inside the <img></img> tag; otherwise, add the image caption "
    "inside <img></img>. Watermarks should be wrapped in brackets. Ex: "
    "<watermark>OFFICIAL COPY</watermark>. Page numbers should be wrapped in brackets. Ex: "
    "<page_number>14</page_number> or <page_number>9/22</page_number>. "
    "Prefer using ☐ and ☑ for check boxes."
)


def rasterize_pdf_pages(
    pdf_path: Path,
    *,
    max_pages: int | None = None,
    scale: float = 2.0,
) -> list[Image.Image]:
    """Return RGB PIL images for PDF pages (up to max_pages if set)."""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page_count = len(doc)
        limit = page_count if max_pages is None else min(page_count, max_pages)
        out: list[Image.Image] = []
        for i in range(limit):
            pil = doc[i].render(scale=scale).to_pil()
            if pil.mode != "RGB":
                pil = pil.convert("RGB")
            out.append(pil)
        return out
    finally:
        doc.close()


def load_image(path: Path) -> Image.Image:
    """Load an image file as RGB PIL."""
    with Image.open(path) as img:
        if img.mode != "RGB":
            return img.convert("RGB")
        return img.copy()


def vision_describe(
    client: OpenAI,
    model: str,
    pil_image: Image.Image,
    prompt: str,
    *,
    max_tokens: int = 15000,
    max_retries: int = 4,
) -> str:
    """One chat completion: PNG-encode image and return model text response."""
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.standard_b64encode(buf.getvalue()).decode(
        "ascii"
    )

    last: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            r = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            last = e
            if attempt < max_retries:
                time.sleep(min(2**attempt, 30))
    assert last is not None
    raise last
