"""Discover material: JSONL rows ``{source_type, content}`` in ``material.json``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

MATERIAL_SCHEMA = "pump-science.material.v1"
MATERIAL_FILENAME = "material.json"


def _emit(source_type: str, record: Any) -> dict[str, Any]:
    return {"source_type": source_type, "content": record}


def _iter_openalex_rows(report: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for block_key, source_type in (
        ("openalex_grounding", "openalex_grounding"),
        ("openalex_risk", "openalex_risk"),
    ):
        block = report.get(block_key)
        if not isinstance(block, dict):
            continue
        meta = {k: block.get(k) for k in ("search_terms", "work_count") if block.get(k) is not None}
        if meta:
            yield _emit(f"{source_type}_meta", meta)
        for work in block.get("works") or []:
            if isinstance(work, dict):
                yield _emit(source_type, work)


def iter_material_records(report: dict[str, Any]) -> Iterator[dict[str, Any]]:
    compound = report.get("compound_name")
    if compound is not None:
        yield _emit("compound_name", {"compound_name": compound})

    meta = report.get("metadata")
    if isinstance(meta, dict) and meta:
        yield _emit("discover_metadata", meta)

    epmc = report.get("europe_pmc")
    if isinstance(epmc, dict):
        stats = epmc.get("query_stats")
        if isinstance(stats, dict) and stats:
            yield _emit("europe_pmc_query_stats", stats)
        for art in epmc.get("articles") or []:
            if isinstance(art, dict):
                yield _emit("europe_pmc", art)

    ct = report.get("clinical_trials")
    if isinstance(ct, dict):
        ct_meta = {k: ct.get(k) for k in ("study_count", "version_holder") if ct.get(k) is not None}
        if ct_meta:
            yield _emit("clinical_trials_meta", ct_meta)
        for study in ct.get("studies") or []:
            if isinstance(study, dict):
                yield _emit("clinical_trials", study)

    kg = report.get("kegg")
    if isinstance(kg, dict):
        kg_summary = {
            k: kg.get(k)
            for k in (
                "kegg_drug_ids",
                "pathway_ids",
                "pathway_count",
                "pathway_names",
                "longevity_pathway_flags",
                "truncated",
                "pathway_get_limit",
            )
            if kg.get(k) is not None
        }
        if kg_summary:
            yield _emit("kegg_summary", kg_summary)
        for pw in kg.get("pathways") or []:
            if isinstance(pw, dict):
                yield _emit("kegg_pathway", pw)

    chembl = report.get("chembl")
    if isinstance(chembl, dict):
        header = {
            k: chembl.get(k)
            for k in ("molecule_chembl_id", "pref_name", "search_name_used")
            if chembl.get(k) is not None
        }
        if header:
            yield _emit("chembl_molecule", header)
        for mech in chembl.get("mechanisms") or []:
            if isinstance(mech, dict):
                yield _emit("chembl_mechanism", mech)
        for act in chembl.get("activities") or []:
            if isinstance(act, dict):
                yield _emit("chembl_activity", act)

    pb = report.get("pubchem_bioassays")
    if isinstance(pb, dict):
        block = {k: pb.get(k) for k in ("cid", "assay_count", "nlg_skipped_reason") if k in pb}
        if block:
            yield _emit("pubchem_bioassays_meta", block)
        for summary in pb.get("summaries") or []:
            if isinstance(summary, dict):
                yield _emit("pubchem_bioassay", summary)

    of = report.get("openfda")
    if isinstance(of, dict):
        ae = of.get("adverse_events")
        if isinstance(ae, dict):
            yield _emit("openfda_faers", ae)
        for label in of.get("drug_labels") or []:
            if isinstance(label, dict):
                yield _emit("openfda_drug_label", label)

    yield from _iter_openalex_rows(report)


def append_material_rows(path: Path, rows: list[dict[str, Any]]) -> int:
    """Append JSONL rows to material file; returns number of lines written."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(rows)


