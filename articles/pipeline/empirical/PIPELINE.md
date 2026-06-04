# Empirical Evidence Review Pipeline

This folder contains the second half of the claim extraction pipeline — the
stages that take classified and grouped claims and produce a final
evidence-graded review. These scripts run after the upstream extraction steps
(spaCy tagging, LLM extraction, validation, classification, grouping) and are
designed specifically for empirical research papers where claims can be checked
against cited references.

## Pipeline overview

```
grouped.json
    |
    v
[1] triage.py              Deterministic bucket assignment
    |
    v
triaged.json
    |
    v
[2] retrieve_compare.py    Citation resolution + evidence grading (OpenAlex + LLM)
    |
    v
retrieve_compare_llm.json
    |
    v
[3] prep.py                Narrative generation + evidence-weighted scoring
    |
    v
prepped_evidence.json
    |
    v
[4] review.py              LLM rationale generation + condensation
    |
    v
review.json  <─────────────────────────────────────────────────────┐
                                                                   │ (patches originality
[5] originality_check.py   OpenAlex literature search +           │  category in-place)
                           per-work similarity scoring +           │
                           LLM originality statement ─────────────►┘
    |
    v
originality.json
    |
    v
review.json  <─────────────────────────────────────────────────────┐
                                                                   │ (patches/inserts categories
[6] screener.py            Sliding-window document scan +          │  for dimensions with findings)
                           LLM screening + rationale writing ─────►┘
    |
    v
screener.json
    |
    v
[7] score.py               Unified scoring: recomputes all scores,
                           weighted composite, review statement
    |
    v
review.json (final)        (optional: overview.json from score.py)
    |
    v
[8] evidence-doc.py        Audit Markdown: provenance, traceability (no LLM)
    |
    v
evidence_audit.md
```

## Scripts

### triage.py

Deterministic (no LLM) step that sorts each claim into one of six buckets
based on its `claim_type`, classification tags, `semantic_category`, and
`relevancy_score`. First match wins.

| Bucket | What goes here |
|---|---|
| `empirical` | Data-verifiable claims — Facts with evidential tags, Assertions with causal/comparative tags |
| `methodological` | How the work was done — Methodological, Measurement+Methodological, Benchmark |
| `boilerplate_method` | Standard procedure steps (temperatures, reagents) with low relevancy |
| `aspirational` | Gaps, hypotheses, future work — Roadmap claims, novelty/impact tags |
| `contextual` | Background, definitions, framing — Definitional, Background, Synthesis tags |
| `noise` | Low relevancy, figure captions, missing tags, expectation-only claims |

**Input:** `grouped.json` (from `group.py`)
**Output:** `triaged.json` — same dimension structure but claims are nested
under `buckets.{bucket_name}` instead of flat `members`.

**CLI:**
```
python triage.py grouped.json -o triaged.json --mappings ../mappings.json
```

---

### retrieve_compare.py

The core evidence grading step. For each non-noise claim it:

1. Scans the source chunk and neighbouring chunks for inline citation numbers
   (e.g., superscript `29` or parenthetical `[5, 6]`).
2. Looks up each citation in the paper's reference list (parsed from `full.md`)
   to find DOIs.
3. Fetches bibliographic metadata and abstracts from the OpenAlex API (cached
   locally to avoid repeat calls).
4. Sends each claim + its cited abstracts to the LLM, which judges whether the
   references actually support the specific claim.

Each claim is assigned an `evidence_grade`:

| Grade | Meaning |
|---|---|
| `strong` | Cited references directly support the claim |
| `moderate` | Cited references partially support the claim |
| `weak` | Cited references offer only tangential support |
| `self_reported` | Paper's own finding (abstract/results section) — no external cite expected |
| `self_reported_method` | Paper's own methodology description — no external cite expected |
| `unsupported` | Cited references do not actually support the specific claim |
| `unreferenced` | Claim has no inline citations where one would be expected |
| `unverifiable` | Cited references lack accessible abstracts |

The `self_reported` logic uses `_effective_semantic_category()` which falls back
to `section_heading` keywords when the KB's `semantic_category` field is
unreliable (set to `"other"`).  Sections detected: `abstract`, `method`,
`result`, and `discussion` (including conclusion/interpretation headings).

