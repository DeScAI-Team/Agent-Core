"""Sliding-window document screener.

Reads the full paper (full.md) through overlapping windows and screens each
window against every mapping dimension and cross-cutting tag.  Findings are
deduplicated, grouped by dimension, and an LLM writes rationales for each
dimension that surfaced relevant observations.  Finally, review.json is
patched with new or enriched categories.

Stages:
  1. window_builder     — split full.md into overlapping ~2500-token windows,
                          extract citation numbers, attach OpenAlex abstracts
  2. window_screener    — LLM screens each window for dimension-relevant signals
  3. dedup_aggregate    — deduplicate findings, group by dimension
  4. category_writer    — LLM writes a rationale per dimension with findings
  5. patch_review       — merge screener results into review.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from openai import OpenAI

from retrieve_compare import (
    extract_citation_numbers,
    parse_reference_index,
    reconstruct_abstract,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE = Path(__file__).resolve().parent
PIPELINE_DIR = _BASE.parent
PROMPTS_DIR = _BASE / "prompts"
MAPPINGS_PATH = PIPELINE_DIR / "mappings.json"

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")

MAX_RETRIES = 4
WINDOW_TOKEN_TARGET = 1500
WINDOW_OVERLAP_TOKENS = 300
SCREENER_MAX_TOKENS = 1024
CATEGORY_WRITER_MAX_TOKENS = 2048
ABSTRACT_SNIPPET_MAX = 200
MAX_CITED_ABSTRACTS_PER_WINDOW = 3
MODEL_CONTEXT_LIMIT = 16384

_FENCE_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.I)

VALID_SEVERITIES = frozenset({"info", "concern", "red_flag"})

EXCLUDED_DIMENSIONS = frozenset({"cross_cutting", "governance_accountability"})
MAX_REVIEW_CATEGORIES = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    return max(int(len(text) / 3.2), int(len(text.split()) / 0.75))


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _normalize_doi_key(doi: str) -> str:
    d = doi.strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    return d.lower()


def _lookup_ox(cache: dict[str, Any], doi: str | None) -> dict[str, Any] | None:
    if not doi:
        return None
    key = _normalize_doi_key(doi)
    rec = cache.get(key)
    return rec if isinstance(rec, dict) else None


def _strip_model_noise(text: str) -> str:
    t = text.strip()
    fm = _FENCE_BLOCK.search(t)
    if fm:
        t = fm.group(1).strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL).strip()
    t = re.sub(r"<think>.*", "", t, flags=re.DOTALL).strip()
    t = re.sub(r"<reasoning>.*?</reasoning>", "", t, flags=re.DOTALL).strip()
    t = re.sub(r"<reasoning>.*", "", t, flags=re.DOTALL).strip()
    return t


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    for candidate in (_strip_model_noise(raw), raw.strip()):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class _ContextOverflow(Exception):
    """Raised when the prompt exceeds the model context window."""


def _is_context_overflow(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "context length" in msg or "maximum context" in msg or "too many tokens" in msg


def _llm_call(
    client: OpenAI,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
) -> str:
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
            return text
        except _ContextOverflow:
            raise
        except Exception as exc:
            if _is_context_overflow(exc):
                raise _ContextOverflow(str(exc)) from exc
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}")
            if attempt == MAX_RETRIES:
                raise
    return ""


def _llm_json_call(
    client: OpenAI,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
) -> dict[str, Any] | None:
    raw = _llm_call(client, system_prompt, user_content, max_tokens)
    return _parse_json_object(raw)


# ---------------------------------------------------------------------------
# Stage 1 — Window Builder
# ---------------------------------------------------------------------------

def _split_windows(text: str) -> list[dict[str, Any]]:
    """Split document text into overlapping token windows."""
    words = text.split()
    if not words:
        return []

    stride_words = int((WINDOW_TOKEN_TARGET - WINDOW_OVERLAP_TOKENS) * 0.75)
    window_words = int(WINDOW_TOKEN_TARGET * 0.75)
    stride_words = max(stride_words, 1)
    window_words = max(window_words, stride_words)

    windows: list[dict[str, Any]] = []
    i = 0
    while i < len(words):
        end = min(i + window_words, len(words))
        chunk = " ".join(words[i:end])
        windows.append({
            "window_idx": len(windows),
            "text": chunk,
            "token_estimate": _estimate_tokens(chunk),
        })
        if end >= len(words):
            break
        i += stride_words

    return windows


def window_builder(
    full_md_text: str,
    ref_index: dict[int, dict[str, Any]],
    openalex_cache: dict[str, Any],
    stderr: Any,
) -> list[dict[str, Any]]:
    """Stage 1: build windows with citation context."""
    max_ref = max(ref_index.keys()) if ref_index else None
    windows = _split_windows(full_md_text)
    print(f"  Built {len(windows)} windows", file=stderr)

    for win in windows:
        cites = extract_citation_numbers(win["text"], max_ref)
        abstracts: list[dict[str, Any]] = []
        for ref_n in cites:
            ref_meta = ref_index.get(ref_n) or {}
            doi = ref_meta.get("doi")
            ox = _lookup_ox(openalex_cache, str(doi) if doi else None)
            if not ox or ox.get("status") != "ok":
                continue
            ab = ox.get("abstract")
            if not ab:
                continue
            snippet = ab[:ABSTRACT_SNIPPET_MAX] if len(ab) > ABSTRACT_SNIPPET_MAX else ab
            abstracts.append({
                "ref_number": ref_n,
                "doi": doi,
                "title": ox.get("title"),
                "abstract_snippet": snippet,
            })
        win["citation_numbers"] = cites
        win["cited_abstracts"] = abstracts[:MAX_CITED_ABSTRACTS_PER_WINDOW]

    cited_windows = sum(1 for w in windows if w["cited_abstracts"])
    print(
        f"  {cited_windows}/{len(windows)} windows have cited abstracts attached",
        file=stderr,
    )
    return windows


# ---------------------------------------------------------------------------
# Stage 2 — Window Screening (LLM)
# ---------------------------------------------------------------------------

def _build_dimension_checklist(mappings: dict) -> str:
    lines: list[str] = []
    dims = mappings.get("dimensions", {})
    for key, dim in dims.items():
        tags = ", ".join(dim.get("tags", []))
        lines.append(
            f"- **{dim['label']}** (`{key}`): {dim['question']}  "
            f"Tags: {tags}"
        )
    cc = mappings.get("cross_cutting", {})
    if cc:
        tags = ", ".join(cc.get("tags", []))
        lines.append(
            f"- **{cc.get('label', 'Cross-Cutting')}** (`cross_cutting`): "
            f"{cc.get('description', '')}  Tags: {tags}"
        )
    return "\n".join(lines)


def _build_coverage_summary(review: dict) -> str:
    cats = review.get("categories", {})
    if not cats:
        return "No categories have been evaluated yet."
    lines: list[str] = []
    for key, cat in cats.items():
        if not isinstance(cat, dict):
            continue
        score = cat.get("score")
        rationale = cat.get("rationale", "")
        has_content = bool(rationale and len(rationale) > 50)
        lines.append(
            f"- `{key}`: score={score}, "
            f"{'has rationale' if has_content else 'minimal/no rationale'}"
        )
    return "\n".join(lines) if lines else "No categories have been evaluated yet."


def window_screener(
    windows: list[dict[str, Any]],
    mappings: dict,
    review: dict,
    system_prompt: str,
    client: OpenAI,
    stderr: Any,
) -> list[dict[str, Any]]:
    """Stage 2: screen each window via LLM."""
    checklist = _build_dimension_checklist(mappings)
    coverage = _build_coverage_summary(review)

    all_dim_keys = set(mappings.get("dimensions", {}).keys())
    if mappings.get("cross_cutting"):
        all_dim_keys.add("cross_cutting")

    all_findings: list[dict[str, Any]] = []
    n = len(windows)

    system_tokens = _estimate_tokens(system_prompt)
    checklist_block = f"\n--- DIMENSION CHECKLIST ---\n{checklist}\n"
    coverage_block = f"\n--- EXISTING REVIEW COVERAGE ---\n{coverage}\n"
    static_tokens = (
        system_tokens
        + _estimate_tokens(checklist_block)
        + _estimate_tokens(coverage_block)
        + SCREENER_MAX_TOKENS
    )
    user_budget = MODEL_CONTEXT_LIMIT - static_tokens - 200  # 200 token safety margin

    for win in windows:
        idx = win["window_idx"]
        print(f"  Screening window {idx + 1}/{n} ...", file=stderr)

        user_parts = [
            f"[Window {idx + 1} of {n}]\n",
            f"--- DOCUMENT TEXT ---\n{win['text']}\n",
        ]

        refs_block = ""
        if win["cited_abstracts"]:
            ref_lines = ["\n--- CITED REFERENCE ABSTRACTS (from this window) ---\n"]
            for ref in win["cited_abstracts"]:
                ref_lines.append(
                    f"[ref {ref['ref_number']}] {ref.get('title', 'untitled')}\n"
                    f"{ref['abstract_snippet']}\n"
                )
            refs_block = "\n".join(ref_lines)

        text_tokens = _estimate_tokens("\n".join(user_parts))
        refs_tokens = _estimate_tokens(refs_block)
        if text_tokens + refs_tokens > user_budget and refs_block:
            refs_block = ""
            print(f"    Window {idx + 1}: dropped cited abstracts to fit context", file=stderr)

        if refs_block:
            user_parts.append(refs_block)
        user_parts.append(checklist_block)
        user_parts.append(coverage_block)

        user_content = "\n".join(user_parts)

        try:
            parsed = _llm_json_call(
                client, system_prompt, user_content, SCREENER_MAX_TOKENS,
            )
        except _ContextOverflow:
            print(f"    Window {idx + 1}: SKIPPED — prompt too large for context", file=stderr)
            continue
        if not parsed:
            print(f"    Window {idx + 1}: no parseable JSON returned", file=stderr)
            continue

        findings = parsed.get("findings")
        if not isinstance(findings, list):
            continue

        for f in findings:
            if not isinstance(f, dict):
                continue
            dim = f.get("dimension", "")
            if dim not in all_dim_keys:
                continue
            if dim in EXCLUDED_DIMENSIONS:
                continue
            severity = f.get("severity", "info")
            if severity not in VALID_SEVERITIES:
                severity = "info"
            f["severity"] = severity
            f["window_idx"] = idx
            all_findings.append(f)

        print(
            f"    Window {idx + 1}: {len(findings)} finding(s)",
            file=stderr,
        )

    return all_findings


# ---------------------------------------------------------------------------
# Stage 3 — Deduplication & Aggregation
# ---------------------------------------------------------------------------

def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.casefold().strip())


def _is_duplicate(a: dict, b: dict) -> bool:
    if a.get("dimension") != b.get("dimension"):
        return False
    obs_a = _normalize_text(a.get("observation", ""))
    obs_b = _normalize_text(b.get("observation", ""))
    if not obs_a or not obs_b:
        return False
    shorter, longer = sorted([obs_a, obs_b], key=len)
    if shorter in longer:
        return True
    words_a = set(obs_a.split())
    words_b = set(obs_b.split())
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
    return overlap > 0.75


def dedup_aggregate(
    findings: list[dict[str, Any]],
    stderr: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Stage 3: deduplicate and group by dimension."""
    deduped: list[dict[str, Any]] = []
    for f in findings:
        if not any(_is_duplicate(f, existing) for existing in deduped):
            deduped.append(f)

    removed = len(findings) - len(deduped)
    if removed:
        print(f"  Removed {removed} duplicate finding(s)", file=stderr)

    by_dim: dict[str, list[dict[str, Any]]] = {}
    for f in deduped:
        dim = f["dimension"]
        by_dim.setdefault(dim, []).append(f)

    for dim, items in sorted(by_dim.items()):
        print(f"  {dim}: {len(items)} finding(s)", file=stderr)

    return by_dim


