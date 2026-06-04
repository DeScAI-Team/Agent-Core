#!/usr/bin/env python3
"""Simplify review.json rationales and review_statement for a general audience.

Reads a review JSON (single compound or combination), calls the LLM once per
text field, and writes overview.json with the same scores and structure.

Usage:
  python overview.py reviews/compounds/OMIGU/review/review.json
  python overview.py review.json -o review/overview.json --model mixtral-8x7b-instruct
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from openai import OpenAI, RateLimitError

_COMPOUNDS_DIR = Path(__file__).resolve().parents[1]
if str(_COMPOUNDS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS_DIR))

from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402

_PROMPT_PATH = _COMPOUNDS_DIR / "prompts" / "compound-review-overview.md"
MAX_TOKENS = max(256, int(os.environ.get("OVERVIEW_MAX_TOKENS", "1024")))
MAX_RETRIES = 3

_END_THINK_MARKERS = (
    "</think>",
    "</think>",
    "</thinking>",
    "</reasoning>",
    "</thought>",
)

_FIELD_LABELS = {
    "review_statement": "review_statement (executive summary)",
    "scientific_grounding": "scientific_grounding",
    "risk_assessment": "risk_assessment",
    "compatibility": "compatibility",
}


def strip_reasoning_markup(s: str) -> str:
    """Drop chain-of-thought wrappers; prefer text after the last end-thinking marker."""
    t = s.strip()
    low = t.lower()
    best_idx = -1
    best_len = 0
    for m in _END_THINK_MARKERS:
        pos = low.rfind(m.lower())
        if pos > best_idx:
            best_idx = pos
            best_len = len(m)
    if best_idx >= 0:
        t = t[best_idx + best_len:].lstrip()
    block_patterns = (
        r"<think\b[^>]*>[\s\S]*?</think>",
        r"<thinking\b[^>]*>[\s\S]*?</thinking>",
        r"<reasoning\b[^>]*>[\s\S]*?</reasoning>",
        r"<thought\b[^>]*>[\s\S]*?</thought>",
        r"<redacted_thinking\b[^>]*>[\s\S]*?</think>",
    )
    for _ in range(8):
        prev = t
        for pat in block_patterns:
            t = re.sub(pat, "", t, flags=re.IGNORECASE)
        if t == prev:
            break
    t = re.sub(r"<think\b[^>]*>[\s\S]*$", "", t, flags=re.IGNORECASE).strip()
    return t.strip()


def call_llm(client: OpenAI, model: str, system_prompt: str, user_content: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    extra_body: dict[str, object] = {
        "top_k": -1,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    def _complete() -> object:
        kw: dict[str, object] = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            temperature=0,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
            messages=messages,
        )
        try:
            return client.chat.completions.create(**kw, extra_body=extra_body)
        except TypeError:
            return client.chat.completions.create(**kw)

    for attempt in range(MAX_RETRIES):
        try:
            response = _complete()
            content = response.choices[0].message.content
            return strip_reasoning_markup((content or "").strip())
        except RateLimitError:
            wait = (2 ** attempt) * 5
            print(
                f"  [RATE LIMIT] attempt {attempt + 1}/{MAX_RETRIES} — waiting {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)
        except Exception as e:
            msg = str(e)[:120]
            print(
                f"  [ERROR] attempt {attempt + 1}/{MAX_RETRIES}: {msg}",
                file=sys.stderr,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
            else:
                return ""
    return ""


def simplify_field(
    client: OpenAI,
    model: str,
    system_prompt: str,
    field_key: str,
    research_name: str,
    original: str,
) -> str:
    text = (original or "").strip()
    if not text:
        return text
    label = _FIELD_LABELS.get(field_key, field_key)
    user_content = (
        f"Research: {research_name}\n"
        f"Field: {label}\n\n"
        f"Original text:\n{text}"
    )
    simplified = call_llm(client, model, system_prompt, user_content)
    if not simplified:
        print(
            f"  [overview] LLM returned empty for {field_key}; keeping original",
            file=sys.stderr,
        )
        return text
    return simplified


def _review_display_name(review: dict) -> str:
    return (review.get("compound_token") or review.get("research_name") or "").strip()


def _composite_score_ceil(value: Any) -> int | None:
    if value is None:
        return None
    return int(math.ceil(float(value)))


def build_overview(review: dict, client: OpenAI, model: str, system_prompt: str) -> dict:
    display_name = _review_display_name(review)
    overview: dict = {}
    if review.get("compound_token"):
        overview["compound_token"] = review["compound_token"]
    else:
        overview["research_name"] = display_name
    overview["review_date"] = review.get("review_date")
    overview["composite_score"] = _composite_score_ceil(review.get("composite_score"))
    overview["review_statement"] = simplify_field(
        client,
        model,
        system_prompt,
        "review_statement",
        display_name,
        review.get("review_statement") or "",
    )
    overview["categories"] = {}
    categories = review.get("categories") or {}
    for cat_key, cat_val in categories.items():
        if not isinstance(cat_val, dict):
            continue
        overview["categories"][cat_key] = {
            "score": cat_val.get("score"),
            "rationale": simplify_field(
                client,
                model,
                system_prompt,
                cat_key,
                display_name,
                cat_val.get("rationale") or "",
            ),
        }
    return overview


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("review_json", type=Path, help="Path to review.json")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output path (default: overview.json next to input)",
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("REVIEWER_MODEL", "mixtral-8x7b-instruct"),
        help="Chat model name",
    )
    args = ap.parse_args()

    in_path = args.review_json.expanduser().resolve()
    if not in_path.is_file():
        print(f"Not found: {in_path}", file=sys.stderr)
        return 1

    out_path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else in_path.parent / "overview.json"
    )

    with in_path.open(encoding="utf-8") as fh:
        review = json.load(fh)

    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

    print(f"Simplifying {in_path} …", file=sys.stderr)
    overview = build_overview(review, client, args.model, system_prompt)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(overview, fh, ensure_ascii=False, indent=2)

    print(f"Done. Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
