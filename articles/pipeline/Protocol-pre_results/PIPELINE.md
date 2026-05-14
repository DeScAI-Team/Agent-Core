# Protocol / Pre-Results Evidence Review Pipeline

This folder contains the second half of the claim extraction pipeline adapted
for **registered reports, pre-registrations, and study protocols** — documents
that describe planned methodology before results exist. These scripts run
after the upstream extraction steps (spaCy tagging, LLM extraction,
validation, classification, grouping) and are designed specifically for
pre-results documents where claims describe **planned** approaches rather
than observed outcomes.

## Key differences from the empirical pipeline

| Aspect | Empirical | Protocol |
|--------|-----------|----------|
| Document type | Published paper with results | Pre-registration, protocol, registered report |
| Self-reported grades | Claims in results/abstract exempt from citation checking | Not applicable — no own-findings exist |
| Triage buckets | `empirical`, `methodological`, `boilerplate_method`, `aspirational`, `contextual` | `design_specification`, `methodological`, `background_rationale`, `aspirational`, `contextual` |
| Primary evidence question | Do cited references support the claims made? | Do cited references justify the planned design choices? |
| Protocol-specific grades | — | `design_precedent`, `established_method` |
| Screener focus | Results contradictions, overclaiming, COI | Protocol completeness, SPIRIT items, missing pre-specifications |

## Pipeline overview

```
grouped.json
    |
    v
[1] triage.py              Deterministic bucket assignment (protocol-specific)
    |
    v
triaged.json
    |
    v
[2] retrieve_compare.py    Citation resolution + evidence grading (no self-reported)
    |
    v
retrieve_compare_llm.json
    |
    v
[3] prep.py                Narrative generation + design justification scoring
    |
    v
prepped_evidence.json
    |
    v
[4] review.py              LLM rationale generation + condensation
    |
    v
review.json  <────────────────────────────────────────────────────────┐
                                                                      │ (patches originality
[5] originality_check.py   OpenAlex literature search +               │  category in-place)
                           per-work similarity scoring +              │
                           LLM originality statement ────────────────►┘
    |
    v
originality.json
    |
    v
review.json  <────────────────────────────────────────────────────────┐
                                                                      │ (patches/inserts categories
[6] screener.py            Sliding-window protocol scan +             │  for dimensions with findings)
                           LLM screening + rationale writing ────────►┘
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

Deterministic (no LLM) step that sorts each claim into one of five buckets
based on its `claim_type`, classification tags, and `relevancy_score`.
First match wins.

| Bucket | What goes here |
|--------|----------------|
| `design_specification` | Core protocol design claims — hypotheses, endpoints, sample sizes, randomization, blinding, planned measurements |
| `methodological` | Planned analytical methods, statistical analysis plan, measurement procedures |
| `background_rationale` | Literature-grounded justification for the study, gap identification |
| `aspirational` | Expected outcomes, anticipated impact, feasibility claims |
| `contextual` | Definitions, background, framing |
| `noise` | Low relevancy, figure captions, missing tags |

**Input:** `grouped.json` (from `group.py`)
**Output:** `triaged.json`

**CLI:**
```
python triage.py grouped.json -o triaged.json --mappings ../mappings.json
```

---

### retrieve_compare.py

Citation resolution and evidence grading adapted for protocols:

- **No self-reported reclassification** — protocols have no own-findings
- **Protocol-specific grades:** `design_precedent` (cited reference shows prior
  use of same design), `established_method` (method is well-established)
- All `unreferenced` claims are flagged without exemption — protocols should
  justify every major design choice with cited literature

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

---

### prep.py

Builds protocol-aware narrative sentences for each claim:

- Evidence weights include `design_precedent` (0.85) and `established_method` (0.90)
- No grades are excluded from scoring (no self-reported exclusions)
- Recognizes protocol-specific triage buckets

**Input:** `retrieve_compare_llm.json`
**Output:** `prepped_evidence.json`

**CLI:**
```
python prep.py retrieve_compare_llm.json -o prepped_evidence.json
```

---

### review.py

Three-stage LLM pipeline producing per-dimension rationales focused on
**design justification quality** rather than evidence for results.

| Stage | What it does |
|-------|--------------|
| 1. `narrative_finder` | Chunks claim narratives into ~1000-token segments per dimension |
| 2. `rationale_gen` | LLM writes a partial design-justification rationale per chunk |
| 3. `rationale_condenser` | LLM merges partial rationales into one per dimension |

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

Assesses the originality of the planned study design against existing
literature. Uses local prompts framed for "study protocol" rather than
"research paper". Core logic reused from empirical.

**Input:** `text_knowledge_base.jsonl`, optionally `full.md`
**Outputs:** `originality.json`, patched `review.json`

**CLI:**
```
python originality_check.py \
    --directory pipe-test/ \
    --fullmd full.md \
    -o originality.json