# ---------------------------------------------------------------------------
# Stage 4 — Category Writer (LLM)
# ---------------------------------------------------------------------------

def _get_label(dim_key: str, mappings: dict) -> str:
    dims = mappings.get("dimensions", {})
    if dim_key in dims:
        return dims[dim_key].get("label", dim_key)
    if dim_key == "cross_cutting":
        return mappings.get("cross_cutting", {}).get("label", "Cross-Cutting / Flags")
    return dim_key.replace("_", " ").title()


def _get_question(dim_key: str, mappings: dict) -> str:
    dims = mappings.get("dimensions", {})
    if dim_key in dims:
        return dims[dim_key].get("question", "")
    return ""


def category_writer(
    grouped: dict[str, list[dict[str, Any]]],
    mappings: dict,
    review: dict,
    writer_prompt: str,
    client: OpenAI,
    stderr: Any,
) -> dict[str, dict[str, Any]]:
    """Stage 4: LLM writes a rationale + score per dimension with findings."""
    results: dict[str, dict[str, Any]] = {}
    existing_cats = review.get("categories", {})

    for dim_key, findings in grouped.items():
        if not findings:
            continue
        if dim_key in EXCLUDED_DIMENSIONS:
            continue

        label = _get_label(dim_key, mappings)
        question = _get_question(dim_key, mappings)
        existing = existing_cats.get(dim_key)

        print(f"  Writing rationale for {label} ({len(findings)} findings) ...", file=stderr)

        findings_text = json.dumps(findings, indent=2, ensure_ascii=False)

        user_parts = [
            f"Dimension: {label} (`{dim_key}`)",
            f"Guiding question: {question}" if question else "",
            f"\nScreener findings ({len(findings)} total):\n{findings_text}",
        ]
        if existing and isinstance(existing, dict):
            user_parts.append(
                f"\n\nExisting review rationale (from claim-level pipeline):\n"
                f"Score: {existing.get('score')}\n"
                f"Rationale: {existing.get('rationale', '(none)')}"
            )
        else:
            user_parts.append(
                "\n\nThis dimension has NO existing coverage in the review. "
                "You are providing the first assessment."
            )

        user_content = "\n".join(p for p in user_parts if p)

        parsed = _llm_json_call(
            client, writer_prompt, user_content, CATEGORY_WRITER_MAX_TOKENS,
        )
        if not parsed:
            print(f"    {label}: LLM returned no parseable JSON", file=stderr)
            continue

        rationale = parsed.get("rationale", "")

        if not rationale:
            print(f"    {label}: empty rationale, skipping", file=stderr)
            continue

        results[dim_key] = {
            "score": None,
            "rationale": rationale,
            "is_new": existing is None,
            "finding_count": len(findings),
            "_findings": findings,
        }

    return results