Self-reported fact detection covers claim types `Fact`, `Interpretation`,
`Result`, and `Assertion` with empirical tags (`Observational`, `Measurement`,
`Causal`, `Comparative`, `Correlational`, `Methodological`, `Benchmark`,
`Performance`) in abstract, result, or discussion sections.  When the KB
provides no tags, the claim type alone is sufficient for section-based
detection.  Self-reported method detection covers all claims in method sections
plus `Methodological`-tagged claims in result or discussion sections.

**Post-LLM reclassification:** claims that the LLM grades as `unsupported` or
`unreferenced` are reclassified to `self_reported` / `self_reported_method` if
they match the section-based self-reported criteria.  This prevents the paper's
own experimental results from being penalised when incidental citations do not
support the specific finding (which is expected for primary data).

**Input:** `triaged.json`, `text_knowledge_base.jsonl`, `full.md`
**Output:** `retrieve_compare_llm.json` (or `_out.json` with `--skip-llm`)

**CLI:**
```
python retrieve_compare.py triaged.json \
    --kb text_knowledge_base.jsonl \
    --fullmd full.md \
    --openalex-cache openalex_cache.json \
    -o retrieve_compare_llm.json
```

Add `--skip-llm` to skip the LLM evidence auditor and only do citation
resolution + deterministic grading.

---

### prep.py

Builds an LLM-readable narrative sentence for each claim and computes
per-dimension grade distributions for downstream rationale generation.
Scoring is handled entirely by `score.py`.

- Reads the evidence-graded JSON and adds a `claim_narrative` field to every
  claim using the template in `prompts/evidence_narrative_template.md`.
- Computes `evidence_grade_distribution` per dimension (counts of each grade)
  so `review.py` can feed accurate statistics to the rationale condenser.
- Strips out `noise` bucket claims.

**Input:** `retrieve_compare_llm.json`
**Output:** `prepped_evidence.json`

**CLI:**
```
python prep.py retrieve_compare_llm.json -o prepped_evidence.json
```

---

### review.py

Three-stage LLM pipeline that produces per-dimension rationales. Does not
compute scores — `score.py` is the single authority for all scoring (both
per-dimension and composite). The review statement is also generated by
`score.py` after all scores are finalized.

| Stage | What it does |
|---|---|
| 1. `narrative_finder` | Chunks claim narratives into ~1000-token segments per dimension |
| 2. `rationale_gen` | LLM writes a partial evidence-quality rationale per chunk |
| 3. `rationale_condenser` | LLM merges partial rationales into one per dimension |

The rationale generation prompt (in `prompts/rationale_generation_prompt.md`)
is tuned for evidence grading — it instructs the LLM to distinguish between
claims with strong external support, claims where citations don't actually back
the assertion, and self-reported findings.

**Input:** `prepped_evidence.json`, `mappings.json`
**Output:** `review.json`

**CLI:**
```
python review.py prepped_evidence.json \
    --mappings ../mappings.json \
    -o review.json \
    --pre-condensed-dump pre_condensed_rationales.json
```

---

### originality_check.py

Assesses the originality of the paper against the broader published literature.
Operates independently of the claim-level pipeline — it reads the raw
`text_knowledge_base.jsonl` chunks directly rather than triaged or graded claims.

**Stages:**

| Stage | What it does |
|---|---|
| 1. `abstract_extractor` | Collects chunks whose `section_heading` contains "abstract" to reconstruct the paper's own abstract. Falls back to parsing `full.md` if none found. |
| 2. `term_generator` | Batches all KB chunks (`--chunk-batch-size` at a time) and calls the LLM once per batch to generate `--terms-per-chunk` targeted academic search queries per chunk. |
| 3. `openalex_searcher` | Hits `GET /works?search={term}` on the OpenAlex API for each term, rebuilds abstracts from the inverted index, deduplicates results by OpenAlex work ID, and caches everything locally. |
| 4a. `similarity_scorer` | One LLM call scores every retrieved work 0.00–1.00 on conceptual and methodological similarity to the paper abstract. Computes `avg_similarity` and `originality_score = 1.00 − avg_similarity`. |
| 4b. `originality_writer` | LLM writes a 3–5 paragraph originality statement (novel contributions, prior-art overlap, contextual positioning, score) given the paper abstract, all retrieved works, and the computed originality score. |
| patch | Overwrites `categories.originality` in `review.json` with the new score and statement. Final scoring is handled by `score.py`. |

