#!/usr/bin/env python3
"""
Build document_profile.json from deterministic sources + one small LLM call.

Inputs (all produced by earlier pipeline steps):
  --dir   : articles/data/<stem>/  — contains full.md, metadata.json, page_*.md
  --jsonl : articles/data/text_knowledge_base.jsonl

Deterministic (no LLM):
  title, abstract, keywords, structure  <- text_knowledge_base.jsonl chunks
  doi, published_date                   <- regex on page_001.md
  figures                               <- <img>…</img> scan over page_*.md
  source block                          <- metadata.json verbatim

LLM (single call, authors only):
  authors, affiliations                 <- byline chunk text (chunk_id 0)

Env: VLLM_BASE_URL, VLLM_API_KEY, VALIDATOR_MODEL (same as rest of pipeline)
     DOCUMENT_PROFILE_MODEL overrides VALIDATOR_MODEL if set.
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

PIPELINE = Path(__file__).resolve().parent
ARTICLES = PIPELINE.parent
REPO_ROOT = ARTICLES.parent

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[1]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
DEFAULT_MODEL = os.environ.get(
    "DOCUMENT_PROFILE_MODEL",
    os.environ.get("VALIDATOR_MODEL", "/model"),
)

MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Helpers copied from claim-extract/add_data.py (no shared module yet)
# ---------------------------------------------------------------------------

def _strip_thinking_for_parse(text: str) -> str:
    """Strip reasoning/thinking wrappers before JSON extraction."""
    t = text.strip()
    block_patterns = (
        r"<think>.*?</think>",
        r"<think\b[^>]*>[\s\S]*?</think>",
        r"<thinking\b[^>]*>[\s\S]*?</thinking>",
        r"<reasoning\b[^>]*>[\s\S]*?</reasoning>",
        r"<thought\b[^>]*>[\s\S]*?</thought>",
        r"<redacted_thinking\b[^>]*>[\s\S]*?</think>",
    )
    for _ in range(8):
        prev = t
        for pat in block_patterns:
            t = re.sub(pat, "", t, flags=re.DOTALL | re.IGNORECASE)
        if t == prev:
            break
    for pat in (
        r"<think>.*",
        r"<think\b[^>]*>[\s\S]*$",
        r"<thinking\b[^>]*>[\s\S]*$",
        r"<reasoning\b[^>]*>[\s\S]*$",
        r"<thought\b[^>]*>[\s\S]*$",
        r"<redacted_thinking\b[^>]*>[\s\S]*$",
    ):
        t = re.sub(pat, "", t, flags=re.DOTALL | re.IGNORECASE).strip()
    return t


def _extract_json_object(raw_text: str) -> dict:
    """Extract and parse a JSON object from model text output."""
    text = raw_text.strip()
    if not text:
        raise ValueError("empty response")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("no JSON object found")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("parsed JSON is not an object")
    return parsed


# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------

def load_chunks(jsonl_path: Path, doc_name: str) -> list[dict]:
    """Load all chunks from JSONL that belong to doc_name, in chunk_id order."""
    chunks: list[dict] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("doc_name") == doc_name:
                chunks.append(rec)
    chunks.sort(key=lambda c: c.get("chunk_id", 0))
    return chunks


def extract_title(chunks: list[dict]) -> str | None:
    """
    Title = section_heading of the first chunk whose heading is long enough to
    be a paper title (> 20 chars), or the longest heading in the first 5 chunks.
    """
    candidates = [c.get("section_heading", "") for c in chunks[:5]]
    long = [h for h in candidates if len(h) > 20]
    if long:
        return max(long, key=len)
    if candidates:
        return max(candidates, key=len) or None
    return None


def extract_abstract(chunks: list[dict]) -> str | None:
    """Return verbatim text of the first chunk with semantic_category == 'abstract'."""
    for c in chunks:
        if c.get("semantic_category") == "abstract":
            return c.get("text", "").strip() or None
    return None


def extract_keywords(abstract_text: str | None) -> list[str]:
    """Parse 'Keywords: ...' line from the abstract text."""
    if not abstract_text:
        return []
    for line in reversed(abstract_text.splitlines()):
        m = re.match(r"\*?Keywords\*?[:\s]+(.+)", line, re.IGNORECASE)
        if m:
            raw = m.group(1)
            return [k.strip() for k in re.split(r"[,;]", raw) if k.strip()]
    return []


def extract_structure(chunks: list[dict]) -> list[dict]:
    """Unique headings in order with semantic category, chunk_ids, and concatenated body text."""
    seen: dict[str, dict] = {}
    for c in chunks:
        heading = c.get("section_heading", "").strip()
        if not heading:
            continue
        if heading not in seen:
            seen[heading] = {
                "heading": heading,
                "semantic_category": c.get("semantic_category", "other"),
                "chunk_ids": [],
                "_body_parts": [],
            }
        seen[heading]["chunk_ids"].append(c.get("chunk_id"))
        body_text = c.get("text", "").strip()
        if body_text:
            seen[heading]["_body_parts"].append(body_text)

    result = []
    for entry in seen.values():
        parts = entry.pop("_body_parts")
        entry["body"] = "\n\n".join(parts) if parts else None
        result.append(entry)
    return result


def _page_number_before(text: str, pos: int) -> int | None:
    """Find the last <!-- page N --> marker before position pos in text."""
    matches = list(re.finditer(r"<!--\s*page\s+(\d+)\s*-->", text[:pos], re.IGNORECASE))
    if matches:
        return int(matches[-1].group(1))
    return None


_LOGO_PATTERNS = re.compile(
    r"^(researchhub\s+journal\s*(logo)?|journal\s+logo|logo)$",
    re.IGNORECASE,
)

_MIN_FIGURE_CAPTION_LEN = 20


def _is_noise_caption(caption: str) -> bool:
    """Return True for generic logo/watermark tags that are not real figures."""
    stripped = caption.strip()
    if len(stripped) < _MIN_FIGURE_CAPTION_LEN:
        return True
    if _LOGO_PATTERNS.match(stripped):
        return True
    return False


def extract_figures(page_md_files: list[Path]) -> list[dict]:
    """
    Scan page_*.md files for <img>caption</img> tags.
    Filters out generic logo/watermark captions (short text or known logo patterns).
    """
    figures: list[dict] = []
    for md_file in sorted(page_md_files):
        m = re.search(r"page_(\d+)\.md$", md_file.name)
        file_page = int(m.group(1)) if m else None
        text = md_file.read_text(encoding="utf-8")
        for img_match in re.finditer(r"<img>([^<]*)</img>", text, re.DOTALL):
            caption = img_match.group(1).strip()
            if not caption or _is_noise_caption(caption):
                continue
            page = file_page if file_page is not None else _page_number_before(text, img_match.start())
            figures.append({"page": page, "caption": caption})
    return figures


def extract_doi(page1_text: str) -> str | None:
    m = re.search(r"\b(10\.\d{4,9}/\S+)", page1_text)
    if m:
        return m.group(1).rstrip(".,;)")
    return None


def extract_published_date(page1_text: str) -> str | None:
    m = re.search(r"\*{0,2}Published[:\s]*\*{0,2}\s*(.+)", page1_text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip("*")
    return None


# ---------------------------------------------------------------------------
# LLM author extraction
# ---------------------------------------------------------------------------

_AUTHOR_SYSTEM = (
    "You are a bibliographic parser. "
    "Extract author names and affiliations from the byline text provided. "
    "Return ONLY a valid JSON object with exactly these keys:\n"
    '  "authors": [{"name": string, "affiliation_indices": [int], "is_corresponding": bool, "email": string|null}],\n'
    '  "affiliations": [string]\n'
    "Copy values verbatim from the text. "
    "affiliation_indices are 0-based positions in the affiliations array. "
    "If a value is absent or unclear, use null (for strings) or [] (for lists). "
    "Do not invent names, institutions, or emails."
)


def extract_authors_llm(
    client: OpenAI,
    model: str,
    byline_text: str,
    max_tokens: int,
) -> dict:
    """Single LLM call to parse author/affiliation byline. Returns dict with authors/affiliations."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": _AUTHOR_SYSTEM},
                    {"role": "user", "content": f"BYLINE:\n{byline_text}"},
                ],
            )
            raw = (r.choices[0].message.content or "").strip()
            raw = _strip_thinking_for_parse(raw)
            parsed = _extract_json_object(raw)
            return {
                "authors": parsed.get("authors") or [],
                "affiliations": parsed.get("affiliations") or [],
            }
        except Exception as e:
            last_err = e
            print(
                f"  [WARN] Author extraction attempt {attempt}/{MAX_RETRIES} failed: {e}",
                file=sys.stderr,
            )
    print(
        f"  [ERROR] Author extraction failed after {MAX_RETRIES} retries; using empty.",
        file=sys.stderr,
    )
    return {"authors": [], "affiliations": []}


