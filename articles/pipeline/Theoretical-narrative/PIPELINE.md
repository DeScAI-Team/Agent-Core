# Theoretical-narrative Review Pipeline

**Target document types:** Literature reviews, meta-analyses, theoretical frameworks, position papers, opinion/perspective pieces, and narrative analyses that synthesize existing literature or argue a thesis without generating new experimental data.

**Key distinction from empirical pipeline:** These papers cite heavily but produce no original experimental data. Every substantive claim should trace to cited literature. The core evaluation question shifts from "does the cited evidence support this finding?" to "does the cited literature actually support this argument/synthesis/thesis?"

## Pipeline overview

The theoretical-narrative pipeline shares steps 1-5 (claim extraction) with the empirical pipeline, then diverges at step 6 (triage) with domain-specific processing through step 13.

```
Steps 1-5 (shared):
  spaCy → LLM extraction → validation → classify → group → grouped.json

Steps 6-13 (Theoretical-narrative/):
  triage → retrieve_compare → prep → review →
  originality_check → screener → score → evidence-doc
```

## Steps 6-13: Theoretical-narrative review

### Step 6 — `triage.py`

Deterministically sorts each claim in `grouped.json` into one of five review buckets. First match wins.

| Bucket | Claim types | Key tags | Description |
|--------|------------|----------|-------------|
| `thesis_argument` | Assertion, Fact | Causal, Correlational, Comparative, Mechanistic, Performance, Observational, Interpretive | Core argumentative claims that cite literature to build a thesis |
| `synthesis` | Fact, Assertion | Background, Synthesis, SourceAttribution | Literature-summarizing claims that characterize or aggregate prior work |
| `methodological` | Fact, Assertion | Methodological, (Measurement + Methodological), sole Benchmark | Meta-analysis or systematic review methodology |
| `aspirational` | Roadmap, Fact, Assertion | GapStatement, Hypothesis, NoveltyAssertion, FutureWork, Feasibility, ImpactPotential | Gaps, hypotheses, future work |
| `contextual` | Fact, Assertion | Definitional, (Assertion-only: Prescriptive, Hedge) | Background definitions, framing |

**Removed from empirical:** `empirical` and `boilerplate_method` buckets, `_is_expectation_claim`, `_is_boilerplate_method`, key messages dedup.

**Added:** Dominance warning when thesis_argument + synthesis don't dominate — for theoretical papers these should be the primary buckets.

**Quality gates:** Figure/table captions, `claim_type=None`, empty `claim_classification_1`, `relevancy_score < 0.3` → noise.

**Output:** `triaged.json`

### Step 7 — `retrieve_compare.py`

Enriches each claim with citation resolution, OpenAlex metadata, and LLM evidence grading.

**Imports from empirical:** Citation extraction, OpenAlex caching, reference parsing, LLM evidence auditing infrastructure.

**Critical differences from empirical:**
- **No self_reported reclassification.** Theoretical papers have no own experimental data to exempt. Every unreferenced substantive claim stays `unreferenced`.
- **Overclaim grade.** When the paper draws stronger conclusions than the cited references warrant, the evidence auditor can assign `overclaim` as either a per-reference verdict or overall grade.
- **Argumentation-focused auditor prompt.** The LLM evidence auditor is instructed to check whether cited references actually support the specific *argumentative* claim, synthesis, or interpretation — not just the general topic.

**Evidence grades:**

| Grade | Meaning |
|-------|---------|
| `strong` | Cited references directly support the argumentative claim |
| `moderate` | Cited references partially support the claim or interpretation |
| `weak` | Cited references offer only tangential support |
| `overclaim` | Paper draws stronger conclusions than references warrant |
| `unsupported` | Cited references do not back the specific argument |
| `unreferenced` | No inline citations where literature support expected |
| `unverifiable` | Cited references lack accessible abstracts |

**Output:** `retrieve_compare_llm.json` (or `retrieve_compare_out.json` with `--skip-llm`)

### Step 8 — `prep.py`

Builds LLM-readable narrative sentences for each claim and computes per-dimension grade distributions.

**Key configuration:**
- `KEEP_BUCKETS`: `{thesis_argument, synthesis, methodological, contextual, aspirational}`
- `SCORE_EXCLUDED_GRADES`: `frozenset()` (empty — no grades exempt from scoring)
- `EVIDENCE_WEIGHTS`: `strong=1.0, moderate=0.8, weak=0.5, unverifiable=0.4, unreferenced=0.35, unsupported=0.25, overclaim=0.2, pending=0.3`

The `overclaim` weight (0.2) is lower than `unsupported` (0.25) because an overclaim implies the references are topically relevant but the author extends beyond them — a more specific argumentative failure.

**Output:** `prepped_evidence.json`

### Step 9 — `review.py`

Three-stage LLM rationale generation:

