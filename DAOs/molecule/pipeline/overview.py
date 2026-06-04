#!/usr/bin/env python3
"""Simplify each text field in review.json into a general-audience overview.json.

One LLM call per field (review_statement + each category rationale), no thinking,
preserving scores. Mirrors compounds/pipeline/overview.py but uses the DAO
overview prompt and the DAO field map.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from llm_client import call, discover_model, load_prompt, make_client  # noqa: E402
from review import with_evidence_note  # noqa: E402

FIELD_LABELS = {
    "review_statement": "review_statement (executive summary)",
    "research_output_quality": "research_output_quality",
    "scientific_grounding": "scientific_grounding",
    "execution_competence": "execution_competence",
    "team_credibility": "team_credibility",
    "mission_clarity": "mission_clarity",
    "governance_tokenomics": "governance_tokenomics",
}


def _simplify_field(
    client,
    *,
    model: str,
    system: str,
    research_name: str,
    field_key: str,
    original: str,
) -> str:
    text = (original or "").strip()
    if not text:
        return text
    label = FIELD_LABELS.get(field_key, field_key)
    user = f"Research DAO: {research_name}\nField: {label}\n\nOriginal text:\n{text}"
    out = call(client, model=model, system=system, user=user, max_tokens=1024).strip()
    if not out:
        print(f"  [overview] empty result for {field_key}; keeping original", file=sys.stderr)
        return text
    return out


def run(*, review_path: Path, output_path: Path, model: str | None = None) -> dict[str, Any]:
    review = json.loads(review_path.read_text(encoding="utf-8"))
    client = make_client()
    m = model or discover_model(
        client,
        env_var="LLM_MODEL",
        fallback_envs=("VALIDATOR_MODEL",),
    )
    print(f"[overview] model: {m}")

    system = load_prompt("dao-overview.md")
    name = review.get("research_dao") or "Unknown"

    overview: dict[str, Any] = {
        "research_dao": review.get("research_dao"),
        "review_date": review.get("review_date"),
        "composite_score": review.get("composite_score"),
        "review_statement": with_evidence_note(
            _simplify_field(
                client,
                model=m,
                system=system,
                research_name=name,
                field_key="review_statement",
                original=review.get("review_statement") or "",
            )
        ),
        "categories": {},
    }
    for cat, payload in (review.get("categories") or {}).items():
        if not isinstance(payload, dict):
            continue
        overview["categories"][cat] = {
            "score": payload.get("score"),
            "rationale": with_evidence_note(
                _simplify_field(
                    client,
                    model=m,
                    system=system,
                    research_name=name,
                    field_key=cat,
                    original=payload.get("rationale") or "",
                )
            ),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(overview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[overview] wrote {output_path}")
    return overview


def main() -> int:
    parser = argparse.ArgumentParser(description="Simplify DAO review.json into overview.json")
    parser.add_argument("review", type=Path, help="Path to review/review.json")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Path to overview.json")
    parser.add_argument("--model", type=str, default=None)
    args = parser.parse_args()

    run(review_path=args.review.resolve(), output_path=args.output.resolve(), model=args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