# ---------------------------------------------------------------------------
# Main assembly
# ---------------------------------------------------------------------------

def build_profile(
    out_dir: Path,
    jsonl_path: Path,
    client: OpenAI | None,
    model: str,
    max_tokens: int,
    dry_run: bool,
) -> dict:
    doc_name = out_dir.name

    # --- source block from metadata.json ---
    meta_path = out_dir / "metadata.json"
    if not meta_path.is_file():
        print(f"error: metadata.json not found in {out_dir}", file=sys.stderr)
        sys.exit(1)
    with meta_path.open(encoding="utf-8") as f:
        metadata = json.load(f)

    source = {
        "pdf": metadata.get("pdf"),
        "model": metadata.get("model"),
        "base_url": metadata.get("base_url"),
        "pages": metadata.get("pages"),
        "render_scale": metadata.get("render_scale"),
        "full_markdown_relative": "full.md",
        "chunks_jsonl": str(jsonl_path.resolve()),
    }

    # --- JSONL chunks ---
    if not jsonl_path.is_file():
        print(f"error: JSONL not found: {jsonl_path}", file=sys.stderr)
        sys.exit(1)
    chunks = load_chunks(jsonl_path, doc_name)
    if not chunks:
        print(f"warning: no chunks found for doc_name={doc_name!r} in {jsonl_path}", file=sys.stderr)

    title = extract_title(chunks)
    abstract_text = extract_abstract(chunks)
    keywords = extract_keywords(abstract_text)
    structure = extract_structure(chunks)

    # byline = chunk_id 0 text (authors + affiliations line from Docling)
    byline_text = chunks[0].get("text", "").strip() if chunks else ""

    # --- regex from page_001.md ---
    page1_path = out_dir / "page_001.md"
    page1_text = page1_path.read_text(encoding="utf-8") if page1_path.is_file() else ""
    doi = extract_doi(page1_text)
    published_date = extract_published_date(page1_text)

    # --- figures from all page_*.md ---
    page_mds = sorted(out_dir.glob("page_*.md"))
    figures = extract_figures(page_mds)

    # --- LLM: authors only ---
    if dry_run or not client:
        author_data: dict = {"authors": [], "affiliations": []}
        if dry_run:
            print("  [dry-run] Skipping LLM author extraction.")
    else:
        print(f"  Extracting authors via LLM from byline ({len(byline_text)} chars)…")
        author_data = extract_authors_llm(client, model, byline_text, max_tokens)

    return {
        "source": source,
        "bibliographic": {
            "title": title,
            "subtitle": None,
            "authors": author_data["authors"],
            "affiliations": author_data["affiliations"],
            "journal": None,
            "published_date": published_date,
            "doi": doi,
            "volume": None,
            "issue": None,
        },
        "classification": {
            "keywords": keywords,
            "article_type": "journal",
        },
        "content_pointers": {
            "abstract_markdown": abstract_text,
            "language": "en",
        },
        "structure": structure,
        "figures": figures,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Build document_profile.json from JSONL chunks + page markdown files. "
            "Uses a single LLM call only for author/affiliation parsing."
        )
    )
    p.add_argument(
        "--dir",
        type=Path,
        required=True,
        help="Output folder from read-paper.py (contains full.md, metadata.json, page_*.md).",
    )
    p.add_argument(
        "--jsonl",
        type=Path,
        required=True,
        help="text_knowledge_base.jsonl produced by add_data.py.",
    )
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--base-url", type=str, default=VLLM_BASE_URL)
    p.add_argument("--api-key", type=str, default=VLLM_API_KEY)
    p.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="Max tokens for the author LLM call (default 1024).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip LLM; write profile with empty authors list.",
    )
    args = p.parse_args()

    if load_dotenv:
        load_dotenv(REPO_ROOT / ".env")

    out_dir = args.dir.expanduser().resolve()
    if not out_dir.is_dir():
        print(f"error: --dir not found: {out_dir}", file=sys.stderr)
        sys.exit(1)

    jsonl_path = args.jsonl.expanduser().resolve()

    client: OpenAI | None = None
    if not args.dry_run:
        client = OpenAI(base_url=args.base_url, api_key=args.api_key)

    print(f"Building profile for: {out_dir.name}")
    profile = build_profile(
        out_dir=out_dir,
        jsonl_path=jsonl_path,
        client=client,
        model=args.model,
        max_tokens=args.max_tokens,
        dry_run=args.dry_run,
    )

    out_path = out_dir / "document_profile.json"
    out_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(f"Done: {out_path}")


if __name__ == "__main__":
    main()