**Scoring:**

```
similarity_score  (per work)  = 0.00–1.00  (LLM-assigned)
avg_similarity                = mean of all per-work scores
originality_score             = 1.00 − avg_similarity
```

**Input:** `text_knowledge_base.jsonl`, optionally `full.md`
**Outputs:** `originality.json`, patched `review.json`

**CLI:**
```
python originality_check.py \
    --directory pipe-test2/ \
    [--fullmd ../full.md]                      # auto-discovered from parent dirs \
    [--kb pipe-test2/text_knowledge_base.jsonl] \
    [--openalex-cache pipe-test2/originality_openalex_cache.json] \
    [-o pipe-test2/originality.json] \
    [--review pipe-test2/review.json] \
    [--terms-per-chunk 1]                      # search terms generated per KB chunk \
    [--max-results-per-term 5]                 # OpenAlex results fetched per term \
    [--chunk-batch-size 4]                     # chunks processed per LLM call \
    [--skip-llm]                               # OpenAlex search only, no scoring or statement
```

`originality.json` schema:

```json
{
  "doc_name": "...",
  "check_date": "...",
  "paper_abstract": "...",
  "search_terms": ["..."],
  "related_works_count": 105,
  "avg_similarity_score": 0.1234,
  "originality_score": 0.8766,
  "related_works": [
    {
      "openalex_id": "https://openalex.org/W...",
      "doi": "...",
      "title": "...",
      "year": 2023,
      "cited_by_count": 42,
      "abstract": "...",
      "search_term": "zebrafish ALS BMAA transcriptomics",
      "similarity_score": 0.34
    }
  ],
  "originality_statement": "..."
}
```

---

### screener.py

Sliding-window document screener that reads `full.md` through overlapping
windows and screens each window against all mapping dimensions and
cross-cutting tags. Catches signals the claim-level pipeline misses:
conflicts of interest, methodological red flags, hedging patterns, missing
disclosures, and implicit strengths/weaknesses.

Only produces output for dimensions where it actually finds relevant
observations — does not force-fill empty categories.

**Stages:**

| Stage | What it does |
|---|---|
| 1. `window_builder` | Splits `full.md` into ~2500-token windows with ~500-token overlap. Extracts citation numbers per window and attaches OpenAlex abstracts from the existing cache. |
| 2. `window_screener` | One LLM call per window screens against the full dimension checklist from `mappings.json`. Returns structured findings with dimension, tags, severity, quote, and observation. |
| 3. `dedup_aggregate` | Deduplicates findings across overlapping windows (word-overlap threshold), groups by dimension. |
| 4. `category_writer` | For each dimension with findings, one LLM call writes a rationale (no score — scoring is handled by `score.py`). Aware of existing review coverage. |
| 5. `patch_review` | Inserts new categories or appends screener observations to existing rationales in `review.json`. Final scoring is handled by `score.py`. |

**Input:** `full.md`, `openalex_cache.json`, `mappings.json`, `review.json`
**Outputs:** `screener.json` (diagnostic), patched `review.json`

**CLI:**
```
python screener.py \
    --fullmd full.md \
    --openalex-cache openalex_cache.json \
    --mappings ../mappings.json \
    --review review.json \
    -o screener.json \
    [--skip-llm]                               # only build windows, no LLM screening
```

`screener.json` schema:

```json
{
  "doc_name": "...",
  "check_date": "...",
  "windows_count": 20,
  "total_findings_raw": 35,
  "total_findings_deduped": 22,
  "dimensions_with_findings": ["team_credibility", "cross_cutting", "..."],
  "findings_by_dimension": {
    "team_credibility": [
      {
        "dimension": "team_credibility",
        "tags": ["Affiliation", "ConflictOfInterest"],
        "severity": "concern",
        "quote": "Canurta Therapeutics, Mississauga...",
        "observation": "All authors except one are employed by...",
        "section": "author affiliations",
        "window_idx": 0
      }
    ]
  },
  "writer_results": {
    "team_credibility": {
      "score": null,
      "rationale": "..."
    }
  }
}
```

---

### score.py

Unified scoring step that runs after `review.py`, `originality_check.py`, and
`screener.py`. Reads
`review.json` (with rationales from `review.py`, `originality_check.py`, and
`screener.py`), recomputes all scores using consistent methodologies, computes
a weighted composite score, and regenerates the review statement.

