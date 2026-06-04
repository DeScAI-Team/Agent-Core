"""Protocol-aware review generation pipeline.

Reuses the core architecture from empirical/review.py (narrative_finder,
rationale_gen, rationale_condenser) but loads prompts from the local
Protocol-pre_results/prompts/ directory.

Key differences from empirical:
- Prompts are tuned for design justification rather than evidence for results
- Statistics line uses "design support" / "established precedent" language
  instead of "self-reported findings"
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date
from pathlib import Path

from openai import OpenAI

_BASE = Path(__file__).resolve().parent
PIPELINE_DIR = _BASE.parent
PROMPTS_DIR = _BASE / "prompts"
MAPPINGS_PATH = PIPELINE_DIR / "mappings.json"

_EMPIRICAL = PIPELINE_DIR / "empirical"
import sys
if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

from review import (  # noqa: E402
    _estimate_tokens,
    _llm_call,
    _get_label,
    _deduplicate_sentences,
    EXCLUDED_DIMENSIONS,
    RATIONALE_GEN_MAX_TOKENS,
    CONDENSER_MAX_TOKENS,
    TOKEN_CHUNK_TARGET,
)

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def narrative_finder(prepped: dict, mappings: dict) -> dict:
    """Chunk claim_narrative strings into ~1000-token segments per dimension.

    Protocol-aware: uses "design_precedent" and "established_method" in grade
    distribution counts instead of "self_reported".
    """
    result = {}

    for group_key, group_data in prepped.items():
        if group_key in EXCLUDED_DIMENSIONS:
            continue
        if not isinstance(group_data, dict):
            continue
        members = group_data.get("members", [])
        if not members:
            continue

        score = group_data.get("score", 0.5)
        label = _get_label(group_key, mappings)
        grade_dist = group_data.get("evidence_grade_distribution", {})

        total_claims = len(members)
        strong_mod = sum(grade_dist.get(g, 0) for g in (
            "strong", "moderate", "design_precedent", "established_method",
        ))
        unsup = sum(grade_dist.get(g, 0) for g in ("unsupported", "unreferenced"))

        doc_name = ""
        narratives = []
        for member in members:
            narr = member.get("claim_narrative")
            if narr:
                narratives.append(narr)
            if not doc_name:
                doc_name = member.get("doc_name", "")

        chunks: list[str] = []
        current_parts: list[str] = []
        current_tokens = 0

        for narrative in narratives:
            narr_tokens = _estimate_tokens(narrative)
            if current_tokens + narr_tokens > TOKEN_CHUNK_TARGET and current_parts:
                chunks.append("\n\n".join(current_parts))
                current_parts = [narrative]
                current_tokens = narr_tokens
            else:
                current_parts.append(narrative)
                current_tokens += narr_tokens

        if current_parts:
            chunks.append("\n\n".join(current_parts))

        result[group_key] = {
            "score": score,
            "label": label,
            "doc_name": doc_name,
            "narrative_chunks": chunks,
            "total_claims": total_claims,
            "strong_moderate_precedent": strong_mod,
            "unsupported_unreferenced": unsup,
            "evidence_grade_distribution": grade_dist,
        }

    return result


def rationale_gen(chunked: dict, prompt_text: str, client: OpenAI) -> dict:
    result = {}

    for group_key, group_data in chunked.items():
        rationales: list[str] = []
        n_chunks = len(group_data["narrative_chunks"])

        for idx, chunk in enumerate(group_data["narrative_chunks"]):
            print(
                f"  [{group_data['label']}] generating rationale "
                f"({idx + 1}/{n_chunks}) ..."
            )
            chunk_context = (
                f"[This is chunk {idx + 1} of {n_chunks} for this dimension. "
                f"Analyze only these claims without repeating analysis from other chunks.]\n\n{chunk}"
            )
            rationale = _llm_call(
                client, prompt_text, chunk_context, max_tokens=RATIONALE_GEN_MAX_TOKENS
            )
            rationales.append(rationale)

        result[group_key] = {
            "score": group_data["score"],
            "label": group_data["label"],
            "doc_name": group_data["doc_name"],
            "rationales": rationales,
            "total_claims": group_data["total_claims"],
            "strong_moderate_precedent": group_data["strong_moderate_precedent"],
            "unsupported_unreferenced": group_data["unsupported_unreferenced"],
            "evidence_grade_distribution": group_data["evidence_grade_distribution"],
        }

    return result


def rationale_condenser(groups: dict, condense_prompt: str, client: OpenAI) -> dict:
    result = {}

    for group_key, group_data in groups.items():
        rationales = group_data["rationales"]
        label = group_data["label"]
        total = group_data["total_claims"]
        smp = group_data["strong_moderate_precedent"]
        uu = group_data["unsupported_unreferenced"]

        if len(rationales) > 1:
            print(f"  [{label}] condensing {len(rationales)} rationales ...")

            stats_line = (
                f"Of {total} claims evaluated for {label}, "
                f"{smp} had strong or moderate external support or established precedent, "
                f"and {uu} were unsupported or unreferenced."
            )

            user_message = (
                f"[GROUND TRUTH STATISTICS - Use this exact opening line:]\n"
                f"{stats_line}\n\n"
                f"[CRITICAL: The partial rationales below may contain 'in this subset' statistics that are "
                f"APPROXIMATIONS and may be INCORRECT. IGNORE those chunk-level counts completely. "
                f"Use ONLY the ground truth line above for your opening sentence.]\n\n"
                f"[Now synthesize the following partial rationales into a single coherent analysis.]\n\n"
                f"---\n\n"
                + "\n\n---\n\n".join(rationales)
            )

            condensed = _llm_call(client, condense_prompt, user_message)
            final_rationale = _deduplicate_sentences(condensed)
        else:
            final_rationale = rationales[0] if rationales else ""

        result[group_key] = {
            "score": group_data["score"],
            "label": label,
            "doc_name": group_data["doc_name"],
            "rationale": final_rationale,
        }

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build protocol-aware review.json from prepped evidence claims."
    )
    parser.add_argument(
        "prepped_json",
        help="Path to prepped_evidence.json",
    )
    parser.add_argument(
        "--mappings", type=Path, default=MAPPINGS_PATH,
        help=f"mappings.json path (default: {MAPPINGS_PATH})",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write review JSON here (default: stdout)",
    )
    parser.add_argument(
        "--pre-condensed-dump", type=Path, default=None,
        help="Write Stage-2 per-chunk rationales here before condensation",
    )
    args = parser.parse_args()

    prepped_path = Path(args.prepped_json).expanduser().resolve()
    mappings_path = args.mappings.expanduser().resolve()

    print("Loading inputs ...")
    prepped = json.loads(prepped_path.read_text(encoding="utf-8"))
    mappings = json.loads(mappings_path.read_text(encoding="utf-8"))

    rationale_prompt = _load_prompt("rationale_generation_prompt.md")
    condense_prompt = _load_prompt("rationale_condenser_prompt.md")

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    print("\n=== Stage 1: narrative_finder ===")
    chunked = narrative_finder(prepped, mappings)
    for key, data in chunked.items():
        print(f"  {data['label']}: {len(data['narrative_chunks'])} chunk(s)")

    print("\n=== Stage 2: rationale_gen ===")
    with_rationales = rationale_gen(chunked, rationale_prompt, client)

    if args.pre_condensed_dump:
        dump_path = args.pre_condensed_dump.expanduser().resolve()
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(
            json.dumps(with_rationales, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nPre-condensation rationales written to {dump_path}")

    print("\n=== Stage 3: rationale_condenser ===")
    condensed = rationale_condenser(with_rationales, condense_prompt, client)

    doc_name = ""
    for group_data in condensed.values():
        if group_data.get("doc_name"):
            doc_name = group_data["doc_name"]
            break

    categories = {}
    for group_key, group_data in condensed.items():
        categories[group_key] = {
            "score": group_data["score"],
            "rationale": group_data["rationale"],
        }

    review_obj = {
        "research_name": doc_name,
        "review_date": date.today().strftime("%B %d, %Y"),
        "average_score": None,
        "review_statement": "",
        "categories": categories,
    }

    text = json.dumps(review_obj, indent=2, ensure_ascii=False) + "\n"

    if args.output is not None:
        out_path = args.output.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"\nReview written to {out_path}")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
