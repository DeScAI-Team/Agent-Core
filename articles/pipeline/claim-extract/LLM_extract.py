import json
import time
import os
import re
from openai import OpenAI, RateLimitError
from dotenv import load_dotenv

load_dotenv()

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("VALIDATOR_MODEL", "mixtral-8x7b-instruct")
MAX_RETRIES = 4

client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)


def extract_hybrid_claims(record):
    """
    Sends spaCy-tagged text to the LLM for reasoning-based claim extraction.
    <Scientific_claim> tokens act as hints; the model also surfaces hidden claims
    embedded in surrounding context.
    """
    prompt = f"""You are a scientific claim extractor for research paper quality review.

Your job is to extract SUBSTANTIVE scientific claims — statements that carry
scientific weight and would matter when evaluating the paper's quality,
validity, or contribution. The text may contain <Scientific_claim>...</Scientific_claim>
tags as hints, but use your judgment: only extract a tagged span if it meets
the significance criteria below.

EXTRACT these kinds of claims:
- Novel experimental findings and measured results (with data, p-values, effect sizes)
- Interpretive or mechanistic assertions the authors make about their data
- Comparisons between treatments, conditions, or prior work
- Causal or correlational claims linking variables
- Conclusions about what the data means for the field
- Knowledge gaps, hypotheses, or future directions that frame the study's contribution
- Quality or validity statements about the study's own data (e.g. reproducibility claims)

DO NOT EXTRACT:
- Routine protocol steps (reagents used, temperatures, incubation times, plate layouts)
- Tool or software names without a substantive claim about them
- Standard operating parameters (wash buffers, centrifuge speeds, staining protocols)
- Figure/table legends that merely describe what a visualization shows
- Definitions of common scientific terms or well-known biological facts
  that provide background but are not contested or study-specific
- Administrative details (ethics approvals, sample storage, equipment model numbers)

Classify each claim as:
  Fact — a specific measured result, quantitative finding, or empirically observed outcome
  Assertion — an interpretation, inference, or argued conclusion drawn from data
  Roadmap — a stated gap, hypothesis, limitation, or future direction

Every claim must be DECONTEXTUALIZED: fully self-contained and understandable
without the surrounding text.

Respond ONLY with one JSON object per line (JSONL). No markdown, no explanation.
Fields: "claim_type", "claim"

TEXT:
{record["text"]}"""

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=1024,
                temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                messages=[{"role": "user", "content": prompt}],
            )
            content = response.choices[0].message.content
            return (content or "").strip()
        except RateLimitError:
            wait = (2 ** attempt) * 5
            print(f"  [RATE LIMIT] chunk {record['chunk_id']}, attempt {attempt+1}/{MAX_RETRIES} — waiting {wait}s...")
            time.sleep(wait)
        except Exception as e:
            print(f"  [ERROR] chunk {record['chunk_id']}: {str(e)[:120]}")
            return ""
    print(f"  [FAILED] chunk {record['chunk_id']} exhausted retries")
    return ""

_data_base = os.environ.get("CLAIM_EXTRACT_DATA_DIR") or os.path.dirname(__file__)
input_path = os.path.join(_data_base, "test_output_tagged.jsonl")
output_path = os.path.join(_data_base, "final_claims_for_audit.jsonl")


with open(input_path, "r") as infile, open(output_path, "w") as outfile:
    for line in infile:
        record = json.loads(line)

        if "claims" not in record:
            print(f"[chunk {record['chunk_id']}] skipped (reference section)")
            continue

        if not record["claims"] and len(record.get("text", "")) < 80:
            print(f"[chunk {record['chunk_id']}] skipped (no claims, minimal text)")
            continue

        print(f"Processing chunk {record['chunk_id']} | {record['section_heading'][:60]}...")

        raw_output = extract_hybrid_claims(record)

        written = 0
        for claim_line in raw_output.split("\n"):
            claim_line = claim_line.strip()
            if not claim_line:
                continue
            try:
                clean = claim_line.replace("```json", "").replace("```", "").strip()
                claim_data = json.loads(clean)

                claim_data["chunk_id"] = record["chunk_id"]
                claim_data["doc_name"] = record["doc_name"]
                claim_data["category"] = record.get("category", "")
                claim_data["section_heading"] = record.get("section_heading", "")
                claim_data["semantic_category"] = record.get("semantic_category", "other")

                outfile.write(json.dumps(claim_data) + "\n")
                written += 1
            except Exception:
                continue

        print(f"  → {written} claims extracted")

print(f"\nDone. Final claims written to: {output_path}")
