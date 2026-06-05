# Unified Arweave Uploader

Standalone upload module for [Agent Core](../README.md) pipeline outputs. Reads wallet configuration from the **repo-root [`.env`](../.env)** only (`PATH_TO_KEYFILE`, `AGENT_WALLET`).

Pipelines no longer upload in-process. [`orchestrate.py`](../orchestrate.py) runs this module **after each review item** completes. Successful review uploads auto-mark the matching entry as `reviewed` in [`crawlers/output/crawl-log.json`](../crawlers/output/crawl-log.json) (v2 schema). The orchestrator uploads that file to Arweave every 5 successful review uploads. The final orchestrator step is R2 snapshot (`python -m snapshotter`), not upload.

## Setup

```bash
cd uploader
npm install
```

Ensure repo-root `.env` includes:

```env
AGENT_WALLET=your-wallet-address
PATH_TO_KEYFILE=./arweave-keyfile-....json
```

Paths in `PATH_TO_KEYFILE` are resolved relative to the **repo root** if not absolute.

## Usage

From the repo root:

```bash
python -m uploader --recipe article --dir reviews/articles/<stem>/review [--resume]
python -m uploader --recipe proposal --dir reviews/proposals/proposal_<id>/review [--resume]
python -m uploader --recipe dao --dir reviews/DAOs/<SYMBOL>/review [--resume]
python -m uploader --recipe compounds --dir reviews/compounds/<TICKER>/review [--resume]
python -m uploader --recipe crawl-log --file crawlers/output/crawl-log.json [--output-dir crawlers/output]
```

### Flags

| Flag | Purpose |
|------|---------|
| `--recipe` | `article`, `proposal`, `dao`, `compounds`, or `crawl-log` |
| `--dir` | **Inputs only** — source files to upload |
| `--output-dir` | **Metadata only** — writes `upload_metadata.json`; defaults to `--dir` |
| `--file` | Input file for `crawl-log` |
| `--crawl-date` | Override `Crawl-Date` tag for crawl-log |
| `--resume` | Skip steps already recorded in output-dir metadata |
| `--crawl-log-file` | Override crawl-log path for auto-marking after review uploads (default: `crawlers/output/crawl-log.json`) |

Separate inputs and metadata:

```bash
python -m uploader --recipe article \
  --dir path/to/sources \
  --output-dir path/to/metadata \
  --resume
```

## Recipes

All review recipes (article, proposal, dao, compounds) use the same 3-step pattern:

1. Upload evidence markdown
2. Upload review JSON (temp copy with `evidence_audit` field injected after `review_date`)
3. Upload overview JSON (same `evidence_audit` field injection)

Source files on disk are never modified. `review_statement` text is unchanged on upload.

### Article (3 steps)

Inputs in `--dir`: `evidence_audit.md`, `review.json`, `overview.json`

Tags: `doctype`, `platform=ResearchHub`, `category=Article`, `research_name`, `review_date`

### Proposal (3 steps)

Inputs: `evidence_audit.md`, `review.json`, `overview.json`

Tags: `platform=ResearchHub`, `category=Proposal`, `doctype` (`EvidenceAudit` / `review` / `overview`), `name`, `date`

### DAO (3 steps)

Inputs: `evidence_audit.md`, `review.json`, `overview.json`

Tags: `doctype`, `DaoName`, `platform=Molecule`, `category=ResearchDAO`, `date`

### Compounds (3 steps)

Inputs: `evidence_audit.md`, `*-review.json` (or `review.json`), `overview.json`

Tags: `doctype`, `platform=PumpScience`, `category=compounds`, `compounds`, `date`

### Crawl-log (1 step)

Upload-only recipe (no reviewed marking). Use for post-crawl snapshots and orchestrator checkpoints. Do **not** pass `--resume` for checkpoint uploads.

Tags: `doctype=crawllog`, `Crawl-Date`, `Content-Type=application/json`

Crawl-log v2 entries are objects with optional `reviewed: "reviewed"`:

- `researchhub.files[]`: `{ "path": "papers/..." }` or `{ "path": "proposals/...", "reviewed": "reviewed" }`
- `molecule.folders[]`: `{ "name": "..." }`
- `pumpScience.tickers[]`: `{ "ticker": "..." }`

## Library API

```python
from uploader.runner import run_recipe

run_recipe("article", dir="reviews/articles/<stem>/review", resume=False)
run_recipe("crawl-log", file="crawlers/output/crawl-log.json")
```

## Output

Writes `upload_metadata.json` to `--output-dir` with transaction IDs, URLs, and tags per step. Source files on disk are never modified (only temp copies with the injected `evidence_audit` field are uploaded).

## Node layer

Uploads use Turbo SDK via `arweaveService.js` / `arweaveServiceCLI.js` in this directory (`npm install` required).

## Related documentation

- Agent Core overview: [README.md](../README.md)
