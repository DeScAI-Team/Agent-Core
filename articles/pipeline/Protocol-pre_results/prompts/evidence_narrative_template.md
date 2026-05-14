# Protocol-aware claim narrative template (LLM-facing)

Plain-text rendering of each claim for downstream review prompts.
Placeholders are filled by `Protocol-pre_results/prep.py`.

## Evidence grade labels

| Grade | Meaning |
|-------|---------|
| strong | Cited reference(s) directly support the planned design choice |
| moderate | Cited reference(s) partially support the planned design choice |
| weak | Cited reference(s) offer only tangential support for the design choice |
| design_precedent | Cited reference demonstrates prior successful use of the same design element |
| established_method | Cited reference confirms the planned method is well-established |
| unsupported | Cited reference(s) do not actually support the specific design claim |
| unreferenced | Design claim has no inline citations where justification would be expected |
| unverifiable | Cited references lack accessible abstracts for verification |

## Relevancy tiers

`relevancy_score` is clamped to `[0.0, 1.0]`, then bucketed:

| Range | Verbal label |
|-------|--------------|
| 0.0 – 0.2 | low relevancy |
| 0.2 – 0.4 | slightly relevant |
| 0.4 – 0.6 | moderately relevant |
| 0.6 – 0.8 | very relevant |
| 0.8 – 1.0 | extremely relevant |

Missing or non-numeric scores use: **relevancy unknown**.

## Sentence template

{doc_name} presents the claim '{claim}' in the {section_heading} section. This claim was deemed {verdict} ({rationale}). Evidence grade: {evidence_grade} — {evidence_summary} This claim is rated as {relevancy_label} for the core of this protocol.
