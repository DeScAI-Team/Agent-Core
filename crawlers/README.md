# Crawlers

Ingestion layer for [Agent Core](../README.md). Public documentation covers **ResearchHub** and **Molecule** crawlers only.

The repo crawl step runs before review pipelines (`python orchestrate.py` or `python orchestrate.py --skip-crawl` to reuse existing output). All crawl artifacts land under `crawlers/output/` (gitignored).

## Documented crawlers

| Crawler | README | Output |
|---------|--------|--------|
| ResearchHub (papers + proposals) | [research-hub/README.md](research-hub/README.md) | `crawlers/output/researchhub/` |
| Molecule (IPNFT datarooms + web) | [molecule/crawler/README.md](molecule/crawler/README.md) | `crawlers/output/molecule/ipnfts/` |

## Manual ResearchHub run

```bash
python crawlers/research-hub/crawl-for-review.py \
  --output-dir crawlers/output/researchhub
```

## Manual Molecule run

```bash
cd crawlers/molecule/crawler
npm run cli -- crawl --output-dir @crawlers/output/molecule/ipnfts --index --concurrency 4
```

## Incremental skip

When `AGENT_WALLET` is set, the crawl orchestrator loads the latest on-chain crawl-log (v2) and writes `crawlers/output/.crawl-skip.json` containing **only entries marked `reviewed`**. Unreviewed crawl-log entries are re-crawled on the next run (recovering work after ephemeral container restarts). Pass the skip file to ResearchHub or Molecule crawlers:

```bash
--crawl-skip-file crawlers/output/.crawl-skip.json
```

## Downstream review routes

| Crawl output | Review pipeline |
|--------------|-----------------|
| `researchhub/papers/` | [articles/pipeline/run_full_pipeline.py](../articles/pipeline/run_full_pipeline.py) |
| `researchhub/proposals/` | [proposals/pipeline/proposal_pipe.py](../proposals/pipeline/proposal_pipe.py) |
| `molecule/ipnfts/<SYMBOL>/` | [DAOs/molecule/pipeline/run_dao_review.py](../DAOs/molecule/pipeline/run_dao_review.py) |

Compound reviews consume a separate local manifest under `crawlers/output/`; see [compounds/README.md](../compounds/README.md) for the review route (not the ingestion layer).

## Related documentation

- Agent Core overview: [README.md](../README.md)
