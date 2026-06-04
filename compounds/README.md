# Compounds Pipeline (Pump Science)

Compound screening route for [Agent Core](../README.md). End-to-end pipeline for screening a compound's longevity research potential using public APIs and local LLM inference. All outputs are **research screening aids only — not medical advice, prescribing guidance, or regulatory submissions**.

[`orchestrate.py`](../orchestrate.py) invokes [`compounds/orchestrate.py`](orchestrate.py) for each token in `crawlers/output/pump.science/compound-tokens.json`. Single-compound runs use [`pipeline/single/run_review.py`](pipeline/single/run_review.py) directly.

---

## Setup

```bash
pip install -r requirements.txt
```

`discover.py` requires `requests`. `tag-group-filter.py`, `topic_grouper.py`, `review.py`, and `overview.py` require `openai` (and optionally `python-dotenv` via `llm_env.py`).

---

## Pipeline at a glance

Run the full chain with **`pipeline/single/run_review.py --compound <name>`** (single compound) or **`compounds/orchestrate.py --compounds ...`** (multi-compound bundles). The top-level agent uses root [`orchestrate.py`](../orchestrate.py).

```
discover.py --incremental  →  material.json (+ delta-tag each round → longevity/risk JSONL)
topic_grouper.py         →  longevity_groups.json, risk_groups.json
review.py                →  review/review.json
overview.py              →  review/overview.json (plain-language; same scores)
evidence-doc.py          →  review/evidence_audit.md (non-LLM audit)
```

**Discover:** batched fetch with per-source caps; dedupe at collection; between rounds only **new** rows are LLM-tagged (`tag-group-filter --incremental`). Legacy one-shot: `discover.py --one-shot`.

**Review LLM pattern:** one call per topic group (bulleted JSON summary), then three synthesis calls. Typical Rh2-scale runs: ~26 group calls + 3 synthesis calls after discover completes.

All outputs live under **`reviews/compounds/<TICKER>/`**:

```
reviews/compounds/DOCS/
  review/          review.json, overview.json, evidence_audit.md
  steps/           all intermediate artifacts
```

Multi-compound tokens (e.g. OMIGU) use the same layout:

```
reviews/compounds/OMIGU/
  steps/
    Omipalisib/              per-compound pipeline outputs
    Ginsenoside_Rh2/
    Urolithin_A/
    omipa-ginse-uroli-bundle.json   combination evidence (v3)
  review/
    review.json              combo review (4 LLM passes)
    overview.json            plain-language combo review (same scores)
    evidence_audit.md        non-LLM combination audit
  steps/<Compound>/review/   per-compound review.json, overview.json, evidence_audit.md
```

### Multi-compound (`orchestrate.py` with ≥2 compounds)

| Step | Script | Output |
|------|--------|--------|
| 1 (×N) | `run_review.py` | `steps/<Compound>/` artifacts + `steps/<Compound>/review/{review.json,overview.json,evidence_audit.md}` |
| 2 | `interactions.py` | `steps/<slug>-bundle.json` — reads **only** kept review artifacts (see below) |
| 3 | `review-multiple.py` | `review/review.json` — combo grounding, risk, compatibility, statement |
| 4 | `overview.py` | `review/overview.json` — simplified text (skip with `--skip-overview`) |
| 5 | `evidence-doc.py` | `review/evidence_audit.md` — combination audit (orchestrator only) |

**Bundle inputs (per compound)** — same files `review.py` uses, not raw `material.json`:

- `longevity.json`, `risk.json` (tag-group-filter exports)
- `longevity_topic_summaries.json`, `risk_topic_summaries.json`
- `review/review.json` (scores + rationales; or legacy `review.json` beside steps)

**Full combo run:**

```bash
cd compounds
python orchestrate.py --compounds Omipalisib "Ginsenoside Rh2" "Urolithin A" --skip-upload
```

**After updating one compound** (e.g. re-ran `run_review.py` for Rh2 only) — refresh bundle + combo review + audit without re-discovering all three:

```bash
python orchestrate.py \
  --compounds Omipalisib "Ginsenoside Rh2" "Urolithin A" \
  --skip-discover --skip-individual --skip-upload
```

**Bundle only** (no LLM):

```bash
python pipeline/multi/interactions.py \
  --compounds Omipalisib "Ginsenoside Rh2" "Urolithin A" \
  --data-root ../reviews/compounds/OMIGU/steps \
  -o ../reviews/compounds/OMIGU/steps/omipa-ginse-uroli-bundle.json
```

**Evidence audit only** (requires bundle + `review/review.json`):

```bash
python pipeline/evidence-doc.py \
  --combination-bundle ../reviews/compounds/OMIGU/steps/omipa-ginse-uroli-bundle.json \
  --combo-review ../reviews/compounds/OMIGU/review/review.json \
  -o ../reviews/compounds/OMIGU/review/evidence_audit.md
```

Compound names must match a row in `crawlers/output/pump.science/compound-tokens.json` (ticker resolved automatically).

Each step is offline from the previous one — you can re-run any stage without re-fetching upstream data.

**BioAssay-NLG (optional, for PubChem bioassay prose):** clone [BioAssay-NLG](https://github.com/DeScAI-Team/BioAssay-NLG) into `compounds/BioAssay-NLG/`. SIDs are resolved via PubChem `compound/cid/{cid}/sids/JSON` (no local database).

---

## Scripts

### `discover.py` — fetch compound data from public APIs

**Why it exists:** Consolidates regulatory, trial, pathway, literature, bioactivity, and bioassay data into one timestamped JSON artifact. PubChem synonym resolution fans out queries across sources so obscure names (e.g. herbal metabolites) match more often.

**What it fetches:**
- **PubChem PUG REST** — CID + synonyms (`metadata.query_names`); bioassay summaries (top 8 AIDs, NLG prose via BioAssay-NLG + PubChem `cid/sids`)
- **OpenFDA** — FAERS + labels queried per alias (`generic_name`, `substance_name`, `brand_name`)
- **ClinicalTrials.gov v2** — merged studies across aliases
- **KEGG REST** — drug IDs / pathways across aliases
- **Europe PMC** — longevity, aging, lifespan, healthspan, senescence, and multi-synonym OR queries; deduped by pmid → pmcid → doi → id
- **ChEMBL** — molecule match, mechanisms (targets), bioactivities

All HTTP calls use a **10 s timeout** by default (`DISCOVER_TIMEOUT` env to override). Failures land in `metadata.failures`; partial data is always written.

**Label result filtering:** Labels kept when **any** query alias matches as substring in `openfda.generic_name`, `substance_name`, or `brand_name`.

**CLI** (from `compounds/`, via `run_review.py` or directly):

```bash
python pipeline/single/discover.py --compound metformin \
  --compound-dir ../reviews/compounds/DOCS/steps --incremental \
  --output ../reviews/compounds/DOCS/steps/material.json
```

Legacy monolithic `report_*.json` under `steps/` is converted to `material.json` when using `--skip-discover`.

---

### `run_review.py` — single-compound pipeline driver

Runs discover (or tag-only) → topic grouper → review → overview. Resolves ticker via `token_lookup.py` and writes under `reviews/compounds/<TICKER>/`.

```bash
python pipeline/single/run_review.py --compound "Ginsenoside Rh2"
python pipeline/single/run_review.py --compound "Ginsenoside Rh2" --skip-discover --skip-overview
```

---

### `tag-group-filter.py` — tag and filter material rows

Tags each `material.json` row with `longevity_relevance` and `risk_relevance`, applies rule overrides, and writes filtered JSONL:

- `longevity.json` — longevity-tagged rows that pass review filter (excludes oncology-only mechanism hits)
- `risk.json` — human safety / interactions / literature tox; excludes in vitro assay cytotoxicity noise

```bash
python3 pipeline/single/tag-group-filter.py steps/material.json --out-dir steps
```

---

### `topic_grouper.py` — group filtered rows by topic

Dedupes, keyword-buckets, and TF-IDF-splits rows into capped topic groups (default max 12 units per group). **Always processes longevity and risk in one command:**

```bash
python3 pipeline/single/topic_grouper.py steps/
# → steps/longevity_groups.json, steps/risk_groups.json
```

Each group contains normalized `units` with `unit_id`, `citation`, `excerpt`, and relevance tags.

---

### `review.py` — two-stage group review

**Stage 1:** one LLM call per topic group → `longevity_topic_summaries.json` / `risk_topic_summaries.json`  
**Stage 2:** three synthesis calls from topic bullets only → scientific grounding, risk, review statement

Requires pre-built `*_groups.json` (from `topic_grouper.py`). Compound name comes from `material.json` or `--compound`.

```bash
python3 pipeline/single/review.py ../reviews/compounds/GING2/steps/longevity.json \
  --risk ../reviews/compounds/GING2/steps/risk.json \
  --longevity-groups ../reviews/compounds/GING2/steps/longevity_groups.json \
  --risk-groups ../reviews/compounds/GING2/steps/risk_groups.json \
  --compound "Ginsenoside Rh2" \
  --run-root ../reviews/compounds/GING2
```

Output: `review/review.json` and (via `run_review.py`) `review/overview.json`.

---

### `overview.py` — plain-language review copy

Rewrites `review_statement` and category rationales for a general audience; scores unchanged.

```bash
python pipeline/overview.py ../reviews/compounds/OMIGU/review/review.json
```

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `REVIEWER_MODEL` | falls back to `TAGGER_MODEL` → `CLASSIFIER_MODEL` → `VALIDATOR_MODEL` | Model for all three passes |
| `REVIEWER_MAX_TOKENS` | `2048` | Completion budget per pass |
| `LLM_BASE_URL`, `LLM_API_KEY` | same as [`llm_env.py`](llm_env.py) | API endpoint |

---

## End-to-end example

```bash
cd compounds

python orchestrate.py --compounds Doxycycline --skip-upload
# → reviews/compounds/DOCS/review/review.json
# → reviews/compounds/DOCS/steps/…
```

Replace compound names with those listed in `crawlers/output/pump.science/compound-tokens.json`. Each step can be re-run independently without repeating upstream steps.

---

## Output files

| File | Produced by | Contents |
|------|-------------|---------|
| `steps/material.json` | `discover.py` | JSONL material rows (`source_type` + `content`) |
| `steps/longevity.json`, `steps/risk.json` | `tag-group-filter.py` | Filtered tagged rows |
| `steps/material_tagged.jsonl` | `tag-group-filter.py` | Full tagged audit trail |
| `steps/longevity_groups.json`, `steps/risk_groups.json` | `topic_grouper.py` | Topic groups + normalized units |
| `steps/longevity_topic_summaries.json`, `steps/risk_topic_summaries.json` | `review.py` stage 1 | Per-group bullet summaries |
| `review/review.json` | `review.py` stage 2 | Final review |
| `review/overview.json` | `overview.py` | Plain-language review |
| `review/evidence_audit.md` | `evidence-doc.py` | Non-LLM audit (single via `run_review.py`; combo via orchestrator) |
| `steps/<slug>-bundle.json` | `interactions.py` | Combination evidence bundle (v3, review-pipeline artifacts only) |
| `steps/<Compound>/review/review.json` | `run_review.py` (multi) | Per-compound review embedded in bundle |

All paths are relative to `reviews/compounds/<TICKER>/`.

---

## Deep technical reference

For the full pipeline logic — prompt text, parsing code, allowlists, scoring formulas, environment variables, and guidance for adding features or fixing bugs — see **[REVIEW_LOGIC.md](REVIEW_LOGIC.md)**.

## Related documentation

- Agent Core overview: [README.md](../README.md)
- Review logic deep dive: [REVIEW_LOGIC.md](REVIEW_LOGIC.md)
