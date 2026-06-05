# Arweave uploading in Review-Generator

This repo uploads review outputs, evidence audits, crawl logs, and related artifacts to [Arweave](https://www.arweave.org/) via the [ArDrive Turbo SDK](https://github.com/ardriveapp/turbo-sdk). Uploads are signed with an Arweave wallet keyfile (JWK). Turbo requires a funded wallet (AR and/or Turbo credits).

## Two upload stacks

There are **two** Node-backed configurations. Pipelines use one or the other today; both upload through Turbo and return `https://arweave.net/<txId>` URLs.

| Stack | Wallet config | Node entrypoint | Used by |
|-------|---------------|-----------------|---------|
| **Unified** (`uploader/`) | Repo-root [`.env`](.env): `PATH_TO_KEYFILE`, `AGENT_WALLET` | `uploader/arweaveServiceCLI.js` | `python -m uploader`, crawl pipeline |
| **Article uploader** (`articles/article_uploader/`) | [`Arweave-Cli/.env`](Arweave-Cli/.env): `WALLET_PATH` | `articles/article_uploader/upload_cli.js` | `proposals/uploader.py`, article pipeline |

**Recommendation:** Point both env files at the **same** keyfile (e.g. `./arweave-keyfile-<address>.json` at repo root). Never commit keyfiles or `.env` — they are gitignored.

### Unified stack setup

```bash
cd uploader && npm install
```

Repo-root `.env` (see [`env-example.txt`](env-example.txt); add wallet lines locally):

```env
AGENT_WALLET=your-wallet-address
PATH_TO_KEYFILE=./arweave-keyfile-....json
```

`PATH_TO_KEYFILE` is resolved relative to the **repo root** when not absolute.

### Article-uploader stack setup

```bash
cd Arweave-Cli && npm install
cd ../articles/article_uploader && npm install
```

`Arweave-Cli/.env` (copy from [`Arweave-Cli/.env.example`](Arweave-Cli/.env.example)):

```env
WALLET_PATH=../arweave-keyfile-....json
```

Paths in `WALLET_PATH` are resolved from `Arweave-Cli/` when relative.

---

## Unified uploader (`python -m uploader`)

The preferred **recipe-based** CLI for new work and for crawl logs. Orchestrates sequential uploads, appends cross-links into temp JSON copies (never mutates sources on disk), and writes [`upload_metadata.json`](articles/article_uploader/upload_metadata.schema.json) to an output directory. Review recipes auto-mark matching entries as `reviewed` in `crawlers/output/crawl-log.json` (v2) on successful upload; [`orchestrate.py`](../orchestrate.py) uploads that file every 5 review uploads.

Full module docs: [`uploader/README.md`](uploader/README.md).

### Commands

From repo root:

```bash
python -m uploader --recipe article --dir articles/data/<study>/output [--resume]
python -m uploader --recipe proposal --dir proposals/data/<id> [--resume]
python -m uploader --recipe dao --dir DAOs/molecule/output/<DAO>/synthesis [--resume]
python -m uploader --recipe compounds --dir path/to/compound/output [--resume]
python -m uploader --recipe crawl-log --file crawlers/output/crawl-log.json [--output-dir crawlers/output]
```

| Flag | Purpose |
|------|---------|
| `--recipe` | `article`, `proposal`, `dao`, `compounds`, `crawl-log` |
| `--dir` | Input directory (review recipes) |
| `--output-dir` | Where to write `upload_metadata.json` (defaults to `--dir` or crawl file parent) |
| `--file` | Input file (`crawl-log` only) |
| `--crawl-date` | Override `Crawl-Date` tag for crawl-log |
| `--resume` | Skip steps already present in existing metadata (review recipes only; do not use for crawl-log checkpoints) |
| `--crawl-log-file` | Override crawl-log path for auto-marking after review uploads |

### Recipes (steps and tags)

| Recipe | Steps | Key inputs | Platform / category | Linking |
|--------|-------|------------|---------------------|---------|
| **article** | 3 | `evidence_audit.md` → `review.json` → `overview.json` | ResearchHub / Article | `evidence_audit` field on review + overview JSON |
| **proposal** | 3 | `evidence_audit.md` → `review.json` → `overview.json` | ResearchHub / Proposal | Same |
| **dao** | 3 | `evidence_audit.md` → `review.json` → `overview.json` | Molecule / ResearchDAO | Same |
| **compounds** | 3 | `evidence_audit.md` → `review.json` → `overview.json` | PumpScience / compounds | Same |
| **crawl-log** | 1 | `crawl-log.json` | `doctype=crawllog`, `Crawl-Date` | None |

Tag fields vary by recipe (`research_name`, `compounds`, `DaoName`, `name`, `date`, etc.). `Content-Type` is auto-detected from the file extension when not set in tags.

### Library API

```python
from uploader.runner import run_recipe

run_recipe("article", dir="articles/data/foo/output", resume=False)
run_recipe("crawl-log", file="crawlers/output/crawl-log.json", output_dir="crawlers/output")
```

### Crawl pipeline integration

[`crawlers/full-crawl.mjs`](crawlers/full-crawl.mjs) runs after crawls merge `crawl-log.json` and invokes:

```bash
python -m uploader --recipe crawl-log --file crawlers/output/crawl-log.json --output-dir crawlers/output
```

Unless `--no-upload` is passed. It also writes [`crawlers/output/crawl-upload-receipt.json`](crawlers/output/crawl-upload-receipt.json) (CLI exit code and stderr). **Success** is reflected in `crawlers/output/upload_metadata.json` (`crawl_log.txid`); the receipt may be stale if a later retry succeeded.

Crawl-log v2 uses object entries with optional `reviewed: "reviewed"`. Crawlers skip only reviewed items; [`orchestrate.py`](../orchestrate.py) triggers additional crawl-log uploads every 5 successful review bundle uploads.

---

## Legacy per-pipeline uploaders

These Python scripts call `articles/article_uploader/upload_cli.js` and require **`Arweave-Cli/.env`**. They remain wired into orchestrators until fully migrated to `python -m uploader`.

### Compounds — unified uploader

Invoked automatically by [`compounds/orchestrate.py`](compounds/orchestrate.py) unless `--skip-upload`.

```bash
python -m uploader --recipe compounds --dir reviews/compounds/<TICKER>/review
```

The `review/` directory must contain `review.json` and `overview.json`. Multi-compound runs should also include `evidence_audit.md` (from `evidence-doc.py --combination-bundle`). Ticker is resolved from `crawlers/output/pump.science/compound-tokens.json`.

Multi-compound tokens use the same layout: combo review + evidence in `reviews/compounds/<TICKER>/review/`; all intermediates (including per-compound subdirs) under `steps/`.

### Articles — [`articles/article_uploader/`](articles/article_uploader/)

Used from [`articles/pipeline/run_full_pipeline.py`](articles/pipeline/run_full_pipeline.py) unless `--skip-upload`:

```bash
python articles/article_uploader/uploader.py --output-dir "path/to/article/output" [--resume]
```

Three-step: evidence → review (with evidence link) → overview (with DeScAi review link). Docs: [`articles/article_uploader/README.md`](articles/article_uploader/README.md).

### Proposals — [`proposals/uploader.py`](proposals/uploader.py)

Used from [`proposals/pipeline/proposal_pipe.py`](proposals/pipeline/proposal_pipe.py) unless `--skip-upload`:

```bash
python proposals/uploader.py --output-dir proposals/data/<id> [--resume]
```

Two-step evidence + review, ResearchHub / Proposal tags.

---

## Evidence audit field injection

Review recipes follow the same pattern:

1. Upload the evidence markdown and record `txid`.
2. Build a **temporary** JSON copy with `"evidence_audit": "https://arweave.net/<txid>"` inserted immediately after `review_date`.
3. Upload the modified review JSON, then repeat steps 2–3 for `overview.json`.
4. Persist `upload_metadata.json` after each step so `--resume` can continue after failures.

Original files in the pipeline output directory are **not** modified. No text is appended to `review_statement`.

---

## Metadata output

`upload_metadata.json` records ISO `upload_date`, file paths, and per-step objects:

```json
{
  "upload_date": "2026-05-30T22:51:08.022648+00:00",
  "evidence_audit": {
    "txid": "...",
    "url": "https://arweave.net/...",
    "tags": [{ "name": "doctype", "value": "evidence" }]
  },
  "review": {
    "txid": "...",
    "url": "https://arweave.net/...",
    "descai_url": "https://descai.net/review/...",
    "tags": []
  }
}
```

Shape varies by recipe (`crawl_log`, `overview`, `synthesis_dir`, etc.). Schema reference: [`articles/article_uploader/upload_metadata.schema.json`](articles/article_uploader/upload_metadata.schema.json) (articles include `overview`; compounds metadata uses `review_file` / `evidence_file`).

---

## Standalone Arweave CLI

[`Arweave-Cli/`](Arweave-Cli/) is a minimal Node upload/retrieve module and CLI for ad hoc files:

```bash
cd Arweave-Cli
node arweaveServiceCLI.js path/to/file.json
python upload_orchestrator.py -f path/to/file.json   # writes upload_reciept.json
```

See [`Arweave-Cli/README.md`](Arweave-Cli/README.md).

---

## Skip upload flags

| Pipeline | Flag |
|----------|------|
| `compounds/orchestrate.py` | `--skip-upload` |
| `articles/pipeline/run_full_pipeline.py` | `--skip-upload` |
| `proposals/pipeline/proposal_pipe.py` | `--skip-upload` |
| `crawlers/full-crawl.mjs` | `--no-upload` |
| Root [`orchestrate.py`](orchestrate.py) | `--skip-upload` (propagates to sub-pipelines) |

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `Missing PATH_TO_KEYFILE in repo-root .env` | Unified uploader: set wallet vars in repo-root `.env` |
| `Missing WALLET_PATH in .env` (article uploader) | Create `Arweave-Cli/.env` with `WALLET_PATH` |
| `Keyfile not found: /home/.../projects/arweave-keyfile-...` | `PATH_TO_KEYFILE` resolved wrong — use `./arweave-keyfile-....json` relative to **repo root** |
| `Invalid JSON response` from Node CLI | Run `npm install` in `uploader/` or `articles/article_uploader/`; check Node stderr |
| Crawl receipt `success: false` but metadata has `txid` | First attempt failed; a later run succeeded — trust `upload_metadata.json` |
| Compound upload skipped | Missing `review/review.json`, `review/overview.json`, or `review/evidence_audit.md` under `reviews/compounds/<TICKER>/` |
| Turbo / insufficient balance errors | Fund wallet or add Turbo credits |

---

## Related paths

| Path | Role |
|------|------|
| [`uploader/`](uploader/) | Unified recipes, Node CLI, core sequential logic |
| [`articles/article_uploader/`](articles/article_uploader/) | Shared Turbo service + CLI for legacy Python uploaders |
| [`Arweave-Cli/`](Arweave-Cli/) | Wallet `.env` for article_uploader stack; standalone CLI |
| [`compounds/orchestrate.py`](compounds/orchestrate.py) | Invokes `python -m uploader --recipe compounds` unless `--skip-upload` |
