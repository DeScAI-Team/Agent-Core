#!/usr/bin/env python3
"""Lightweight proposal review pipeline.

Takes a JSON file from the ResearchHub crawler (fulltext + funding metadata),
runs a sliding-window screener, OpenAlex originality search, funding realism
assessment, and produces a scored review.json compatible with the article
pipeline frontend.

Usage:
  python proposal_pipe.py --input-json ../test-data/proposals/proposal_4459.json
  python proposal_pipe.py --input-json ... --output-dir ./output
  python proposal_pipe.py --input-json ... --skip-llm   # debug: windows only
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

# ---------------------------------------------------------------------------
# Path setup — import reusable pieces from articles/pipeline/empirical/
# ---------------------------------------------------------------------------

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parent.parent
_EMPIRICAL = _REPO_ROOT / "articles" / "pipeline" / "empirical"
_PROMPTS = _BASE / "prompts"
_MAPPINGS = _BASE / "proposal_mappings.json"

if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

from originality_check import (  # noqa: E402
    generate_search_terms,
    fetch_related_works,
    score_related_works,
    write_originality_statement,
    _parse_json_array,
    _reconstruct_abstract,
    OPENALEX_SLEEP,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("VALIDATOR_MODEL", "/model")
OPENALEX_EMAIL = os.environ.get("OPENALEX_EMAIL", "team@descai.org")

WINDOW_TOKEN_TARGET = 1500
WINDOW_OVERLAP_TOKENS = 300
SCREENER_MAX_TOKENS = 1024
CATEGORY_WRITER_MAX_TOKENS = 2048
FUNDING_MAX_TOKENS = 1024
STATEMENT_MAX_TOKENS = 1024
TERM_GEN_MAX_TOKENS = 1024
SIMILARITY_BATCH_SIZE = 25
MODEL_CONTEXT_LIMIT = 16384

TERMS_PER_CHUNK = 3
TERM_BATCH_SIZE = 5
MAX_RESULTS_PER_TERM = 5

MAX_RETRIES = 4
VALID_SEVERITIES = frozenset({"info", "concern", "red_flag"})
EXCLUDED_DIMENSIONS = frozenset({"cross_cutting"})
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.I)


def _llm_call(client: OpenAI, system_prompt: str, user_content: str, max_tokens: int) -> str:
    """Local LLM call that preserves JSON output intact (no prose truncation)."""
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
        except Exception as exc:
            err_str = str(exc).lower()
            if "context length" in err_str or "maximum context" in err_str:
                raise
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
    return ""


def _estimate_tokens(text: str) -> int:
    return max(int(len(text) / 3.2), int(len(text.split()) / 0.75))


def _load_prompt(filename: str) -> str:
    return (_PROMPTS / filename).read_text(encoding="utf-8")


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
            pass
        brace_start = candidate.find("{")
        brace_end = candidate.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                obj = json.loads(candidate[brace_start : brace_end + 1])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    return None


def _llm_json_call(
    client: OpenAI, system_prompt: str, user_content: str, max_tokens: int,
    retries: int = 2,
) -> dict[str, Any] | None:
    for attempt in range(retries + 1):
        raw = _llm_call(client, system_prompt, user_content, max_tokens)
        result = _parse_json_object(raw)
        if result is not None:
            return result
        if attempt < retries:
            print(f"    JSON parse failed (attempt {attempt + 1}), retrying ...", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def load_proposal(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def extract_fulltext(proposal: dict) -> str:
    body = proposal.get("proposal-body", {})
    return body.get("text", "")


def extract_abstract(fulltext: str) -> str:
    """Pull the abstract from the proposal fulltext, or use the first ~2000 chars."""
    m = re.search(
        r"(?:^|\n)\s*(?:#{1,3}\s*)?(?:abstract|summary)\s*\n(.*?)(?=\n#{1,3}\s|\n\s*(?:background|introduction|methods|objectives)\s*\n|\Z)",
        fulltext,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(text) > 100:
            return text
    return fulltext[:3000].strip()


def compute_funding_snapshot(proposal: dict) -> dict[str, Any] | None:
    goal = proposal.get("goal_amount")
    raised = proposal.get("amount_raised", 0)
    contributors = proposal.get("contributor_count", 0)
    dates = proposal.get("dates", {})

    if not goal or goal <= 0:
        return None

    now = datetime.now(timezone.utc)

    start_str = dates.get("fundraise_start")
    end_str = dates.get("fundraise_end")

    try:
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else now
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else now
    except (ValueError, AttributeError):
        return {
            "goal_amount": goal,
            "amount_raised": round(raised, 2),
            "percent_funded": round(raised / goal * 100, 1),
            "contributor_count": contributors,
            "days_remaining": None,
            "days_total": None,
            "on_track": None,
        }

    days_total = max((end - start).total_seconds() / 86400, 1)
    days_elapsed = max((now - start).total_seconds() / 86400, 0)
    days_remaining = max((end - now).total_seconds() / 86400, 0)

    velocity = raised / max(days_elapsed, 1)
    projected = velocity * days_total

    return {
        "goal_amount": goal,
        "amount_raised": round(raised, 2),
        "percent_funded": round(raised / goal * 100, 1),
        "contributor_count": contributors,
        "days_remaining": round(days_remaining),
        "days_total": round(days_total),
        "on_track": projected >= goal,
    }


# ---------------------------------------------------------------------------
# Stage 1 — Window Builder
# ---------------------------------------------------------------------------

def split_windows(text: str) -> list[dict[str, Any]]:
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


# ---------------------------------------------------------------------------
# Stage 2 — Sliding-Window Screener
# ---------------------------------------------------------------------------

def _build_dimension_checklist(mappings: dict) -> str:
    lines: list[str] = []
    dims = mappings.get("dimensions", {})
    for key, dim in dims.items():
        tags = ", ".join(dim.get("tags", []))
        tag_str = f"  Tags: {tags}" if tags else ""
        lines.append(f"- **{dim['label']}** (`{key}`): {dim['question']}{tag_str}")
    cc = mappings.get("cross_cutting", {})
    if cc:
        tags = ", ".join(cc.get("tags", []))
        lines.append(
            f"- **{cc.get('label', 'Cross-Cutting')}** (`cross_cutting`): "
            f"{cc.get('description', '')}  Tags: {tags}"
        )
    return "\n".join(lines)


def _build_coverage_summary(dim_findings: dict[str, int]) -> str:
    if not dim_findings:
        return "No dimensions have been screened yet."
    lines: list[str] = []
    for key, count in dim_findings.items():
        lines.append(f"- `{key}`: {count} finding(s) so far")
    return "\n".join(lines)


def screen_windows(
    windows: list[dict[str, Any]],
    mappings: dict,
    system_prompt: str,
    client: OpenAI,
    stderr: Any,
) -> list[dict[str, Any]]:
    checklist = _build_dimension_checklist(mappings)
    all_dim_keys = set(mappings.get("dimensions", {}).keys())
    if mappings.get("cross_cutting"):
        all_dim_keys.add("cross_cutting")

    all_findings: list[dict[str, Any]] = []
    dim_counts: dict[str, int] = {}
    n = len(windows)

    system_tokens = _estimate_tokens(system_prompt)
    checklist_block = f"\n--- DIMENSION CHECKLIST ---\n{checklist}\n"
    static_tokens = system_tokens + _estimate_tokens(checklist_block) + SCREENER_MAX_TOKENS
    user_budget = MODEL_CONTEXT_LIMIT - static_tokens - 300

    for win in windows:
        idx = win["window_idx"]
        print(f"  Screening window {idx + 1}/{n} ...", file=stderr)

        coverage_block = f"\n--- COVERAGE SO FAR ---\n{_build_coverage_summary(dim_counts)}\n"
        user_parts = [
            f"[Window {idx + 1} of {n}]\n",
            f"--- PROPOSAL TEXT ---\n{win['text']}\n",
            checklist_block,
            coverage_block,
        ]
        user_content = "\n".join(user_parts)

        text_tokens = _estimate_tokens(user_content)
        if text_tokens > user_budget:
            trimmed = " ".join(win["text"].split()[:int(user_budget * 0.6)])
            user_parts[1] = f"--- PROPOSAL TEXT ---\n{trimmed}\n"
            user_content = "\n".join(user_parts)

        parsed = _llm_json_call(client, system_prompt, user_content, SCREENER_MAX_TOKENS)
        if not parsed:
            print(f"    Window {idx + 1}: no parseable JSON returned", file=stderr)
            continue

        findings = parsed.get("findings")
        if not isinstance(findings, list):
            continue

        window_count = 0
        for f in findings:
            if not isinstance(f, dict):
                continue
            dim = f.get("dimension", "")
            if dim not in all_dim_keys or dim in EXCLUDED_DIMENSIONS:
                continue
            severity = f.get("severity", "info")
            if severity not in VALID_SEVERITIES:
                severity = "info"
            f["severity"] = severity
            f["window_idx"] = idx
            all_findings.append(f)
            dim_counts[dim] = dim_counts.get(dim, 0) + 1
            window_count += 1

        print(f"    Window {idx + 1}: {window_count} finding(s)", file=stderr)

    return all_findings


# ---------------------------------------------------------------------------
# Stage 2b — Dedup & Aggregate
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
    findings: list[dict[str, Any]], stderr: Any,
) -> dict[str, list[dict[str, Any]]]:
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
# Stage 2c — Category Writer
# ---------------------------------------------------------------------------

def write_category_rationales(
    grouped: dict[str, list[dict[str, Any]]],
    mappings: dict,
    writer_prompt: str,
    client: OpenAI,
    stderr: Any,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}

    for dim_key, findings in grouped.items():
        if not findings or dim_key in EXCLUDED_DIMENSIONS:
            continue

        dims = mappings.get("dimensions", {})
        dim_info = dims.get(dim_key, {})
        label = dim_info.get("label", dim_key.replace("_", " ").title())
        question = dim_info.get("question", "")

        print(f"  Writing rationale for {label} ({len(findings)} findings) ...", file=stderr)

        findings_text = json.dumps(findings, indent=2, ensure_ascii=False)
        user_parts = [
            f"Dimension: {label} (`{dim_key}`)",
            f"Guiding question: {question}" if question else "",
            f"\nScreener findings ({len(findings)} total):\n{findings_text}",
            "\n\nThis dimension has no prior coverage. You are providing the first assessment.",
        ]
        user_content = "\n".join(p for p in user_parts if p)

        parsed = _llm_json_call(client, writer_prompt, user_content, CATEGORY_WRITER_MAX_TOKENS)
        if not parsed:
            print(f"    {label}: LLM returned no parseable JSON", file=stderr)
            continue

        rationale = parsed.get("rationale", "")
        if not rationale:
            print(f"    {label}: empty rationale, skipping", file=stderr)
            continue

        results[dim_key] = {
            "rationale": rationale,
            "finding_count": len(findings),
            "_findings": findings,
        }

    return results


# ---------------------------------------------------------------------------
# Stage 2 scoring — rubric penalty for screener-scored dimensions
# ---------------------------------------------------------------------------

def score_rubric_dimension(
    findings: list[dict[str, Any]], rubric: dict,
) -> float:
    baseline = rubric.get("baseline", 0.6)
    penalties = rubric.get("penalties", {})
    floor = rubric.get("floor", 0.1)

    score = baseline
    for f in findings:
        sev = f.get("severity", "info")
        score += penalties.get(sev, 0.0)

    return max(floor, min(1.0, round(score, 4)))


# ---------------------------------------------------------------------------
# Stage 3 — OpenAlex Search + Originality
# ---------------------------------------------------------------------------

def run_originality(
    fulltext: str,
    abstract: str,
    output_dir: Path,
    client: OpenAI,
    stderr: Any,
) -> dict[str, Any]:
    """Generate search terms from fulltext, query OpenAlex, score similarity."""
    print("\n=== Stage 3: OpenAlex Search + Originality ===", file=stderr)

    search_term_prompt = _load_prompt("search_term_prompt.md")
    similarity_prompt = _load_prompt("similarity_scorer_prompt.md")
    originality_prompt = _load_prompt("originality_statement_prompt.md")

    words = fulltext.split()
    chunk_size = 500
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk_text = " ".join(words[i:i + chunk_size])
        chunks.append({"text": chunk_text, "section_heading": f"chunk_{i // chunk_size}"})

    if not chunks:
        return {"score": 0.5, "rationale": "No text available to analyze for originality."}

    print(f"  Split fulltext into {len(chunks)} chunks for term generation", file=stderr)

    search_terms = generate_search_terms(
        chunks, client, search_term_prompt,
        terms_per_chunk=TERMS_PER_CHUNK,
        batch_size=TERM_BATCH_SIZE,
    )
    print(f"  Generated {len(search_terms)} search terms", file=stderr)

    if not search_terms:
        return {"score": 0.5, "rationale": "Could not generate search terms for originality comparison."}

    cache_path = output_dir / "openalex_search_cache.json"
    related_works, _ = fetch_related_works(
        search_terms, cache_path, OPENALEX_EMAIL, MAX_RESULTS_PER_TERM, stderr,
    )
    print(f"  {len(related_works)} deduplicated related works found", file=stderr)

    if not related_works:
        return {"score": 0.8, "rationale": "No related works found in OpenAlex, suggesting high originality."}

    scored_works, avg_similarity, originality_score = score_related_works(
        abstract, related_works, client, similarity_prompt,
    )

    statement = write_originality_statement(
        abstract, scored_works, originality_score, client, originality_prompt,
    )

    originality_data = {
        "originality_score": originality_score,
        "avg_similarity_score": avg_similarity,
        "related_works_count": len(scored_works),
        "related_works": scored_works,
    }
    orig_path = output_dir / "originality.json"
    orig_path.write_text(
        json.dumps(originality_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  Originality data saved to {orig_path}", file=stderr)

    return {
        "score": originality_score,
        "rationale": statement,
    }


# ---------------------------------------------------------------------------
# Stage 4 — Funding Realism
# ---------------------------------------------------------------------------

def _extract_budget_text(fulltext: str) -> str:
    """Try to extract the budget section from the proposal text."""
    patterns = [
        r"(?:^|\n)\s*(?:#{1,3}\s*)?(?:budget|funding|cost)\s*(?:outline|breakdown|summary|estimate)?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
        r"(?:budget|estimated.*?budget|overall.*?budget).*?\n(.*?)(?=\n#{1,3}\s|\n\s*(?:open.?science|references|limitations|timeline|milestones)\s*\n|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, fulltext, re.IGNORECASE | re.DOTALL)
        if m and len(m.group(1).strip()) > 50:
            return m.group(1).strip()[:3000]
    return ""


def assess_funding_realism(
    proposal: dict,
    fulltext: str,
    funding_snapshot: dict[str, Any],
    screener_findings: list[dict[str, Any]],
    client: OpenAI,
    stderr: Any,
) -> dict[str, Any]:
    print("\n=== Stage 4: Funding Realism ===", file=stderr)

    funding_prompt = _load_prompt("funding_realism_prompt.md")
    budget_text = _extract_budget_text(fulltext)

    authors_str = ", ".join(
        a.get("name", "Unknown") for a in proposal.get("authors", [])
    )
    institutions_str = ", ".join(proposal.get("institutions", [])) or "none listed"

    scope_findings = [
        f for f in screener_findings
        if any(
            kw in (f.get("observation", "") + f.get("section", "")).lower()
            for kw in ("budget", "cost", "fund", "scope", "timeline", "personnel", "equipment", "method")
        )
    ]
    scope_summary = ""
    if scope_findings:
        scope_summary = "\n".join(
            f"- [{f.get('severity', 'info')}] {f.get('observation', '')}"
            for f in scope_findings[:8]
        )

    user_parts = [
        f"PROPOSAL TITLE: {proposal.get('title', 'Untitled')}",
        f"AUTHORS: {authors_str}",
        f"INSTITUTIONS: {institutions_str}",
        f"\nFUNDING SNAPSHOT:\n{json.dumps(funding_snapshot, indent=2)}",
    ]
    if budget_text:
        user_parts.append(f"\nBUDGET SECTION FROM PROPOSAL:\n{budget_text}")
    else:
        user_parts.append("\nBUDGET SECTION: Not found in proposal text.")
    if scope_summary:
        user_parts.append(f"\nRELEVANT SCREENER OBSERVATIONS:\n{scope_summary}")

    user_content = "\n".join(user_parts)
    parsed = _llm_json_call(client, funding_prompt, user_content, FUNDING_MAX_TOKENS)

    snap_summary = (
        f"Funding snapshot: ${funding_snapshot['goal_amount']:,.0f} requested, "
        f"${funding_snapshot['amount_raised']:,.2f} raised "
        f"({funding_snapshot['percent_funded']:.1f}% funded), "
        f"{funding_snapshot['contributor_count']} contributor(s), "
    )
    dr = funding_snapshot.get("days_remaining")
    dt = funding_snapshot.get("days_total")
    if dr is not None and dt is not None:
        snap_summary += f"{dr} of {dt} days remaining, "
    snap_summary += f"{'on track' if funding_snapshot.get('on_track') else 'behind pace'}."

    if not parsed:
        print("  Funding realism: LLM returned no parseable JSON — retrying with raw prompt", file=stderr)
        raw_retry = _llm_call(client, funding_prompt, user_content, FUNDING_MAX_TOKENS)
        if raw_retry and len(raw_retry.strip()) > 50:
            rationale = snap_summary + " " + raw_retry.strip()
            print(f"  Funding realism: used raw text fallback", file=stderr)
            return {"score": 0.5, "rationale": rationale}

        return {
            "score": 0.5,
            "rationale": snap_summary + " Automated assessment could not be completed.",
        }

    score = parsed.get("overall_score", 0.5)
    try:
        score = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        score = 0.5

    rationale_parts = []
    if parsed.get("rationale"):
        rationale_parts.append(parsed["rationale"])
    else:
        if parsed.get("scope_reasoning"):
            rationale_parts.append(parsed["scope_reasoning"])
        if parsed.get("momentum_reasoning"):
            rationale_parts.append(parsed["momentum_reasoning"])

    rationale = " ".join(rationale_parts) if rationale_parts else "No rationale generated."
    rationale = snap_summary + " " + rationale

    print(f"  Funding realism score: {score:.2f}", file=stderr)
    return {"score": score, "rationale": rationale}


# ---------------------------------------------------------------------------
# Stage 5 — Score + Output
# ---------------------------------------------------------------------------

def compute_composite(
    category_scores: dict[str, dict[str, Any]],
    dimension_weights: dict[str, float],
) -> float:
    present = {
        k: dimension_weights.get(k, 0.0)
        for k in category_scores
        if dimension_weights.get(k, 0.0) > 0
        and category_scores[k].get("score") is not None
    }
    total_weight = sum(present.values())
    if total_weight == 0:
        scores = [
            v["score"] for v in category_scores.values()
            if v.get("score") is not None
        ]
        return round(sum(scores) / len(scores), 4) if scores else 0.5

    weighted_sum = sum(
        category_scores[k]["score"] * w for k, w in present.items()
    )
    return round(weighted_sum / total_weight, 4)


def generate_review_statement(
    review_obj: dict, statement_prompt: str, client: OpenAI,
) -> str:
    context = json.dumps(
        {
            "proposal_name": review_obj.get("proposal_name", ""),
            "composite_score": review_obj.get("composite_score", 0),
            "categories": {
                k: {"score": v.get("score"), "rationale": v.get("rationale", "")}
                for k, v in review_obj.get("categories", {}).items()
            },
        },
        indent=2,
    )
    print("  Generating top-level review statement ...", file=sys.stderr)
    return _llm_call(client, statement_prompt, context, STATEMENT_MAX_TOKENS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Proposal review pipeline: JSON input -> screener -> "
        "OpenAlex originality -> funding realism -> scored review.json"
    )
    parser.add_argument(
        "--input-json", required=True, type=Path,
        help="Path to proposal JSON from the crawler",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Output directory (default: proposals/data/<proposal_id>/)",
    )
    parser.add_argument(
        "--mappings", type=Path, default=_MAPPINGS,
        help=f"proposal_mappings.json path (default: {_MAPPINGS})",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip all LLM calls — output window debug info only",
    )
    parser.add_argument(
        "--skip-openalex", action="store_true",
        help="Skip OpenAlex search (use cached results if available)",
    )
    args = parser.parse_args()

    stderr = sys.stderr
    input_path = args.input_json.expanduser().resolve()
    mappings = json.loads(args.mappings.expanduser().resolve().read_text(encoding="utf-8"))
    dimension_weights = mappings.get("dimension_weights", {})
    rubrics = mappings.get("rubrics", {})

    print(f"\n  Loading proposal: {input_path}", file=stderr)
    proposal = load_proposal(input_path)
    fulltext = extract_fulltext(proposal)
    abstract = extract_abstract(fulltext)
    title = proposal.get("title", "Untitled Proposal")
    proposal_id = proposal.get("id", "unknown")

    if not fulltext:
        print("  ERROR: No fulltext found in proposal JSON", file=stderr)
        sys.exit(1)

    output_dir = args.output_dir
    if not output_dir:
        output_dir = _BASE.parent / "data" / str(proposal_id)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Title: {title}", file=stderr)
    print(f"  Output: {output_dir}", file=stderr)
    print(f"  Fulltext length: {len(fulltext)} chars", file=stderr)

    # ------------------------------------------------------------------
    # Stage 1 — Window Builder
    # ------------------------------------------------------------------
    print("\n=== Stage 1: Window Builder ===", file=stderr)
    windows = split_windows(fulltext)
    print(f"  Built {len(windows)} windows", file=stderr)

    if args.skip_llm:
        debug_out = {
            "proposal_id": proposal_id,
            "title": title,
            "windows_count": len(windows),
            "windows": [
                {"window_idx": w["window_idx"], "token_estimate": w["token_estimate"]}
                for w in windows
            ],
        }
        out_path = output_dir / "debug_windows.json"
        out_path.write_text(
            json.dumps(debug_out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\n  Debug output: {out_path}", file=stderr)
        return

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    # ------------------------------------------------------------------
    # Stage 2 — Sliding-Window Screener
    # ------------------------------------------------------------------
    print("\n=== Stage 2: Sliding-Window Screener ===", file=stderr)
    screener_prompt = _load_prompt("screener_system_prompt.md")
    all_findings = screen_windows(windows, mappings, screener_prompt, client, stderr)
    print(f"  Total raw findings: {len(all_findings)}", file=stderr)

    print("\n  Deduplicating ...", file=stderr)
    grouped = dedup_aggregate(all_findings, stderr)

    writer_prompt = _load_prompt("category_writer_prompt.md")
    writer_results = write_category_rationales(
        grouped, mappings, writer_prompt, client, stderr,
    )

    # Build categories from screener results
    categories: dict[str, dict[str, Any]] = {}
    for dim_key, result in writer_results.items():
        dim_findings = result.get("_findings", [])
        rubric = rubrics.get(dim_key)

        if rubric:
            score = score_rubric_dimension(dim_findings, rubric)
        else:
            score = 0.5

        categories[dim_key] = {
            "score": score,
            "rationale": result["rationale"],
        }

    # Ensure all expected screener dimensions exist even if no findings
    expected_screener_dims = ["scientific_grounding", "evidential_strength"]
    for dim_key in expected_screener_dims:
        if dim_key not in categories:
            dim_info = mappings.get("dimensions", {}).get(dim_key, {})
            label = dim_info.get("label", dim_key.replace("_", " ").title())
            rubric = rubrics.get(dim_key)
            score = rubric.get("baseline", 0.5) if rubric else 0.5
            categories[dim_key] = {
                "score": score,
                "rationale": (
                    f"The document screener did not surface specific findings "
                    f"for {label}. This may indicate that the proposal text did "
                    f"not contain enough detail in this area for the screener to "
                    f"evaluate, or that the relevant content was spread across "
                    f"sections in a way that did not trigger dimension-specific "
                    f"observations. A baseline score has been assigned."
                ),
            }
            print(f"  {label}: no screener findings — assigned baseline score {score}", file=stderr)

    # ------------------------------------------------------------------
    # Stage 3 — OpenAlex Originality
    # ------------------------------------------------------------------
    if not args.skip_openalex:
        originality_result = run_originality(
            fulltext, abstract, output_dir, client, stderr,
        )
        categories["originality"] = {
            "score": originality_result.get("score", 0.5),
            "rationale": originality_result.get("rationale", ""),
        }
    else:
        print("\n  --skip-openalex: skipping originality check", file=stderr)

    # ------------------------------------------------------------------
    # Stage 4 — Funding Realism
    # ------------------------------------------------------------------
    funding_snapshot = compute_funding_snapshot(proposal)
    if funding_snapshot:
        funding_result = assess_funding_realism(
            proposal, fulltext, funding_snapshot, all_findings, client, stderr,
        )
        categories["funding_realism"] = {
            "score": funding_result.get("score", 0.5),
            "rationale": funding_result.get("rationale", ""),
        }
    else:
        print("\n  No funding data — skipping funding realism", file=stderr)

    # ------------------------------------------------------------------
    # Stage 5 — Composite Score + Review Statement + Output
    # ------------------------------------------------------------------
    print("\n=== Stage 5: Score + Output ===", file=stderr)

    # Convert all scores to 0-100
    for cat in categories.values():
        raw = cat.get("score")
        if raw is not None:
            cat["score"] = round(raw * 100)

    composite_raw = compute_composite(
        {k: {"score": v["score"] / 100.0} for k, v in categories.items() if v.get("score") is not None},
        dimension_weights,
    )
    composite = round(composite_raw * 100)

    review_obj: dict[str, Any] = {
        "proposal_name": title,
        "review_date": date.today().strftime("%B %d, %Y"),
        "composite_score": composite,
        "review_statement": "",
        "categories": categories,
    }

    statement_prompt = _load_prompt("review_statement_prompt.md")
    review_obj["review_statement"] = generate_review_statement(
        review_obj, statement_prompt, client,
    )

    # Write output
    out_path = output_dir / "review.json"
    out_path.write_text(
        json.dumps(review_obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n  Review written to {out_path}", file=stderr)

    # Write screener diagnostic
    diag_path = output_dir / "screener_findings.json"
    diag = {
        "proposal_id": proposal_id,
        "title": title,
        "windows_count": len(windows),
        "total_findings_raw": len(all_findings),
        "total_findings_deduped": sum(len(v) for v in grouped.values()),
        "dimensions_with_findings": sorted(grouped.keys()),
        "findings_by_dimension": {
            dim: items for dim, items in sorted(grouped.items())
        },
    }
    diag_path.write_text(
        json.dumps(diag, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  Screener diagnostics: {diag_path}", file=stderr)

    # Build evidence audit trail (no LLM)
    print("\n=== Evidence Audit Trail ===", file=stderr)
    from evidence_doc import build_evidence_doc  # noqa: E402
    screener_data = json.loads(diag_path.read_text(encoding="utf-8"))
    originality_path = output_dir / "originality.json"
    originality_data = (
        json.loads(originality_path.read_text(encoding="utf-8"))
        if originality_path.is_file()
        else None
    )
    audit_text = build_evidence_doc(
        review_obj,
        screener_data,
        originality_data,
        review_path=out_path,
        screener_path=diag_path,
        originality_path=originality_path if originality_path.is_file() else None,
        output_path=output_dir / "evidence_audit.md",
    )
    audit_path = output_dir / "evidence_audit.md"
    audit_path.write_text(audit_text, encoding="utf-8")
    print(f"  Evidence audit: {audit_path}", file=stderr)

    print(f"\n  PIPELINE COMPLETE — composite score: {composite}/100", file=stderr)


if __name__ == "__main__":
    main()
