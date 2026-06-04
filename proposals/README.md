# Proposals review pipeline

Reviews ResearchHub funding proposals from crawler JSON (`proposal_*.json`).

## Environment

Uses the same `LLM_*` and `TAGGER_*` variables as articles ([`proposals/llm_env.py`](llm_env.py) re-exports [`articles/llm_env.py`](../articles/llm_env.py)):

- **Tagger** (`TAGGER_BASE_URL`): sliding-window screener JSON
- **Review LLM** (`LLM_BASE_URL`): category rationales, funding realism, originality statement, review statement, overview

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

```bash
python proposals/pipeline/proposal_pipe.py \
  --input-json crawlers/output/researchhub/proposals/proposal_4459.json \
  --output-dir reviews/proposals/proposal_4459
```

Upload via orchestrator step 6 or:

```bash
python -m uploader --recipe proposal --dir reviews/proposals/proposal_4459/review
```
