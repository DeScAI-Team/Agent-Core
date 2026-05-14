"""Evidence-aware review generation pipeline for theoretical/narrative papers.

Transforms prepped evidence JSON (from Theoretical-narrative/prep.py) into a
structured review JSON by extracting narratives, generating argumentation-quality
rationales via a local vLLM endpoint, and condensing multi-chunk rationales.

Stages:
  1. narrative_finder    — chunk narratives into ~1000-token segments
  2. rationale_gen       — LLM generates per-chunk argumentation rationales
  3. rationale_condenser — LLM merges chunks into one rationale per dimension

The review statement is generated later by score.py (Step 12) after all
scores are finalized.
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

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("VALIDATOR_MODEL", "/model")

MAX_RETRIES = 4
RATIONALE_GEN_MAX_TOKENS = 512
CONDENSER_MAX_TOKENS = 4096
TOKEN_CHUNK_TARGET = 1000

EXCLUDED_DIMENSIONS = frozenset({"governance_accountability", "cross_cutting"})


def _estimate_tokens(text: str) -> int:
    return int(len(text.split()) / 0.75)


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def _llm_call(
    client: OpenAI,
    system_prompt: str,
    user_content: str,
    max_tokens: int = CONDENSER_MAX_TOKENS,
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

            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length" or (text and text[-1] not in ".!?"):
                last_period = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
                if last_period > 0:
                    text = text[: last_period + 1].strip()

            return text
        except Exception as exc:
            err_str = str(exc).lower()
            if "context length" in err_str or "maximum context" in err_str:
                raise
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}")
            if attempt == MAX_RETRIES:
                raise
    return ""


def _get_label(group_key: str, mappings: dict) -> str:
    if group_key in mappings.get("dimensions", {}):
        return mappings["dimensions"][group_key]["label"]
    if group_key == "cross_cutting":
        return mappings.get("cross_cutting", {}).get("label", "Cross-Cutting / Flags")
    return group_key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Stage 1 – narrative_finder
# ---------------------------------------------------------------------------

def narrative_finder(prepped: dict, mappings: dict) -> dict:
    """Chunk claim_narrative strings into ~1000-token segments per dimension."""
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
        strong_mod = sum(grade_dist.get(g, 0) for g in ("strong", "moderate"))
        overclaim = grade_dist.get("overclaim", 0)
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
            "strong_moderate": strong_mod,
            "overclaim": overclaim,
            "unsupported_unreferenced": unsup,
            "evidence_grade_distribution": grade_dist,
        }

    return result


# ---------------------------------------------------------------------------
# Stage 2 – rationale_gen
# ---------------------------------------------------------------------------

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
            "strong_moderate": group_data["strong_moderate"],
            "overclaim": group_data["overclaim"],
            "unsupported_unreferenced": group_data["unsupported_unreferenced"],
            "evidence_grade_distribution": group_data["evidence_grade_distribution"],
        }

    return result


# ---------------------------------------------------------------------------
# Stage 3 – rationale_condenser
# ---------------------------------------------------------------------------

def _deduplicate_sentences(text: str) -> str:
    if not text:
        return text
    sentences = re.split(r"([.!?])\s+", text)
    full_sentences = []
    for i in range(0, len(sentences) - 1, 2):
        if i + 1 < len(sentences):
            full_sentences.append(sentences[i] + sentences[i + 1])
    if len(sentences) % 2 == 1:
        full_sentences.append(sentences[-1])
    seen: set[str] = set()
    deduplicated = []
    for sentence in full_sentences:
        normalized = sentence.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduplicated.append(sentence)
    return " ".join(deduplicated).strip()


def rationale_condenser(groups: dict, condense_prompt: str, client: OpenAI) -> dict:
    result = {}

    for group_key, group_data in groups.items():
        rationales = group_data["rationales"]
        label = group_data["label"]
        total = group_data["total_claims"]
        sm = group_data["strong_moderate"]
        oc = group_data["overclaim"]
        uu = group_data["unsupported_unreferenced"]

        if len(rationales) > 1:
            print(f"  [{label}] condensing {len(rationales)} rationales ...")

            stats_line = (
                f"Of {total} claims evaluated for {label}, "
                f"{sm} had strong or moderate citation support, "
                f"{oc} were overclaimed beyond what references demonstrate, "
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


# ---------------------------------------------------------------------------
# Stage 4 – review_statement_gen
# ---------------------------------------------------------------------------

def review_statement_gen(
    review_obj: dict, statement_prompt: str, client: OpenAI
) -> str:
    context = json.dumps(
        {
            "research_name": review_obj["research_name"],
            "average_score": review_obj["average_score"],
            "categories": {
                k: {"score": v["score"], "rationale": v["rationale"]}
                for k, v in review_obj["categories"].items()
            },
        },
        indent=2,
    )
    print("  Generating top-level review statement ...")
    return _llm_call(client, statement_prompt, context)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build argumentation-quality review.json from prepped evidence claims."
    )
    parser.add_argument(
        "prepped_json",
        help="Path to prepped_evidence.json (from Theoretical-narrative/prep.py)",
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

    # Stage 1
    print("\n=== Stage 1: narrative_finder ===")
    chunked = narrative_finder(prepped, mappings)
    for key, data in chunked.items():
        print(f"  {data['label']}: {len(data['narrative_chunks'])} chunk(s)")

    # Stage 2
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

    # Stage 3
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
