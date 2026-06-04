# Unified Arweave Uploader

Standalone upload module for Review-Generator pipeline outputs. Reads wallet configuration from the **repo-root [`.env`](../.env)** only (`PATH_TO_KEYFILE`, `AGENT_WALLET`).

Article and proposal pipelines no longer upload in-process; [`orchestrate.py`](../orchestrate.py) runs this module as step 6 (or invoke manually).

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

Tags: `doctype=crawllog`, `Crawl-Date`, `Content-Type=application/json`

## Library API

```python
from uploader.runner import run_recipe

run_recipe("article", dir="articles/data/foo/output", resume=False)
run_recipe("crawl-log", file="crawlers/output/crawl-log.json")
```

## Output

Writes `upload_metadata.json` to `--output-dir` with transaction IDs, URLs, and tags per step. Source files on disk are never modified (only temp copies with the injected `evidence_audit` field are uploaded).

## Node layer

Uploads use Turbo SDK via `arweaveService.js` / `arweaveServiceCLI.js` in this directory (`npm install` required).