def init_material_file(path: Path, compound: str, *, fresh: bool = True) -> None:
    """Create or truncate material.json with compound_name header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = _emit("compound_name", {"compound_name": compound})
    if fresh or not path.is_file():
        path.write_text(
            json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


def serialize_material_jsonl(report: dict[str, Any]) -> str:
    lines = [
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        for row in iter_material_records(report)
    ]
    return ("\n".join(lines) + "\n") if lines else ""


def _is_jsonl_material(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            return False
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return False
        return isinstance(row, dict) and "source_type" in row
    return False


def load_material_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if _is_jsonl_material(text):
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                records.append(row)
        return records
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level JSON must be an object")
    if isinstance(data.get("records"), list):
        return [r for r in data["records"] if isinstance(r, dict)]
    return list(iter_material_records(extract_discover_report(data)))


def report_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    epmc_articles: list[dict[str, Any]] = []
    epmc_stats: dict[str, Any] | None = None
    ct_studies: list[dict[str, Any]] = []
    ct_meta: dict[str, Any] = {}
    kg_summary: dict[str, Any] = {}
    kg_pathways: list[dict[str, Any]] = []
    chembl_header: dict[str, Any] = {}
    chembl_mechs: list[dict[str, Any]] = []
    chembl_acts: list[dict[str, Any]] = []
    pb_meta: dict[str, Any] = {}
    pb_summaries: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    faers: dict[str, Any] | None = None
    oa_g_meta: dict[str, Any] = {}
    oa_g_works: list[dict[str, Any]] = []
    oa_r_meta: dict[str, Any] = {}
    oa_r_works: list[dict[str, Any]] = []

    for row in records:
        if not isinstance(row, dict):
            continue
        st = row.get("source_type")
        content = row.get("content")
        if st == "compound_name" and isinstance(content, dict):
            report["compound_name"] = content.get("compound_name")
        elif st == "discover_metadata" and isinstance(content, dict):
            report["metadata"] = content
        elif st == "europe_pmc_query_stats" and isinstance(content, dict):
            epmc_stats = content
        elif st == "europe_pmc" and isinstance(content, dict):
            epmc_articles.append(content)
        elif st == "clinical_trials_meta" and isinstance(content, dict):
            ct_meta.update(content)
        elif st == "clinical_trials" and isinstance(content, dict):
            ct_studies.append(content)
        elif st == "kegg_summary" and isinstance(content, dict):
            kg_summary.update(content)
        elif st == "kegg_pathway" and isinstance(content, dict):
            kg_pathways.append(content)
        elif st == "chembl_molecule" and isinstance(content, dict):
            chembl_header.update(content)
        elif st == "chembl_mechanism" and isinstance(content, dict):
            chembl_mechs.append(content)
        elif st == "chembl_activity" and isinstance(content, dict):
            chembl_acts.append(content)
        elif st == "pubchem_bioassays_meta" and isinstance(content, dict):
            pb_meta.update(content)
        elif st == "pubchem_bioassay" and isinstance(content, dict):
            pb_summaries.append(content)
        elif st == "openfda_faers" and isinstance(content, dict):
            faers = content
        elif st == "openfda_drug_label" and isinstance(content, dict):
            labels.append(content)
        elif st == "openalex_grounding_meta" and isinstance(content, dict):
            oa_g_meta.update(content)
        elif st == "openalex_grounding" and isinstance(content, dict):
            oa_g_works.append(content)
        elif st == "openalex_risk_meta" and isinstance(content, dict):
            oa_r_meta.update(content)
        elif st == "openalex_risk" and isinstance(content, dict):
            oa_r_works.append(content)

    if epmc_articles or epmc_stats:
        report["europe_pmc"] = {
            "articles": epmc_articles,
            "unique_count": len(epmc_articles),
            **({"query_stats": epmc_stats} if epmc_stats else {}),
        }
    if ct_studies or ct_meta:
        report["clinical_trials"] = {
            **ct_meta,
            "studies": ct_studies,
            "study_count": ct_meta.get("study_count", len(ct_studies)),
        }
    if kg_summary or kg_pathways:
        report["kegg"] = {**kg_summary, "pathways": kg_pathways}
    if chembl_header or chembl_mechs or chembl_acts:
        report["chembl"] = {
            **chembl_header,
            "mechanisms": chembl_mechs,
            "activities": chembl_acts,
        }
    if pb_meta or pb_summaries:
        report["pubchem_bioassays"] = {
            **pb_meta,
            "summaries": pb_summaries,
            "assay_count": pb_meta.get("assay_count", len(pb_summaries)),
        }
    if faers is not None or labels:
        report["openfda"] = {
            "adverse_events": faers,
            "drug_labels": labels,
        }
    if oa_g_meta or oa_g_works:
        report["openalex_grounding"] = {**oa_g_meta, "works": oa_g_works}
    if oa_r_meta or oa_r_works:
        report["openalex_risk"] = {**oa_r_meta, "works": oa_r_works}
    return report


def extract_discover_report(data: dict[str, Any]) -> dict[str, Any]:
    """Nested discover report from material wrapper or legacy report JSON."""
    if data.get("schema_hint") == MATERIAL_SCHEMA and isinstance(data.get("report"), dict):
        return data["report"]
    if isinstance(data.get("report"), dict):
        return data["report"]
    return data


def material_path_to_report(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{path}: empty material file")
    if _is_jsonl_material(text):
        return report_from_records(load_material_records(path))
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level JSON must be an object")
    if isinstance(data.get("records"), list):
        return report_from_records(data["records"])
    return extract_discover_report(data)


def find_discover_source(compound_dir: Path) -> Path | None:
    material = compound_dir / MATERIAL_FILENAME
    if material.is_file():
        return material.resolve()
    reports = sorted(compound_dir.glob("report_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0].resolve() if reports else None


def ensure_material_json(
    compound_dir: Path,
    compound_name: str | None = None,
) -> Path | None:
    """Return ``material.json`` path, converting the newest ``report_*.json`` if needed."""
    material = compound_dir / MATERIAL_FILENAME
    if material.is_file():
        return material.resolve()
    reports = sorted(compound_dir.glob("report_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return None
    report_path = reports[0]
    try:
        report = material_path_to_report(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if compound_name and not report.get("compound_name"):
        report["compound_name"] = compound_name
    text = serialize_material_jsonl(report)
    if not text.strip():
        return None
    material.write_text(text, encoding="utf-8")
    return material.resolve()
