# Proposals review pipeline

ResearchHub funding-proposal review route for [Agent Core](../README.md). Reviews proposals from crawler JSON (`proposal_*.json`).

[`orchestrate.py`](../orchestrate.py) calls [`pipeline/proposal_pipe.py`](pipeline/proposal_pipe.py) for each file in `crawlers/output/researchhub/proposals/`.

## Pipeline flow

1. Load crawler JSON (fulltext + funding metadata)
2. Sliding-window screener (tagger LLM)
3. OpenAlex originality search + statement
4. Per-category LLM rationales and scores
5. Composite score (weighted mean, int-ceil 0–100)
6. Plain-language overview
7. Evidence audit (`evidence_audit.md`)

## Categories

From [`pipeline/proposal_mappings.json`](pipeline/proposal_mappings.json):

| Key | Label | What it measures |
|-----|-------|------------------|
| `scientific_grounding` | Scientific Grounding | Methods and rationale grounded in cited literature |
| `evidential_strength` | Evidential Strength | Preliminary evidence strong enough for the claims |
| `originality` | Originality | Genuinely novel proposed work |
| `funding_realism` | Funding Realism | Requested funding realistic for scope; campaign on track |

## Environment

Uses the same `LLM_*` and `TAGGER_*` variables as articles ([`proposals/llm_env.py`](llm_env.py) re-exports [`articles/llm_env.py`](../articles/llm_env.py)):

- **Tagger** (`TAGGER_BASE_URL`): sliding-window screener JSON
- **Review LLM** (`LLM_BASE_URL`): category rationales, funding realism, originality statement, review statement, overview
- **OpenAlex** (`OPENALEX_EMAIL`): originality search User-Agent

## Crawl input

Proposals are fetched by the ResearchHub crawler:

```bash
python crawlers/research-hub/crawl-for-review.py \
  --output-dir crawlers/output/researchhub
```

Each proposal is saved as `crawlers/output/researchhub/proposals/proposal_<id>.json`.

## Output layout

`--output-dir` is the **run root** (e.g. `reviews/proposals/proposal_32265/`):

| Path | Contents |
|------|----------|
| `steps/screener_findings.json` | Screener diagnostics |
| `steps/originality.json` | OpenAlex payload |
| `steps/openalex_search_cache.json` | Search cache |
| `review/review.json` | Scored review (integer 0–100 scores; `score` + `rationale` per category) |
| `review/overview.json` | Layperson copy |
| `review/evidence_audit.md` | Audit trail |

## Run

From the repository root:

```bash
python proposals/pipeline/proposal_pipe.py \
  --input-json crawlers/output/researchhub/proposals/proposal_4459.json \
  --output-dir reviews/proposals/proposal_4459
```

### CLI flags

| Flag | Purpose |
|------|---------|
| `--input-json` | Path to crawler proposal JSON (required) |
| `--output-dir` | Run root (default derived from proposal id under `reviews/proposals/`) |
| `--mappings` | Path to `proposal_mappings.json` (default: `pipeline/proposal_mappings.json`) |
| `--skip-llm` | Skip all LLM calls — output window debug info only |
| `--skip-openalex` | Skip OpenAlex search (use cached results if available) |

Upload via orchestrator or manually:

```bash
python -m uploader --recipe proposal --dir reviews/proposals/proposal_4459/review
```

## Related documentation

- ResearchHub crawler: [crawlers/research-hub/README.md](../crawlers/research-hub/README.md)
- Agent Core overview: [README.md](../README.md)