```

---

### screener.py

Sliding-window document screener with protocol-specific focus:

- Protocol completeness (SPIRIT/PRISMA-P checklist items)
- Missing pre-specifications (endpoints, power calculations, analysis plans)
- Registration status and oversight mechanisms
- Conflicts of interest and funding disclosure

**Input:** `full.md`, `openalex_cache.json`, `mappings.json`, `review.json`
**Outputs:** `screener.json`, patched `review.json`

**CLI:**
```
python screener.py \
    --fullmd full.md \
    --openalex-cache openalex_cache.json \
    --mappings ../mappings.json \
    --review review.json \
    -o screener.json
```

---

### score.py

Unified scoring with the same framework as empirical (evidence-weighted,
rubric-penalty, composite) but using protocol-specific weights and prompts.

**Input:** `review.json`, `prepped_evidence.json`, `originality.json`,
`screener.json`, `mappings.json`
**Output:** `review.json` (final), optional `overview.json`

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

---

### evidence-doc.py

Deterministic audit trail generator (no LLM). Directly reuses the empirical
implementation — the audit generator reads `review.json` generically.

**Input:** `review.json`, retrieve JSON, `screener.json`, `originality.json`
**Output:** `evidence_audit.md`

**CLI:**
```
python evidence-doc.py --directory /path/to/run -o evidence_audit.md
```

---

## Evidence grades

| Grade | Meaning |
|-------|---------|
| `strong` | Cited references directly support the planned design choice |
| `moderate` | Cited references partially support the planned design choice |
| `weak` | Cited references offer only tangential support |
| `design_precedent` | Cited reference demonstrates prior successful use of the same design element |
| `established_method` | Cited reference confirms the planned method is well-established |
| `unsupported` | Cited references do not actually support the specific design claim |
| `unreferenced` | Design claim has no inline citations where justification would be expected |
| `unverifiable` | Cited references lack accessible abstracts |

---

## Prompts

All prompts live in `prompts/` and are loaded at runtime:

| File | Used by | Purpose |
|------|---------|---------|
| `evidence_narrative_template.md` | `prep.py` | Sentence template with protocol-aware grade descriptions |
| `rationale_generation_prompt.md` | `review.py` Stage 2 | Instructs LLM to analyze design justification quality per chunk |
| `rationale_condenser_prompt.md` | `review.py` Stage 3 | Instructs LLM to merge partial rationales preserving grade distribution |
| `review_statement_prompt.md` | `score.py` Stage 6 | Instructs LLM to write a protocol-quality summary |
| `overview_rationale_prompt.md` | `score.py` Stage 7 | Simplifies rationales for layperson overview |
| `search_term_prompt.md` | `originality_check.py` Stage 2 | Generate search terms from protocol chunks |
| `similarity_scorer_prompt.md` | `originality_check.py` Stage 4a | Score each related work's similarity |
| `originality_statement_prompt.md` | `originality_check.py` Stage 4b | Write originality statement for protocol designs |
| `screener_system_prompt.md` | `screener.py` Stage 2 | Screen for protocol completeness, SPIRIT items, missing pre-specifications |
| `screener_category_writer_prompt.md` | `screener.py` Stage 4 | Write rationale from aggregated screener findings |

---

## Running the full pipeline

Use `protocol-pipe.py` in this directory:

```
python protocol-pipe.py --input-dir "../../../articles/data/my-protocol"
python protocol-pipe.py --input-dir ... --from-step 6   # triage onwards
python protocol-pipe.py --input-dir ... --skip-llm      # skip LLM steps
```

Steps 6-13 correspond to this folder's scripts:

| Step | Script |
|------|--------|
| 6 | `triage.py` |
| 7 | `retrieve_compare.py` |
| 8 | `prep.py` |
| 9 | `review.py` |
| 10 | `originality_check.py` |
| 11 | `screener.py` |
| 12 | `score.py` |
| 13 | `evidence-doc.py` |

---

## Output files

| File | Description |
|------|-------------|
| `triaged.json` | Claims sorted into protocol triage buckets per dimension |
| `openalex_cache.json` | Cached OpenAlex DOI lookup responses |
| `retrieve_compare_llm.json` | Claims enriched with evidence grades and citation analysis |
| `prepped_evidence.json` | Claims with narrative sentences and grade distributions |
| `pre_condensed_rationales.json` | Stage 2 rationales before condensation (debug artifact) |
| `review.json` | Final structured review with scores, rationales, and review statement |
| `overview.json` | Plain-language companion to review.json |
| `originality_openalex_cache.json` | Cached OpenAlex search results for originality |
| `originality.json` | Full originality report |
| `screener.json` | Document screener diagnostic output |
| `evidence_audit.md` | Human-readable audit trail |