**Scoring rules by source type:**

| Source | Method | Formula |
|---|---|---|
| Claim-level dims (`scientific_rigor`, `evidential_strength`) | `evidence_grade_weighted` | Relevancy-weighted mean of evidence grade weights from `prepped_evidence.json` (uses `compute_evidence_score` defined in `prep.py`) |
| `originality` | `literature_similarity` | Pass-through of `originality_score` from `originality.json` |
| Screener-only dims (`team_credibility`, `financial_integrity`, etc.) | `rubric_penalty` | `max(floor, baseline + sum(penalties[severity]))` using rubrics from `mappings.json` |

**Composite score:** Weighted average of all present dimension scores using
`dimension_weights` from `mappings.json`. Weights are renormalized to sum to
1.0 for the dimensions actually present in the review.

**Stages:**

| Stage | What it does |
|---|---|
| 1. score evidence dims | Recompute evidence-grade scores from `prepped_evidence.json` |
| 2. score originality | Read score from `originality.json` |
| 3. score rubric dims | Apply penalty rubrics to screener findings from `screener.json` |
| 4. merge scores | Combine all scores with rationales from `review.json` |
| 5. composite score | Compute weighted composite from `dimension_weights` |
| 6. review statement | Regenerate top-level summary via LLM |

**Input:** `review.json`, `prepped_evidence.json`, `originality.json`,
`screener.json`, `mappings.json`
**Output:** `review.json` (final — overwrites in place)

**CLI:**
```
python score.py \
    --review review.json \
    --prepped-evidence prepped_evidence.json \
    --originality originality.json \
    --screener screener.json \
    --mappings ../mappings.json \
    -o review.json
```

Add `--skip-llm` to skip review statement regeneration and keep the existing
(or empty) statement.

`review.json` final schema:

```json
{
  "research_name": "...",
  "review_date": "...",
  "composite_score": 48,
  "review_statement": "...",
  "categories": {
    "scientific_rigor": {
      "score": 35,
      "rationale": "..."
    },
    "originality": {
      "score": 90,
      "rationale": "..."
    }
  }
}
```

---

### evidence-doc.py

Deterministic **audit trail** generator (no LLM). Runs **after** `score.py`. Reads
final `review.json`, retrieve_compare output (`retrieve_compare_llm.json` or
`retrieve_compare_out.json`), `screener.json`, and optionally `originality.json`;
writes **`evidence_audit.md`** — compact Markdown for public-facing traceability
alongside published `review.json` / `overview.json`.

The document includes:

- **Provenance:** UTC generation time, generator version, optional git revision,
  `VALIDATOR_MODEL` env (upstream LLM steps), SHA-256 fingerprints of inputs,
  which retrieve_compare filename was used.
- **Composite score note:** pointer to `score.py` and live `dimension_weights`
  from `mappings.json`.
- **Category scores** table (from `review.json`).
- **Evidence grade counts** and **claim-level detail** for non–self-reported
  grades (same exclusion rule as `prep.SCORE_EXCLUDED_GRADES`).
- **Screener findings** (quotes + observations).
- **Originality** stats + top related works by similarity (no abstracts).

Reference lines explain `(no verdict — missing abstract)` vs
`(no verdict — ungraded)` instead of opaque placeholders.

Default UTF-8 size budget **120 KiB** (`--max-bytes`); trimming drops rationale
verbosity, screener `info`, strong/moderate claim rows, or switches to compact
provenance before exceeding the limit. Use `--skip-provenance` for minimal
output.

**Input:** `review.json`; retrieve_compare JSON; `screener.json`;
`originality.json` (optional)
**Output:** `evidence_audit.md` (default: alongside `review.json` in the run dir)

**CLI:**
```
python evidence-doc.py --directory /path/to/run -o evidence_audit.md
```

Orchestrators call this automatically as **step 13** after scoring (`run_pipe2.py`,
`empirical-pipe.py`).

---

## Prompts

All prompts live in `prompts/` and are loaded at runtime:

