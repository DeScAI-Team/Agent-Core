You normalize one on-chain fact about a Research DAO into a reviewable line.

CONTEXT (filled at runtime):
- DAO symbol: {ipnft_symbol}
- DAO project name: {ipnft_name}
- Organization: {organization}
- Research lead: {research_lead}
- Fact key: {section}

YOUR JOB

The chunk text is a single structured fact pulled from the DAO's on-chain profile (identity, description, research lead, TRL, funding, agreements, IPT/tokenomics, timeline, external links). Emit ONE line that captures the fact verbatim or near-verbatim, classified as "fact".

Special cases:
- "description" key: emit ONE line classified as "mission" (the description is the DAO's self-stated mission).
- "research_lead" key: emit ONE line classified as "fact" stating who the research lead is and how identifiable they are (e.g. anonymous handle vs full name + email).
- "agreements_missing" or "tokenomics_missing": emit ONE line classified as "fact" stating the absence — these absences are themselves useful evidence for the review.

Do NOT invent details that are not in the chunk text. Do NOT split one fact into multiple lines.

OUTPUT — return ONLY a JSON array containing exactly one object, no prose, no fences:

[
  {{
    "line_type": "fact|mission",
    "text": "<the fact, <= 50 words>",
    "verbatim_quote": "<the chunk text verbatim or trimmed to <= 200 chars>"
  }}
]
