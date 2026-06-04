"""Originality check for a research paper.

Reads the full paper extraction (text_knowledge_base.jsonl) in chunk batches,
uses an LLM to generate targeted OpenAlex search terms from each batch, fetches
related-work abstracts via the OpenAlex search API, then calls the LLM to write
an originality statement comparing those abstracts against the paper's own abstract.

Stages:
  1. abstract_extractor  — pull the paper abstract from KB chunks or full.md
  2. term_generator      — batch KB chunks → LLM → search terms
  3. openalex_searcher   — search OpenAlex per term, cache results, deduplicate
  4a. similarity_scorer  — LLM scores each related work 0.00-1.00; computes
                           avg_similarity and originality_score = 1.00 - avg
  4b. originality_writer — LLM writes statement referencing the computed score

Usage:
  python originality_check.py --directory "articles/data/document (10)/pipe-test2"
  python originality_check.py --directory ... --skip-llm
  python originality_check.py --directory ... --terms-per-chunk 4 --max-results-per-term 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE = Path(__file__).resolve().parent
PROMPTS_DIR = _BASE / "prompts"

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")
OPENALEX_EMAIL = os.environ.get("OPENALEX_EMAIL", "team@descai.org")

OPENALEX_SLEEP = 1.0 / 8.0          # polite-pool rate limit
MAX_RETRIES = 4
TERM_GEN_MAX_TOKENS = 1024
SIMILARITY_MAX_TOKENS = 1024
SIMILARITY_BATCH_SIZE = 25          # works per LLM scoring call
STATEMENT_MAX_TOKENS = 1024
ABSTRACT_SNIPPET_MAX = 400          # chars of related-work abstract shown to LLM
WRITER_INPUT_TOKEN_BUDGET = 10000   # max input tokens for originality writer prompt
WRITER_CHARS_PER_TOKEN = 3.2        # conservative estimate (covers non-English, punctuation)
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.I)

# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Abstract reconstruction (mirrors retrieve_compare.py)
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    if not inverted or not isinstance(inverted, dict):
        return None
    slots: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                slots.append((pos, str(word)))
    if not slots:
        return None
    slots.sort(key=lambda x: x[0])
    return " ".join(w for _, w in slots)


# ---------------------------------------------------------------------------
# LLM helpers (mirrors review.py)
# ---------------------------------------------------------------------------

def _llm_call(client: OpenAI, system_prompt: str, user_content: str, max_tokens: int) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            raw = response.choices[0].message.content
            if raw is None:
                raise ValueError("LLM returned None content")
            text = raw.strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
            finish = response.choices[0].finish_reason
            if finish == "length" or (text and text[-1] not in ".!?]\"'"):
                last = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
                if last > 0:
                    text = text[: last + 1].strip()
            return text
        except Exception as exc:
            err_str = str(exc).lower()
            if "context length" in err_str or "maximum context" in err_str:
                raise
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
    return ""


def _parse_json_array(raw: str) -> list[str] | None:
    """Extract a JSON array of strings from LLM output.

    Falls back to regex extraction of quoted strings when the array is
    truncated (e.g. the closing bracket was cut off by a token limit).
    """
    raw = raw.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if not raw:
        return None
    # try fence block first
    m = _FENCE_BLOCK.search(raw)
    candidate = m.group(1).strip() if m else raw
    # find first [
    start = candidate.find("[")
    if start != -1:
        candidate = candidate[start:]
        # try complete array
        end = candidate.rfind("]")
        complete = candidate[: end + 1] if end != -1 else None
        if complete:
            try:
                parsed = json.loads(complete)
                if isinstance(parsed, list):
                    return [str(s) for s in parsed if str(s).strip()]
            except json.JSONDecodeError:
                pass
        # truncated — close the array and try again
        closed = candidate.rstrip().rstrip(",").rstrip() + "]"
        try:
            parsed = json.loads(closed)
            if isinstance(parsed, list):
                return [str(s) for s in parsed if str(s).strip()]
        except json.JSONDecodeError:
            pass
    # last resort: extract all quoted strings
    strings = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', raw)
    if strings:
        return [s for s in strings if s.strip()]
    return None


def _parse_json_object_array(raw: str) -> list[dict[str, Any]] | None:
    """Extract a JSON array of objects from LLM output."""
    raw = raw.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    m = _FENCE_BLOCK.search(raw)
    candidate = m.group(1).strip() if m else raw
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start != -1 and end != -1:
        candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list) and all(isinstance(x, dict) for x in parsed):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


# ---------------------------------------------------------------------------
# Stage 4a — Similarity scorer
# ---------------------------------------------------------------------------

def _score_batch(
    paper_abstract: str,
    batch: list[tuple[int, dict[str, Any]]],
    client: OpenAI,
    prompt_text: str,
    stderr: Any,
) -> dict[int, float]:
    """Score a single batch of works. Returns {global_index: score}."""
    lines = [
        "PAPER ABSTRACT UNDER REVIEW:",
        paper_abstract,
        "",
        "RELATED WORKS TO SCORE:",
    ]
    for global_idx, work in batch:
        abstract = work.get("abstract") or ""
        if abstract and len(abstract) > ABSTRACT_SNIPPET_MAX:
            abstract = abstract[:ABSTRACT_SNIPPET_MAX].rstrip() + " ..."
        year = work.get("year") or "n.d."
        title = work.get("title") or "(no title)"
        abstract_str = f"\n   Abstract: {abstract}" if abstract else "\n   Abstract: (not available)"
        lines.append(f"\n[{global_idx}] {title} ({year}){abstract_str}")

    user_msg = "\n".join(lines)
    raw = _llm_call(client, prompt_text, user_msg, max_tokens=SIMILARITY_MAX_TOKENS)
    scored = _parse_json_object_array(raw)

    result: dict[int, float] = {}
    if scored:
        for item in scored:
            try:
                idx = int(item.get("index") or 0)
                score = float(item.get("similarity_score") or 0.10)
                score = max(0.0, min(1.0, score))
                result[idx] = score
            except (TypeError, ValueError):
                continue
    else:
        print(
            "  similarity_scorer: WARNING — could not parse batch scores. "
            f"Raw: {repr(raw)[:300]}",
            file=stderr,
        )
    return result


def score_related_works(
    paper_abstract: str,
    related_works: list[dict[str, Any]],
    client: OpenAI,
    prompt_text: str,
) -> tuple[list[dict[str, Any]], float, float]:
    """Score each related work's similarity to the paper abstract via LLM.

    Batches works into groups of SIMILARITY_BATCH_SIZE to stay within
    the model context window. Returns (enriched_works, avg_similarity,
    originality_score). Works without abstracts default to 0.10 similarity.
    """
    if not related_works:
        return related_works, 0.0, 1.0

    stderr = sys.stderr
    indexed = list(enumerate(related_works, 1))
    batches = [
        indexed[i:i + SIMILARITY_BATCH_SIZE]
        for i in range(0, len(indexed), SIMILARITY_BATCH_SIZE)
    ]

    print(
        f"  similarity_scorer: scoring {len(related_works)} related works "
        f"in {len(batches)} batch(es) ...",
        file=stderr,
    )

    score_map: dict[int, float] = {}
    for batch_num, batch in enumerate(batches, 1):
        print(
            f"  similarity_scorer: batch {batch_num}/{len(batches)} "
            f"({len(batch)} works) ...",
            file=stderr,
        )
        batch_scores = _score_batch(paper_abstract, batch, client, prompt_text, stderr)
        score_map.update(batch_scores)

    enriched: list[dict[str, Any]] = []
    scores: list[float] = []
    for i, work in enumerate(related_works, 1):
        entry = dict(work)
        sim = score_map.get(i, 0.10)
        entry["similarity_score"] = round(sim, 2)
        scores.append(sim)
        enriched.append(entry)

    avg_similarity = round(sum(scores) / len(scores), 4) if scores else 0.0
    originality_score = round(1.0 - avg_similarity, 4)

    print(
        f"  similarity_scorer: avg_similarity={avg_similarity:.4f}  "
        f"originality_score={originality_score:.4f}",
        file=stderr,
    )
    return enriched, avg_similarity, originality_score


# ---------------------------------------------------------------------------
# Stage 1 — Abstract extractor
# ---------------------------------------------------------------------------

def extract_paper_abstract(kb_path: Path, fullmd_path: Path | None) -> tuple[str, str]:
    """Return (abstract_text, doc_name).

    Tries KB chunks first (section_heading contains 'abstract'), then full.md.
    """
    doc_name = ""
    abstract_parts: list[str] = []

    if kb_path.exists():
        with kb_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not doc_name:
                    doc_name = str(rec.get("doc_name") or "")
                heading = str(rec.get("section_heading") or "").lower()
                if "abstract" in heading:
                    text = str(rec.get("text") or "").strip()
                    if text:
                        abstract_parts.append(text)

    if abstract_parts:
        return " ".join(abstract_parts), doc_name

    # Fallback: parse full.md
    if fullmd_path and fullmd_path.exists():
        text = fullmd_path.read_text(encoding="utf-8")
        m = re.search(
            r"#{1,3}\s*ABSTRACT\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            abstract_text = re.sub(r"\s+", " ", m.group(1)).strip()
            # strip markdown artifacts
            abstract_text = re.sub(r"<[^>]+>", "", abstract_text).strip()
            if not doc_name:
                title_m = re.search(r"#\s+(.+)", text)
                if title_m:
                    doc_name = title_m.group(1).strip()
            return abstract_text, doc_name

    return "", doc_name


# ---------------------------------------------------------------------------
# Stage 2 — Term generator
# ---------------------------------------------------------------------------

def load_kb_chunks(kb_path: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if not kb_path.exists():
        print(f"originality_check.py: KB not found: {kb_path}", file=sys.stderr)
        return chunks
    with kb_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    chunks.append(rec)
            except json.JSONDecodeError:
                continue
    return chunks


def generate_search_terms(
    chunks: list[dict[str, Any]],
    client: OpenAI,
    prompt_text: str,
    *,
    terms_per_chunk: int,
    batch_size: int,
) -> list[str]:
    """Batch KB chunks and call LLM to get search terms. Returns deduplicated list."""
    all_terms: list[str] = []
    seen: set[str] = set()
    total = len(chunks)

    for batch_start in range(0, total, batch_size):
        batch = chunks[batch_start : batch_start + batch_size]
        num_chunks = len(batch)
        total_terms = num_chunks * terms_per_chunk

        # Build system prompt with filled-in placeholders
        system = (
            prompt_text
            .replace("{terms_per_chunk}", str(terms_per_chunk))
            .replace("{total_terms}", str(total_terms))
            .replace("{num_chunks}", str(num_chunks))
        )

        # Build user message: chunk texts
        parts = []
        for i, chunk in enumerate(batch, 1):
            heading = chunk.get("section_heading") or ""
            text = chunk.get("text") or ""
            parts.append(f"[Chunk {i} — {heading}]\n{text}")
        user_msg = "\n\n".join(parts)

        batch_end = min(batch_start + batch_size, total)
        batch_num = batch_start // batch_size + 1
        print(
            f"  term_generator: chunks {batch_start + 1}-{batch_end}/{total} "
            f"→ requesting {total_terms} term(s) ...",
            file=sys.stderr,
        )

        try:
            raw = _llm_call(client, system, user_msg, max_tokens=TERM_GEN_MAX_TOKENS)
        except Exception as exc:
            print(
                f"  term_generator: WARNING — LLM error on batch {batch_num}, skipping: {exc}",
                file=sys.stderr,
            )
            continue

        terms = _parse_json_array(raw)
        if terms is None:
            print(
                f"  term_generator: WARNING — could not parse JSON array from LLM output "
                f"(batch {batch_num}). Raw: {repr(raw)[:300]}",
                file=sys.stderr,
            )
            continue

        for term in terms:
            norm = term.strip().lower()
            if norm and norm not in seen:
                seen.add(norm)
                all_terms.append(term.strip())

    return all_terms


# ---------------------------------------------------------------------------
# Stage 3 — OpenAlex searcher
# ---------------------------------------------------------------------------

OPENALEX_SEARCH_URL = "https://api.openalex.org/works"


def _search_one_openalex(
    query: str,
    session: requests.Session,
    email: str,
    n: int,
    stderr: Any,
) -> list[dict[str, Any]]:
    """Call OpenAlex /works?search= and return work records with abstracts."""
    params = {
        "search": query,
        "per-page": str(n),
        "mailto": email,
        "select": "id,doi,title,type,publication_year,cited_by_count,abstract_inverted_index",
    }
    headers = {
        "User-Agent": f"Claim-extractor/1.0 (mailto:{email})",
        "Accept": "application/json",
    }
    time.sleep(OPENALEX_SLEEP)
    try:
        r = session.get(OPENALEX_SEARCH_URL, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        print(f"originality_check.py: OpenAlex request error for {query!r}: {exc}", file=stderr)
        return []

    if r.status_code != 200:
        print(
            f"originality_check.py: OpenAlex HTTP {r.status_code} for search {query!r}",
            file=stderr,
        )
        return []

    data = r.json()
    results = data.get("results") or []
    works: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        inv = item.get("abstract_inverted_index")
        abstract = _reconstruct_abstract(inv) if isinstance(inv, dict) else None
        works.append({
            "openalex_id": item.get("id") or "",
            "doi": item.get("doi") or "",
            "title": item.get("title") or item.get("display_name") or "",
            "type": item.get("type") or "",
            "year": item.get("publication_year"),
            "cited_by_count": item.get("cited_by_count"),
            "abstract": abstract,
        })
    return works


def fetch_related_works(
    search_terms: list[str],
    cache_path: Path,
    email: str,
    max_per_term: int,
    stderr: Any,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Search OpenAlex for all terms (with caching). Returns (deduped_works, cache)."""
    # Load existing cache
    cache: dict[str, list[dict[str, Any]]] = {}
    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cache = raw
        except (json.JSONDecodeError, OSError) as exc:
            print(
                f"originality_check.py: warning: could not read cache {cache_path}: {exc}",
                file=stderr,
            )

    session = requests.Session()
    hits = misses = 0

    for term in search_terms:
        cache_key = f"search:{term}"
        if cache_key in cache:
            hits += 1
            continue
        misses += 1
        print(f"  openalex_searcher: fetching results for {term!r} ...", file=stderr)
        works = _search_one_openalex(term, session, email, max_per_term, stderr)
        cache[cache_key] = works

    # Flush cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"originality_check.py: OpenAlex search cache hits={hits} new_fetches={misses} → {cache_path}",
        file=stderr,
    )

    # Deduplicate by openalex_id, annotate with search_term
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for term in search_terms:
        cache_key = f"search:{term}"
        for work in cache.get(cache_key, []):
            oid = work.get("openalex_id") or work.get("doi") or work.get("title") or ""
            if oid and oid in seen_ids:
                continue
            if oid:
                seen_ids.add(oid)
            entry = dict(work)
            entry["search_term"] = term
            deduped.append(entry)

    return deduped, cache