| File | Used by | Purpose |
|---|---|---|
| `evidence_narrative_template.md` | `prep.py` | Sentence template with `{evidence_grade}` and `{evidence_summary}` placeholders |
| `rationale_generation_prompt.md` | `review.py` Stage 2 | Instructs LLM to analyse evidence quality per chunk |
| `rationale_condenser_prompt.md` | `review.py` Stage 3 | Instructs LLM to merge partial rationales preserving grade distribution |
| `review_statement_prompt.md` | `score.py` Stage 6 | Instructs LLM to write a top-level evidence-aware summary |
| `search_term_prompt.md` | `originality_check.py` Stage 2 | Instructs LLM to generate targeted academic search queries from KB chunks |
| `similarity_scorer_prompt.md` | `originality_check.py` Stage 4a | Instructs LLM to score each related work 0.00–1.00 against the paper abstract |
| `originality_statement_prompt.md` | `originality_check.py` Stage 4b | Instructs LLM to write a structured originality statement incorporating the computed score |
| `screener_system_prompt.md` | `screener.py` Stage 2 | Instructs LLM to screen a document window for dimension-relevant signals, COI, red flags |
| `screener_category_writer_prompt.md` | `screener.py` Stage 4 | Instructs LLM to write a rationale from aggregated screener findings (no score) |

---

## Running the full pipeline

Use `run_pipe2.py` at the repo root to run everything end-to-end:

```
python run_pipe2.py                  # steps 1-13, LLM enabled
python run_pipe2.py --from-step 6   # triage → evidence grading → review → originality → screener → scoring → evidence audit
python run_pipe2.py --from-step 8   # prep + review + originality + screener + scoring + evidence audit
python run_pipe2.py --from-step 10  # originality check + screener + scoring + evidence audit
python run_pipe2.py --from-step 12  # unified scoring + evidence audit (needs review.json + prepped_evidence + originality + screener)
python run_pipe2.py --from-step 13  # evidence audit only (needs review.json + retrieve_compare output + screener.json; originality optional)
python run_pipe2.py --skip-llm      # skip LLM in retrieve_compare; originality + screener + scoring use --skip-llm too
```

Steps 6–13 correspond to this folder's scripts:

| Step | Script |
|---|---|
| 6 | `triage.py` |
| 7 | `retrieve_compare.py` |
| 8 | `prep.py` |
| 9 | `review.py` |
| 10 | `originality_check.py` |
| 11 | `screener.py` |
| 12 | `score.py` |
| 13 | `evidence-doc.py` |

The same empirical steps (6–13) are run by `empirical-pipe.py` when driving the
full pipeline from an input directory. **`empirical-pipe.py`** stores all
intermediates under **`<research-folder>/steps/`** and publishes **`review.json`**,
**`overview.json`**, and **`evidence_audit.md`** under **`<research-folder>/review/`** (legacy standalone runs may use `output/`).
The **`run_pipe2.py`** helper uses a **flat** layout in its output directory instead.

---

## Output files

A typical run produces these files. Layout depends on the driver script:

- **`empirical-pipe.py`:** intermediates below live under **`steps/`**; **`review.json`**,
  **`overview.json`**, and **`evidence_audit.md`** under **`output/`**.
- **`run_pipe2.py`:** the same filenames usually appear in one folder (see that script).

| File | Description |
|---|---|
| `triaged.json` | Claims sorted into triage buckets per dimension |
| `openalex_cache.json` | Cached OpenAlex DOI lookup responses (used by `retrieve_compare.py`) |
| `retrieve_compare_llm.json` | Claims enriched with evidence grades and citation analysis |
| `prepped_evidence.json` | Claims with narrative sentences and grade distributions (scoring done by `score.py`) |
| `pre_condensed_rationales.json` | Stage 2 rationales before condensation (debug artifact) |
| `review.json` | Final structured review: integer `composite_score` and per-category scores (0–100, ceil), `rationale` only, `review_statement` |
| `overview.json` | Plain-language companion to `review.json` (written by `score.py` when LLM overview is enabled) |
| `originality_openalex_cache.json` | Cached OpenAlex search results (used by `originality_check.py`) |
| `originality.json` | Full originality report: search terms, related works with similarity scores, originality score, and statement |
| `screener.json` | Document screener diagnostic output: windows, findings by dimension, writer rationales |
| `evidence_audit.md` | Human-readable audit trail from `evidence-doc.py`: provenance, scores, claim/citation trace, screener quotes, originality table (no LLM) |
