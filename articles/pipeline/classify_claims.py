"""
Claim classification enricher — reads validated claims JSONL, classifies each claim
via tagger LLM (OpenAI-compatible chat.completions), appends claim_classification_* fields.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
import time
from pathlib import Path

from openai import OpenAI, RateLimitError

_ARTICLES = Path(__file__).resolve().parents[1]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))

from llm_env import TAGGER_API_KEY, TAGGER_BASE_URL, make_client, tagger_model  # noqa: E402

REPO_ROOT = _ARTICLES
PROMPT_PATH = REPO_ROOT / "prompts" / "claim_classification_prompt_v4 (1).md"

MAX_RETRIES = 4
MAX_CLASSIFIER_TOKENS = 128


@functools.lru_cache(maxsize=1)
def load_classifier_prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def parse_tags_from_prompt_md(text: str) -> frozenset[str]:
    """Build allowlist from the Tags: section (lines until first blank line)."""
    lines = text.splitlines()
    try:
        idx = next(i for i, line in enumerate(lines) if line.strip() == "Tags:")
    except StopIteration:
        raise ValueError("Tags: section not found in classifier prompt markdown") from None

    tags: list[str] = []
    for line in lines[idx + 1 :]:
        if not line.strip():
            break
        tags.append(line.strip())
    return frozenset(tags)


def classify_claim_raw(client: OpenAI, model: str, claim: str) -> str:
    system_prompt = load_classifier_prompt_text()
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=MAX_CLASSIFIER_TOKENS,
                temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": claim},
                ],
            )
            content = response.choices[0].message.content
            return (content or "").strip()
        except RateLimitError:
            wait = (2**attempt) * 5
            print(f"  [RATE LIMIT] attempt {attempt + 1}/{MAX_RETRIES} — waiting {wait}s...")
            time.sleep(wait)
        except Exception as e:
            msg = str(e)[:120]
            print(f"  [ERROR] attempt {attempt + 1}/{MAX_RETRIES}: {msg}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(2**attempt)
            else:
                return ""
    return ""


def parse_validate_truncate_tags(raw: str, allowed: frozenset[str]) -> list[str]:
    s = raw.strip()
    s = re.sub(r"```(?:\w*)?|```", "", s).strip()

    seen: set[str] = set()
    out: list[str] = []
    for token in s.split():
        if token in allowed and token not in seen:
            seen.add(token)
            out.append(token)
    return out[:3]


def split_tags_to_classification_fields(tags: list[str]) -> dict[str, list[str]]:
    return {
        "claim_classification_1": [tags[0]] if len(tags) >= 1 else [],
        "claim_classification_2": [tags[1]] if len(tags) >= 2 else [],
        "claim_classification_3": [tags[2]] if len(tags) >= 3 else [],
    }


def main() -> None:
    default_in = REPO_ROOT / "data" / "validated_claims.jsonl"
    default_out = REPO_ROOT / "data" / "classified_claims.jsonl"

    parser = argparse.ArgumentParser(description="Enrich validated claims JSONL with claim classifications.")
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=default_in,
        help=f"Input JSONL (default: {default_in})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=default_out,
        help=f"Output JSONL (default: {default_out})",
    )
    args = parser.parse_args()

    prompt_text = load_classifier_prompt_text()
    allowed = parse_tags_from_prompt_md(prompt_text)

    client = make_client(tagger=True)
    model = tagger_model()

    input_path = args.input.resolve()
    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    n = 0
    with input_path.open(encoding="utf-8") as inf, output_path.open("w", encoding="utf-8") as outf:
        for line in inf:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            claim = str(record.get("claim") or "").strip()

            if not claim:
                tags: list[str] = []
            else:
                n += 1
                if n % 25 == 0:
                    print(f"  [{n}] classified...")
                raw = classify_claim_raw(client, model, claim)
                tags = parse_validate_truncate_tags(raw, allowed)

            enriched = {**record, **split_tags_to_classification_fields(tags)}
            outf.write(json.dumps(enriched, ensure_ascii=False) + "\n")

    print(f"Done. Wrote {output_path}")


if __name__ == "__main__":
    main()
