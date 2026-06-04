#!/usr/bin/env python3
r"""Generate a three-section compound review from ``longevity.json`` and ``risk.json``.

Pipeline:
1. Group rows by topic (``topic_grouper``) into capped chunks.
2. Summarize each topic group via LLM.
3. Synthesize scientific grounding, risk, and final review statement from summaries.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI, RateLimitError
except ModuleNotFoundError:
    OpenAI = None  # type: ignore[assignment]

    class RateLimitError(Exception):  # type: ignore[no-redef]
        pass

_COMPOUNDS_DIR = Path(__file__).resolve().parents[2]
if str(_COMPOUNDS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS_DIR))
from llm_env import LLM_BASE_URL, LLM_API_KEY  # noqa: E402


def get_available_model(client: OpenAI, env_var_name: str, fallback_env_vars: list[str]) -> str:
    """
    Discover available model from the LLM server or environment variables.

    Priority:
    1. Query /v1/models endpoint
    2. Check env_var_name (e.g., TAGGER_MODEL, REVIEWER_MODEL)
    3. Check fallback_env_vars (e.g., CLASSIFIER_MODEL, VALIDATOR_MODEL)
    4. Raise error with helpful message
    """
    # Try to query vLLM for available models
    try:
        models = client.models.list()
        if models.data and len(models.data) > 0:
            model_id = models.data[0].id
            print(f"  Auto-discovered model: {model_id}", file=sys.stderr)
            return model_id
    except Exception as e:
        print(f"  Could not auto-discover models from LLM server: {e}", file=sys.stderr)
    
    # Fallback to environment variables
    for env_name in [env_var_name] + fallback_env_vars:
        val = os.environ.get(env_name)
        if val:
            print(f"  Using model from {env_name}: {val}", file=sys.stderr)
            return val
    
    # No model found - provide helpful error
    env_list = ", ".join([env_var_name] + fallback_env_vars)
    raise ValueError(
        f"No model specified and could not auto-discover from LLM server.\n"
        f"Please set one of these environment variables: {env_list}\n"
        f"Or ensure the LLM server is running at {LLM_BASE_URL}"
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
_SINGLE_DIR = Path(__file__).resolve().parent
if str(_SINGLE_DIR) not in sys.path:
    sys.path.insert(0, str(_SINGLE_DIR))
from topic_grouper import (  # noqa: E402
    build_groups_document,
    resolve_compound_name,
)

TOPIC_SUMMARY_PROMPT_PATH = REPO_ROOT / "prompts" / "pump-science-topic-group-summary.md"
GROUNDING_PROMPT_PATH = REPO_ROOT / "prompts" / "pump-science-grounding-from-groups.md"
RISK_PROMPT_PATH = REPO_ROOT / "prompts" / "pump-science-risk-from-groups.md"
STATEMENT_PROMPT_PATH = REPO_ROOT / "prompts" / "pump-science-review-statement-from-groups.md"

COVERAGE_SOURCES = (
    "europe_pmc",
    "openalex_grounding",
    "pubchem_bioassays",
    "chembl",
    "clinical_trials",
    "kegg",
    "openfda_labels",
    "faers",
)
_SOURCE_TO_COVERAGE: dict[str, str] = {
    "europe_pmc": "europe_pmc",
    "openalex_work": "openalex_grounding",
    "openalex_grounding": "openalex_grounding",
    "pubchem_bioassay": "pubchem_bioassays",
    "pubchem_bioassays_meta": "pubchem_bioassays",
    "chembl_mechanism": "chembl",
    "chembl_activity": "chembl",
    "chembl_molecule": "chembl",
    "clinical_trials": "clinical_trials",
    "kegg_pathway": "kegg",
    "kegg_summary": "kegg",
    "openfda_drug_label": "openfda_labels",
    "openfda_faers": "faers",
}

# Model discovery happens later in main() after client is created
MAX_RETRIES = 4
MAX_REVIEWER_TOKENS = max(256, int(os.environ.get("REVIEWER_MAX_TOKENS", "2048")))

# Limited safety evidence should not imply "low risk".
DEFAULT_RISK_SCORE_PCT = 25.0
# Limited longevity-tagged evidence should not imply "no scientific case".
DEFAULT_GROUNDING_SCORE_PCT = 50.0

_END_THINK_MARKERS = (
    "</think>",
    "</think>",
    "</thinking>",
    "</reasoning>",
    "</thought>",
)


def strip_reasoning_markup(s: str) -> str:
    """Drop chain-of-thought wrappers; prefer text after the last end-thinking marker."""
    t = s.strip()
    low = t.lower()
    best_idx = -1
    best_len = 0
    for m in _END_THINK_MARKERS:
        pos = low.rfind(m.lower())
        if pos > best_idx:
            best_idx = pos
            best_len = len(m)
    if best_idx >= 0:
        t = t[best_idx + best_len:].lstrip()
    block_patterns = (
        r"<think\b[^>]*>[\s\S]*?</think>",
        r"<thinking\b[^>]*>[\s\S]*?</thinking>",
        r"<reasoning\b[^>]*>[\s\S]*?</reasoning>",
        r"<thought\b[^>]*>[\s\S]*?</thought>",
        r"<redacted_thinking\b[^>]*>[\s\S]*?</think>",
    )
    for _ in range(8):
        prev = t
        for pat in block_patterns:
            t = re.sub(pat, "", t, flags=re.IGNORECASE)
        if t == prev:
            break
    # Strip unclosed opening think block (model output thinking but never closed the tag).
    t = re.sub(r"<think\b[^>]*>[\s\S]*$", "", t, flags=re.IGNORECASE).strip()
    return t.strip()


def call_llm(client: OpenAI, model: str, system_prompt: str, user_content: str) -> str:
    """Send a chat completion and return the stripped response text."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    extra_body = {
        "top_k": -1,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    def _complete() -> object:
        kw: dict[str, object] = dict(
            model=model,
            max_tokens=MAX_REVIEWER_TOKENS,
            temperature=0,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
            messages=messages,
        )
        try:
            return client.chat.completions.create(**kw, extra_body=extra_body)
        except TypeError:
            return client.chat.completions.create(**kw)

    for attempt in range(MAX_RETRIES):
        try:
            response = _complete()
            content = response.choices[0].message.content
            return strip_reasoning_markup((content or "").strip())
        except RateLimitError:
            wait = (2 ** attempt) * 5
            print(
                f"  [RATE LIMIT] attempt {attempt + 1}/{MAX_RETRIES} — waiting {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)
        except Exception as e:
            msg = str(e)[:120]
            print(
                f"  [ERROR] attempt {attempt + 1}/{MAX_RETRIES}: {msg}",
                file=sys.stderr,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
            else:
                return ""
    return ""


def _fraction_to_percent(value: float | None) -> float | None:
    """Stance-derived scores are 0–1; overview-style output uses 0–100."""
    if value is None:
        return None
    v = float(value)
    if v <= 1.0:
        v *= 100.0
    return round(v, 2)


def _score_percent_int(value: float) -> int:
    """Publish 0–100 scores as integers (round up, matching composite_score)."""
    return int(math.ceil(float(value)))


def _composite_score(
    scientific_grounding_pct: float | None,
    risk_assessment_pct: float | None,
) -> int | None:
    """Higher is better: average grounding with inverted risk; ceil to next integer for output."""
    raw: float | None = None
    if scientific_grounding_pct is not None and risk_assessment_pct is not None:
        raw = (scientific_grounding_pct + (100.0 - risk_assessment_pct)) / 2.0
    elif scientific_grounding_pct is not None:
        raw = float(scientific_grounding_pct)
    elif risk_assessment_pct is not None:
        raw = 100.0 - risk_assessment_pct
    if raw is None:
        return None
    return int(math.ceil(raw))


def _compound_subject_line(names: list[str]) -> str:
    if not names:
        return "Compound(s): (unknown)."
    if len(names) == 1:
        return f"Compound(s): {names[0]}."
    if len(names) == 2:
        return f"Compound(s): {names[0]} and {names[1]}."
    return "Compound(s): " + ", ".join(names[:-1]) + f", and {names[-1]}."


def default_output_path(longevity_input: Path, compound_name: str, run_root: Path | None = None) -> Path:
    if run_root is not None:
        review_parent = run_root / "review"
    else:
        steps = longevity_input.parent
        if steps.name == "steps":
            review_parent = steps.parent / "review"
        else:
            review_parent = steps.parent.parent / "review"
    review_parent.mkdir(parents=True, exist_ok=True)
    return review_parent / "review.json"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _default_groups_path(longevity_input: Path, axis: str) -> Path:
    return longevity_input.parent / f"{axis}_groups.json"


def _default_summaries_path(longevity_input: Path, axis: str) -> Path:
    return longevity_input.parent / f"{axis}_topic_summaries.json"


def _summaries_for_synthesis(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact topic summaries for stage-2 prompts (bullets only, no unit_ids)."""
    out: list[dict[str, Any]] = []
    for s in summaries:
        if not isinstance(s, dict):
            continue
        out.append({
            "topic_id": s.get("topic_id"),
            "topic_label": s.get("topic_label"),
            "bullets": s.get("bullets") or [],
        })
    return out


def _load_topic_summaries(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("summaries"), list):
        return data["summaries"]
    if isinstance(data, list):
        return data
    raise ValueError(f"Expected summaries list or object with 'summaries' in {path}")


def _write_topic_summaries(path: Path, axis: str, compound_name: str, summaries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "axis": axis,
        "compound_name": compound_name,
        "summary_count": len(summaries),
        "summaries": summaries,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  Wrote {len(summaries)} {axis} topic summaries -> {path}", file=sys.stderr)


def _load_groups_required(
    path: Path,
    axis: str,
    *,
    allow_inline: bool,
    rows: list[dict[str, Any]],
    input_path: Path,
) -> dict[str, Any]:
    if path.is_file():
        return load_groups_file(path)
    if allow_inline:
        print(
            f"  {axis}: {path.name} not found — building inline (use topic_grouper.py in production)",
            file=sys.stderr,
        )
        return build_groups_document(rows, axis, input_path)
    raise FileNotFoundError(
        f"Missing {path.name}. Run:\n"
        f"  python3 compounds/pipeline/single/topic_grouper.py {input_path.parent}\n"
        f"Or pass --{axis}-groups PATH, or --allow-inline-grouping for development."
    )


def _build_debug_payloads(
    compound_name: str,
    longevity_doc: dict[str, Any],
    risk_doc: dict[str, Any],
    longevity_groups: list[dict[str, Any]],
    risk_groups: list[dict[str, Any]],
    longevity_units: list[dict[str, Any]],
    risk_units: list[dict[str, Any]],
) -> dict[str, Any]:
    stage1_longevity = [
        {
            "topic_id": g.get("topic_id"),
            "topic_label": g.get("topic_label"),
            "unit_count": len(g.get("units") or []),
            "payload": {
                "compound_name": compound_name,
                "topic_id": g.get("topic_id"),
                "topic_label": g.get("topic_label"),
                "units": g.get("units") or [],
            },
        }
        for g in longevity_groups
    ]
    stage1_risk = [
        {
            "topic_id": g.get("topic_id"),
            "topic_label": g.get("topic_label"),
            "unit_count": len(g.get("units") or []),
            "payload": {
                "compound_name": compound_name,
                "topic_id": g.get("topic_id"),
                "topic_label": g.get("topic_label"),
                "units": g.get("units") or [],
            },
        }
        for g in risk_groups
    ]
    empty_summaries: list[dict[str, Any]] = []
    return {
        "compound_name": compound_name,
        "stage0_groups": {
            "longevity_doc": longevity_doc,
            "risk_doc": risk_doc,
        },
        "stage1_topic_summary_calls": {
            "longevity": stage1_longevity,
            "risk": stage1_risk,
        },
        "stage2_synthesis": {
            "scientific_grounding": build_grounding_payload(
                compound_name, longevity_units, empty_summaries,
            ),
            "risk": build_risk_payload(compound_name, risk_units, empty_summaries),
            "review_statement": build_statement_context(
                compound_name,
                longevity_doc,
                risk_doc,
                "<scientific_grounding paragraph>",
                "<risk paragraph>",
            ),
        },
    }


def _tag_counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        val = row.get(key)
        if isinstance(val, str):
            counts[val] = counts.get(val, 0) + 1
    return counts


def scientific_grounding_score(longevity_rows: list[dict[str, Any]]) -> float | None:
    direct = sum(1 for row in longevity_rows if row.get("longevity_relevance") == "direct_longevity")
    indirect = sum(1 for row in longevity_rows if row.get("longevity_relevance") == "indirect_longevity_mechanism")
    total = direct + indirect
    if total == 0:
        return None
    return round((direct + 0.65 * indirect) / total, 2)


def aggregate_risk_score(risk_rows: list[dict[str, Any]]) -> float | None:
    weights = {
        "direct_human_safety": 0.75,
        "interaction_or_combination_risk": 0.7,
        "toxicity_or_adverse_signal": 0.6,
        "pharmacology_risk_theoretical": 0.35,
    }
    vals = [weights[tag] for row in risk_rows if isinstance((tag := row.get("risk_relevance")), str) and tag in weights]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def _coverage_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    present: set[str] = set()
    for row in rows:
        st = row.get("source_type")
        if isinstance(st, str):
            mapped = _SOURCE_TO_COVERAGE.get(st)
            if mapped:
                present.add(mapped)
    return {name: {"present": name in present} for name in COVERAGE_SOURCES}


def load_groups_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _parse_topic_summary(raw: str, group: dict[str, Any]) -> dict[str, Any]:
    text = strip_reasoning_markup(raw)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("bullets"):
            return parsed
    except json.JSONDecodeError:
        pass
    bullets = [line.lstrip("- ").strip() for line in text.splitlines() if line.strip()]
    unit_ids = [u.get("unit_id") for u in group.get("units") or [] if isinstance(u, dict)]
    return {
        "topic_id": group.get("topic_id"),
        "topic_label": group.get("topic_label"),
        "bullets": bullets or ["No summary produced for this topic group."],
        "unit_ids": [x for x in unit_ids if x],
    }


def summarize_topic_groups(
    client: OpenAI,
    model: str,
    compound_name: str,
    groups: list[dict[str, Any]],
    summary_prompt: str,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for i, group in enumerate(groups, start=1):
        payload = {
            "compound_name": compound_name,
            "topic_id": group.get("topic_id"),
            "topic_label": group.get("topic_label"),
            "units": group.get("units") or [],
        }
        print(
            f"    topic {i}/{len(groups)}: {group.get('topic_id')} ({len(payload['units'])} units)",
            file=sys.stderr,
        )
        raw = call_llm(client, model, summary_prompt, json.dumps(payload, ensure_ascii=False))
        summaries.append(_parse_topic_summary(raw, group))
    return summaries


def build_grounding_payload(
    compound_name: str,
    longevity_rows: list[dict[str, Any]],
    topic_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "compound_name": compound_name,
        "coverage": _coverage_from_rows(longevity_rows),
        "tag_counts": _tag_counts(longevity_rows, "longevity_relevance"),
        "topic_summaries": _summaries_for_synthesis(topic_summaries),
    }


def build_risk_payload(
    compound_name: str,
    risk_rows: list[dict[str, Any]],
    topic_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "compound_name": compound_name,
        "coverage": _coverage_from_rows(risk_rows),
        "tag_counts": _tag_counts(risk_rows, "risk_relevance"),
        "topic_summaries": _summaries_for_synthesis(topic_summaries),
    }


def build_statement_context(
    compound_name: str,
    longevity_doc: dict[str, Any],
    risk_doc: dict[str, Any],
    grounding_text: str,
    risk_text: str,
) -> dict[str, Any]:
    longevity_rows = longevity_doc.get("units") or []
    risk_rows = risk_doc.get("units") or []
    return {
        "compound_name": compound_name,
        "scientific_grounding": grounding_text,
        "risk": risk_text,
        "evidence_summary": {
            "longevity_topic_groups": longevity_doc.get("group_count", 0),
            "risk_topic_groups": risk_doc.get("group_count", 0),
            "longevity_units": len(longevity_rows),
            "risk_units": len(risk_rows),
            "longevity_tag_counts": _tag_counts(longevity_rows, "longevity_relevance"),
            "risk_tag_counts": _tag_counts(risk_rows, "risk_relevance"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a longevity review (scientific_grounding + risk + review_statement) "
            "from longevity.json and risk.json via LLM."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="longevity.json (JSONL from tag-group-filter.py).",
    )
    parser.add_argument(
        "--risk",
        type=Path,
        default=None,
        metavar="PATH",
        help="risk.json (default: sibling risk.json next to longevity input).",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output JSON path (default: <run-root>/review/review.json).",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="Run root reviews/compounds/<TICKER>/ (default: inferred from input path).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="NAME",
        help="Model id (default: auto-discovered or REVIEWER_MODEL/TAGGER_MODEL/CLASSIFIER_MODEL/VALIDATOR_MODEL).",
    )
    parser.add_argument(
        "--compound",
        type=str,
        default=None,
        help="Compound name (default: material.json compound_name row).",
    )
    parser.add_argument(
        "--longevity-groups",
        type=Path,
        default=None,
        metavar="PATH",
        help="longevity_groups.json (default: sibling of longevity.json).",
    )
    parser.add_argument(
        "--risk-groups",
        type=Path,
        default=None,
        metavar="PATH",
        help="risk_groups.json (default: sibling of longevity.json).",
    )
    parser.add_argument(
        "--allow-inline-grouping",
        action="store_true",
        help="Build groups in-process if *_groups.json is missing (dev only).",
    )
    parser.add_argument(
        "--skip-group-summaries",
        action="store_true",
        help="Load longevity/risk_topic_summaries.json instead of LLM per-group calls.",
    )
    parser.add_argument(
        "--debug-payloads",
        action="store_true",
        help="Print stage-0/1/2 payloads to stderr and exit without calling the LLM.",
    )
    args = parser.parse_args()

    in_path: Path = args.input.expanduser().resolve()
    compound_dir = in_path.parent

    risk_path = args.risk.expanduser().resolve() if args.risk else compound_dir / "risk.json"
    longevity_groups_path = (
        args.longevity_groups.expanduser().resolve()
        if args.longevity_groups
        else _default_groups_path(in_path, "longevity")
    )
    risk_groups_path = (
        args.risk_groups.expanduser().resolve()
        if args.risk_groups
        else _default_groups_path(in_path, "risk")
    )

    print(f"Loading longevity: {in_path}", file=sys.stderr)
    longevity_rows = load_jsonl(in_path)
    print(f"Loading risk: {risk_path}", file=sys.stderr)
    risk_rows = load_jsonl(risk_path) if risk_path.is_file() else []

    compound_name = resolve_compound_name(
        compound_dir,
        longevity_rows + risk_rows,
        override=args.compound,
    )

    print(
        f"  compound={compound_name!r}  "
        f"longevity_rows={len(longevity_rows)}  "
        f"risk_rows={len(risk_rows)}",
        file=sys.stderr,
    )

    topic_summary_prompt = TOPIC_SUMMARY_PROMPT_PATH.read_text(encoding="utf-8")
    grounding_prompt = GROUNDING_PROMPT_PATH.read_text(encoding="utf-8")
    risk_prompt = RISK_PROMPT_PATH.read_text(encoding="utf-8")
    statement_prompt = STATEMENT_PROMPT_PATH.read_text(encoding="utf-8")

    try:
        longevity_doc = _load_groups_required(
            longevity_groups_path,
            "longevity",
            allow_inline=args.allow_inline_grouping,
            rows=longevity_rows,
            input_path=in_path,
        )
        if risk_rows or risk_groups_path.is_file():
            risk_doc = _load_groups_required(
                risk_groups_path,
                "risk",
                allow_inline=args.allow_inline_grouping,
                rows=risk_rows,
                input_path=risk_path,
            )
        else:
            risk_doc = {"groups": [], "units": [], "group_count": 0, "unit_count": 0}
    except FileNotFoundError as e:
        print(f"review.py: {e}", file=sys.stderr)
        return 1

    longevity_groups = longevity_doc.get("groups") or []
    risk_groups = risk_doc.get("groups") or []
    longevity_units = longevity_doc.get("units") or []
    risk_units = risk_doc.get("units") or []

    print(
        f"  longevity_groups={len(longevity_groups)}  risk_groups={len(risk_groups)}",
        file=sys.stderr,
    )

    if args.debug_payloads:
        debug = _build_debug_payloads(
            compound_name,
            longevity_doc,
            risk_doc,
            longevity_groups,
            risk_groups,
            longevity_units,
            risk_units,
        )
        print(json.dumps(debug, ensure_ascii=False, indent=2), file=sys.stderr)
        return 0

    if OpenAI is None:
        print("review.py: missing dependency: openai. Install it to run review generation.", file=sys.stderr)
        return 1

    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

    model = args.model or get_available_model(
        client,
        "REVIEWER_MODEL",
        ["TAGGER_MODEL", "CLASSIFIER_MODEL", "VALIDATOR_MODEL"],
    )

    longevity_summaries_path = _default_summaries_path(in_path, "longevity")
    risk_summaries_path = _default_summaries_path(in_path, "risk")

    if args.skip_group_summaries:
        if not longevity_summaries_path.is_file():
            print(
                f"review.py: --skip-group-summaries requires {longevity_summaries_path}",
                file=sys.stderr,
            )
            return 1
        longevity_summaries = _load_topic_summaries(longevity_summaries_path)
        print(
            f"  Loaded {len(longevity_summaries)} longevity summaries from {longevity_summaries_path}",
            file=sys.stderr,
        )
        if risk_groups:
            if not risk_summaries_path.is_file():
                print(
                    f"review.py: --skip-group-summaries requires {risk_summaries_path}",
                    file=sys.stderr,
                )
                return 1
            risk_summaries = _load_topic_summaries(risk_summaries_path)
            print(
                f"  Loaded {len(risk_summaries)} risk summaries from {risk_summaries_path}",
                file=sys.stderr,
            )
        else:
            risk_summaries = []
    else:
        print("  [1/N] Summarizing longevity topic groups...", file=sys.stderr)
        longevity_summaries = summarize_topic_groups(
            client, model, compound_name, longevity_groups, topic_summary_prompt,
        )
        _write_topic_summaries(
            longevity_summaries_path, "longevity", compound_name, longevity_summaries,
        )

        print("  [2/N] Summarizing risk topic groups...", file=sys.stderr)
        risk_summaries = summarize_topic_groups(
            client, model, compound_name, risk_groups, topic_summary_prompt,
        )
        _write_topic_summaries(risk_summaries_path, "risk", compound_name, risk_summaries)

    print("  [3/N] Generating scientific_grounding...", file=sys.stderr)
    grounding_payload = build_grounding_payload(compound_name, longevity_units, longevity_summaries)
    grounding_text = call_llm(
        client,
        model,
        grounding_prompt,
        json.dumps(grounding_payload, ensure_ascii=False),
    )

    sg_score = scientific_grounding_score(longevity_units)
    risk_score = aggregate_risk_score(risk_units)
    risk_pct = _fraction_to_percent(risk_score)
    if risk_pct is None:
        risk_pct = DEFAULT_RISK_SCORE_PCT

    print("  [4/N] Generating risk statement...", file=sys.stderr)
    risk_payload = build_risk_payload(compound_name, risk_units, risk_summaries)
    risk_text = call_llm(
        client,
        model,
        risk_prompt,
        json.dumps(risk_payload, ensure_ascii=False),
    )

    print("  [5/N] Generating review_statement...", file=sys.stderr)
    statement_ctx = build_statement_context(
        compound_name,
        longevity_doc,
        risk_doc,
        grounding_text,
        risk_text,
    )
    review_statement = call_llm(
        client,
        model,
        statement_prompt,
        json.dumps(statement_ctx, ensure_ascii=False),
    )

    _now = datetime.now(timezone.utc)
    review_date = f"{_now.strftime('%B')} {_now.day}, {_now.year}"

    sg_pct = _fraction_to_percent(sg_score)
    if sg_pct is None:
        sg_pct = DEFAULT_GROUNDING_SCORE_PCT
    composite = _composite_score(sg_pct, risk_pct)

    subject = _compound_subject_line([compound_name])
    stmt = (review_statement or "").strip()
    review_statement_out = f"{subject} {stmt}".strip() if stmt else subject

    run_root = args.run_root.expanduser().resolve() if args.run_root else None
    out_path: Path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else default_output_path(in_path, compound_name, run_root=run_root)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    review = {
        "research_name": compound_name,
        "review_date": review_date,
        "composite_score": composite,
        "review_statement": review_statement_out,
        "categories": {
            "scientific_grounding": {
                "score": _score_percent_int(sg_pct),
                "rationale": grounding_text,
            },
            "risk_assessment": {
                "score": _score_percent_int(risk_pct),
                "rationale": risk_text,
            },
        },
    }

    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(review, fh, ensure_ascii=False, indent=2)

    print(f"Done. Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
