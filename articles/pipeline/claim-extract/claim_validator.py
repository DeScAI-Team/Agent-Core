"""
Claim Validation Module — Phase 2 of the DeScAi pipeline.

Reads extraction JSONL (final_claims_for_audit.jsonl), builds per-claim context
from the source chunks (sliding window around the source chunk + claim-type-mapped
sections), and validates each claim via async vLLM calls.

Context assembly (build_claim_context):
  Layer A — source chunk ± CHUNK_WINDOW neighbors (always included).
  Layer B — additional chunks whose section_heading matches entries in
            CLAIM_TYPE_SECTION_MAP for the claim's type.

Verdicts: "supported" | "unsupported" | "insufficient_info"
Fallbacks: if the primary JSON call fails after MAX_RETRIES, a dedicated
  verdict-only prompt (articles/prompts/verdict_fallback_prompt.md) attempts to recover
  a valid verdict, followed by a rationale-only prompt
  (articles/prompts/rationale_fallback_prompt.md) if needed.
Malformed / error responses are flagged with validation_error=True, not dropped.
"""

import json
import asyncio
import os
import re
from collections import defaultdict

import sys
from pathlib import Path as _Path

from openai import AsyncOpenAI

_ARTICLES = _Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402

# === CONFIG ===
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY

MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "mixtral-8x7b-instruct")
CONCURRENCY           = int(os.environ.get("VALIDATOR_CONCURRENCY", "15"))

MAX_RETRIES           = 2
KEY_SECTION_MAX_CHARS = int(os.environ.get("VALIDATOR_KEY_SECTION_MAX_CHARS", "6000"))

_data_base = os.environ.get("CLAIM_EXTRACT_DATA_DIR") or os.path.dirname(__file__)
INPUT_CLAIMS = os.path.join(_data_base, "final_claims_for_audit.jsonl")
SOURCE_CHUNKS = os.environ.get(
    "VALIDATOR_SOURCE_CHUNKS",
    os.path.join(_data_base, "text_knowledge_base.jsonl"),
)
OUTPUT_VALIDATED = os.path.join(_data_base, "validated_claims.jsonl")

KEY_SECTION_KEYWORDS = [
    "method", "result", "conclusion", "finding",
    "discussion", "experiment", "analysis", "outcome",
]
SEMANTIC_KEY_BUCKETS = {"method", "result", "conclusion"}

METHOD_SECTION_HINTS = ["method", "materials", "approach", "experiment", "protocol"]
RESULT_SECTION_HINTS = ["result", "finding", "analysis", "observation"]
CONCLUSION_SECTION_HINTS = ["conclusion", "summary", "implication", ]

CHUNK_WINDOW       = 1  # how many neighbors on each side of the source chunk

CLAIM_TYPE_SECTION_MAP = {
    "Fact":      ["result", "finding", "data", "efficacy"],
    "Assertion": ["result", "discussion", "conclusion"],
    "Roadmap":   ["discussion", "conclusion"],
}

VALID_VERDICTS = {"supported", "unsupported", "insufficient_info"}

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "prompts",
)

VALIDATION_PROMPT = """\
You are a scientific claim validator. Given key sections of a research document \
and a single extracted claim, assess whether the sections support the claim.

Respond ONLY in valid JSON with:
"verdict" ("supported" | "unsupported" | "insufficient_info"),
"rationale" (max 50 words),
"relevancy_score" (a float 0.00-1.00 using the tiers below).

RELEVANCY SCORING TIERS — choose carefully:
0.00-0.20 (low_relevancy): General domain knowledge restated from literature. \
Textbook facts, established biology/chemistry, cited prior work. \
Example: "AGEs form through non-enzymatic glycation."
0.20-0.40 (slightly_relevant): Administrative or procedural details specific to this study \
but not scientific findings. Timelines, funding sources, ethics statements, registration info.
0.40-0.60 (moderately_relevant): Methodological choices specific to this study. \
Tools selected, protocols designed, statistical approaches chosen by the authors. \
Example: "BLI kinetic data will be fit to a 1:1 Langmuir model."
0.60-0.80 (very_relevant): Study-specific design decisions, hypotheses, or interpretive claims \
that shape the research direction. Example: "A hit rate of >=1% would be considered evidence."
0.80-1.00 (extremely_relevant): Novel findings, unique results, or core conclusions generated \
by this researcher. Pilot data outcomes, proof-of-concept results, first-of-kind demonstrations. \
Example: "AlphaFold2 predicts pLDDT > 85 for 15-20% of designs."

KEY SECTIONS: {key_sections}

CLAIM: {claim}"""


