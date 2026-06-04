# pump-science — Review Pipeline: Technical Reference

Technical guide for the current compounds review pipeline. Outputs are research screening aids only — not medical advice.

---

## 0. Current pipeline

### Single compound

**Entry:** `pipeline/single/run_review.py --compound <name>`

```
discover.py (incremental)  →  steps/material.json
tag-group-filter.py        →  steps/longevity.json, steps/risk.json, material_tagged.jsonl
topic_grouper.py           →  steps/longevity_groups.json, steps/risk_groups.json
review.py                  →  review/review.json
overview.py                →  review/overview.json
evidence-doc.py            →  review/evidence_audit.md
```

Ticker and paths come from [`token_lookup.py`](token_lookup.py) → `reviews/compounds/<TICKER>/`.

### Multi-compound

**Entry:** `orchestrate.py --compounds A B C` (names must match `compound-tokens.json`)

```
run_review.py (per compound)  →  steps/<Compound>/review/{review.json,overview.json,evidence_audit.md} + filtered artifacts
interactions.py               →  steps/<slug>-bundle.json  (schema v3)
review-multiple.py            →  review/review.json (+ compatibility)
overview.py                   →  review/overview.json
evidence-doc.py (orchestrator) →  review/evidence_audit.md  (combination; no LLM)
```

Per-compound inputs for the bundle (not raw `material.json`):

| File | Role |
|------|------|
| `longevity.json`, `risk.json` | Filtered tagged rows |
| `longevity_topic_summaries.json`, `risk_topic_summaries.json` | Review stage-1 bullets |
| `review/review.json` | Per-compound scores and rationales (under `steps/<Compound>/review/` or sibling `review/` for single ticker) |

Upload: `python -m uploader --recipe compounds --dir reviews/compounds/<TICKER>/review` (see repo-root `uploader.md`).

**Smoke test (no LLM):** `python3 pipeline/multi/test_interactions_sources.py`

---

## Tagging (`tag-group-filter.py`)

Each material row gets `longevity_relevance` and `risk_relevance` via LLM + rule overrides.

| Export | Keeps |
|--------|--------|
| `longevity.json` | `direct_longevity` / `indirect_longevity_mechanism` + longevity review filter |
| `risk.json` | Risk signal + human-safety review filter (excludes assay cytotoxicity noise) |

Prompts: `compound-excerpt-tagging.md`, `compound-risk-profile.md`.

Incremental mode (`--incremental`): only new material keys are LLM-tagged after each discover round.

---

## Grouping (`topic_grouper.py`)

Per row: source-specific excerpt, dedupe by DOI/PMID/title, keyword bucket, greedy TF-IDF split (max 12 units/group). Processes longevity and risk in one invocation.

---

## Review (`review.py`)

| Stage | Calls | Prompt |
|-------|-------|--------|
| 1 | 1 per topic group | `pump-science-topic-group-summary.md` |
| 2a | 1 | `pump-science-grounding-from-groups.md` |
| 2b | 1 | `pump-science-risk-from-groups.md` |
| 2c | 1 | `pump-science-review-statement-from-groups.md` |

Stage 2 uses **bullets only**. Scores are deterministic from flat unit tag counts (defaults: grounding 50%, risk 25% when empty).

**Output schema:**

```json
{
  "research_name": "Ginsenoside Rh2",
  "review_date": "June 1, 2026",
  "composite_score": 62.5,
  "review_statement": "Compound(s): Ginsenoside Rh2. ...",
  "categories": {
    "scientific_grounding": { "score": 72.0, "rationale": "..." },
    "risk_assessment": { "score": 45.0, "rationale": "..." }
  }
}
```

`strip_reasoning_markup` is applied to all LLM outputs. Use `--debug-payloads` to inspect payloads without calling the LLM.

---

## Overview (`overview.py`)

One LLM call per text field (`review_statement` + each category `rationale`). Prompt: `compound-review-overview.md`. Same JSON shape and scores as `review.json`.

---

## Discover (`discover.py`)

Default in `run_review.py`: `--incremental` with per-source caps (`discover_lib/limits.py`), dedupe at collection, delta-tag via `tag-group-filter --incremental`.

OpenAlex uses fixed search terms in `discover_lib/openalex.py` (no LLM term generation).

Env: `DISCOVER_LIMITS_JSON`, `DISCOVER_MAX_ROUNDS`, `DISCOVER_HTTP_PER_ROUND`, `DISCOVER_TIMEOUT`.

Legacy `report_*.json` under `steps/` is converted to `material.json` when using `--skip-discover`.

---

## Combo review (`review-multiple.py`)

Four LLM passes on the interactions bundle:

- `pump-science-combination-scientific-grounding-evaluation.md`
- `pump-science-combination-risk-statement-evaluation.md`
- `pump-science-compatibility-evaluation.md`
- `pump-science-combination-review-statement-evaluation.md`

Adds `categories.compatibility` to the combo `review.json`. Top-level id field is ``compound_token`` (e.g. ``OMIGU (Omipalisib + Ginsenoside Rh2 + Urolithin A)``), not ``research_name``.

---

## Evidence audit (`evidence-doc.py`)

No LLM. Two modes:

- **Single:** `--data-dir` + `--compound` (run automatically at end of `run_review.py`). Traces pipeline artifacts and `review.json` rationales. Output beside review as `evidence_audit.md`.
- **Combination:** `--combination-bundle` (orchestrator after combo review). Traces bundle fields, per-compound pipeline artifacts, and combo review rationales.

Per-compound audits under `steps/<Compound>/review/` are ignored when building the combination audit.

---

## Environment variables

| Variable | Used by | Default / notes |
|----------|---------|-----------------|
| `LLM_BASE_URL`, `LLM_API_KEY` | tag, review, overview, review-multiple | via `llm_env.py` |
| `TAGGER_BASE_URL`, `TAGGER_API_KEY` | tag-group-filter | fall back to LLM_* |
| `TAGGER_MODEL`, `TAGGER_RISK_MODEL`, `TAGGER_MAX_TOKENS` | tag-group-filter | |
| `REVIEWER_MODEL`, `REVIEWER_MAX_TOKENS` | review, review-multiple, overview | |
| `OVERVIEW_MAX_TOKENS` | overview.py | default 1024 |
| `COMPOUND_TOKENS_FILE` | token_lookup, orchestrate | `crawlers/output/pump.science/compound-tokens.json` |

Load `.env` from repo root through `compounds/llm_env.py`.

---

## Known gotchas

- **vLLM temperature:** Launch with `--generation-config vllm` if bundled `generation_config.json` overrides client `temperature=0`.
- **Ticker mismatch:** Compound names must match `intervention` in `compound-tokens.json` exactly (normalized).
- **Combo evidence audit:** Combination audit is orchestrator-only; single-compound runs also produce `evidence_audit.md` via `run_review.py`.
- **Reasoning models:** If tags or JSON summaries fail parsing, raise `TAGGER_MAX_TOKENS` / `REVIEWER_MAX_TOKENS` and check stripped output.

---

## `material.json` row shape

JSONL lines: `source_type` + `content` object. Tagged exports add `longevity_relevance`, `risk_relevance`, and optional `risk_severity` on each row.

See `discover_lib/material.py` for serialization and `report_*.json` conversion.