# ---------------------------------------------------------------------------
# Stage 5 — Patch review.json
# ---------------------------------------------------------------------------

def patch_review(
    review_path: Path,
    writer_results: dict[str, dict[str, Any]],
    stderr: Any,
) -> None:
    """Merge screener categories into review.json."""
    if not writer_results:
        print("  No screener categories to patch.", file=stderr)
        return

    if not review_path.exists():
        print(
            f"screener.py: review.json not found at {review_path} — skipping patch",
            file=stderr,
        )
        return

    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"screener.py: could not read review.json: {exc} — skipping patch",
            file=stderr,
        )
        return

    categories = review.get("categories")
    if not isinstance(categories, dict):
        print(
            "screener.py: review.json has no 'categories' dict — skipping patch",
            file=stderr,
        )
        return

    for ex_dim in EXCLUDED_DIMENSIONS:
        if ex_dim in categories:
            del categories[ex_dim]
            print(f"  Removed excluded category: {ex_dim}", file=stderr)

    existing_keys = set(categories.keys())
    slots_available = MAX_REVIEW_CATEGORIES - len(existing_keys)

    enrichments: dict[str, dict[str, Any]] = {}
    insertions: dict[str, dict[str, Any]] = {}
    for dim_key, result in writer_results.items():
        if dim_key in existing_keys:
            enrichments[dim_key] = result
        else:
            insertions[dim_key] = result

    for dim_key, result in enrichments.items():
        existing = categories[dim_key]
        rationale = result["rationale"]
        old_rationale = existing.get("rationale", "")
        if old_rationale:
            merged = (
                f"{old_rationale}\n\n"
                f"[Document screener — {result['finding_count']} additional "
                f"observation(s)]: {rationale}"
            )
        else:
            merged = rationale
        categories[dim_key] = {
            "score": existing.get("score", 0.5),
            "rationale": merged,
        }
        print(
            f"  Enriched existing category: {dim_key} "
            f"(score: {categories[dim_key]['score']})",
            file=stderr,
        )

    inserted = 0
    severity_order = {"red_flag": 0, "concern": 1, "info": 2}
    ranked = sorted(
        insertions.items(),
        key=lambda kv: min(
            severity_order.get(f.get("severity", "info"), 2)
            for f in kv[1].get("_findings", [{}])
        ) if kv[1].get("_findings") else 2,
    )
    for dim_key, result in ranked:
        if inserted >= slots_available:
            print(
                f"  Skipped new category {dim_key}: "
                f"at {MAX_REVIEW_CATEGORIES}-category cap",
                file=stderr,
            )
            continue
        categories[dim_key] = {
            "score": None,
            "rationale": result["rationale"],
        }
        inserted += 1
        print(
            f"  Inserted new category: {dim_key} "
            f"(score: {categories[dim_key]['score']})",
            file=stderr,
        )

    review_path.write_text(
        json.dumps(review, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"  review.json patched: {len(writer_results)} category/ies → {review_path}",
        file=stderr,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sliding-window document screener: find dimension-relevant "
        "signals the claim pipeline missed."
    )
    parser.add_argument(
        "--fullmd", required=True, type=Path,
        help="Path to full.md (the full paper in markdown)",
    )
    parser.add_argument(
        "--openalex-cache", type=Path, default=None,
        help="Path to openalex_cache.json (from retrieve_compare step)",
    )
    parser.add_argument(
        "--mappings", type=Path, default=MAPPINGS_PATH,
        help=f"mappings.json path (default: {MAPPINGS_PATH})",
    )
    parser.add_argument(
        "--review", type=Path, default=None,
        help="Path to review.json to read existing coverage and patch results into",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write screener diagnostic JSON here (default: stdout)",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip LLM calls — only build windows and output them for debugging",
    )
    args = parser.parse_args()

    stderr = sys.stderr
    fullmd_path = args.fullmd.expanduser().resolve()
    mappings_path = args.mappings.expanduser().resolve()

    print("Loading inputs ...", file=stderr)
    full_md_text = fullmd_path.read_text(encoding="utf-8")
    mappings = json.loads(mappings_path.read_text(encoding="utf-8"))

    openalex_cache: dict[str, Any] = {}
    if args.openalex_cache:
        cache_path = args.openalex_cache.expanduser().resolve()
        if cache_path.exists():
            openalex_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"  Loaded OpenAlex cache ({len(openalex_cache)} entries)", file=stderr)

    review: dict[str, Any] = {}
    review_path: Path | None = None
    if args.review:
        review_path = args.review.expanduser().resolve()
        if review_path.exists():
            review = json.loads(review_path.read_text(encoding="utf-8"))
            cats = review.get("categories", {})
            print(
                f"  Loaded review.json ({len(cats)} existing categories)",
                file=stderr,
            )

    # ------------------------------------------------------------------
    # Stage 1 — Window Builder
    # ------------------------------------------------------------------
    print("\n=== Stage 1: window_builder ===", file=stderr)
    ref_index = parse_reference_index(full_md_text, stderr)
    windows = window_builder(full_md_text, ref_index, openalex_cache, stderr)

    if args.skip_llm:
        print("  --skip-llm: stopping after window builder", file=stderr)
        output = {
            "doc_name": review.get("research_name", review.get("research_dao_name", "")),
            "check_date": date.today().strftime("%B %d, %Y"),
            "windows_count": len(windows),
            "windows": [
                {
                    "window_idx": w["window_idx"],
                    "token_estimate": w["token_estimate"],
                    "citation_numbers": w["citation_numbers"],
                    "cited_abstracts_count": len(w["cited_abstracts"]),
                }
                for w in windows
            ],
            "findings": [],
            "grouped_findings": {},
            "writer_results": {},
        }
        text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            out_path = args.output.expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"\nScreener debug output written to {out_path}", file=stderr)
        else:
            print(text, end="")
        return

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    # ------------------------------------------------------------------
    # Stage 2 — Window Screening
    # ------------------------------------------------------------------
    print("\n=== Stage 2: window_screener ===", file=stderr)
    screener_prompt = _load_prompt("screener_system_prompt.md")
    findings = window_screener(
        windows, mappings, review, screener_prompt, client, stderr,
    )
    print(f"  Total raw findings: {len(findings)}", file=stderr)

    # ------------------------------------------------------------------
    # Stage 3 — Dedup & Aggregate
    # ------------------------------------------------------------------
    print("\n=== Stage 3: dedup_aggregate ===", file=stderr)
    grouped = dedup_aggregate(findings, stderr)

    writer_results: dict[str, dict[str, Any]] = {}

    if not grouped:
        print("  No findings to process. Skipping stages 4-5.", file=stderr)
    else:
        # ------------------------------------------------------------------
        # Stage 4 — Category Writer
        # ------------------------------------------------------------------
        print("\n=== Stage 4: category_writer ===", file=stderr)
        writer_prompt = _load_prompt("screener_category_writer_prompt.md")
        writer_results = category_writer(
            grouped, mappings, review, writer_prompt, client, stderr,
        )

        # ------------------------------------------------------------------
        # Stage 5 — Patch review.json
        # ------------------------------------------------------------------
        if writer_results and review_path:
            print("\n=== Stage 5: patch_review ===", file=stderr)
            patch_review(review_path, writer_results, stderr)
        elif writer_results and not review_path:
            print(
                "\n  No --review path provided; skipping review.json patch.",
                file=stderr,
            )

    # ------------------------------------------------------------------
    # Write diagnostic output
    # ------------------------------------------------------------------
    output = {
        "doc_name": review.get("research_name", review.get("research_dao_name", "")),
        "check_date": date.today().strftime("%B %d, %Y"),
        "windows_count": len(windows),
        "total_findings_raw": len(findings),
        "total_findings_deduped": sum(len(v) for v in grouped.values()),
        "dimensions_with_findings": sorted(grouped.keys()),
        "findings_by_dimension": {
            dim: items for dim, items in sorted(grouped.items())
        },
        "writer_results": {
            dim: {"score": r["score"], "rationale": r["rationale"]}
            for dim, r in writer_results.items()
        },
    }

    text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        out_path = args.output.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"\nScreener output written to {out_path}", file=stderr)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