def _extract_first_json_object(text: str) -> str:
    """Return the first balanced JSON object substring from text.

    This is resilient to leading/trailing prose and code fences.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object start found")

    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unterminated JSON object")




def load_chunk_index(source_jsonl: str) -> dict:
    """Load all chunks into {doc_name: {chunk_id: record}} for per-claim context assembly."""
    index: dict = defaultdict(dict)
    if not os.path.exists(source_jsonl):
        print(f"[WARN] Source chunks file not found: {source_jsonl}")
        return {}
    with open(source_jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            index[rec["doc_name"]][rec["chunk_id"]] = rec
    return dict(index)


def load_key_sections(source_jsonl: str) -> dict:
    """Build doc_name -> key-section text with section-aware truncation.

    We prioritize coverage across methodology/results/conclusion by reserving
    budget per bucket, then filling with "other" key sections.
    """
    doc_sections: dict = defaultdict(lambda: {
        "method": [],
        "result": [],
        "conclusion": [],
        "other": [],
    })

    def classify_heading(heading: str) -> str:
        if any(h in heading for h in METHOD_SECTION_HINTS):
            return "method"
        if any(h in heading for h in RESULT_SECTION_HINTS):
            return "result"
        if any(h in heading for h in CONCLUSION_SECTION_HINTS):
            return "conclusion"
        return "other"

    def trim_join(parts: list, budget: int) -> str:
        if budget <= 0 or not parts:
            return ""
        out = []
        used = 0
        for part in parts:
            if used >= budget:
                break
            remaining = budget - used
            if len(part) <= remaining:
                out.append(part)
                used += len(part)
            else:
                out.append(part[:remaining])
                used = budget
        return "\n\n".join(out)

    if not os.path.exists(source_jsonl):
        print(f"[WARN] Source chunks file not found: {source_jsonl}")
        return {}

    with open(source_jsonl) as f:
        for line in f:
            rec = json.loads(line)
            heading = rec.get("section_heading", "").lower()
            semantic_category = str(rec.get("semantic_category", "")).strip().lower()

            bucket = None
            if semantic_category in SEMANTIC_KEY_BUCKETS:
                bucket = semantic_category
            elif any(kw in heading for kw in KEY_SECTION_KEYWORDS):
                bucket = classify_heading(heading)

            if bucket:
                doc_sections[rec["doc_name"]][bucket].append(
                    f"[{rec['section_heading']}]\n{rec.get('text', '')}"
                )

    packed = {}
    for doc, buckets in doc_sections.items():
        core_budget = int(KEY_SECTION_MAX_CHARS * 0.9)
        core_each = max(core_budget // 3, 1)
        other_budget = max(KEY_SECTION_MAX_CHARS - (core_each * 3), 0)

        parts = [
            trim_join(buckets["method"], core_each),
            trim_join(buckets["result"], core_each),
            trim_join(buckets["conclusion"], core_each),
            trim_join(buckets["other"], other_budget),
        ]
        text = "\n\n".join(p for p in parts if p).strip()
        packed[doc] = text[:KEY_SECTION_MAX_CHARS]

    return packed

def _fmt_chunk(rec: dict) -> str:
    return f"[{rec.get('section_heading', 'Unknown')}]\n{rec.get('text', '')}"




def build_claim_context(record: dict, chunk_index: dict) -> str:
    """Assemble per-claim context: source chunk + window, then claim-type mapped sections."""
    doc_name = record.get("doc_name", "")
    doc_chunks = chunk_index.get(doc_name, {})
    if not doc_chunks:
        return "No key sections available for this document."

    source_id = record.get("chunk_id", 0)
    claim_type = record.get("claim_type", "")

    # Layer A: source chunk + sliding window
    window_ids = [
        cid for cid in range(source_id - CHUNK_WINDOW, source_id + CHUNK_WINDOW + 1)
        if cid in doc_chunks
    ]
    layer_a_parts = [_fmt_chunk(doc_chunks[cid]) for cid in window_ids]
    layer_a_ids = set(window_ids)

    # Layer B: claim-type-to-section mapped chunks
    section_hints = CLAIM_TYPE_SECTION_MAP.get(claim_type, ["method", "result", "conclusion"])
    layer_b_parts = []
    for cid in sorted(doc_chunks.keys()):
        if cid in layer_a_ids:
            continue
        heading = doc_chunks[cid].get("section_heading", "").lower()
        if any(hint in heading for hint in section_hints):
            layer_b_parts.append(_fmt_chunk(doc_chunks[cid]))

    budget_a = int(KEY_SECTION_MAX_CHARS * 0.5)
    budget_b = KEY_SECTION_MAX_CHARS - budget_a

    def truncate_parts(parts: list, budget: int) -> str:
        out, used = [], 0
        for part in parts:
            if used >= budget:
                break
            remaining = budget - used
            if len(part) <= remaining:
                out.append(part)
                used += len(part)
            else:
                out.append(part[:remaining])
                used = budget
        return "\n\n".join(out)

    text_a = truncate_parts(layer_a_parts, budget_a)
    text_b = truncate_parts(layer_b_parts, budget_b)
    combined = "\n\n".join(p for p in (text_a, text_b) if p).strip()
    return combined or "No key sections available for this document."


def _load_prompt(filename: str) -> str:
    path = os.path.join(PROMPTS_DIR, filename)
    with open(path) as f:
        return f.read()


async def _verdict_fallback(
    client: AsyncOpenAI, key_sections: str, claim: str,
) -> str | None:
    """Single-shot LLM call that returns a bare verdict word, or None on failure."""
    template = _load_prompt("verdict_fallback_prompt.md")
    prompt = template.format(key_sections=key_sections, claim=claim)
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Respond with exactly one word."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=32,
            temperature=0.0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = (resp.choices[0].message.content or "").strip().lower().strip(".")
        if raw in VALID_VERDICTS:
            return raw
    except Exception as exc:
        print(f"  [VERDICT_FB] fallback error: {exc!s:.100}")
    return None


async def _rationale_fallback(
    client: AsyncOpenAI, key_sections: str, claim: str, verdict: str,
) -> str:
    """Single-shot LLM call that returns a rationale string.
    Always returns usable text — falls back to a generic message."""
    template = _load_prompt("rationale_fallback_prompt.md")
    prompt = template.format(key_sections=key_sections, claim=claim, verdict=verdict)
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Respond with only the rationale text."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=128,
            temperature=0.0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw and len(raw) > 5:
            return raw[:250]
    except Exception as exc:
        print(f"  [RATIONALE_FB] fallback error: {exc!s:.100}")
    return "Verdict assigned via fallback; manual review recommended."


async def validate_claim(
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    record: dict,
    chunk_index: dict,
) -> dict:
    """Validate a single claim, retrying on transient errors.

    Never drops a record — malformed / error responses are flagged with
    validation_error=True so downstream phases can filter or re-run them.
    Falls back to dedicated verdict/rationale prompts when the primary
    JSON call fails to produce valid fields.
    """
    claim_text = record.get("claim", "")
    key_sections = build_claim_context(record, chunk_index)
    base_prompt = VALIDATION_PROMPT.format(
        key_sections=key_sections,
        claim=claim_text,
    )
    current_max_tokens = 384

    verdict = None
    rationale = None
    relevancy = None
    last_error_msg = ""

    async with semaphore:
        # --- primary retry loop (unchanged structure) ---
        for attempt in range(MAX_RETRIES):
            try:
                prompt = base_prompt
                if attempt > 0:
                    prompt += (
                        "\n\nIMPORTANT: Return one complete valid JSON object only. "
                        "Do not include markdown or extra text."
                    )
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Respond with exactly one valid JSON object and nothing else."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=current_max_tokens,
                    temperature=0.0,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                content = response.choices[0].message.content
                raw = (content or "").strip()
                clean = re.sub(r"```(?:json)?|```", "", raw).strip()
                json_blob = _extract_first_json_object(clean)
                parsed = json.loads(json_blob)

                verdict = parsed.get("verdict", "")
                rationale = parsed.get("rationale", "")
                try:
                    relevancy = float(parsed.get("relevancy_score", 0.0))
                    if not 0.0 <= relevancy <= 1.0:
                        relevancy = None
                except (TypeError, ValueError):
                    relevancy = None

                if verdict in VALID_VERDICTS and rationale:
                    final_relevancy = relevancy if relevancy is not None else 0.0
                    return {
                        **record,
                        "verdict": verdict,
                        "rationale": rationale,
                        "relevancy_score": final_relevancy,
                    }

                # Partial success — break to fallbacks instead of retrying
                if verdict in VALID_VERDICTS or rationale:
                    break

                raise ValueError(f"unexpected verdict: {verdict!r}")

            except json.JSONDecodeError as exc:
                last_error_msg = f"JSON parse error (attempt {attempt + 1}): {exc}"
                print(f"  [MALFORMED] chunk {record.get('chunk_id')} — {last_error_msg}")
                current_max_tokens = min(512, current_max_tokens + 64)

            except ValueError as exc:
                last_error_msg = str(exc)
                print(f"  [INVALID]   chunk {record.get('chunk_id')} — {last_error_msg}")
                if "unterminated JSON object" in last_error_msg:
                    current_max_tokens = min(512, current_max_tokens + 64)

            except Exception as exc:
                last_error_msg = str(exc)[:120]
                print(f"  [ERROR]     chunk {record.get('chunk_id')} attempt {attempt + 1} — {last_error_msg}")
                # No backoff: target is localhost llama-server (deterministic, no rate-limiting).
                # Sleeping here would hold the semaphore and block other concurrent claims.

        # --- fallback: verdict ---
        if verdict not in VALID_VERDICTS:
            print(f"  [FALLBACK]  chunk {record.get('chunk_id')} — running verdict fallback")
            verdict = await _verdict_fallback(client, key_sections, claim_text)
            if verdict is None:
                return {
                    **record,
                    "verdict": "validation_error",
                    "rationale": last_error_msg[:120],
                    "relevancy_score": None,
                    "validation_error": True,
                }

        # --- fallback: rationale ---
        if not rationale or rationale == last_error_msg[:120]:
            print(f"  [FALLBACK]  chunk {record.get('chunk_id')} — running rationale fallback")
            rationale = await _rationale_fallback(
                client, key_sections, claim_text, verdict,
            )

        final_relevancy = relevancy if relevancy is not None else 0.0
        return {
            **record,
            "verdict": verdict,
            "rationale": rationale,
            "relevancy_score": final_relevancy,
        }


async def main() -> None:
    client    = AsyncOpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    print("Loading chunk index from source chunks...")
    chunk_index = load_chunk_index(SOURCE_CHUNKS)
    print(f"  {len(chunk_index)} document(s) indexed.")

    claims: list = []
    with open(INPUT_CLAIMS) as f:
        for line in f:
            line = line.strip()
            if line:
                claims.append(json.loads(line))
    print(f"  {len(claims)} claims to validate.\n")

    missing_docs = sorted(
        {
            rec.get("doc_name", "")
            for rec in claims
            if rec.get("doc_name", "") not in chunk_index
        }
    )
    if missing_docs:
        print(
            f"  WARNING: {len(missing_docs)} claim document(s) have no chunks "
            "in SOURCE_CHUNKS. Validation quality will be poor for those docs."
        )
        print(f"  Missing docs sample: {missing_docs[:5]}\n")

    counter = [0]

    async def tracked(idx: int, record: dict) -> tuple[int, dict]:
        result = await validate_claim(client, semaphore, record, chunk_index)
        counter[0] += 1
        n = counter[0]
        if n % 25 == 0 or n == len(claims):
            print(f"  [{n}/{len(claims)}] processed...")
        return idx, result

    # Submit claims grouped by doc_name (stable within doc) so successive requests
    # to the same llama-server slot share a long static prefix → much higher prefix
    # cache hit rate. Re-sort results to original input order before writing.
    indexed = list(enumerate(claims))
    indexed.sort(key=lambda pair: (pair[1].get("doc_name", ""), pair[0]))
    gathered = await asyncio.gather(*[tracked(idx, rec) for idx, rec in indexed])
    gathered.sort(key=lambda pair: pair[0])
    results = [rec for _, rec in gathered]

    errors = sum(1 for r in results if r.get("validation_error"))
    with open(OUTPUT_VALIDATED, "w") as f:
        for rec in results:
            f.write(json.dumps(rec) + "\n")

    print(f"\nDone. {len(results)} records written to: {OUTPUT_VALIDATED}")
    if errors:
        print(f"  WARNING: {errors} record(s) flagged with validation_error=True — review manually.")


if __name__ == "__main__":
    asyncio.run(main())
