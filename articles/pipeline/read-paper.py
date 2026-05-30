#!/usr/bin/env python3
"""
PDF → raster pages → one vLLM call per page (Nanonets-OCR2-3B via OpenAI-compatible API).

  vllm serve nanonets/Nanonets-OCR2-3B

Env: VLLM_BASE_URL, VLLM_API_KEY, READ_PAPER_MODEL (defaults match review.py + HF model id).
Output: articles/data/<pdf_stem>/page_XXX.md and full.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from openai import OpenAI

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

from vision_client import PDF_OCR_PROMPT, rasterize_pdf_pages, vision_describe

PIPELINE = Path(__file__).resolve().parent
ARTICLES = PIPELINE.parent
REPO_ROOT = ARTICLES.parent
DEFAULT_DATA = ARTICLES / "data"

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
DEFAULT_MODEL = os.environ.get("READ_PAPER_MODEL", "nanonets/Nanonets-OCR2-3B")


def rasterize_pdf(pdf_path: Path, scale: float = 2.0) -> list:
    """Open PDF and return one RGB PIL image per page (in order)."""
    return rasterize_pdf_pages(pdf_path, max_pages=None, scale=scale)


def read_page(
    client: OpenAI,
    model: str,
    pil_image,
    *,
    max_tokens: int = 15000,
    max_retries: int = 4,
) -> str:
    """One chat completion: PNG-encode the page image and return OCR markdown text."""
    return vision_describe(
        client,
        model,
        pil_image,
        PDF_OCR_PROMPT,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )


def _safe_stem(stem: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._-") or "document"
    return s[:120]


def main() -> None:
    p = argparse.ArgumentParser(description="PDF → Nanonets OCR via local vLLM.")
    p.add_argument("--pdf", type=Path, required=True)
    p.add_argument("--out-root", type=Path, default=DEFAULT_DATA)
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--base-url", type=str, default=VLLM_BASE_URL)
    p.add_argument("--api-key", type=str, default=VLLM_API_KEY)
    p.add_argument("--render-scale", type=float, default=2.0)
    p.add_argument("--max-tokens", type=int, default=15000)
    p.add_argument("--max-retries", type=int, default=4)
    args = p.parse_args()

    if load_dotenv:
        load_dotenv(REPO_ROOT / ".env")

    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        print(f"error: not a PDF file: {pdf}", file=sys.stderr)
        sys.exit(1)

    out_dir = args.out_root.expanduser().resolve() / _safe_stem(pdf.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI(base_url=args.base_url, api_key=args.api_key)

    pages = rasterize_pdf(pdf, args.render_scale)
    if not pages:
        print("error: PDF has no pages", file=sys.stderr)
        sys.exit(1)

    chunks: list[str] = []
    for i, img in enumerate(pages, start=1):
        text = read_page(
            client,
            args.model,
            img,
            max_tokens=args.max_tokens,
            max_retries=args.max_retries,
        )
        (out_dir / f"page_{i:03d}.md").write_text(text + "\n", encoding="utf-8")
        chunks.append(f"<!-- page {i} -->\n\n{text}")

    (out_dir / "full.md").write_text("\n\n---\n\n".join(chunks) + "\n", encoding="utf-8")
    (out_dir / "metadata.json").write_text(
        json.dumps(
            {
                "pdf": str(pdf),
                "output_folder": str(out_dir),
                "model": args.model,
                "base_url": args.base_url,
                "pages": len(pages),
                "render_scale": args.render_scale,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Done: {len(pages)} pages → {out_dir}")


if __name__ == "__main__":
    main()
