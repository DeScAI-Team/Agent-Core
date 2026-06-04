# DeScAi — Claim Extraction & Validation Pipeline

## Pipeline Overview

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `add_data.py` (semantic headings via local vLLM) | PDF files | `text_knowledge_base.jsonl` |
| 2 | `spacy_test.py` | `text_knowledge_base.jsonl` | `test_output_tagged.jsonl` |
| 3 | `LLM_extract.py` (claim extraction via local vLLM) | `test_output_tagged.jsonl` | `final_claims_for_audit.jsonl` |
| 4 | `claim_validator.py` | `final_claims_for_audit.jsonl` | `validated_claims.jsonl` |
| 5 | `claim-classifier/classify_claims.py` | `validated_claims.jsonl` | `claim-classifier/classified_claims.jsonl` (or `-o`, e.g. under `data/`) |
| 6 | `group-and-score/group.py` | Classified claims JSONL (`claim_classification_*` fields) | Grouped JSON (stdout or `-o`) |
| 7 | `group-and-score/prep.py` | JSON from step 6 | Same structure + `claim_narrative` per claim |
| 8 | `review-gen/review.py` | `prepped.json` | `review.json` |
| 9 (optional) | `Arweave-Cli/upload_orchestrator.py` | `review.json` | On-chain upload; receipt JSON (`--receipt`, default under `Arweave-Cli/`) |

**Dependencies (LLM steps):** Install `openai` and `python-dotenv` (`pip install openai python-dotenv`). Steps 1–5 and **8** do **not** require `anthropic` or `ANTHROPIC_API_KEY`. Docling, spaCy, Transformers, etc. are still required for PDF chunking and tagging as before. Steps 6–7 are **stdlib-only** (JSON in/out). Step **9** needs Node and `Arweave-Cli` setup.

Step 5 details: [claim-classifier/README.md](claim-classifier/README.md).

---

## Invocation

```bash
# Step 1 — Convert PDFs to chunks
python claim-extract-test/add_data.py --folder <pdf_dir> -o claim-extract-test/text_knowledge_base.jsonl

# Step 2 — spaCy dependency tagging
python claim-extract-test/spacy_test.py

# Step 3 — LLM claim extraction (local vLLM, OpenAI-compatible API)
python claim-extract-test/LLM_extract.py

# Step 4 — Claim validation (local vLLM)
python claim-extract-test/claim_validator.py

# Step 5 — Claim classification tags (local vLLM)
python claim-classifier/classify_claims.py

# Step 6 — Group claims by scoring dimension (see group-and-score below)
python group-and-score/group.py claim-classifier/classified_claims.jsonl -o group-and-score/grouped.json

# Step 7 — Add LLM-facing narrative text to each claim in grouped output
python group-and-score/prep.py group-and-score/grouped.json -o group-and-score/prepped.json
```

Pipe **group → prep** without an intermediate file (use `-` for stdin; required on Windows—`/dev/stdin` does not exist):

```bash
python group-and-score/group.py claim-classifier/classified_claims.jsonl | python group-and-score/prep.py - -o group-and-score/prepped.json
```

**One-shot end-to-end** (steps 1–8; writes steps 5–8 under `data/` by default, or `--artifacts-dir`):

```bash
python run_e2e_pipeline.py --dry-run
python run_e2e_pipeline.py
python run_e2e_pipeline.py --pdf path/to/paper.pdf --artifacts-dir data
python run_e2e_pipeline.py --upload   # optional step 9: Arweave upload of data/review.json
```

---

## Configuration — local vLLM (OpenAI-compatible)

Set via environment variables or a `.env` file in the project root. See `env-example.txt`. Articles use three endpoints via `articles/llm_env.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` / `LLM_API_KEY` | `:8000` | Review LLM (`VLLM_*` deprecated alias) |
| `LLM_MODEL` / `VALIDATOR_MODEL` | `/model` | Review model id |
| `TAGGER_BASE_URL` / `TAGGER_MODEL` | `:8002` or LLM | Claim tags + section headings |
| `VISION_MODEL_URL` / `READ_PAPER_MODEL` | `:8001` | PDF OCR |
| `CLASSIFIER_MODEL` | (tagger fallback) | Optional override for claim classification |
| `VALIDATOR_CONCURRENCY` | `15` | Max concurrent async validation requests (step 4 only) |
| `VALIDATOR_KEY_SECTION_MAX_CHARS` | `24000` | Max chars of context injected per claim validation prompt (step 4) |
| `VALIDATOR_SOURCE_CHUNKS` | `text_knowledge_base.jsonl` | Source chunks file for building per-claim validation context (step 4) |

---

## Output Schema — `validated_claims.jsonl`

Each line is a JSON object extending the extraction schema with three new fields:

```json
{
  "claim_type": "Fact | Assertion | Roadmap",
  "claim": "Decontextualized claim text.",
  "chunk_id": 1,
  "doc_name": "paper_filename",
  "category": "bio-new",
  "section_heading": "Results",
  "verdict": "supported | unsupported | insufficient_info",
  "rationale": "≤50-word explanation of the verdict.",
  "relevancy_score": 0.85,
  "validation_error": true
}
```

> `validation_error` is only present (and `true`) when both the primary call **and** the verdict fallback failed to produce a valid verdict. These records are **flagged, not dropped**, so Phase 3 can handle or re-run them.

### Context assembly (step 4)

