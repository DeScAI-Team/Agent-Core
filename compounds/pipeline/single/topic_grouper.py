#!/usr/bin/env python3
"""Group tagged longevity.json and risk.json into topic chunks for review LLM calls.

Always processes both axes in one invocation. Default layout (same as tag-group-filter):

  compound_dir/longevity.json  -> compound_dir/longevity_groups.json
  compound_dir/risk.json       -> compound_dir/risk_groups.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

LONGEVITY_BUCKETS: list[tuple[str, re.Pattern[str]]] = [
    ("senescence_sasp", re.compile(r"\b(senescence|sasp|cellular senescence|bystander)\b", re.I)),
    ("mitochondria_ros", re.compile(r"\b(mitochondri|ros|reactive oxygen|oxidative stress|atp|membrane potential)\b", re.I)),
    ("autophagy_mtor", re.compile(r"\b(autophagy|mTOR|AMPK|sirtuin|NAD\+?|proteostasis)\b", re.I)),
    ("immune_inflammation", re.compile(r"\b(inflammaging|macrophage|immune|inflammation|cytokine|treg)\b", re.I)),
    ("metabolism", re.compile(r"\b(metabolic|obesity|diabetes|atherosclerosis|nafld|metabolic syndrome)\b", re.I)),
    ("lifespan_healthspan", re.compile(r"\b(lifespan|life span|longevity|healthspan|health span|anti[- ]?aging|extend(ed)? lifespan)\b", re.I)),
    ("cancer_apoptosis", re.compile(r"\b(cancer|apoptosis|cytotoxic|tumor|hepatocellular|breast cancer|cell viability)\b", re.I)),
    ("general_longevity", re.compile(r"\b(ginseng|ginsenoside|panax|triterpenoid|herbal)\b", re.I)),
]

RISK_BUCKETS: list[tuple[str, re.Pattern[str]]] = [
    ("faers_adverse", re.compile(r"\b(faers|adverse event|adverse reaction|reaction_terms)\b", re.I)),
    ("cytotoxicity", re.compile(r"\b(toxicity|toxic|cytotoxicity|cell viability|apoptosis|necrosis|ic50|gi50)\b", re.I)),
    ("chemo_combination", re.compile(r"\b(chemotherapy|radiotherapy|cisplatin|doxorubicin|combined with|co-administration|combination therapy)\b", re.I)),
    ("human_safety", re.compile(r"\b(contraindicat|boxed warning|adverse reaction|tolerability|pregnancy|hepatic impairment)\b", re.I)),
    ("cardiac_qt", re.compile(r"\b(qt|torsade|arrhythmia|cardiac|myocardial)\b", re.I)),
    ("liver_kidney", re.compile(r"\b(hepatotoxic|nephrotoxic|liver|kidney|renal|hepatic)\b", re.I)),
    ("interaction_metabolism", re.compile(r"\b(cyp\d|cytochrome|p-glycoprotein|transporter|warfarin|statin|drug interaction)\b", re.I)),
    ("general_risk", re.compile(r"\b(risk|safety|adverse|toxic)\b", re.I)),
]

MAX_GROUP_SIZE = 12
SIMILARITY_THRESHOLD = 0.32
MIN_GROUP_SIZE_FOR_CLUSTER = 2


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{3,}", text.lower())


def _join_excerpt_parts(parts: list[str], max_len: int = 4000) -> str:
    text = " ".join(p.strip() for p in parts if p and str(p).strip())
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _excerpt_for_row(row: dict[str, Any]) -> str:
    """Source-specific excerpt text for grouping and review units."""
    content = row.get("content")
    if not isinstance(content, dict):
        return ""
    source_type = str(row.get("source_type") or "")

    if source_type in ("europe_pmc", "openalex_grounding", "openalex_risk", "openalex_work"):
        parts: list[str] = []
        title = content.get("title")
        if isinstance(title, str) and title.strip():
            parts.append(title.strip())
        abstract = content.get("abstract")
        if isinstance(abstract, str) and abstract.strip():
            parts.append(abstract.strip())
        return _join_excerpt_parts(parts)

    if source_type == "pubchem_bioassay":
        parts = []
        aid = content.get("aid")
        if aid is not None:
            parts.append(f"AID {aid}")
        title = content.get("title")
        if isinstance(title, str) and title.strip():
            parts.append(title.strip())
        for key in ("prose", "assay_description"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
                break
        return _join_excerpt_parts(parts)

    if source_type in ("chembl_activity", "chembl_mechanism"):
        parts = []
        for key in (
            "molecule_pref_name",
            "target_pref_name",
            "assay_description",
            "mechanism_of_action",
            "action_type",
        ):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        measure = []
        for key in ("standard_type", "standard_value", "standard_units", "pchembl_value"):
            val = content.get(key)
            if val is not None and str(val).strip():
                measure.append(str(val).strip())
        if measure:
            parts.append(" ".join(measure))
        return _join_excerpt_parts(parts)

    if source_type == "openfda_faers":
        parts = []
        count = content.get("report_count")
        if count is not None:
            parts.append(f"FAERS report count: {count}")
        terms = content.get("reaction_terms")
        if isinstance(terms, list) and terms:
            parts.append("Reaction terms: " + ", ".join(str(t) for t in terms[:20]))
        elif isinstance(terms, str) and terms.strip():
            parts.append(terms.strip())
        return _join_excerpt_parts(parts)

    if source_type == "openfda_drug_label":
        parts = []
        for key in (
            "boxed_warning",
            "warnings",
            "contraindications",
            "drug_interactions",
            "adverse_reactions",
        ):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(f"{key.replace('_', ' ')}: {val.strip()[:800]}")
            elif isinstance(val, list) and val:
                parts.append(
                    f"{key.replace('_', ' ')}: "
                    + "; ".join(str(x) for x in val[:8] if x)
                )
        return _join_excerpt_parts(parts)

    if source_type == "clinical_trials":
        parts = []
        for key in ("brief_title", "official_title", "nct_id", "phase", "status"):
            val = content.get(key)
            if val is not None and str(val).strip():
                parts.append(f"{key}: {val}")
        summary = content.get("brief_summary") or content.get("description")
        if isinstance(summary, str) and summary.strip():
            parts.append(summary.strip())
        return _join_excerpt_parts(parts)

    if source_type in ("kegg_pathway", "kegg_summary"):
        parts = []
        for key in ("pathway_name", "name", "description", "pathway_id"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        return _join_excerpt_parts(parts)

    parts = []
    for key in ("title", "abstract", "prose", "assay_description", "reaction_terms"):
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        elif isinstance(val, list):
            parts.extend(str(x) for x in val[:20] if x)
    return _join_excerpt_parts(parts)


def _row_text(row: dict[str, Any]) -> str:
    return _excerpt_for_row(row)


def _title_for_row(row: dict[str, Any], content: dict[str, Any]) -> str | None:
    source_type = str(row.get("source_type") or "")
    if source_type == "pubchem_bioassay":
        title = content.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        aid = content.get("aid")
        if aid is not None:
            return f"PubChem bioassay AID {aid}"
    if source_type in ("chembl_activity", "chembl_mechanism"):
        for key in ("assay_description", "target_pref_name", "molecule_pref_name"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:120]
    if source_type == "openfda_faers":
        count = content.get("report_count")
        return f"FAERS adverse events (n={count})" if count is not None else "FAERS adverse events"
    if source_type == "openfda_drug_label":
        brand = content.get("brand_name") or content.get("openfda_brand_name")
        if isinstance(brand, str) and brand.strip():
            return brand.strip()[:120]
        return "OpenFDA drug label"
    if source_type == "clinical_trials":
        for key in ("brief_title", "official_title"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:120]
    title = content.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


_GENERIC_DIR_NAMES = frozenset({"data", "steps", "review", "compound", "compounds"})


def resolve_compound_name(
    compound_dir: Path,
    rows: list[dict[str, Any]] | None = None,
    override: str | None = None,
) -> str:
    """Resolve compound name from CLI, material.json, row content, or directory."""
    if override and override.strip():
        return override.strip()

    material_path = compound_dir / "material.json"
    if material_path.is_file():
        for row in load_jsonl(material_path):
            if row.get("source_type") == "compound_name":
                content = row.get("content")
                if isinstance(content, dict):
                    name = content.get("compound_name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()
            if row.get("source_type") == "discover_metadata":
                content = row.get("content")
                if isinstance(content, dict):
                    name = content.get("compound") or content.get("compound_name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()

    if rows:
        for row in rows:
            content = row.get("content")
            if isinstance(content, dict):
                name = content.get("compound_name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
            name = row.get("compound_name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    fallback = compound_dir.name
    if fallback in _GENERIC_DIR_NAMES:
        return "(unknown compound)"
    return fallback


def _dedupe_key(row: dict[str, Any]) -> str:
    from discover_lib.dedupe import dedupe_key_for_row

    key = dedupe_key_for_row(row)
    if key:
        return key
    return f"hash:{hash(_row_text(row))}"


def _bucket_label(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> str:
    for label, pattern in patterns:
        if pattern.search(text):
            return label
    return "other"


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a and not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a[k] * b[k] for k in keys)
    na = math.sqrt(sum(a[k] ** 2 for k in a))
    nb = math.sqrt(sum(b[k] ** 2 for k in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _greedy_clusters(bucket_rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a large bucket into sub-groups using TF-IDF cosine similarity."""
    vectors = [Counter(_tokenize(_row_text(r))) for r in bucket_rows]
    remaining = list(range(len(bucket_rows)))
    clusters: list[list[int]] = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        centroid = vectors[seed].copy()
        i = 0
        while i < len(remaining) and len(cluster) < MAX_GROUP_SIZE:
            idx = remaining[i]
            if _cosine(centroid, vectors[idx]) >= SIMILARITY_THRESHOLD:
                cluster.append(idx)
                remaining.pop(i)
                for term, count in vectors[idx].items():
                    centroid[term] += count
            else:
                i += 1
        clusters.append(cluster)
    return [[bucket_rows[i] for i in cluster] for cluster in clusters]


