# ResearchHub Crawler

Ingestion route for [Agent Core](../../README.md). Fetches ResearchHub **in-journal papers** and **active funding proposals** for the article and proposal review pipelines.

## Integration

Run as part of the Agent Core crawl step (`python orchestrate.py`), or invoke this crawler directly:

- Papers → [`articles/pipeline/run_full_pipeline.py`](../../articles/pipeline/run_full_pipeline.py)
- Proposals → [`proposals/pipeline/proposal_pipe.py`](../../proposals/pipeline/proposal_pipe.py)

## Entry points

| Script | Output |
|--------|--------|
| [`papers.py`](papers.py) | `crawlers/output/researchhub/papers/PaperRecord_*.json` — journal metadata + PDF URL |
| [`proposal.py`](proposal.py) | `crawlers/output/researchhub/proposals/proposal_*.json` — active fundraises with fulltext |
| [`crawl-for-review.py`](crawl-for-review.py) | Runs `papers.py` and `proposal.py` **in parallel** |

## Output layout

```text
crawlers/output/researchhub/
├── papers/
│   ├── PaperRecord_<id>.json
│   └── papers_manifest.json
└── proposals/
    ├── proposal_<id>.json
    └── manifest.json
```

## Manual run

From the repository root:

```bash
python crawlers/research-hub/crawl-for-review.py \
  --output-dir crawlers/output/researchhub
```

Run scripts individually:

```bash
python crawlers/research-hub/papers.py \
  --output-dir crawlers/output/researchhub/papers

python crawlers/research-hub/proposal.py \
  --output-dir crawlers/output/researchhub/proposals
```

## Incremental skip

When `AGENT_WALLET` is set, the crawl orchestrator loads the latest on-chain crawl-log and writes `crawlers/output/.crawl-skip.json`. Pass it to skip already-crawled files:

```bash
python crawlers/research-hub/crawl-for-review.py \
  --output-dir crawlers/output/researchhub \
  --crawl-skip-file crawlers/output/.crawl-skip.json
```

## Setup

- Python 3.10+
- Repo-root `pip install -r requirements.txt` (includes `requests`, `beautifulsoup4`)
- No API keys required — ResearchHub endpoints are public

## Architecture

```
crawlers/research-hub/
├── core/
│   ├── api_client.py      HTTP client, pagination, rate limiting
│   ├── base_crawler.py      Shared crawler base
│   └── incremental.py       Last-run timestamp tracking
├── papers.py                Journal feed export
├── proposal.py              Active funding proposals
└── crawl-for-review.py      Parallel driver (used by crawl orchestrator)
```

## Related documentation

- Crawlers overview: [crawlers/README.md](../README.md)
- Agent Core overview: [README.md](../../README.md)
- Articles pipeline: [articles/README.md](../../articles/README.md)
- Proposals pipeline: [proposals/README.md](../../proposals/README.md)