Each claim receives targeted context built from `text_knowledge_base.jsonl` rather than a single static blob per document:

- **Layer A (sliding window):** the source chunk (matched by `chunk_id`) and its ±2 neighbors.
- **Layer B (claim-type mapping):** additional chunks whose `section_heading` matches entries in `CLAIM_TYPE_SECTION_MAP` for the claim's `claim_type` (`Fact`, `Assertion`, or `Roadmap`).

Both layers are truncated to `VALIDATOR_KEY_SECTION_MAX_CHARS` (default 24 000 chars; 40% reserved for Layer A, 60% for Layer B).

### Fallback prompts (step 4)

If the primary JSON call fails after `MAX_RETRIES` (default 3), two fallback prompts fire sequentially:

1. **`prompts/verdict_fallback_prompt.md`** — asks the model for a single-word verdict (no JSON). If this also fails, the record is flagged `validation_error=True`.
2. **`prompts/rationale_fallback_prompt.md`** — given a valid verdict, asks the model for a plain-text rationale. If this fails, a generic placeholder is used.

### Relevancy score tiers

The `relevancy_score` (0.00–1.00) is guided by five tiers in the validation prompt:

| Range | Tier | Description |
|-------|------|-------------|
| 0.00–0.20 | low relevancy | General domain knowledge restated from literature |
| 0.20–0.40 | slightly relevant | Administrative/procedural details (timelines, funding, ethics) |
| 0.40–0.60 | moderately relevant | Methodological choices specific to the study |
| 0.60–0.80 | very relevant | Study-specific design decisions, hypotheses, interpretive claims |
| 0.80–1.00 | extremely relevant | Novel findings, unique results, core conclusions by this researcher |

### Verdict meanings

| Verdict | Meaning |
|---------|---------|
| `supported` | Key sections substantiate the claim |
| `unsupported` | Key sections contradict or do not support the claim |
| `insufficient_info` | Key sections lack enough detail to judge |
| `validation_error` | Both primary call and verdict fallback failed (flagged) |

---

## Output schema — `classified_claims.jsonl` (step 5)

Each line is the same object as `validated_claims.jsonl`, plus three arrays (each length 0 or 1) for the first three distinct allowlisted tags returned by the classifier. See [claim-classifier/README.md](claim-classifier/README.md) for behavior, defaults, and examples.

---

## Grouping & narratives (`group-and-score/`, steps 6–7)

**Mappings.** [group-and-score/mappings.json](group-and-score/mappings.json) defines dimensions and a `tag_index` from classifier tag names (e.g. `Methodological`, `Hypothesis`) to group ids (e.g. `scientific_rigor`, `originality`). Tags absent from `tag_index` are ignored for grouping.

**`group.py`** reads one JSON object per line (same schema as step 5). It first **drops bogus placeholder records** (empty `claim` text and known non-claims such as `No scientific claims identified in the text.` from extractor/validator artifacts). Those lines are omitted from grouping and scoring; a count is printed to **stderr** when any are removed. For each remaining line it unions tags from `claim_classification_1`, `claim_classification_2`, and `claim_classification_3`, maps each tag through `tag_index`, and places the **full claim object** in every matching group **once per group** (duplicate tags that map to the same dimension still yield a single copy). Empty groups are omitted from the output.

In the reorganized repo layout, the same script lives at [`articles/pipeline/group.py`](articles/pipeline/group.py).

Each group value is an object:

```json
{
  "score": 0.75,
  "members": [ { "...full claim fields..." }, ... ]
}
```

- **`score`**: First compute the **verdict support ratio** — `supported / (supported + unsupported)` over that group’s `members`, counting only `verdict` values `supported` and `unsupported` (claims with `insufficient_info` or any other verdict still do not contribute to that ratio’s numerator or denominator). Then **blend relevancy**: let **mean_rel** be the mean of `relevancy_score` over members that have a numeric score in **0.0–1.0** (from step 4). Let **eff_rel** = **mean_rel^γ** with default **γ = 0.5** (square root), so low group-mean relevancy is less punishing than a linear product; set **`RELEVANCY_BLEND_EXPONENT=1`** for legacy `support_ratio × mean_rel`. Let **blended** = `support_ratio × eff_rel` when at least one relevancy is present, else `support_ratio` alone. Then **small‑n shrink** toward **0.5**: with **n = len(members)** and prior **`SCORE_SHRINK_PRIOR` = 5** in [`articles/pipeline/group.py`](articles/pipeline/group.py), **`score` = round(clamp(blended × n/(n+5) + 0.5 × 5/(n+5), 0, 1), 4)`** — so scores with few members are pulled toward 0.5; large groups are almost unchanged. If the support-ratio denominator would be zero, `score` is JSON `null`.

**`prep.py`** reads that grouped JSON and writes the same structure with one extra string field on each member: **`claim_narrative`**, built from [prompts/claim_llm_narrative_template.md](prompts/claim_llm_narrative_template.md) (sentence under `## Sentence template`). Relevancy wording follows five buckets on `relevancy_score` (0.0–1.0), documented in that file. Override the template path with `--template`.

**CLI quick reference**

| Script | Arguments |
|--------|-----------|
| `group.py` | `<input.jsonl>` [`-o` out.json] [`--mappings` path] |
| `prep.py` | `<grouped.json>` or `-` (stdin) [`-o` out.json] [`--template` path] |
