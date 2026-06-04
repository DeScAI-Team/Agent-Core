#!/usr/bin/env python3
r"""Generate a four-category combination review from an interactions.py evidence bundle via vLLM.

Reads the JSON output of ``interactions.py`` and calls the LLM four times:

1. ``prompts/pump-science-combination-scientific-grounding-evaluation.md``
   Receives per-compound {name, score, grounding_rationale}.
   Produces a combined ``scientific_grounding`` paragraph.

2. ``prompts/pump-science-combination-risk-statement-evaluation.md``
   Receives per-compound {name, risk_rationale, spl_available, spl_interaction_excerpts}.
   Produces a combined ``risk`` paragraph.

3. ``prompts/pump-science-compatibility-evaluation.md``
   Receives the cross_reference bundle + per-compound {kegg_flags_present, spl_interaction_excerpts,
   mechanism_snippets}.
   Produces the ``compatibility`` paragraph.

4. ``prompts/pump-science-combination-review-statement-evaluation.md``
   Receives a compact bundle (all three paragraphs + scores + combination metadata).
   Produces the final ``review_statement`` paragraph.

Output is written as ``<repo>/reviews/compounds/<combination>/<combination>-combo-review.json``
by default, or to a path supplied via ``-o``. The JSON shape matches article ``overview.json``
(fewer fields): ``compound_token``, ``review_date``, ``composite_score`` (0–100),
``review_statement`` (with a ``Compound(s): …`` prefix), and ``categories`` with
percent scores (0–100) plus rationales.

Usage:
  python review-multiple.py reviews/compounds/OMIGU/steps/omipa-ginse-uroli-bundle.json
  python review-multiple.py bundle.json -o my-review.json --model mixtral-8x7b-instruct
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

import sys

from openai import OpenAI, RateLimitError

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
PROMPTS_DIR = REPO_ROOT / "prompts"

_PROMPT_GROUNDING = PROMPTS_DIR / "pump-science-combination-scientific-grounding-evaluation.md"
_PROMPT_RISK = PROMPTS_DIR / "pump-science-combination-risk-statement-evaluation.md"
_PROMPT_COMPAT = PROMPTS_DIR / "pump-science-compatibility-evaluation.md"
_PROMPT_STATEMENT = PROMPTS_DIR / "pump-science-combination-review-statement-evaluation.md"

DEFAULT_RISK_SCORE_PCT = 25.0

# Model discovery happens later in main() after client is created
MAX_RETRIES = 4
MAX_REVIEWER_TOKENS = max(256, int(os.environ.get("REVIEWER_MAX_TOKENS", "2048")))

_END_THINK_MARKERS = (
    "</think>",
    "</think>",
    "</thinking>",
    "</reasoning>",
    "</thought>",
)


# ---------------------------------------------------------------------------
# LLM helpers (identical contract to review.py)
# ---------------------------------------------------------------------------

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

    extra_body: dict[str, object] = {
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


# ---------------------------------------------------------------------------
# Context builders — one per LLM pass
# ---------------------------------------------------------------------------

def _build_grounding_ctx(bundle: dict) -> dict:
    """Pass 1: per-compound grounding scores and rationales."""
    compounds_list = []
    for name, ev in bundle.get("compounds", {}).items():
        compounds_list.append({
            "compound_name": name,
            "scientific_grounding_score": ev.get("scientific_grounding_score"),
            "scientific_grounding_rationale": ev.get("scientific_grounding_rationale") or "",
        })
    return {
        "combination_name": bundle.get("combination_name", "Unknown combination"),
        "compounds": compounds_list,
    }


def _interaction_snippets(ev: dict) -> list[str]:
    return [
        s for s in (
            (iu.get("snippet") or "") for iu in (ev.get("interaction_evidence") or [])
            if isinstance(iu, dict)
        )
        if isinstance(s, str) and s.strip()
    ][:5]


def _build_risk_ctx(bundle: dict) -> dict:
    """Pass 2: per-compound risk rationales and SPL / tagged interaction excerpts."""
    compounds_list = []
    for name, ev in bundle.get("compounds", {}).items():
        spl = ev.get("spl") or {}
        compounds_list.append({
            "compound_name": name,
            "risk_rationale": ev.get("risk_rationale") or "",
            "spl_available": spl.get("label_matched", False),
            "spl_interaction_excerpts": spl.get("interaction_excerpts") or [],
            "interaction_evidence": _interaction_snippets(ev),
            "risk_tagged_count": ev.get("risk_tagged_count") or 0,
        })
    return {
        "combination_name": bundle.get("combination_name", "Unknown combination"),
        "ticker": bundle.get("ticker"),
        "intervention": bundle.get("intervention"),
        "compounds": compounds_list,
    }


def _build_compat_ctx(bundle: dict) -> dict:
    """Pass 3: cross-reference bundle + per-compound pathway/mechanism context."""
    compounds_list = []
    for name, ev in bundle.get("compounds", {}).items():
        spl = ev.get("spl") or {}
        kegg = ev.get("kegg") or {}
        mechanism_snippets: list[str] = []
        for summary in ev.get("longevity_topic_summaries") or []:
            if isinstance(summary, dict):
                for bullet in summary.get("bullets") or []:
                    if isinstance(bullet, str) and bullet.strip():
                        mechanism_snippets.append(bullet.strip())
        for mu in ev.get("longevity_evidence") or ev.get("mechanism_units") or []:
            if isinstance(mu, dict) and mu.get("snippet"):
                mechanism_snippets.append(str(mu["snippet"]))
        mechanism_snippets = mechanism_snippets[:8]
        compounds_list.append({
            "compound_name": name,
            "kegg_flags_present": kegg.get("flags_present") or [],
            "spl_available": spl.get("label_matched", False),
            "spl_interaction_excerpts": spl.get("interaction_excerpts") or [],
            "mechanism_snippets": mechanism_snippets,
            "interaction_evidence": _interaction_snippets(ev),
        })

    xref = bundle.get("cross_reference") or {}
    return {
        "combination_name": bundle.get("combination_name", "Unknown combination"),
        "ticker": bundle.get("ticker"),
        "intervention": bundle.get("intervention"),
        "compounds": compounds_list,
        "cross_reference": {
            "shared_pathways": xref.get("shared_pathways") or [],
            "explicit_mentions": xref.get("explicit_mentions") or [],
            "spl_coverage_summary": xref.get("spl_coverage_summary") or "",
        },
    }


def _build_statement_ctx(
    bundle: dict,
    grounding_text: str,
    risk_text: str,
    compat_text: str,
) -> dict:
    """Pass 4: compact bundle — all three paragraphs + aggregate metadata."""
    compounds_scores = [
        {
            "compound_name": name,
            "scientific_grounding_score": ev.get("scientific_grounding_score"),
            "risk_score": ev.get("risk_score"),
        }
        for name, ev in bundle.get("compounds", {}).items()
    ]

    # Aggregate coverage: collect coverage dicts across compounds (may be empty for some).
    # We report which fields were present in at least one compound.
    coverage_union: dict = {}
    for ev in bundle.get("compounds", {}).values():
        for field, info in (ev.get("coverage") or {}).items():
            if info.get("present") and field not in coverage_union:
                coverage_union[field] = True

    return {
        "combination_name": bundle.get("combination_name", "Unknown combination"),
        "ticker": bundle.get("ticker"),
        "intervention": bundle.get("intervention"),
        "compounds": compounds_scores,
        "total_units": None,
        "coverage": coverage_union or None,
        "scientific_grounding": grounding_text,
        "risk": risk_text,
        "compatibility": compat_text,
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _default_output_path(in_path: Path, combination_name: str, run_root: Path | None = None) -> Path:
    if run_root is not None:
        review_dir = run_root / "review"
    else:
        # bundle lives in steps/
        steps = in_path.parent
        review_dir = steps.parent / "review" if steps.name == "steps" else steps / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    return review_dir / "review.json"


def _avg_score(bundle: dict, field: str) -> float | None:
    scores = [
        ev.get(field)
        for ev in bundle.get("compounds", {}).values()
        if ev.get(field) is not None
    ]
    return round(sum(scores) / len(scores), 4) if scores else None


def _fraction_to_percent(value: float | None) -> float | None:
    """Scores from bundles are 0–1; overview-style output uses 0–100."""
    if value is None:
        return None
    v = float(value)
    if v <= 1.0:
        v *= 100.0
    return round(v, 2)


def _score_percent_int(value: float) -> int:
    return int(math.ceil(float(value)))


def _combo_compound_token(bundle: dict[str, Any], compound_names: list[str]) -> str:
    """Display id for combo reviews, e.g. ``OMIGU (Omipalisib + Ginsenoside Rh2 + Urolithin A)``."""
    joined = " + ".join(compound_names)
    ticker = str(bundle.get("ticker") or "").strip()
    if ticker:
        return f"{ticker} ({joined})"
    return joined


def _compound_subject_line(names: list[str]) -> str:
    if not names:
        return "Compound(s): (unknown)."
    if len(names) == 1:
        return f"Compound(s): {names[0]}."
    if len(names) == 2:
        return f"Compound(s): {names[0]} and {names[1]}."
    return "Compound(s): " + ", ".join(names[:-1]) + f", and {names[-1]}."


def _compat_signal_preamble(bundle: dict) -> str:
    xref = bundle.get("cross_reference") or {}
    sp = xref.get("shared_pathways") or []
    em = xref.get("explicit_mentions") or []
    if not isinstance(sp, list):
        sp = []
    if not isinstance(em, list):
        em = []
    if sp:
        sp_part = "Shared KEGG longevity pathway flags (hypothesis-level overlap): " + ", ".join(str(x) for x in sp) + ". "
    else:
        sp_part = "No shared KEGG longevity pathway flags between compounds. "
    return sp_part + f"Explicit name-token mentions of partner compounds in SPL/mechanism text: {len(em)}. "


def _compatibility_percent_score(bundle: dict) -> float:
    xref = bundle.get("cross_reference") or {}
    sp = xref.get("shared_pathways") or []
    em = xref.get("explicit_mentions") or []
    n_sp = len(sp) if isinstance(sp, list) else 0
    n_em = len(em) if isinstance(em, list) else 0
    raw = 100.0 - (n_sp * 14.0 + n_em * 12.0)
    return round(max(42.0, min(100.0, raw)), 2)


def _mean_of_numbers(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _stitched_per_compound_rationales(
    bundle: dict,
    rationale_key: str,
    score_key: str,
) -> str:
    """Deterministic fallback when combination LLM returns empty text."""
    blocks: list[str] = []
    compounds = bundle.get("compounds")
    if not isinstance(compounds, dict):
        return ""
    for name in sorted(compounds.keys()):
        ev = compounds[name]
        if not isinstance(ev, dict):
            continue
        rat = (ev.get(rationale_key) or "").strip()
        if not rat:
            continue
        score = ev.get(score_key)
        if score is not None:
            blocks.append(f"{name} (score {score}): {rat}")
        else:
            blocks.append(f"{name}: {rat}")
    return "\n\n".join(blocks)


def _llm_text_or_fallback(
    client: OpenAI,
    model: str,
    prompt: str,
    ctx_json: str,
    fallback: str,
    label: str,
) -> str:
    text = call_llm(client, model, prompt, ctx_json).strip()
    if text:
        return text
    fb = fallback.strip()
    if fb:
        print(
            f"  WARN: {label} LLM returned empty; using per-compound rationales from bundle.",
            file=sys.stderr,
        )
        return fb
    print(f"  WARN: {label} LLM returned empty and no bundle fallback available.", file=sys.stderr)
    return ""


def _fallback_review_statement(bundle: dict, compound_names: list[str]) -> str:
    parts: list[str] = []
    compounds = bundle.get("compounds")
    if isinstance(compounds, dict):
        for name in compound_names:
            ev = compounds.get(name)
            if not isinstance(ev, dict):
                continue
            stmt = (ev.get("review_statement") or "").strip()
            if stmt:
                parts.append(stmt)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a four-category combination review "
            "(scientific_grounding + risk + compatibility + review_statement) "
            "from an interactions.py evidence bundle via vLLM."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="JSON evidence bundle produced by interactions.py.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output JSON path (default: <repo>/reviews/compounds/<combination>/<combination>-combo-review.json).",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="Run root reviews/compounds/<TICKER>/ (default: inferred from bundle path).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="NAME",
        help="Model id (default: auto-discovered or REVIEWER_MODEL/TAGGER_MODEL/CLASSIFIER_MODEL/VALIDATOR_MODEL).",
    )
    args = parser.parse_args()

    in_path: Path = args.input.expanduser().resolve()

    print(f"Loading {in_path}", file=sys.stderr)
    try:
        bundle = json.loads(in_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: Cannot read input bundle: {exc}", file=sys.stderr)
        return 1

    combination_name: str = bundle.get("combination_name") or "Unknown combination"
    compound_names = list(bundle.get("compounds", {}).keys())

    print(f"  combination: {combination_name}", file=sys.stderr)
    print(f"  compounds:   {', '.join(compound_names)}", file=sys.stderr)

    # Check that per-compound review data is present.
    missing_review = [
        name for name, ev in bundle.get("compounds", {}).items()
        if not ev.get("scientific_grounding_rationale") and not ev.get("risk_rationale")
    ]
    if missing_review:
        print(
            f"  WARN: missing review data for: {missing_review}. "
            "Run review.py for each compound before review-multiple.py.",
            file=sys.stderr,
        )

    grounding_prompt = _PROMPT_GROUNDING.read_text(encoding="utf-8")
    risk_prompt = _PROMPT_RISK.read_text(encoding="utf-8")
    compat_prompt = _PROMPT_COMPAT.read_text(encoding="utf-8")
    statement_prompt = _PROMPT_STATEMENT.read_text(encoding="utf-8")

    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    
    # Discover model
    model = args.model or get_available_model(
        client,
        "REVIEWER_MODEL",
        ["TAGGER_MODEL", "CLASSIFIER_MODEL", "VALIDATOR_MODEL"]
    )

    grounding_fb = _stitched_per_compound_rationales(
        bundle, "scientific_grounding_rationale", "scientific_grounding_score"
    )
    risk_fb = _stitched_per_compound_rationales(bundle, "risk_rationale", "risk_score")

    # ---- Pass 1: combined scientific grounding ----
    print("  [1/4] Generating combined scientific_grounding...", file=sys.stderr)
    grounding_ctx = _build_grounding_ctx(bundle)
    grounding_text = _llm_text_or_fallback(
        client,
        model,
        grounding_prompt,
        json.dumps(grounding_ctx, ensure_ascii=False),
        grounding_fb,
        "scientific_grounding",
    )

    # ---- Pass 2: combined risk statement ----
    print("  [2/4] Generating combined risk statement...", file=sys.stderr)
    risk_ctx = _build_risk_ctx(bundle)
    risk_text = _llm_text_or_fallback(
        client,
        model,
        risk_prompt,
        json.dumps(risk_ctx, ensure_ascii=False),
        risk_fb,
        "risk_assessment",
    )

    # ---- Pass 3: compatibility ----
    print("  [3/4] Generating compatibility assessment...", file=sys.stderr)
    compat_ctx = _build_compat_ctx(bundle)
    compat_text = call_llm(
        client, model, compat_prompt, json.dumps(compat_ctx, ensure_ascii=False)
    ).strip()
    if not compat_text:
        compat_text = (
            "Compatibility assessment could not be generated from the LLM. "
            "Inspect shared_pathways and explicit_mentions in the evidence bundle."
        )
        print("  WARN: compatibility LLM returned empty; using placeholder.", file=sys.stderr)

    # ---- Pass 4: combined review statement ----
    print("  [4/4] Generating combined review_statement...", file=sys.stderr)
    statement_ctx = _build_statement_ctx(bundle, grounding_text, risk_text, compat_text)
    review_statement = call_llm(
        client, model, statement_prompt, json.dumps(statement_ctx, ensure_ascii=False)
    ).strip()
    if not review_statement:
        review_statement = _fallback_review_statement(bundle, compound_names)
        if review_statement:
            print(
                "  WARN: review_statement LLM returned empty; stitched from per-compound statements.",
                file=sys.stderr,
            )

    _now = datetime.now(timezone.utc)
    review_date = f"{_now.strftime('%B')} {_now.day}, {_now.year}"

    run_root = args.run_root.expanduser().resolve() if args.run_root else None
    out_path: Path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else _default_output_path(in_path, combination_name, run_root=run_root)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sg_pct = _fraction_to_percent(_avg_score(bundle, "scientific_grounding_score"))
    risk_pct = _fraction_to_percent(_avg_score(bundle, "risk_score"))
    if risk_pct is None:
        risk_pct = DEFAULT_RISK_SCORE_PCT
    compat_pct = _compatibility_percent_score(bundle)
    compat_rationale = _compat_signal_preamble(bundle) + (compat_text or "").strip()

    composite_raw = _mean_of_numbers([sg_pct, risk_pct, compat_pct])
    composite = int(math.ceil(composite_raw)) if composite_raw is not None else None

    subject = _compound_subject_line(compound_names)
    stmt = (review_statement or "").strip()
    review_statement_out = f"{subject} {stmt}".strip() if stmt else subject

    compound_token = _combo_compound_token(bundle, compound_names)

    review = {
        "compound_token": compound_token,
        "review_date": review_date,
        "composite_score": composite,
        "review_statement": review_statement_out,
        "categories": {
            "scientific_grounding": {
                "score": _score_percent_int(sg_pct) if sg_pct is not None else 50,
                "rationale": grounding_text,
            },
            "risk_assessment": {
                "score": _score_percent_int(risk_pct),
                "rationale": risk_text,
            },
            "compatibility": {
                "score": _score_percent_int(compat_pct),
                "rationale": compat_rationale,
            },
        },
    }

    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(review, fh, ensure_ascii=False, indent=2)

    print(f"Done. Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
