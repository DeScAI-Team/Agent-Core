#!/usr/bin/env python3
"""Tag material JSONL, then write filtered ``longevity.json`` and ``risk.json``.

Input is one JSON object per line, e.g. ``material.json``. Outputs are also
JSONL rows, named ``.json`` to match the material-file convention.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI, RateLimitError
except ModuleNotFoundError:
    OpenAI = None  # type: ignore[assignment]

    class RateLimitError(Exception):  # type: ignore[no-redef]
        pass

_COMPOUNDS_DIR = Path(__file__).resolve().parents[2]
_SINGLE_DIR = Path(__file__).resolve().parent
if str(_COMPOUNDS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS_DIR))
if str(_SINGLE_DIR) not in sys.path:
    sys.path.insert(0, str(_SINGLE_DIR))
from discover_lib.dedupe import dedupe_key_for_row  # noqa: E402
from discover_lib.material import load_material_records  # noqa: E402
from llm_env import TAGGER_API_KEY, TAGGER_BASE_URL  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "prompts" / "compound-excerpt-tagging.md"
PROMPT_RISK_PATH = REPO_ROOT / "prompts" / "compound-risk-profile.md"

MAX_RETRIES = 4
MAX_TAGGER_TOKENS = max(128, int(os.environ.get("TAGGER_MAX_TOKENS", "2048")))
MAX_RISK_PARSE_ATTEMPTS = max(1, int(os.environ.get("TAGGER_RISK_RETRIES", "3")))

_RISK_TOKEN_ALIASES = {"negligble": "negligible"}
_NO_RISK_TAGS = {None, "no_risk_signal", "not_relevant"}
_BACKGROUND_SOURCE_TYPES = {
    "compound_name",
    "discover_metadata",
    "discover_round_meta",
    "europe_pmc_query_stats",
    "clinical_trials_meta",
    "kegg_summary",
    "chembl_molecule",
    "pubchem_bioassays_meta",
    "openalex_grounding_meta",
    "openalex_risk_meta",
}
_RISK_PRIORITY = {
    None: 0,
    "not_relevant": 0,
    "no_risk_signal": 0,
    "pharmacology_risk_theoretical": 1,
    "toxicity_or_adverse_signal": 2,
    "direct_human_safety": 3,
    "interaction_or_combination_risk": 4,
}
_DIRECT_LONGEVITY_RE = re.compile(
    r"\b(lifespan|life span|longevity|healthspan|health span|anti[- ]?aging|antiaging|"
    r"chronological aging|replicative aging|age[- ]related decline)\b",
    re.I,
)
_INDIRECT_LONGEVITY_RE = re.compile(
    r"\b(senescence|sasp|autophagy|mitochondri|mTOR|AMPK|sirtuin|SIRT\d*|NAD\+?|"
    r"inflammaging|oxidative stress|reactive oxygen species|proteostasis|telomere|"
    r"insulin/IGF|IGF-?1|stem cell|immune aging)\b",
    re.I,
)
_LONGEVITY_REVIEW_CONTEXT_RE = re.compile(
    r"\b(lifespan|life span|longevity|healthspan|health span|anti[- ]?aging|antiaging|"
    r"chronological aging|replicative aging|age[- ]related|cellular senescence|"
    r"frailty|geriatric|healthspan extension|lifespan extension)\b",
    re.I,
)
_ONCOLOGY_FRAME_RE = re.compile(
    r"\b(cancer|carcinoma|tumor|tumour|oncolog|neoplasm|malignan|cytotoxic|antitumor|anti-tumor|"
    r"chemotherapy|radiotherapy|xenograft|cell lines?|hepatoma|melanoma|glioma|leukemia|"
    r"NSCLC|breast cancer|colorectal)\b",
    re.I,
)
_ASSAY_RISK_SOURCE_TYPES = frozenset({
    "chembl_mechanism",
    "chembl_activity",
    "pubchem_bioassay",
})
_OPENFDA_RISK_SOURCE_TYPES = frozenset({"openfda_faers", "openfda_drug_label"})
_INTERACTION_RISK_RE = re.compile(
    r"\b(co-?administration|co-?administered|co-?treatment|combination therapy|combined with|"
    r"drug interaction|drug-drug interaction|synerg|potentiat|CYP\d|cytochrome p450|"
    r"P-?gp|p-glycoprotein|transporter|QT|qtc|torsade|anticoagul|bleeding|platelet|"
    r"warfarin|statin)\b",
    re.I,
)
_COMBINATION_TREATMENT_RE = re.compile(
    r"\b(combined with|combination|co-?treatment|co-?administered|co-?administration)\b"
    r"[\s\S]{0,160}\b(chemotherapy|radiotherapy|cisplatin|CDDP|doxorubicin|platinum)\b|"
    r"\b(chemotherapy|radiotherapy|cisplatin|CDDP|doxorubicin|platinum)\b"
    r"[\s\S]{0,160}\b(combined with|combination|co-?treatment|co-?administered|co-?administration)\b",
    re.I,
)
_DIRECT_HUMAN_SAFETY_RE = re.compile(
    r"\b(label|boxed warning|contraindicat|warning|precaution|adverse reaction|"
    r"adverse event|clinical safety|tolerability|pregnancy|lactation|dose adjustment|"
    r"hepatic impairment|renal impairment)\b",
    re.I,
)
_LITERATURE_TOXICITY_RE = re.compile(
    r"\b(toxicity|toxic|cytotoxic|hepatotoxic|nephrotoxic|cardiotoxic|neurotoxic|"
    r"genotoxic|teratogenic|mutagenic|adverse event|adverse reaction|side effect|"
    r"side-effect|death|fatal|dose-limiting|organ toxicity|FAERS|surveillance)\b",
    re.I,
)
_ASSAY_TOXICITY_RE = re.compile(
    r"\b(toxicity|toxic|cytotoxic|hepatotoxic|nephrotoxic|cardiotoxic|neurotoxic|"
    r"genotoxic|teratogenic|mutagenic|adverse|side effect|side-effect|death|fatal|"
    r"body weight|weight loss|cell viability|growth inhibition|inhibition of cell proliferation|"
    r"apoptosis|necrosis|IC50|GI50|LD50|dose-limiting|organ toxicity|FAERS|surveillance)\b",
    re.I,
)
_RULE_IGNORED_TEXT_KEYS = {
    "api_versions",
    "failures",
    "meta",
    "query",
    "query_names",
    "source_queries",
    "search_term",
    "search_terms",
}
_END_THINK_MARKERS = (
    "</think>",
    "</think>",
    "</thinking>",
    "</reasoning>",
    "</thought>",
)
LONGEVITY_GROUP_TAGS = frozenset({"direct_longevity", "indirect_longevity_mechanism"})
RISK_GROUP_TAGS = frozenset({
    "toxicity_or_adverse_signal",
    "interaction_or_combination_risk",
    "direct_human_safety",
    "pharmacology_risk_theoretical",
})


def get_available_model(client: Any, env_var_name: str, fallback_env_vars: list[str]) -> str:
    try:
        models = client.models.list()
        if models.data:
            model_id = models.data[0].id
            print(f"  Auto-discovered model: {model_id}", file=sys.stderr)
            return model_id
    except Exception as exc:
        print(f"  Could not auto-discover models from LLM server: {exc}", file=sys.stderr)

    for env_name in [env_var_name] + fallback_env_vars:
        val = os.environ.get(env_name)
        if val:
            print(f"  Using model from {env_name}: {val}", file=sys.stderr)
            return val
    env_list = ", ".join([env_var_name] + fallback_env_vars)
    raise ValueError(
        f"No model specified and could not auto-discover from LLM server.\n"
        f"Please set one of these environment variables: {env_list}\n"
        f"Or ensure the tagger LLM server is running at {TAGGER_BASE_URL}"
    )


def strip_reasoning_markup(s: str) -> str:
    t = s.strip()
    low = t.lower()
    best_idx = -1
    best_len = 0
    for marker in _END_THINK_MARKERS:
        pos = low.rfind(marker.lower())
        if pos > best_idx:
            best_idx = pos
            best_len = len(marker)
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
    return t.strip()


def _normalize_tag_token(tok: str) -> str:
    t = tok.strip()
    while len(t) > 1 and t[-1] in ".,;:!?'\")]}":
        t = t[:-1].rstrip()
    while len(t) > 1 and t[0] in ".,;: '\"({[":
        t = t[1:].lstrip()
    return t


@functools.lru_cache(maxsize=1)
def load_tagger_prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def load_risk_prompt_text() -> str:
    return PROMPT_RISK_PATH.read_text(encoding="utf-8")


def parse_two_allowlists_from_prompt_md(text: str) -> tuple[frozenset[str], frozenset[str]]:
    lines = text.splitlines()
    blocks: list[list[str]] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "Tags:":
            chunk: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip():
                chunk.append(lines[i].strip())
                i += 1
            if chunk:
                blocks.append(chunk)
        else:
            i += 1
    if len(blocks) < 2:
        raise ValueError("compound-excerpt-tagging.md must contain two Tags: sections") from None
    return frozenset(blocks[0]), frozenset(blocks[1])


def parse_first_tags_allowlist(text: str) -> frozenset[str]:
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == "Tags:":
            chunk: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip():
                chunk.append(lines[i].strip())
                i += 1
            if chunk:
                return frozenset(chunk)
        else:
            i += 1
    raise ValueError("compound-risk-profile.md must contain a Tags: section") from None


def chat_completion(client: Any, model: str, system_prompt: str, user_content: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    def _complete() -> object:
        kw: dict[str, object] = dict(
            model=model,
            max_tokens=MAX_TAGGER_TOKENS,
            temperature=0,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
            messages=messages,
        )
        extra_body = {"top_k": -1, "chat_template_kwargs": {"enable_thinking": False}}
        try:
            return client.chat.completions.create(**kw, extra_body=extra_body)
        except TypeError:
            return client.chat.completions.create(**kw)

    for attempt in range(MAX_RETRIES):
        try:
            response = _complete()
            return strip_reasoning_markup((response.choices[0].message.content or "").strip())
        except RateLimitError:
            wait = (2**attempt) * 5
            print(f"  [RATE LIMIT] attempt {attempt + 1}/{MAX_RETRIES} — waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
        except Exception as exc:
            print(f"  [ERROR] attempt {attempt + 1}/{MAX_RETRIES}: {str(exc)[:120]}", file=sys.stderr)
            if attempt < MAX_RETRIES - 1:
                time.sleep(2**attempt)
            else:
                return ""
    return ""


def _canonical_risk_token(tok: str, allowed: frozenset[str]) -> str | None:
    t = _RISK_TOKEN_ALIASES.get(_normalize_tag_token(tok), _normalize_tag_token(tok))
    return t if t in allowed else None


def parse_risk_enum(raw: str, allowed: frozenset[str]) -> str | None:
    stripped = strip_reasoning_markup(raw)
    sources = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    sources.extend([stripped, raw.strip()])
    for blob in sources:
        s = re.sub(r"```(?:\w*)?|```", "", blob).strip()
        for part in [_normalize_tag_token(x) for x in s.split() if x.strip()]:
            val = _canonical_risk_token(part, allowed)
            if val is not None:
                return val
    return None


def risk_severity_with_retries(client: Any, model: str, user_content: str, allowed: frozenset[str]) -> str | None:
    system_prompt = load_risk_prompt_text()
    for attempt in range(MAX_RISK_PARSE_ATTEMPTS):
        raw = chat_completion(client, model, system_prompt, user_content)
        val = parse_risk_enum(raw, allowed)
        if val is not None:
            return val
        if attempt < MAX_RISK_PARSE_ATTEMPTS - 1:
            print(f"  [risk] parse retry {attempt + 1}/{MAX_RISK_PARSE_ATTEMPTS}", file=sys.stderr)
    return None


def _parse_tokens_one_blob(
    blob: str,
    longevity_tags: frozenset[str],
    risk_tags: frozenset[str],
) -> tuple[str | None, str | None]:
    s = re.sub(r"```(?:\w*)?|```", "", blob.strip()).strip()
    parts = [_normalize_tag_token(t) for t in s.split()]
    parts = [t for t in parts if t]
    if len(parts) >= 2 and parts[0] in longevity_tags and parts[1] in risk_tags:
        return parts[0], parts[1]
    longevity = next((t for t in parts if t in longevity_tags), None)
    risk = next((t for t in parts if t in risk_tags), None)
    return longevity, risk


def parse_longevity_risk_relevance(
    raw: str,
    longevity_tags: frozenset[str],
    risk_tags: frozenset[str],
) -> tuple[str | None, str | None]:
    stripped = strip_reasoning_markup(raw)
    sources: list[str] = []
    lines_s = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    lines_r = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if lines_s:
        sources.append(lines_s[-1])
    if stripped:
        sources.append(stripped)
    if lines_r and (not lines_s or lines_r[-1] != lines_s[-1]):
        sources.append(lines_r[-1])
    sources.append(raw.strip())

    longevity_out: str | None = None
    risk_out: str | None = None
    for blob in sources:
        a, b = _parse_tokens_one_blob(blob, longevity_tags, risk_tags)
        longevity_out = longevity_out or a
        risk_out = risk_out or b
        if longevity_out and risk_out:
            return longevity_out, risk_out
    return longevity_out, risk_out


def _strip_rule_ignored_keys(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: _strip_rule_ignored_keys(v)
            for k, v in value.items()
            if str(k) not in _RULE_IGNORED_TEXT_KEYS
        }
    if isinstance(value, list):
        return [_strip_rule_ignored_keys(v) for v in value]
    return value


def _stringify_for_rules(value: object) -> str:
    try:
        return json.dumps(_strip_rule_ignored_keys(value), ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _record_text_for_rules(record: dict[str, object]) -> str:
    parts = [
        str(record.get("source_type") or ""),
        str(record.get("unit_type") or ""),
        str(record.get("provenance") or ""),
        _stringify_for_rules(record.get("content")),
        _stringify_for_rules(record.get("payload")),
    ]
    return "\n".join(parts)


def _choose_stronger_risk(current: str | None, candidate: str | None) -> str | None:
    if _RISK_PRIORITY.get(candidate, 0) > _RISK_PRIORITY.get(current, 0):
        return candidate
    return current


def _rule_based_longevity(record: dict[str, object], current: str | None) -> str | None:
    source_type = str(record.get("source_type") or "")
    text = _record_text_for_rules(record)
    content = record.get("content")

    if source_type in _BACKGROUND_SOURCE_TYPES:
        if source_type == "kegg_summary" and isinstance(content, dict):
            flags = content.get("longevity_pathway_flags")
            if isinstance(flags, dict) and any(bool(v) for v in flags.values()):
                return "indirect_longevity_mechanism"
        return "background_only"
    if source_type == "clinical_trials" and current in {None, "not_relevant"}:
        return "general_bioactivity"
    if _DIRECT_LONGEVITY_RE.search(text) and current in {None, "background_only", "not_relevant"}:
        current = "direct_longevity"
    elif (
        _INDIRECT_LONGEVITY_RE.search(text)
        and _LONGEVITY_REVIEW_CONTEXT_RE.search(text)
        and current in {
            None,
            "general_bioactivity",
            "background_only",
            "not_relevant",
        }
    ):
        current = "indirect_longevity_mechanism"
    return _demote_non_longevity_focus(record, current)


def _demote_non_longevity_focus(
    record: dict[str, object],
    longevity_relevance: str | None,
) -> str | None:
    if longevity_relevance not in LONGEVITY_GROUP_TAGS:
        return longevity_relevance
    text = _record_text_for_rules(record)
    has_ctx = bool(_LONGEVITY_REVIEW_CONTEXT_RE.search(text))
    oncology = bool(_ONCOLOGY_FRAME_RE.search(text))
    if longevity_relevance == "indirect_longevity_mechanism" and not has_ctx:
        return "general_bioactivity"
    if oncology and not has_ctx:
        return "general_bioactivity"
    if longevity_relevance == "direct_longevity" and oncology and not has_ctx:
        return "general_bioactivity"
    return longevity_relevance


def _demote_assay_cytotox_risk(
    record: dict[str, object],
    risk_relevance: str | None,
) -> str | None:
    source_type = str(record.get("source_type") or "")
    if source_type not in _ASSAY_RISK_SOURCE_TYPES:
        return risk_relevance
    if risk_relevance == "toxicity_or_adverse_signal":
        return "no_risk_signal"
    if risk_relevance == "pharmacology_risk_theoretical":
        return "no_risk_signal"
    return risk_relevance


def _rule_based_risk(record: dict[str, object], current: str | None) -> str | None:
    source_type = str(record.get("source_type") or "")
    if source_type in _BACKGROUND_SOURCE_TYPES:
        return current if current not in _NO_RISK_TAGS else "no_risk_signal"

    text = _record_text_for_rules(record)
    candidate: str | None = None

    if source_type == "openfda_faers":
        return "toxicity_or_adverse_signal"
    if source_type == "openfda_drug_label":
        candidate = "direct_human_safety"
    elif source_type == "clinical_trials":
        if _DIRECT_HUMAN_SAFETY_RE.search(text) or _LITERATURE_TOXICITY_RE.search(text):
            candidate = "direct_human_safety"

    if _INTERACTION_RISK_RE.search(text) or _COMBINATION_TREATMENT_RE.search(text):
        candidate = "interaction_or_combination_risk"
    elif _DIRECT_HUMAN_SAFETY_RE.search(text):
        candidate = _choose_stronger_risk(candidate, "direct_human_safety")
    elif _LITERATURE_TOXICITY_RE.search(text):
        candidate = _choose_stronger_risk(candidate, "toxicity_or_adverse_signal")

    merged = _choose_stronger_risk(current, candidate)
    return _demote_assay_cytotox_risk(record, merged)


def apply_rule_based_relevance_overrides(
    record: dict[str, object],
    longevity_relevance: str | None,
    risk_relevance: str | None,
) -> tuple[str | None, str | None]:
    longevity_relevance = _rule_based_longevity(record, longevity_relevance)
    longevity_relevance = _demote_non_longevity_focus(record, longevity_relevance)
    risk_relevance = _rule_based_risk(record, risk_relevance)
    risk_relevance = _demote_assay_cytotox_risk(record, risk_relevance)
    return longevity_relevance, risk_relevance


def passes_longevity_review(row: dict[str, object]) -> bool:
    tag = row.get("longevity_relevance")
    if tag not in LONGEVITY_GROUP_TAGS:
        return False
    text = _record_text_for_rules(row)
    has_ctx = bool(_LONGEVITY_REVIEW_CONTEXT_RE.search(text))
    oncology = bool(_ONCOLOGY_FRAME_RE.search(text))
    if tag == "indirect_longevity_mechanism":
        return has_ctx and (not oncology or has_ctx)
    if tag == "direct_longevity":
        return has_ctx or not oncology
    return False


def passes_risk_review(row: dict[str, object]) -> bool:
    tag = row.get("risk_relevance")
    if tag not in RISK_GROUP_TAGS:
        return False
    source_type = str(row.get("source_type") or "")
    text = _record_text_for_rules(row)
    if tag in {"direct_human_safety", "interaction_or_combination_risk"}:
        return True
    if tag == "pharmacology_risk_theoretical":
        if source_type in _ASSAY_RISK_SOURCE_TYPES:
            return False
        return not (
            _ONCOLOGY_FRAME_RE.search(text)
            and not _LONGEVITY_REVIEW_CONTEXT_RE.search(text)
        )
    if tag != "toxicity_or_adverse_signal":
        return False
    if source_type in _ASSAY_RISK_SOURCE_TYPES:
        return False
    if source_type in _OPENFDA_RISK_SOURCE_TYPES:
        return True
    if _DIRECT_HUMAN_SAFETY_RE.search(text) or _INTERACTION_RISK_RE.search(text):
        return True
    if _ONCOLOGY_FRAME_RE.search(text) and not _LONGEVITY_REVIEW_CONTEXT_RE.search(text):
        return False
    return source_type in {"clinical_trials", "europe_pmc"} or source_type.startswith("openalex")


def group_filtered_records(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    longevity = [row for row in rows if passes_longevity_review(row)]
    risk = [row for row in rows if passes_risk_review(row)]
    return {"longevity": longevity, "risk": risk}


def filter_review_stats(rows: list[dict[str, object]]) -> dict[str, int]:
    longevity_tagged = sum(
        1 for row in rows if row.get("longevity_relevance") in LONGEVITY_GROUP_TAGS
    )
    risk_tagged = sum(1 for row in rows if row.get("risk_relevance") in RISK_GROUP_TAGS)
    groups = group_filtered_records(rows)
    return {
        "longevity_tagged": longevity_tagged,
        "longevity_exported": len(groups["longevity"]),
        "longevity_dropped": longevity_tagged - len(groups["longevity"]),
        "risk_tagged": risk_tagged,
        "risk_exported": len(groups["risk"]),
        "risk_dropped": risk_tagged - len(groups["risk"]),
    }


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _build_client_and_models(args: argparse.Namespace) -> tuple[Any | None, str | None, str | None]:
    if args.rules_only:
        return None, None, None
    if OpenAI is None:
        print("tag-group-filter.py: missing dependency: openai. Install it or use --rules-only.", file=sys.stderr)
        raise SystemExit(1)

    client = OpenAI(base_url=TAGGER_BASE_URL, api_key=TAGGER_API_KEY)
    model = args.model or get_available_model(
        client,
        "TAGGER_MODEL",
        ["CLASSIFIER_MODEL", "VALIDATOR_MODEL"],
    )
    risk_model = os.environ.get("TAGGER_RISK_MODEL") or model
    return client, model, risk_model


def _tag_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    prompt_text = load_tagger_prompt_text()
    longevity_tags, risk_tags = parse_two_allowlists_from_prompt_md(prompt_text)
    risk_allowed = (
        parse_first_tags_allowlist(load_risk_prompt_text())
        if args.include_risk_severity and not args.rules_only
        else frozenset()
    )
    client, model, risk_model = _build_client_and_models(args)

    tagged: list[dict[str, Any]] = []
    for idx, record in enumerate(rows, start=1):
        if idx % 25 == 0:
            print(f"  [{idx}] tagged...", file=sys.stderr)

        if args.rules_only:
            longevity_raw = record.get("longevity_relevance")
            risk_raw = record.get("risk_relevance")
            longevity = longevity_raw if isinstance(longevity_raw, str) else None
            risk = risk_raw if isinstance(risk_raw, str) else None
        else:
            assert client is not None and model is not None
            user_msg = json.dumps(record, ensure_ascii=False)
            raw = chat_completion(client, model, prompt_text, user_msg)
            longevity, risk = parse_longevity_risk_relevance(raw, longevity_tags, risk_tags)

        longevity, risk = apply_rule_based_relevance_overrides(record, longevity, risk)

        risk_severity = record.get("risk_severity")
        if args.include_risk_severity and not args.rules_only:
            assert client is not None and risk_model is not None
            user_msg = json.dumps(record, ensure_ascii=False)
            risk_severity = risk_severity_with_retries(client, risk_model, user_msg, risk_allowed)

        tagged.append({
            **record,
            "longevity_relevance": longevity,
            "risk_relevance": risk,
            "risk_severity": risk_severity if isinstance(risk_severity, str) else None,
        })

    return tagged


def _row_has_tags(row: dict[str, Any]) -> bool:
    return isinstance(row.get("longevity_relevance"), str) and isinstance(row.get("risk_relevance"), str)


def _tag_rows_incremental(
    rows: list[dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    """Tag only rows not in cache (or missing tags); merge with cached tagged rows."""
    prompt_text = load_tagger_prompt_text()
    longevity_tags, risk_tags = parse_two_allowlists_from_prompt_md(prompt_text)
    risk_allowed = (
        parse_first_tags_allowlist(load_risk_prompt_text())
        if args.include_risk_severity and not args.rules_only
        else frozenset()
    )
    client, model, risk_model = _build_client_and_models(args)

    tagged: list[dict[str, Any]] = []
    new_count = 0
    for idx, record in enumerate(rows, start=1):
        key = dedupe_key_for_row(record)
        if key is None:
            longevity, risk = apply_rule_based_relevance_overrides(
                record,
                record.get("longevity_relevance") if isinstance(record.get("longevity_relevance"), str) else None,
                record.get("risk_relevance") if isinstance(record.get("risk_relevance"), str) else None,
            )
            tagged.append({
                **record,
                "longevity_relevance": longevity,
                "risk_relevance": risk,
                "risk_severity": record.get("risk_severity"),
            })
            continue
        if key in cache and _row_has_tags(cache[key]) and not getattr(args, "retag_all", False):
            tagged.append(cache[key])
            continue

        if args.rules_only:
            longevity_raw = record.get("longevity_relevance")
            risk_raw = record.get("risk_relevance")
            longevity = longevity_raw if isinstance(longevity_raw, str) else None
            risk = risk_raw if isinstance(risk_raw, str) else None
        else:
            assert client is not None and model is not None
            user_msg = json.dumps(record, ensure_ascii=False)
            raw = chat_completion(client, model, prompt_text, user_msg)
            longevity, risk = parse_longevity_risk_relevance(raw, longevity_tags, risk_tags)
            new_count += 1

        longevity, risk = apply_rule_based_relevance_overrides(record, longevity, risk)

        risk_severity = record.get("risk_severity")
        if key and key in cache and cache[key].get("risk_severity"):
            risk_severity = cache[key].get("risk_severity")
        if args.include_risk_severity and not args.rules_only:
            assert client is not None and risk_model is not None
            user_msg = json.dumps(record, ensure_ascii=False)
            risk_severity = risk_severity_with_retries(client, risk_model, user_msg, risk_allowed)

        row_out = {
            **record,
            "longevity_relevance": longevity,
            "risk_relevance": risk,
            "risk_severity": risk_severity if isinstance(risk_severity, str) else None,
        }
        tagged.append(row_out)
        if key:
            cache[key] = row_out

        if new_count and new_count % 25 == 0:
            print(f"  [{idx}] newly tagged...", file=sys.stderr)

    if new_count:
        print(f"  delta-tag: {new_count} new LLM tags", file=sys.stderr)
    return tagged


def _load_tag_cache(path: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return cache
    for row in _load_jsonl(path):
        key = dedupe_key_for_row(row)
        if key:
            cache[key] = row
    return cache


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tag material JSONL and emit longevity.json/risk.json filtered groups."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="material.json (JSONL), legacy report_*.json, or tagged material JSONL.",
    )
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory (default: input directory).")
    parser.add_argument("--longevity-output", type=Path, default=None, help="Default: <out-dir>/longevity.json")
    parser.add_argument("--risk-output", type=Path, default=None, help="Default: <out-dir>/risk.json")
    parser.add_argument(
        "--tagged-output",
        type=Path,
        default=None,
        help="Optional tagged JSONL output before filtering.",
    )
    parser.add_argument("--model", type=str, default=None, metavar="NAME")
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Input already has relevance tags; only apply deterministic guardrails and grouping.",
    )
    parser.add_argument(
        "--include-risk-severity",
        action="store_true",
        help="Also run the separate risk_severity pass. Off by default for speed.",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Reuse tags from --tagged-cache for known rows; LLM-tag only new rows.",
    )
    parser.add_argument(
        "--tagged-cache",
        type=Path,
        default=None,
        help="Tagged JSONL cache (default: <out-dir>/material_tagged.jsonl).",
    )
    parser.add_argument(
        "--retag-all",
        action="store_true",
        help="With --incremental, re-run LLM on all rows (ignore cache tags).",
    )
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        print(f"tag-group-filter.py: not found: {input_path}", file=sys.stderr)
        return 1

    out_dir = args.out_dir.expanduser().resolve() if args.out_dir else input_path.parent
    longevity_path = (
        args.longevity_output.expanduser().resolve()
        if args.longevity_output
        else out_dir / "longevity.json"
    )
    risk_path = (
        args.risk_output.expanduser().resolve()
        if args.risk_output
        else out_dir / "risk.json"
    )

    tagged_cache_path = (
        args.tagged_cache.expanduser().resolve()
        if args.tagged_cache
        else out_dir / "material_tagged.jsonl"
    )

    try:
        rows = load_material_records(input_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"tag-group-filter.py: cannot load input {input_path}: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print(f"tag-group-filter.py: no material rows in {input_path}", file=sys.stderr)
        return 1
    if args.incremental:
        cache = _load_tag_cache(tagged_cache_path)
        tagged = _tag_rows_incremental(rows, cache, args)
    else:
        tagged = _tag_rows(rows, args)
    groups = group_filtered_records(tagged)
    stats = filter_review_stats(tagged)

    write_jsonl(longevity_path, groups["longevity"])
    write_jsonl(risk_path, groups["risk"])
    tagged_out = args.tagged_output.expanduser().resolve() if args.tagged_output else tagged_cache_path
    write_jsonl(tagged_out, tagged)

    print(
        f"Wrote {len(groups['longevity'])} longevity rows -> {longevity_path} "
        f"(dropped {stats['longevity_dropped']} non-longevity-focused of {stats['longevity_tagged']} tagged)\n"
        f"Wrote {len(groups['risk'])} risk rows -> {risk_path} "
        f"(dropped {stats['risk_dropped']} assay/oncology-noise of {stats['risk_tagged']} tagged)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