# ---------------------------------------------------------------------------
# Stage 4 — Originality writer
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    return max(int(len(text) / WRITER_CHARS_PER_TOKEN), int(len(text.split()) / 0.75))


def _format_work_entry(i: int, work: dict[str, Any]) -> str:
    abstract = work.get("abstract") or ""
    if abstract and len(abstract) > ABSTRACT_SNIPPET_MAX:
        abstract = abstract[:ABSTRACT_SNIPPET_MAX].rstrip() + " ..."
    year = work.get("year") or "n.d."
    title = work.get("title") or "(no title)"
    doi = work.get("doi") or ""
    doi_str = f" | DOI: {doi}" if doi else ""
    sim = work.get("similarity_score")
    sim_str = f" | similarity: {sim:.2f}" if sim is not None else ""
    abstract_str = f"\n   Abstract: {abstract}" if abstract else "\n   Abstract: (not available)"
    return f"\n[{i}] {title} ({year}){doi_str}{sim_str}{abstract_str}"


def write_originality_statement(
    paper_abstract: str,
    related_works: list[dict[str, Any]],
    originality_score: float,
    client: OpenAI,
    prompt_text: str,
) -> str:
    """Call LLM to produce the originality statement.

    Sorts works by similarity (highest first) and includes as many as fit
    within WRITER_INPUT_TOKEN_BUDGET so the prompt never exceeds the model
    context window.
    """
    works_with_abstracts = [w for w in related_works if w.get("abstract")]

    ranked = sorted(
        related_works,
        key=lambda w: float(w.get("similarity_score") or 0),
        reverse=True,
    )

    header_lines = [
        "PAPER ABSTRACT UNDER REVIEW:",
        paper_abstract,
        "",
        f"COMPUTED ORIGINALITY SCORE: {originality_score:.4f} (use this exact value in your closing paragraph)",
        "",
        f"RELATED WORKS ({len(works_with_abstracts)} with abstracts out of {len(related_works)} retrieved):",
    ]
    header = "\n".join(header_lines)
    prompt_tokens = _estimate_tokens(prompt_text)
    header_tokens = _estimate_tokens(header)
    budget = WRITER_INPUT_TOKEN_BUDGET - prompt_tokens - header_tokens

    print(
        f"  originality_writer: token budget — system~{prompt_tokens} "
        f"header~{header_tokens} remaining~{budget}",
        file=sys.stderr,
    )

    included: list[str] = []
    tokens_used = 0
    for i, work in enumerate(ranked, 1):
        entry = _format_work_entry(i, work)
        entry_tokens = _estimate_tokens(entry)
        if tokens_used + entry_tokens > budget and included:
            break
        included.append(entry)
        tokens_used += entry_tokens

    user_msg = header + "".join(included)
    if len(included) < len(related_works):
        user_msg += (
            f"\n\n[{len(related_works) - len(included)} additional works omitted "
            f"for brevity — scores already factored into the computed originality score above]"
        )

    print(
        f"  originality_writer: generating statement from {len(included)}/{len(related_works)} "
        f"related works (originality_score={originality_score:.4f}) ...",
        file=sys.stderr,
    )
    return _llm_call(client, prompt_text, user_msg, max_tokens=STATEMENT_MAX_TOKENS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def patch_review_json(
    review_path: Path,
    originality_score: float,
    originality_statement: str,
    stderr: Any,
) -> None:
    """Overwrite the originality category in review.json with the new score and statement.

    Also recomputes average_score from all updated category scores.
    """
    if not review_path.exists():
        print(
            f"originality_check.py: review.json not found at {review_path} — skipping patch",
            file=stderr,
        )
        return

    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"originality_check.py: could not read review.json: {exc} — skipping patch",
            file=stderr,
        )
        return

    categories = review.get("categories")
    if not isinstance(categories, dict):
        print(
            "originality_check.py: review.json has no 'categories' dict — skipping patch",
            file=stderr,
        )
        return

    # Update (or insert) the originality category
    if "originality" not in categories:
        print(
            "originality_check.py: 'originality' key not in categories — inserting it",
            file=stderr,
        )
    categories["originality"] = {
        "score": round(originality_score, 4),
        "rationale": originality_statement,
    }

    review_path.write_text(
        json.dumps(review, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"  review.json patched: originality score={originality_score:.4f} → {review_path}",
        file=stderr,
    )


def _find_fullmd(directory: Path) -> Path | None:
    """Walk up from directory to find full.md."""
    current = directory
    for _ in range(4):
        candidate = current / "full.md"
        if candidate.exists():
            return candidate
        current = current.parent
    return None


def _find_kb(directory: Path) -> Path | None:
    kb = directory / "text_knowledge_base.jsonl"
    if kb.exists():
        return kb
    # also check parent (document dir)
    kb2 = directory.parent / "text_knowledge_base.jsonl"
    if kb2.exists():
        return kb2
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Originality check: search OpenAlex for related work and write originality statement."
    )
    parser.add_argument(
        "--directory", required=True, type=Path,
        help="Pipeline output directory (e.g. articles/data/document (10)/pipe-test2)",
    )
    parser.add_argument(
        "--fullmd", type=Path, default=None,
        help="Path to full.md (auto-discovered from directory parents if omitted)",
    )
    parser.add_argument(
        "--kb", type=Path, default=None,
        help="Path to text_knowledge_base.jsonl (defaults to <directory>/text_knowledge_base.jsonl)",
    )
    parser.add_argument(
        "--openalex-cache", type=Path, default=None,
        help="Cache file for OpenAlex search results (default: <directory>/originality_openalex_cache.json)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output JSON path (default: <directory>/originality.json)",
    )
    parser.add_argument(
        "--terms-per-chunk", type=int, default=1, metavar="N",
        help="Search terms to generate per KB chunk (default: 1)",
    )
    parser.add_argument(
        "--max-results-per-term", type=int, default=5, metavar="N",
        help="Max OpenAlex results to fetch per search term (default: 5)",
    )
    parser.add_argument(
        "--chunk-batch-size", type=int, default=4, metavar="N",
        help="Number of KB chunks to process per LLM call (default: 4)",
    )
    parser.add_argument(
        "--review", type=Path, default=None,
        help="Path to review.json to patch with originality score/statement (default: <directory>/review.json)",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip LLM calls — only run OpenAlex search, output stub JSON",
    )
    args = parser.parse_args()

    directory = args.directory.expanduser().resolve()
    if not directory.exists():
        print(f"originality_check.py: directory not found: {directory}", file=sys.stderr)
        sys.exit(1)

    kb_path = (args.kb or _find_kb(directory) or directory / "text_knowledge_base.jsonl").expanduser().resolve()
    fullmd_path = (args.fullmd or _find_fullmd(directory))
    if fullmd_path:
        fullmd_path = fullmd_path.expanduser().resolve()

    cache_path = (args.openalex_cache or directory / "originality_openalex_cache.json").expanduser().resolve()
    out_path = (args.output or directory / "originality.json").expanduser().resolve()

    stderr = sys.stderr
    client: OpenAI | None = None

    # ------------------------------------------------------------------
    # Stage 1 — Extract paper abstract
    # ------------------------------------------------------------------
    print("\n=== Stage 1: abstract_extractor ===", file=stderr)
    paper_abstract, doc_name = extract_paper_abstract(kb_path, fullmd_path)
    if not paper_abstract:
        print(
            "originality_check.py: WARNING — could not extract paper abstract. "
            "Proceeding with empty abstract (originality statement will be limited).",
            file=stderr,
        )
    else:
        print(f"  Extracted abstract ({len(paper_abstract)} chars) from doc: {doc_name!r}", file=stderr)

    # ------------------------------------------------------------------
    # Stage 2 — Generate search terms
    # ------------------------------------------------------------------
    print("\n=== Stage 2: term_generator ===", file=stderr)
    chunks = load_kb_chunks(kb_path)
    print(f"  Loaded {len(chunks)} chunks from {kb_path}", file=stderr)

    if args.skip_llm:
        print("  --skip-llm: skipping term generation", file=stderr)
        search_terms: list[str] = []
    else:
        search_term_prompt = _load_prompt("search_term_prompt.md")
        if client is None:
            client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        search_terms = generate_search_terms(
            chunks,
            client,
            search_term_prompt,
            terms_per_chunk=args.terms_per_chunk,
            batch_size=args.chunk_batch_size,
        )
        print(f"  Generated {len(search_terms)} unique search terms", file=stderr)

    if not search_terms:
        print(
            "originality_check.py: WARNING — no search terms generated. "
            "OpenAlex will not be queried.",
            file=stderr,
        )

    # ------------------------------------------------------------------
    # Stage 3 — OpenAlex search
    # ------------------------------------------------------------------
    print("\n=== Stage 3: openalex_searcher ===", file=stderr)
    if search_terms:
        related_works, _ = fetch_related_works(
            search_terms,
            cache_path,
            OPENALEX_EMAIL,
            args.max_results_per_term,
            stderr,
        )
        print(f"  Retrieved {len(related_works)} unique related works", file=stderr)
    else:
        related_works = []

    # ------------------------------------------------------------------
    # Stage 4a — Similarity scoring
    # ------------------------------------------------------------------
    print("\n=== Stage 4a: similarity_scorer ===", file=stderr)
    avg_similarity: float = 0.0
    originality_score: float = 1.0

    if args.skip_llm or not related_works or not paper_abstract:
        if args.skip_llm:
            print("  --skip-llm: skipping similarity scoring", file=stderr)
        elif not related_works:
            print("  No related works — skipping similarity scoring", file=stderr)
        else:
            print("  No abstract — skipping similarity scoring", file=stderr)
    else:
        if client is None:
            client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        similarity_prompt = _load_prompt("similarity_scorer_prompt.md")
        related_works, avg_similarity, originality_score = score_related_works(
            paper_abstract,
            related_works,
            client,
            similarity_prompt,
        )

    # ------------------------------------------------------------------
    # Stage 4b — Originality statement
    # ------------------------------------------------------------------
    print("\n=== Stage 4b: originality_writer ===", file=stderr)
    if args.skip_llm or not paper_abstract:
        originality_statement = ""
        if args.skip_llm:
            print("  --skip-llm: skipping originality statement generation", file=stderr)
        else:
            print("  No abstract available — skipping statement generation", file=stderr)
    else:
        if client is None:
            client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
        originality_prompt = _load_prompt("originality_statement_prompt.md")
        originality_statement = write_originality_statement(
            paper_abstract,
            related_works,
            originality_score,
            client,
            originality_prompt,
        )

    # ------------------------------------------------------------------
    # Assemble output
    # ------------------------------------------------------------------
    output = {
        "doc_name": doc_name,
        "check_date": date.today().strftime("%B %d, %Y"),
        "paper_abstract": paper_abstract,
        "search_terms": search_terms,
        "related_works_count": len(related_works),
        "avg_similarity_score": avg_similarity,
        "originality_score": originality_score,
        "related_works": related_works,
        "originality_statement": originality_statement,
    }

    text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"\nOriginality check written to {out_path}", file=stderr)

    # ------------------------------------------------------------------
    # Patch review.json with originality score + statement
    # ------------------------------------------------------------------
    if not args.skip_llm and originality_statement:
        review_path = (args.review or directory / "review.json").expanduser().resolve()
        print("\n=== Patching review.json ===", file=stderr)
        patch_review_json(review_path, originality_score, originality_statement, stderr)


if __name__ == "__main__":
    main()
