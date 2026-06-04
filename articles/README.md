# Articles Pipeline

Research-paper review route for [Agent Core](../README.md). Claim extraction, validation, routing, and evidence grading live under `pipeline/`. The entry driver is [`pipeline/run_full_pipeline.py`](pipeline/run_full_pipeline.py), invoked directly or by [`orchestrate.py`](../orchestrate.py) for each paper in `crawlers/output/researchhub/papers/`.

## Top-level stages (`run_full_pipeline.py`)

| Stage | What it does |
|-------|--------------|
| `fetch` | Download PDF from URL (or use local path) |
| `reader` | OCR / vision read → `steps/full.md` |
| `add_data` | Chunk PDF → `text_knowledge_base.jsonl` |
| `route` | Classify paper type (empirical, theoretical_narrative, protocol) |
| `pipeline` | Run the matched sub-pipeline through to `review/` outputs |

Sub-pipelines share steps 1–5 (claim extract → classify → group). The **empirical** route continues with steps 6–13 below.

## Empirical pipeline (13 steps)

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `pipeline/claim-extract/spacy_test.py` | `text_knowledge_base.jsonl` | `test_output_tagged.jsonl` |
| 2 | `pipeline/claim-extract/LLM_extract.py` | `test_output_tagged.jsonl` | `final_claims_for_audit.jsonl` |
| 3 | `pipeline/claim-extract/claim_validator.py` | `final_claims_for_audit.jsonl` | `validated_claims.jsonl` |
| 4 | `pipeline/classify_claims.py` | `validated_claims.jsonl` | `classified_claims.jsonl` |
| 5 | `pipeline/group.py` | `classified_claims.jsonl` | `grouped.json` |
| 6 | `pipeline/empirical/triage.py` | `grouped.json` | `triaged.json` |
| 7 | `pipeline/empirical/retrieve_compare.py` | `triaged.json` + KB + `full.md` | `retrieve_compare_llm.json` |
| 8 | `pipeline/empirical/prep.py` | `retrieve_compare_llm.json` | `prepped_evidence.json` |
| 9 | `pipeline/empirical/review.py` | `prepped_evidence.json` | `review.json` |
| 10 | `pipeline/empirical/originality_check.py` | KB + `full.md` | `originality.json` (patches `review.json`) |
| 11 | `pipeline/empirical/screener.py` | `full.md` + caches | `screener.json` (patches `review.json`) |
| 12 | `pipeline/empirical/score.py` | `review.json` + all intermediates | `review.json` (final scores + composite) |
| 13 | `pipeline/empirical/evidence-doc.py` | `review.json` + intermediates | `evidence_audit.md` |

Steps 1–5 extract, validate, classify, and group claims. Steps 6–13 grade those claims against cited references, assess originality, screen the full document, compute unified scores, and produce an audit trail. See [`pipeline/empirical/PIPELINE.md`](pipeline/empirical/PIPELINE.md) for detailed documentation of steps 6–13.

## Prerequisites

- Python 3.10+
- `openai`, `python-dotenv` (LLM steps)
- `docling`, `spacy`, `transformers` (PDF chunking and tagging)
- A reachable vLLM (or OpenAI-compatible) server — see the root [README.md](../README.md) for base URL and model configuration

## Running

From the **repository root**:

```bash
python articles/pipeline/run_full_pipeline.py https://example.com/paper.pdf
python articles/pipeline/run_full_pipeline.py paper.pdf --from-step add_data
python articles/pipeline/run_full_pipeline.py paper.pdf --stop-after reader
python articles/pipeline/run_full_pipeline.py reviews/articles/<stem>/   # resume existing run
```

Use `--from-step` / `--stop-after` with stages: `fetch`, `reader`, `add_data`, `route`, `pipeline`. Add `--skip-llm` to skip LLM calls in downstream sub-pipelines.

## Directory layout

```
articles/
├── pipeline/
│   ├── run_full_pipeline.py  Entry driver (fetch → route → sub-pipeline)
│   ├── claim-extract/        Steps 1-3: spaCy tagging, LLM extraction, validation
│   │   ├── add_data.py       PDF → text_knowledge_base.jsonl
│   │   ├── spacy_test.py
│   │   ├── LLM_extract.py
│   │   └── claim_validator.py
│   ├── classify_claims.py    Step 4: semantic claim-type tags
│   ├── group.py              Step 5: group by scoring dimension
│   ├── read-paper.py         Standalone PDF reader
│   ├── classify-paper.py     Paper type classifier (routing)
│   ├── mappings.json         Dimension definitions, tag index, weights, rubrics
│   ├── empirical/            Steps 6-13: evidence grading pipeline
│   ├── Theoretical-narrative/  Theoretical paper sub-pipeline
│   └── Protocol-pre_results/   Protocol paper sub-pipeline
├── prompts/                  Shared prompts (classification, verdict fallbacks, etc.)
└── llm_env.py                LLM endpoint config (shared with proposals/compounds)
```

## Configuration

Set via `.env` in the repo root (`articles/llm_env.py` loads it):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` / `LLM_API_KEY` | `:8000` | Review LLM (extract, validate, evidence, screener, score) |
| `LLM_MODEL` / `VALIDATOR_MODEL` | `/model` | Review model id (`VALIDATOR_MODEL` is a deprecated alias) |
| `TAGGER_BASE_URL` / `TAGGER_API_KEY` | falls back to `LLM_*` | Claim classification + heading labels |
| `TAGGER_MODEL` | auto / `CLASSIFIER_MODEL` | Tagger model id |
| `VISION_MODEL_URL` / `VISION_MODEL_API_KEY` | `:8001` | PDF OCR |
| `READ_PAPER_MODEL` | `nanonets/Nanonets-OCR2-3B` | Vision OCR model id |
| `VALIDATOR_CONCURRENCY` | `15` | Max concurrent validation requests (step 3) |

## Output layout

Per paper under `reviews/articles/<pdf_stem>/`:

- `steps/` — `full.md`, knowledge base, claim JSONL, triage/retrieve caches, screener, originality
- `review/` — `review.json`, `overview.json`, `evidence_audit.md`

## Key outputs

| File | Description |
|------|-------------|
| `review.json` | Final review: `composite_score` and per-category `score` as **integers 0–100** (ceil); each category has `score` + `rationale` only (empty rationales omitted) |
| `overview.json` | Plain-language companion (when LLM overview is enabled in `score.py`) |
| `evidence_audit.md` | Human-readable audit trail: provenance, scores, claim/citation trace, screener quotes, originality |

## Related documentation

- Agent Core overview: [README.md](../README.md)
- Empirical evidence pipeline details: [pipeline/empirical/PIPELINE.md](pipeline/empirical/PIPELINE.md)
