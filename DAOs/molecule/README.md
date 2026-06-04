# Molecule Research DAO Review Pipeline

Pipeline that turns a crawled IPNFT dataroom into a published Research DAO review (`review.json` + `overview.json` + `evidence_audit.md`).

## Flow

`process` → `chunk` → `extract+tag` → `validate` → `group+score` → `review` → `overview` → `evidence`

## Output layout

```text
reviews/DAOs/<SYMBOL>/
├── review/
│   ├── review.json          # full scored review (matches compounds shape)
│   ├── overview.json        # plain-language simplification
│   └── evidence_audit.md    # non-LLM provenance
└── steps/
    ├── bundle/              # multimedia processor output (cached)
    ├── chunks.jsonl         # provenance-tagged text chunks + on-chain facts
    ├── extracted.jsonl      # claim/feature/mission/fact + tags
    ├── validated.jsonl      # + verdict (valid/invalid/positive/negative/neutral) + citations
    ├── groups/<category>.json
    └── group_scores.json
```

## Categories

| Key | What it measures |
|-----|------------------|
| `research_output_quality` | Papers, code, datasets, demos, proposals, integrations actually shipped. |
| `scientific_grounding`    | Mission justification + scientific claims validated against the literature (OpenAlex). |
| `execution_competence`    | Delivery cadence, milestones met, repo activity, demonstrated ability to ship. |
| `team_credibility`        | Research lead identifiable, organization verifiable (minimal weight). |
| `mission_clarity`         | How clear, specific, and well-bounded the stated mission is. |
| `governance_tokenomics`   | On-chain agreements, IPT structure, holder distribution, liquidity. |

Composite score = weighted mean of per-category scores (weights in [`dao_mappings.json`](pipeline/dao_mappings.json)), int-ceil to 0–100. Each per-category score is the deterministic `(valid + positive) / (total − null/neutral/inconclusive)` aggregate.

## Quickstart

```bash
# Single DAO
python DAOs/molecule/pipeline/run_dao_review.py \
  --ipnft-dir output/molecule/ipnfts/BeeARD

# Reuse an existing bundle (skip the OCR/vision/whisper rebuild)
python DAOs/molecule/pipeline/run_dao_review.py \
  --ipnft-dir output/molecule/ipnfts/BeeARD --reuse-bundle

# Skip vision entirely (text-only smoke run)
python DAOs/molecule/pipeline/run_dao_review.py \
  --ipnft-dir output/molecule/ipnfts/BeeARD --skip-vision

# Resume from a specific stage
python DAOs/molecule/pipeline/run_dao_review.py \
  --ipnft-dir output/molecule/ipnfts/BeeARD --from-step review

# Batch
python DAOs/molecule/pipeline/run_dao_review.py \
  --batch output/molecule/ipnfts
```

## Environment

| Variable | Purpose |
|----------|---------|
| `LLM_BASE_URL` / `LLM_API_KEY` | Primary LLM endpoint (extract, validate, review, overview). |
| `TAGGER_BASE_URL` / `TAGGER_API_KEY` | Optional dedicated tagger endpoint (falls back to LLM_*). |
| `READ_PAPER_MODEL` | Vision/OCR model used by the multimedia processor (default: `nanonets/Nanonets-OCR2-3B`). |
| `WHISPER_CPP_BIN` / `WHISPER_MODEL_PATH` | whisper.cpp binary + model for video audio. |
| `OPENALEX_EMAIL` | Used in OpenAlex User-Agent for polite pool. |

`ffmpeg` and `ffprobe` must be on PATH for video processing.

## Stages and entry points

| Stage | Script | Output |
|-------|--------|--------|
| process | [`multimedia_processor.py`](pipeline/multimedia_processor.py) | `steps/bundle/` (PDF JSONs + image JSONs + video frames JSONL + extracted text MDs) |
| chunk | [`chunk.py`](pipeline/chunk.py) | `steps/chunks.jsonl` |
| extract | [`extract_tag.py`](pipeline/extract_tag.py) | `steps/extracted.jsonl` |
| validate | [`validate.py`](pipeline/validate.py) | `steps/validated.jsonl` |
| group | [`group_score.py`](pipeline/group_score.py) | `steps/groups/*.json` + `steps/group_scores.json` |
| review | [`review.py`](pipeline/review.py) | `review/review.json` |
| overview | [`overview.py`](pipeline/overview.py) | `review/overview.json` |
| evidence | [`evidence_audit.py`](pipeline/evidence_audit.py) | `review/evidence_audit.md` |

Each stage script is independently runnable for debugging; the orchestrator just chains them in order.
