"""Build combination evidence from single-compound review pipeline outputs only.

Reads the same kept artifacts ``run_review.py`` uses (not raw material or full tagged cache):
  - longevity.json, risk.json (tag-group-filter exports)
  - longevity_topic_summaries.json, risk_topic_summaries.json (review stage 1)
  - review.json (scores and rationales for combo synthesis)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_MULTI_DIR = Path(__file__).resolve().parent
_COMPOUNDS_DIR = _MULTI_DIR.parent.parent
_SINGLE_DIR = _COMPOUNDS_DIR / "pipeline" / "single"

if str(_COMPOUNDS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPOUNDS_DIR))
if str(_SINGLE_DIR) not in sys.path:
    sys.path.insert(0, str(_SINGLE_DIR))

from discover_lib.dedupe import dedupe_key_for_row  # noqa: E402

_SKIP_SOURCE_TYPES = frozenset({
    "compound_name",
    "discover_metadata",
    "discover_round_meta",
    "europe_pmc_query_stats",
    "clinical_trials_meta",
    "pubchem_bioassays_meta",
    "openalex_grounding_meta",
    "openalex_risk_meta",
    "chembl_molecule",
})

_COVERAGE_SOURCE_MAP = {
    "europe_pmc": "literature",
    "openalex_grounding": "literature",
    "openalex_risk": "literature",
    "openalex_work": "literature",
    "clinical_trials": "clinical_trials",
    "kegg_summary": "kegg",
    "kegg_pathway": "kegg",
    "chembl_mechanism": "chembl",
    "chembl_activity": "chembl",
    "pubchem_bioassay": "pubchem",
    "openfda_faers": "openfda",
    "openfda_drug_label": "openfda",
}

_SNIPPET_MAX = 500
_SPL_INTERACTION_FIELDS = ("drug_interactions",)
_SPL_MECHANISM_FIELDS = ("mechanism_of_action", "clinical_pharmacology", "pharmacokinetics")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return rows


def snippet_from_row(row: dict[str, Any], *, max_len: int = _SNIPPET_MAX) -> str | None:
    content = row.get("content")
    if not isinstance(content, dict):
        return None
    source_type = str(row.get("source_type") or "")
    parts: list[str] = []

    if source_type in ("europe_pmc", "openalex_grounding", "openalex_risk", "openalex_work"):
        for key in ("title", "abstract"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
    elif source_type == "kegg_summary":
        names = content.get("pathway_names")
        if isinstance(names, list):
            parts.extend(str(n) for n in names[:5] if n)
        flags = content.get("longevity_pathway_flags")
        if isinstance(flags, dict):
            active = [k for k, v in flags.items() if v]
            if active:
                parts.append("flags: " + ", ".join(active[:8]))
    elif source_type == "kegg_pathway":
        for key in ("name", "description", "pathway_id"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
    elif source_type in ("chembl_mechanism", "chembl_activity"):
        for key in (
            "mechanism_of_action",
            "target_pref_name",
            "assay_description",
            "action_type",
            "molecule_pref_name",
        ):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
    elif source_type == "openfda_drug_label":
        for key in (*_SPL_MECHANISM_FIELDS, *_SPL_INTERACTION_FIELDS, "warnings", "contraindications"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip()[:400])
            elif isinstance(val, list):
                parts.extend(v.strip()[:200] for v in val if isinstance(v, str) and v.strip())
    elif source_type == "pubchem_bioassay":
        for key in ("title", "prose", "assay_description"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
    else:
        for key in ("title", "description", "abstract", "text"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())

    if not parts:
        return None
    text = " | ".join(parts)
    return text[:max_len] if len(text) > max_len else text


def evidence_units_from_rows(
    rows: list[dict[str, Any]],
    *,
    max_units: int = 15,
) -> list[dict[str, Any]]:
    """Normalized units from filtered longevity.json or risk.json rows."""
    out: list[dict[str, Any]] = []
    for row in rows:
        source_type = str(row.get("source_type") or "")
        if source_type in _SKIP_SOURCE_TYPES:
            continue
        snippet = snippet_from_row(row)
        if not snippet:
            continue
        unit_id = dedupe_key_for_row(row) or f"{source_type}:{len(out)}"
        out.append({
            "unit_id": unit_id,
            "source_type": source_type,
            "longevity_relevance": row.get("longevity_relevance"),
            "risk_relevance": row.get("risk_relevance"),
            "snippet": snippet,
        })
        if len(out) >= max_units:
            break
    return out


def interaction_units_from_risk(
    risk_rows: list[dict[str, Any]],
    *,
    max_units: int = 10,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in risk_rows:
        if row.get("risk_relevance") != "interaction_or_combination_risk":
            continue
        snippet = snippet_from_row(row)
        if not snippet:
            continue
        out.append({
            "unit_id": dedupe_key_for_row(row) or f"interaction:{len(out)}",
            "source_type": row.get("source_type"),
            "risk_relevance": row.get("risk_relevance"),
            "snippet": snippet,
        })
        if len(out) >= max_units:
            break
    return out


def kegg_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    flags: dict[str, bool] = {}
    pathway_names: list[str] = []
    kegg_drug_ids: list[str] = []

    for row in rows:
        if row.get("source_type") != "kegg_summary":
            continue
        content = row.get("content")
        if not isinstance(content, dict):
            continue
        row_flags = content.get("longevity_pathway_flags")
        if isinstance(row_flags, dict):
            for k, v in row_flags.items():
                if v:
                    flags[k] = True
        names = content.get("pathway_names")
        if isinstance(names, list):
            for n in names:
                if n and n not in pathway_names:
                    pathway_names.append(str(n))
        ids = content.get("kegg_drug_ids")
        if isinstance(ids, list):
            for i in ids:
                if i and i not in kegg_drug_ids:
                    kegg_drug_ids.append(str(i))

    active = [k for k, v in flags.items() if v]
    return {
        "pathway_flags": flags,
        "flags_present": active,
        "pathway_names_sample": pathway_names[:10],
        "kegg_drug_ids": kegg_drug_ids,
        "kegg_available": bool(kegg_drug_ids),
    }


def coverage_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    present: set[str] = set()
    for row in rows:
        st = row.get("source_type")
        if isinstance(st, str):
            mapped = _COVERAGE_SOURCE_MAP.get(st)
            if mapped:
                present.add(mapped)
    names = sorted(_COVERAGE_SOURCE_MAP.values())
    return {name: {"present": name in present} for name in names}


def load_topic_summaries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    summaries = data.get("summaries")
    if not isinstance(summaries, list):
        return []
    out: list[dict[str, Any]] = []
    for item in summaries:
        if not isinstance(item, dict):
            continue
        bullets = item.get("bullets")
        if not isinstance(bullets, list):
            bullets = []
        out.append({
            "topic_id": item.get("topic_id"),
            "topic_label": item.get("topic_label"),
            "bullets": [str(b) for b in bullets if b],
            "unit_ids": item.get("unit_ids") if isinstance(item.get("unit_ids"), list) else [],
        })
    return out


def _name_tokens(compound: str) -> frozenset[str]:
    return frozenset(t.lower() for t in re.split(r"[\s\-/,]+", compound) if len(t) >= 4)


def _str_fields(label: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for f in fields:
        val = label.get(f)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
        elif isinstance(val, list):
            out.extend(v.strip() for v in val if isinstance(v, str) and v.strip())
    return out


def spl_from_risk_rows(risk_rows: list[dict[str, Any]], compound: str) -> dict[str, Any]:
    """SPL interaction/mechanism text only from openfda_drug_label rows in risk.json."""
    labels = [
        row["content"]
        for row in risk_rows
        if row.get("source_type") == "openfda_drug_label"
        and isinstance(row.get("content"), dict)
    ]
    if not labels:
        return {
            "label_matched": False,
            "match_note": "no openfda_drug_label rows in risk.json",
            "labels_total": 0,
            "labels_matched": 0,
            "interaction_excerpts": [],
            "mechanism_excerpt": None,
        }

    tokens = _name_tokens(compound)
    matched: list[dict[str, Any]] = []
    for label in labels:
        parts: list[str] = []
        for v in label.values():
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, list):
                parts.extend(x for x in v if isinstance(x, str))
        all_text = " ".join(parts).lower()
        if tokens and any(t in all_text for t in tokens):
            matched.append(label)

    if not matched:
        return {
            "label_matched": False,
            "match_note": (
                f"none of {len(labels)} label row(s) in risk.json matched compound tokens {sorted(tokens)}"
            ),
            "labels_total": len(labels),
            "labels_matched": 0,
            "interaction_excerpts": [],
            "mechanism_excerpt": None,
        }

    interaction_excerpts: list[str] = []
    mechanism_parts: list[str] = []
    seen: set[str] = set()
    for label in matched:
        for text in _str_fields(label, _SPL_INTERACTION_FIELDS):
            t = text[:2000]
            if t not in seen:
                seen.add(t)
                interaction_excerpts.append(t)
        for text in _str_fields(label, _SPL_MECHANISM_FIELDS):
            mechanism_parts.append(text[:1400])

    mechanism_excerpt = " | ".join(mechanism_parts[:3]) if mechanism_parts else None
    return {
        "label_matched": True,
        "match_note": f"{len(matched)} of {len(labels)} risk.json label row(s) matched",
        "labels_total": len(labels),
        "labels_matched": len(matched),
        "interaction_excerpts": interaction_excerpts,
        "mechanism_excerpt": mechanism_excerpt,
    }


def review_json_candidates(data_dir: Path) -> list[Path]:
    """Paths to check for a single-compound review.json under a steps directory."""
    return [
        data_dir / "review" / "review.json",
        data_dir / "review.json",
        data_dir.parent / "review" / "review.json",
    ]


def load_review_json(data_dir: Path) -> dict[str, Any] | None:
    for path in review_json_candidates(data_dir):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else None
            except (OSError, json.JSONDecodeError):
                return None
    legacy = sorted(data_dir.glob("*-review.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if legacy:
        try:
            data = json.loads(legacy[0].read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            pass
    return None


def build_compound_pipeline_evidence(compound: str, data_dir: Path) -> dict[str, Any]:
    """Assemble one compound's bundle section from review-pipeline artifacts only."""
    ev: dict[str, Any] = {
        "compound_name": compound,
        "data_dir": str(data_dir),
        "found": data_dir.is_dir(),
        "warnings": [],
        "pipeline_artifacts": [
            "longevity.json",
            "risk.json",
            "longevity_topic_summaries.json",
            "risk_topic_summaries.json",
            "review.json",
        ],
    }
    if not ev["found"]:
        ev["warnings"].append(f"data directory not found: {data_dir}")
        return ev

    longevity_rows = load_jsonl(data_dir / "longevity.json")
    risk_rows = load_jsonl(data_dir / "risk.json")
    filtered_rows = longevity_rows + risk_rows

    ev["pipeline_counts"] = {
        "longevity_rows": len(longevity_rows),
        "risk_rows": len(risk_rows),
    }

    if not longevity_rows:
        ev["warnings"].append("no longevity.json — run run_review.py (tag-group-filter)")
    if not risk_rows:
        ev["warnings"].append("no risk.json — run run_review.py (tag-group-filter)")

    ev["longevity_evidence"] = evidence_units_from_rows(longevity_rows)
    ev["risk_evidence"] = evidence_units_from_rows(risk_rows, max_units=12)
    ev["interaction_evidence"] = interaction_units_from_risk(risk_rows)
    ev["risk_tagged_count"] = len(ev["interaction_evidence"])

    ev["longevity_topic_summaries"] = load_topic_summaries(data_dir / "longevity_topic_summaries.json")
    ev["risk_topic_summaries"] = load_topic_summaries(data_dir / "risk_topic_summaries.json")

    ev["kegg"] = kegg_from_rows(filtered_rows)
    ev["coverage"] = coverage_from_rows(filtered_rows)
    ev["spl"] = spl_from_risk_rows(risk_rows, compound)

    # Alias for review-multiple compatibility pass (snippets from kept longevity rows).
    ev["mechanism_units"] = [
        {
            "unit_id": u.get("unit_id"),
            "unit_type": u.get("source_type"),
            "source_type": u.get("source_type"),
            "longevity_relevance": u.get("longevity_relevance"),
            "risk_relevance": u.get("risk_relevance"),
            "snippet": u.get("snippet"),
        }
        for u in ev["longevity_evidence"]
    ]

    review = load_review_json(data_dir)
    if review:
        cats = review.get("categories") or {}
        sg = cats.get("scientific_grounding") or {}
        ra = cats.get("risk_assessment") or {}
        ev["review_statement"] = review.get("review_statement")
        ev["scientific_grounding_score"] = sg.get("score")
        ev["scientific_grounding_rationale"] = sg.get("rationale")
        ev["risk_score"] = ra.get("score")
        ev["risk_rationale"] = ra.get("rationale")
    else:
        ev["warnings"].append("no review.json — run run_review.py first")

    return ev
