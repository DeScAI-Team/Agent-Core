#!/usr/bin/env python3
"""
PDF → raster pages → one vision LLM call per page (Nanonets-OCR2-3B via OpenAI-compatible API).

  vllm serve nanonets/Nanonets-OCR2-3B

Env: VISION_MODEL_URL, VISION_MODEL_API_KEY, READ_PAPER_MODEL (via articles/llm_env.py).
Output: <out-root>/<pdf_stem>/ or --out-dir directly (page_*.md, full.md).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ARTICLES = Path(__file__).resolve().parents[1]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))

from llm_env import READ_PAPER_MODEL, VISION_API_KEY, VISION_BASE_URL, make_client  # noqa: E402

from vision_client import PDF_OCR_PROMPT, rasterize_pdf_pages, vision_describe  # noqa: E402

PIPELINE = Path(__file__).resolve().parent
ARTICLES = PIPELINE.parent
DEFAULT_DATA = ARTICLES / "data"


def rasterize_pdf(pdf_path: Path, scale: float = 2.0) -> list:
    """Open PDF and return one RGB PIL image per page (in order)."""
    return rasterize_pdf_pages(pdf_path, max_pages=None, scale=scale)


def read_page(
    client,
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
    p = argparse.ArgumentParser(description="PDF → vision OCR via OpenAI-compatible API.")
    p.add_argument("--pdf", type=Path, required=True)
    p.add_argument(
        "--out-root",
        type=Path,
        default=DEFAULT_DATA,
        help="Parent dir; writes <out-root>/<pdf_stem>/ unless --out-dir is set",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Exact output directory (no extra <stem>/ subfolder)",
    )
    p.add_argument("--model", type=str, default=READ_PAPER_MODEL)
    p.add_argument("--base-url", type=str, default=VISION_BASE_URL)
    p.add_argument("--api-key", type=str, default=VISION_API_KEY)
    p.add_argument("--render-scale", type=float, default=2.0)
    p.add_argument("--max-tokens", type=int, default=15000)
    p.add_argument("--max-retries", type=int, default=4)
    args = p.parse_args()

    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        print(f"error: not a PDF file: {pdf}", file=sys.stderr)
        sys.exit(1)

    if args.out_dir is not None:
        out_dir = args.out_dir.expanduser().resolve()
    else:
        out_dir = args.out_root.expanduser().resolve() / _safe_stem(pdf.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = make_client(vision=True)

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
