#!/usr/bin/env python3
"""Validate each extracted line.

For lines tagged `needs_scientific_support`:
  1. LLM generates 1-2 OpenAlex search queries (no thinking).
  2. Fetch up to N abstracts via openalex_search.search_many.
  3. LLM reads claim + abstracts and returns valid|invalid|inconclusive +
     <=50-word rationale + cited OpenAlex ids.

For lines tagged `polarity_unknown` (and not needs_scientific_support):
  - LLM returns positive|negative|neutral + <=50-word rationale.

Reads:  steps/extracted.jsonl + ipnft_dir/profile.json (for context)
Writes: steps/validated.jsonl with verdict, rationale, citations, retrieved_works
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from llm_client import (  # noqa: E402
    call,
    discover_model,
    load_prompt,
    make_client,
    parse_json_object,
)
from openalex_search import search_many  # noqa: E402

VALID_SCIENTIFIC_VERDICTS = {"valid", "invalid", "inconclusive"}
VALID_STANCE_VERDICTS = {"positive", "negative", "neutral"}
MAX_QUERIES_PER_CLAIM = 2
MAX_WORKS_PER_CLAIM = 6

# Category-scoped criteria. Each block is injected into the stance prompt's
# {category_focus} slot so the validator judges the line ONLY through that lens.
# This prevents cross-category penalty bleed (e.g. a mission line being marked
# negative because the lead is anonymous — anonymity is team_credibility's
# concern, not mission_clarity's).
CATEGORY_FOCUS: dict[str, str] = {
    "research_output_quality": (
        "Lens: research_output_quality. Judge ONLY whether the line evidences "
        "concrete research deliverables shipped or in active progress.\n"
        "FAVORABLE: named papers / preprints / posters / datasets / models / "
        "code repos / demos / dashboards / running experiments / measurable "
        "results / third-party use of the output. Specific named artifacts and "
        "version numbers count even if early.\n"
        "UNFAVORABLE: marketing fluff with no artifact named, vaporware, "
        "promises of future deliverables with no current artifact, abandoned "
        "or unmaintained outputs.\n"
        "OUT OF SCOPE: team identity, governance, tokenomics, mission framing — "
        "ignore those."
    ),
    "scientific_grounding": (
        "Lens: scientific_grounding. Judge ONLY whether the line connects the "
        "DAO's approach to established scientific mechanisms / techniques / "
        "literature.\n"
        "FAVORABLE: named mechanisms, named techniques, named methodologies, "
        "specific assays / models / datasets that have literature support, "
        "explicit references to prior work, framing in terms of well-known "
        "biological/chemical/computational concepts.\n"
        "UNFAVORABLE: hand-wavy science, buzzword soup with no named technique, "
        "claims that contradict known mechanisms, pseudoscientific framing.\n"
        "OUT OF SCOPE: team identity, tokenomics, mission marketing — "
        "ignore those."
    ),
    "execution_competence": (
        "Lens: execution_competence. Judge ONLY whether the line evidences the "
        "DAO actually executes — ships things, hits dates, maintains code, "
        "responds to issues.\n"
        "FAVORABLE: shipped releases, GitHub commits, milestones met, "
        "live demos, public roadmap with completed items, recent activity, "
        "open-sourced code, named integrations or deployments.\n"
        "UNFAVORABLE: stale repos, missed milestones, no public artifacts, "
        "all-talk-no-ship, abandoned plans.\n"
        "OUT OF SCOPE: scientific rigor of the work, mission framing, team "
        "credentials — ignore those."
    ),
    "team_credibility": (
        "Lens: team_credibility. Judge ONLY whether the line evidences a "
        "credible, identifiable team behind the DAO.\n"
        "FAVORABLE: named research lead with verifiable institutional "
        "affiliation, named collaborators with track record, named advisors, "
        "specific publications by team members, prior verifiable work, real "
        "social presence tied to real identities.\n"
        "UNFAVORABLE: anonymous lead where institutional rigor is expected, "
        "unverifiable affiliations, pseudonymous-only team, no named "
        "individuals, contradictions between team claims.\n"
        "OUT OF SCOPE: the science, mission, governance, tokenomics — "
        "ignore those."
    ),
    "mission_clarity": (
        "Lens: mission_clarity. Judge ONLY whether THE MISSION ITSELF is "
        "clear, specific, well-bounded, and internally consistent.\n"
        "FAVORABLE: a specific scientific problem named, a bounded scope, an "
        "identifiable target user / patient population / scientific question, "
        "a coherent statement of what the DAO will and will not do, a "
        "consistent through-line across documents.\n"
        "UNFAVORABLE: vague aspirational fluff with no named problem, scope "
        "that shifts between docs, contradictory statements about purpose, "
        "marketing language with no concrete subject.\n"
        "OUT OF SCOPE: whether the team is anonymous, whether artifacts have "
        "shipped, whether tokens are well-distributed, whether the science is "
        "sound — ignore those entirely. A clearly stated mission can be "
        "FAVORABLE here even if the team is anonymous and nothing has shipped."
    ),
    "governance_tokenomics": (
        "Lens: governance_tokenomics. Judge ONLY whether the line evidences "
        "sound on-chain structure, agreements, token distribution, and "
        "decision-making.\n"
        "FAVORABLE: signed Assignment / Development agreements, named legal "
        "wrappers, healthy holder distribution, real liquidity, real trading "
        "volume, named voting mechanisms, transparent treasury, vesting "
        "schedules disclosed.\n"
        "UNFAVORABLE: missing agreements, concentrated holder distribution, "
        "dead liquidity, opaque governance, undisclosed vesting, no on-chain "
        "structure described.\n"
        "OUT OF SCOPE: scientific rigor, team credentials, mission clarity — "
        "ignore those."
    ),
}

DEFAULT_CATEGORY_FOCUS = (
    "Lens: general DAO health. Judge whether the line is favorable or "
    "unfavorable for the DAO overall, considering deliverables, science, "
    "team, mission, and governance."
)


def _load_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _profile_metadata(ipnft_dir: Path) -> dict[str, str]:
    profile_path = ipnft_dir / "profile.json"
    if not profile_path.exists():
        profile_path = ipnft_dir / "metadata" / "profile.json"
    if not profile_path.exists():
        return {"ipnft_symbol": "", "ipnft_name": "", "organization": "", "research_lead": "", "topic": ""}
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    ipnft = profile.get("ipnft", {}) or {}
    lead = ipnft.get("researchLead", {}) or {}
    return {
        "ipnft_symbol": str(profile.get("symbol") or ipnft.get("initialSymbol") or ""),
        "ipnft_name": str(ipnft.get("name") or ""),
        "organization": str(ipnft.get("organization") or ""),
        "research_lead": str(lead.get("name") or "Anonymous"),
        "topic": str(ipnft.get("topic") or ""),
    }


def _claim_context_block(line: dict[str, Any], meta: dict[str, str]) -> str:
    return (
        f"DAO: {meta['ipnft_symbol']} ({meta['ipnft_name']})\n"
        f"Organization: {meta['organization']}\n"
        f"Topic: {meta['topic']}\n"
        f"Source: {line['source_kind']} | doc: {line.get('doc_title', '')} | section: {line.get('section', '')}\n"
        f"Claim text: {line['text']}\n"
        f"Verbatim quote: {line.get('verbatim_quote', '')}\n"
    )


def _generate_queries(client, *, model: str, line: dict[str, Any], meta: dict[str, str], system: str) -> list[str]:
    user = _claim_context_block(line, meta)
    raw = call(client, model=model, system=system, user=user, max_tokens=300)
    parsed = parse_json_object(raw)
    if not isinstance(parsed, dict):
        return []
    queries = parsed.get("queries", [])
    if not isinstance(queries, list):
        return []
    cleaned = []
    for q in queries:
        if isinstance(q, str) and q.strip():
            cleaned.append(q.strip()[:200])
    return cleaned[:MAX_QUERIES_PER_CLAIM]


def _format_works_for_llm(works: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for w in works:
        oid = w.get("openalex_id") or "?"
        title = (w.get("title") or "").strip()[:200]
        year = w.get("year")
        cites = w.get("cited_by_count")
        abstract = (w.get("abstract") or "").strip()[:1200]
        lines.append(
            f"- [{oid}] {title} (year={year}, cited_by={cites})\n  abstract: {abstract or '(none)'}"
        )
    return "\n".join(lines) if lines else "(no works retrieved)"


def _validate_scientific(
    client,
    *,
    model: str,
    line: dict[str, Any],
    meta: dict[str, str],
    works: list[dict[str, Any]],
    system: str,
) -> dict[str, Any]:
    user = (
        f"{_claim_context_block(line, meta)}\n"
        f"Retrieved works:\n{_format_works_for_llm(works)}\n"
    )
    raw = call(client, model=model, system=system, user=user, max_tokens=400)
    parsed = parse_json_object(raw)
    if not isinstance(parsed, dict):
        return {"verdict": "inconclusive", "rationale": "Validator returned no parsable JSON.", "citations": []}

    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in VALID_SCIENTIFIC_VERDICTS:
        verdict = "inconclusive"

    rationale = str(parsed.get("rationale", "")).strip()[:500]

    available_ids = {w.get("openalex_id") for w in works if w.get("openalex_id")}
    raw_citations = parsed.get("citations") or []
    citations: list[str] = []
    if isinstance(raw_citations, list):
        for c in raw_citations:
            cid = str(c).strip()
            if cid in available_ids and cid not in citations:
                citations.append(cid)

    if verdict == "valid" and not citations:
        verdict = "inconclusive"
        rationale = (rationale + " (downgraded: no in-set citation provided)").strip()

    return {"verdict": verdict, "rationale": rationale, "citations": citations}


def _validate_stance(
    client,
    *,
    model: str,
    line: dict[str, Any],
    meta: dict[str, str],
    system_template: str,
) -> dict[str, Any]:
    focus = CATEGORY_FOCUS.get(line.get("category") or "", DEFAULT_CATEGORY_FOCUS)
    system = system_template.replace("{category_focus}", focus)
    user = json.dumps(
        {
            "ipnft_symbol": meta["ipnft_symbol"],
            "ipnft_name": meta["ipnft_name"],
            "organization": meta["organization"],
            "research_lead": meta["research_lead"],
            "topic": meta["topic"],
            "line_type": line["line_type"],
            "category": line["category"],
            "subgroup": line["subgroup"],
            "source_kind": line["source_kind"],
            "doc_title": line.get("doc_title"),
            "domain": line.get("domain"),
            "section": line.get("section"),
            "text": line["text"],
            "verbatim_quote": line.get("verbatim_quote", ""),
        },
        ensure_ascii=False,
    )
    raw = call(client, model=model, system=system, user=user, max_tokens=300)
    parsed = parse_json_object(raw)
    if not isinstance(parsed, dict):
        return {"verdict": "neutral", "rationale": "Stance returned no parsable JSON."}

    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in VALID_STANCE_VERDICTS:
        verdict = "neutral"
    rationale = str(parsed.get("rationale", "")).strip()[:500]
    return {"verdict": verdict, "rationale": rationale}


def run(
    *,
    extracted_path: Path,
    ipnft_dir: Path,
    output_path: Path,
    model: str | None = None,
    skip_openalex: bool = False,
) -> dict[str, int]:
    client = make_client()
    m = model or discover_model(
        client,
        env_var="LLM_MODEL",
        fallback_envs=("VALIDATOR_MODEL",),
    )
    print(f"[validate] model: {m}")
    if skip_openalex:
        print("[validate] skip_openalex=True — scientific lines will be marked inconclusive without queries")

    meta = _profile_metadata(ipnft_dir)
    query_prompt = load_prompt("dao-validate-query.md")
    sci_prompt = load_prompt("dao-validate-scientific.md")
    stance_prompt = load_prompt("dao-validate-stance.md")

    lines = list(_load_jsonl(extracted_path))
    print(f"[validate] {len(lines)} lines to validate")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"valid": 0, "invalid": 0, "inconclusive": 0, "positive": 0, "negative": 0, "neutral": 0}
    openalex_calls = 0

    with output_path.open("w", encoding="utf-8") as fh:
        for i, line in enumerate(lines, 1):
            row = dict(line)
            try:
                if line.get("needs_scientific_support"):
                    if skip_openalex:
                        result = {"verdict": "inconclusive", "rationale": "skipped (no openalex)", "citations": []}
                        works = []
                    else:
                        queries = _generate_queries(client, model=m, line=line, meta=meta, system=query_prompt)
                        if queries:
                            works = search_many(queries, max_total=MAX_WORKS_PER_CLAIM)
                            openalex_calls += len(queries)
                        else:
                            works = []
                        result = _validate_scientific(
                            client,
                            model=m,
                            line=line,
                            meta=meta,
                            works=works,
                            system=sci_prompt,
                        )
                    row["verdict_kind"] = "scientific"
                    row["verdict"] = result["verdict"]
                    row["rationale"] = result["rationale"]
                    row["citations"] = result["citations"]
                    row["retrieved_works"] = [
                        {
                            "openalex_id": w.get("openalex_id"),
                            "doi": w.get("doi"),
                            "title": w.get("title"),
                            "year": w.get("year"),
                            "cited_by_count": w.get("cited_by_count"),
                            "abstract": (w.get("abstract") or "")[:1500],
                            "search_term": w.get("search_term"),
                        }
                        for w in works
                    ]
                else:
                    # Every non-scientific line gets a stance verdict.
                    # The previous "skipped → neutral" auto-path swallowed ~80% of
                    # extracted lines and prevented categories from scoring.
                    result = _validate_stance(client, model=m, line=line, meta=meta, system_template=stance_prompt)
                    row["verdict_kind"] = "stance"
                    row["verdict"] = result["verdict"]
                    row["rationale"] = result["rationale"]
                    row["citations"] = []
                    row["retrieved_works"] = []

                if row.get("verdict") in counts:
                    counts[row["verdict"]] += 1
            except Exception as exc:
                print(f"  [{i}/{len(lines)}] FAILED on {line.get('line_id')}: {exc}", file=sys.stderr)
                row["verdict_kind"] = "error"
                row["verdict"] = "neutral"
                row["rationale"] = f"validator error: {exc}"
                row["citations"] = []
                row["retrieved_works"] = []

            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            if i % 20 == 0:
                print(f"  [{i}/{len(lines)}] {counts}")

    print(f"[validate] verdict counts: {counts}")
    print(f"[validate] openalex queries issued: {openalex_calls}")
    counts["openalex_calls"] = openalex_calls
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate extracted DAO lines")
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--ipnft-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--skip-openalex", action="store_true")
    args = parser.parse_args()

    run(
        extracted_path=args.extracted.resolve(),
        ipnft_dir=args.ipnft_dir.resolve(),
        output_path=args.output.resolve(),
        model=args.model,
        skip_openalex=args.skip_openalex,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