def _cluster_rows(rows: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """Group rows by keyword bucket; split oversized buckets with greedy TF-IDF clustering."""
    patterns = LONGEVITY_BUCKETS if prefix == "longevity" else RISK_BUCKETS
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = _bucket_label(_row_text(row), patterns)
        buckets[label].append(row)

    raw_groups: list[list[dict[str, Any]]] = []
    for bucket_rows in buckets.values():
        if len(bucket_rows) <= MAX_GROUP_SIZE:
            raw_groups.append(bucket_rows)
        elif len(bucket_rows) < MIN_GROUP_SIZE_FOR_CLUSTER:
            for i in range(0, len(bucket_rows), MAX_GROUP_SIZE):
                raw_groups.append(bucket_rows[i : i + MAX_GROUP_SIZE])
        else:
            raw_groups.extend(_greedy_clusters(bucket_rows))

    out: list[dict[str, Any]] = []
    unit_index = 0
    for gi, group in enumerate(raw_groups):
        topic_id = f"{prefix}_{gi + 1:03d}"
        units: list[dict[str, Any]] = []
        for row in group:
            unit_index += 1
            units.append(normalize_unit(row, prefix, unit_index))
        label = _bucket_label(_row_text(group[0]), patterns) if group else "other"
        out.append({
            "topic_id": topic_id,
            "topic_label": label,
            "row_count": len(group),
            "units": units,
        })
    return out


def normalize_unit(row: dict[str, Any], prefix: str, index: int) -> dict[str, Any]:
    content = row.get("content")
    if not isinstance(content, dict):
        content = {}
    title = _title_for_row(row, content)
    year = content.get("year")
    doi = content.get("doi")
    pmid = content.get("pmid")
    excerpt = _excerpt_for_row(row)
    citation_parts = [f"{prefix}_{index:03d}"]
    if isinstance(title, str) and title.strip():
        citation_parts.append(title.strip()[:120])
    if year:
        citation_parts.append(str(year))
    if isinstance(doi, str) and doi.strip():
        citation_parts.append(doi.strip())
    citation = " — ".join(citation_parts) if len(citation_parts) > 1 else citation_parts[0]
    return {
        "unit_id": f"{prefix}_{index:03d}",
        "source_type": row.get("source_type"),
        "title": title if isinstance(title, str) else None,
        "year": year,
        "doi": doi if isinstance(doi, str) else None,
        "pmid": pmid if isinstance(pmid, str) else None,
        "excerpt": excerpt,
        "citation": citation,
        "longevity_relevance": row.get("longevity_relevance"),
        "risk_relevance": row.get("risk_relevance"),
    }


def group_rows(rows: list[dict[str, Any]], axis: str) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = _dedupe_key(row)
        if key not in deduped:
            deduped[key] = row
            order.append(key)
    unique_rows = [deduped[k] for k in order]
    return _cluster_rows(unique_rows, axis)


def build_groups_document(rows: list[dict[str, Any]], axis: str, input_path: Path | None = None) -> dict[str, Any]:
    groups = group_rows(rows, axis)
    units: list[dict[str, Any]] = []
    for group in groups:
        units.extend(group["units"])
    return {
        "axis": axis,
        "input": str(input_path) if input_path else None,
        "group_count": len(groups),
        "unit_count": len(units),
        "groups": groups,
        "units": units,
    }


def _write_groups(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _group_file(
    input_path: Path,
    output_path: Path,
    axis: str,
) -> dict[str, Any]:
    if not input_path.is_file():
        print(f"  {axis}: missing {input_path} — skipping", file=sys.stderr)
        return build_groups_document([], axis, input_path)
    rows = load_jsonl(input_path)
    doc = build_groups_document(rows, axis, input_path)
    _write_groups(output_path, doc)
    print(
        f"  {axis}: {doc['group_count']} groups, {doc['unit_count']} units -> {output_path}",
        file=sys.stderr,
    )
    return doc


def main() -> int:
    global MAX_GROUP_SIZE, SIMILARITY_THRESHOLD

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "compound_dir",
        nargs="?",
        type=Path,
        help="Directory containing longevity.json and risk.json (default with --out-dir).",
    )
    ap.add_argument("--longevity", type=Path, help="longevity.json path")
    ap.add_argument("--risk", type=Path, help="risk.json path")
    ap.add_argument(
        "--out-dir",
        type=Path,
        help="Output directory for *_groups.json (default: compound_dir or parent of --longevity).",
    )
    ap.add_argument("--max-group-size", type=int, default=12)
    ap.add_argument("--similarity", type=float, default=0.32)
    args = ap.parse_args()

    MAX_GROUP_SIZE = max(2, args.max_group_size)
    SIMILARITY_THRESHOLD = args.similarity

    if args.compound_dir is not None:
        base = args.compound_dir.expanduser().resolve()
        longevity_in = args.longevity.expanduser().resolve() if args.longevity else base / "longevity.json"
        risk_in = args.risk.expanduser().resolve() if args.risk else base / "risk.json"
        out_dir = args.out_dir.expanduser().resolve() if args.out_dir else base
    elif args.longevity is not None:
        longevity_in = args.longevity.expanduser().resolve()
        risk_in = (
            args.risk.expanduser().resolve()
            if args.risk
            else longevity_in.parent / "risk.json"
        )
        out_dir = (
            args.out_dir.expanduser().resolve()
            if args.out_dir
            else longevity_in.parent
        )
    else:
        ap.error("Provide compound_dir or --longevity (and optionally --risk).")

    longevity_out = out_dir / "longevity_groups.json"
    risk_out = out_dir / "risk_groups.json"

    if not longevity_in.is_file():
        print(f"topic_grouper: longevity input not found: {longevity_in}", file=sys.stderr)
        return 1

    print("Grouping longevity and risk...", file=sys.stderr)
    _group_file(longevity_in, longevity_out, "longevity")
    _group_file(risk_in, risk_out, "risk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())