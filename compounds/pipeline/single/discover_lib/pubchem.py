"""PubChem bioassay fetch and BioAssay-NLG summarization."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import bioassay_nlg
from .http import CallBudget, req

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
BIOASSAY_MAX = 8
_RANK_KEYWORDS = re.compile(
    r"longevity|aging|lifespan|healthspan|senescence|cytotox|toxic|viability|"
    r"autophagy|mtor|ampk|apoptosis|prolif|cell.?line|survival|osteoclast",
    re.I,
)


def _parse_assaysummary_rows(data: dict) -> list[dict[str, Any]]:
    """Parse PubChem assaysummary Table into one dict per unique AID."""
    table = data.get("Table") or {}
    columns_raw = (table.get("Columns") or {}).get("Column") or []
    if isinstance(columns_raw, str):
        columns = [columns_raw]
    elif isinstance(columns_raw, list):
        columns = [str(c) for c in columns_raw]
    else:
        columns = []

    rows_in = table.get("Row") or []
    if not isinstance(rows_in, list):
        rows_in = [rows_in] if rows_in else []

    by_aid: dict[int, dict[str, Any]] = {}
    for row in rows_in:
        if not isinstance(row, dict):
            continue
        cells = row.get("Cell") or []
        if not isinstance(cells, list):
            continue
        rec: dict[str, Any] = {}
        for i, col in enumerate(columns):
            if i < len(cells):
                rec[col] = cells[i]
        aid_raw = rec.get("AID")
        try:
            aid = int(aid_raw)
        except (TypeError, ValueError):
            continue
        title = rec.get("Assay Name") or rec.get("Assay Name".replace(" ", "")) or ""
        if aid not in by_aid:
            by_aid[aid] = {"aid": aid, "title": title, "assay_type": rec.get("Assay Type"), "row_count": 1}
        else:
            by_aid[aid]["row_count"] = int(by_aid[aid].get("row_count") or 0) + 1
            if title and not by_aid[aid].get("title"):
                by_aid[aid]["title"] = title
    return list(by_aid.values())


def _rank_assay(summary: dict) -> tuple[int, int]:
    title = str(summary.get("title") or "")
    kw = len(_RANK_KEYWORDS.findall(title))
    active = int(summary.get("row_count") or 0)
    return (kw, active)


def fetch_pubchem_bioassays(
    primary_cid: int | None,
    fail: list,
    budget: CallBudget,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "cid": primary_cid,
        "assay_count": 0,
        "summaries": [],
        "nlg_skipped_reason": None,
    }
    if primary_cid is None:
        out["nlg_skipped_reason"] = "no_pubchem_cid"
        return out

    summary_url = f"{PUG}/compound/cid/{primary_cid}/assaysummary/JSON"
    summary_data = req(summary_url, fail, "pubchem_assaysummary", budget, json_out=True, timeout=20)
    if not isinstance(summary_data, dict):
        return out

    parsed = _parse_assaysummary_rows(summary_data)
    ranked = sorted(parsed, key=_rank_assay, reverse=True)
    top = ranked[:BIOASSAY_MAX]

    if not bioassay_nlg._ensure_extractor_path():
        out["nlg_skipped_reason"] = "BioAssay-NLG not found at compounds/BioAssay-NLG/"
        fail.append({"step": "pubchem_bioassay_nlg", "reason": out["nlg_skipped_reason"]})

    cid_sids = bioassay_nlg.fetch_sids_for_cid(int(primary_cid), fail=fail)
    if not cid_sids and not out.get("nlg_skipped_reason"):
        out["nlg_skipped_reason"] = "no SIDs returned from PubChem for CID"

    summaries: list[dict[str, Any]] = []
    for summary in top:
        aid = summary.get("aid")
        if not aid:
            continue
        assay_url = f"{PUG}/assay/aid/{aid}/JSON"
        assay_json = req(assay_url, fail, f"pubchem_assay:{aid}", budget, json_out=True, timeout=20)
        if not isinstance(assay_json, dict):
            continue
        title = summary.get("title") or ""
        prose = None
        if cid_sids and bioassay_nlg._ensure_extractor_path():
            prose = bioassay_nlg.summarize_assay_for_cid(
                assay_json, int(primary_cid), cid_sids=cid_sids, fail=fail,
            )
        summaries.append({
            "aid": aid,
            "title": title,
            "prose": prose,
            "prose_available": bool(prose),
        })

    out["assay_count"] = len(summaries)
    out["summaries"] = summaries
    return out
