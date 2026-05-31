# Unified Arweave Uploader

Standalone upload module for Review-Generator pipeline outputs. Reads wallet configuration from the **repo-root [`.env`](../.env)** only (`PATH_TO_KEYFILE`, `AGENT_WALLET`).

Existing per-pipeline uploaders are unchanged; use this module directly until pipelines are wired later.

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
python -m uploader --recipe article --dir articles/data/foo/output [--resume]
python -m uploader --recipe proposal --dir proposals/data/4459 [--resume]
python -m uploader --recipe dao --dir DAOs/molecule/output/CLAW/synthesis [--resume]
python -m uploader --recipe compounds --dir path/to/compound/output [--resume]
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

Separate inputs and metadata:

```bash
python -m uploader --recipe article \
  --dir path/to/sources \
  --output-dir path/to/metadata \
  --resume
```

## Recipes

### Article (3 steps)

Inputs in `--dir`: `evidence_audit.md`, `review.json`, `overview.json`

1. Upload evidence → append link to `review.json` → upload review → append link to `overview.json` → upload overview
2. Tags: `doctype`, `platform=ResearchHub`, `category=Article`, `research_name`, `review_date`

### Proposal (2 steps)

Inputs: `evidence_audit.md`, `review.json`

Tags: `platform=ResearchHub`, `category=Proposal`, `doctype` (`EvidenceAudit` / `review`), `name`, `date`

### DAO (3 steps)

Inputs: `dao_evidence_audit.md`, `dao_review.json`, `overview.json`

Tags: `doctype`, `DaoName`, `platform=Molecule`, `category=ResearchDAO`, `date`

### Compounds (2 steps)

Inputs: `evidence_audit.md` and `*-review.json` (or `review.json`)

Tags: `doctype`, `platform=PumpScience`, `category=compounds`, `compounds`, `date`

### Crawl-log (1 step)

Tags: `doctype=crawllog`, `Crawl-Date`, `Content-Type=application/json`

## Library API

```python
from uploader.runner import run_recipe

run_recipe("article", dir="articles/data/foo/output", resume=False)
run_recipe("crawl-log", file="crawlers/output/crawl-log.json")
```

## Output

Writes `upload_metadata.json` to `--output-dir` with transaction IDs, URLs, and tags per step. Source files on disk are never modified (only temp copies with appended links are uploaded).

## Node layer

Uploads use Turbo SDK via `arweaveService.js` / `arweaveServiceCLI.js` in this directory (`npm install` required).