1. **narrative_finder:** Chunks claim narratives into ~1000-token segments per dimension
2. **rationale_gen:** LLM generates per-chunk argumentation-quality rationales
3. **rationale_condenser:** LLM merges chunks into one rationale per dimension

Prompts are tuned for theoretical-narrative context — focusing on argumentation quality, logical coherence, and citation support rather than experimental evidence quality.

The stats line includes `overclaim` count alongside `strong_moderate` and `unsupported_unreferenced`.

**Output:** `review.json` (without review statement — that's added by score.py)

### Step 10 — `originality_check.py`

Assesses the novelty of the paper's synthesis or theoretical framework.

**Imports from empirical:** Abstract extraction, KB chunk loading, OpenAlex search, similarity scoring, originality statement writing.

**Prompt adaptation:** Evaluates novelty of the *synthesis* or *theoretical framework*, not novel experimental contribution. "Does this review offer a genuinely new perspective, or is it a routine summary?"

**Output:** `originality.json`, patches `review.json` with originality score

### Step 11 — `screener.py`

Sliding-window LLM scan of the full paper for signals the claim pipeline misses.

**Imports from empirical:** Window builder, window screener, dedup/aggregate, category writer, review patching.

**Prompt focus for theoretical-narrative papers:**
- Cherry-picking citations (only citing supportive evidence)
- Straw-man or misrepresentation of opposing views
- Logical gaps or non-sequiturs in argument chains
- Missing major competing frameworks or counter-evidence
- Balanced representation of the literature
- Scope claims exceeding what the reviewed literature supports
- Meta-analysis specific: heterogeneity handling, publication bias, PRISMA compliance
- Narrative framing bias

**Output:** `screener.json`, patches `review.json` with additional categories

### Step 12 — `score.py`

Unified scoring step that recomputes all category scores:

1. **Evidence-grade scoring:** Weighted formula from `prep.py` for claim-level dimensions
2. **Originality pass-through:** Score from `originality.json`
3. **Rubric-penalty scoring:** Deterministic formula for screener-only dimensions
4. **Merge:** Combine all scores into unified categories
5. **Composite score:** Weighted average using `dimension_weights` from `mappings.json`
6. **Review statement:** LLM generates top-level summary of argumentation quality
7. **Overview:** LLM generates layperson-readable simplified version

**Output:** Updated `review.json` (with `composite_score` and `review_statement`), `overview.json`

### Step 13 — `evidence-doc.py`

Generates a human-readable Markdown audit trail. No LLM calls.

**Sections:**
- Category scores table
- Evidence grade counts per dimension
- Claim-level trace with citation details
- Document screener findings
- Originality literature overlap

**Output:** `evidence_audit.md`

## Orchestrator

`theoretical-narrative-pipe.py` runs all 13 steps sequentially.

```bash
python theoretical-narrative-pipe.py \
  --input-dir "articles/data/my-review-paper" \
  --from-step 6 \
  --model /model
```

Use `--from-step N` to resume from a specific step. Use `--skip-llm` for dry runs.

## Prompt files

All 10 prompts in `prompts/` are tuned for theoretical-narrative papers:

| File | Used by | Purpose |
|------|---------|---------|
| `evidence_narrative_template.md` | prep.py | Sentence template for claim narratives |
| `rationale_generation_prompt.md` | review.py | Per-chunk argumentation rationale generation |
| `rationale_condenser_prompt.md` | review.py | Multi-chunk rationale condensation |
| `review_statement_prompt.md` | score.py | Top-level review statement |
| `overview_rationale_prompt.md` | score.py | Layperson-readable simplification |
| `search_term_prompt.md` | originality_check.py | OpenAlex search term generation |
| `similarity_scorer_prompt.md` | originality_check.py | Related work similarity scoring |
| `originality_statement_prompt.md` | originality_check.py | Originality statement writing |
| `screener_system_prompt.md` | screener.py | Window-level argumentation screening |
| `screener_category_writer_prompt.md` | screener.py | Dimension rationale from findings |

## Differences from empirical pipeline (summary)

| Aspect | Empirical | Theoretical-narrative |
|--------|-----------|----------------------|
| Self-reported claims | Paper's own findings (exempt from grading) | **None** — all claims graded |
| Triage buckets | empirical, methodological, boilerplate_method, aspirational, contextual | **thesis_argument, synthesis**, methodological, aspirational, contextual |
| Evidence grades | strong/moderate/weak + self_reported/self_reported_method | strong/moderate/weak + **overclaim** (no self_reported) |
| Score exclusions | self_reported, self_reported_method excluded | **None** excluded |
| Screener focus | Missing disclosures, COI, experimental red flags | **Logical coherence, cherry-picking, citation coverage, balanced representation** |
| Originality focus | Novel experimental contribution | **Novel synthesis, reframing, theoretical contribution** |
