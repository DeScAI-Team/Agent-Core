# Molecule IPNFT crawler

Ingestion route for [Agent Core](../../../README.md). TypeScript CLI for Molecule catalog profiles, dataroom downloads, link aggregation, and web crawling.

## Agent Core integration

- Run as part of the Agent Core crawl step (`python orchestrate.py`), or invoke this crawler directly
- Output under `crawlers/output/molecule/ipnfts/<SYMBOL>/` is consumed by [`DAOs/molecule/pipeline/run_dao_review.py`](../../../DAOs/molecule/pipeline/run_dao_review.py)
- See [DAOs/molecule/README.md](../../../DAOs/molecule/README.md) for the review pipeline
- Crawler index: [crawlers/README.md](../../README.md)

## Setup

### Node (profiles, data-bundle, aggregate-links)

```bash
cd crawlers/molecule/crawler
npm install   # or pnpm install
```

Set `MOLECULE_API_KEY` in the repo-root `.env`.

### Python (crawl-links, crawl-nitter)

Install into the repo `Agent/` venv (from repo root):

```bash
source Agent/bin/activate
pip install -r requirements.txt
crawl4ai-setup   # installs Playwright browsers (one-time)
```

The CLI prefers `Agent/bin/python3` when present; otherwise falls back to system `python3`.

## Commands

| Command | Description |
|---------|-------------|
| `npm run cli -- crawl` | Full pipeline: profiles → dataroom → `links.json` → web → nitter |
| `npm run profiles` | Batch-fetch IPNFT profiles |
| `npm run data-bundle` | Download PUBLIC dataroom files |
| `npm run aggregate-links` | Extract URLs from JSON → `links.json` per folder |
| `npm run crawl-links` | Crawl `links.json` URLs with crawl4ai |
| `npm run crawl-nitter` | Fetch nitter timelines → `tweets.json` |

Full pipeline:

```bash
cd crawlers/molecule/crawler
npm run cli -- crawl --output-dir @crawlers/output/molecule/ipnfts --index --concurrency 4
```

Skip optional steps:

```bash
npm run cli -- crawl --output-dir @crawlers/output/molecule/ipnfts --no-crawl-links
npm run cli -- crawl --output-dir @crawlers/output/molecule/ipnfts --no-crawl-nitter
```

Standalone Python crawlers:

```bash
npm run crawl-links -- --ipnfts-dir @crawlers/output/molecule/ipnfts --concurrency 4
npm run crawl-nitter -- --ipnfts-dir @crawlers/output/molecule/ipnfts --folder BeeARD
```

## crawl-links

Reads each IPNFT folder's `links.json` (from `aggregate-links`) and crawls eligible HTTP(S) URLs.

**Skipped at crawl time:**

- `nitter.net` (use `crawl-nitter`)
- `ipfs://` links (not downloaded; may appear in `links.json` if gateways respond during aggregate-links)
- Block explorers (`basescan.org`, `etherscan.io`, …)
- Social links (`t.me`, Telegram, `x.com`, Discord, …)
- URLs with `http_accessible: "no"`

**Crawl modes:**

- **Standard URLs:** single-page crawl → section-deduped `{siteName}.md` under `output/`
- **Doc/docs URLs:** BestFirst deep crawl with prefetch, then **one merged** `{siteName}.md` with section dedupe across all pages
- **Molecule hubs** (`molecule.xyz`, `mint.molecule.to`): single SPA fetch (no site-wide deep crawl), extract outbound links with catalog blocklists, bounded off-site follows (`--max-outbound-follows`, default 12). Global mint footer links (bio.xyz, peptai.xyz, Molecule GitHub, brand assets folder, …) are skipped. On 404 / “Page Not Found”, tries `mint.molecule.to/ipnft/{id}` fallback. Writes `crawl-extracted-links.json` sidecar.
- **snapshot.box:** SPA single-page fetch with longer wait (`--spa-wait-sec`, default 5s)

**Output trimming (default):**

- No media downloads
- Sections deduped by normalized content hash (within a page and across merged doc pages)
- GitBook nav/footer boilerplate stripped
- Pages/sections below `--min-chars` (400) / `--hub-min-chars` (150 for Molecule hubs) / `--min-section-chars` (80) dropped

**Per-IPNFT layout:**

```
BeeARD/
  metadata/
    profile.json
    dataroom.json
    manifest.json          # data-bundle download index
    links.json
    crawl-manifest.json
    crawl-extracted-links.json
    nitter-manifest.json
  output/
    *.pdf, *.mp4, *.mov     # dataroom downloads
    beeard.ai.md             # web crawl
    docs.beeard.ai.md
    mint.molecule.to.md
    tweets.json
```

Legacy folders may have files in the wrong subfolder or at the IPNFT root; loaders fall back when needed.

**Examples:**

```bash
# Single folder smoke test
npm run crawl-links -- --ipnfts-dir @crawlers/output/molecule/ipnfts --folder BeeARD --concurrency 1

# Parallel batch
npm run crawl-links -- --ipnfts-dir @crawlers/output/molecule/ipnfts --max 10 --concurrency 4

# Dry run (show what would be crawled)
npm run crawl-links -- --ipnfts-dir @crawlers/output/molecule/ipnfts --dry-run

# Re-crawl completed URLs
npm run crawl-links -- --ipnfts-dir @crawlers/output/molecule/ipnfts --force
```

**Flags:** `--concurrency` (default 4), `--max`, `--folder`, `--force`, `--dry-run`, `--doc-max-depth` (default 2), `--doc-max-pages` (default 25), `--min-chars` (default 400), `--hub-min-chars` (default 150), `--max-outbound-follows` (default 12), `--spa-wait-sec` (default 5), `--min-section-chars` (default 80), `--crawl-skip-file`.

## crawl-nitter

Collects nitter profile URLs from JSON under `metadata/` (including `links.json` entries with `http_accessible: "no"`). Writes **`output/tweets.json`** and **`metadata/nitter-manifest.json`**.

- **RSS first:** `https://nitter.net/{handle}/rss`
- **HTML fallback** if RSS is empty or blocked
- **`--nitter-base`** (default `https://nitter.net`) and **`--nitter-fallback-bases`** for alternate instances (RSS is often disabled on public instances)

```bash
npm run crawl-nitter -- --ipnfts-dir @crawlers/output/molecule/ipnfts --folder BeeARD --max-tweets 20
```

## Tests

```bash
cd crawlers/molecule/crawler
python -m pytest test_crawl_nitter.py -q
```
