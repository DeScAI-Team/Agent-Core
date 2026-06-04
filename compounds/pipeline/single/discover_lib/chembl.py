"""ChEMBL molecule search, mechanisms, and bioactivities."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .http import CallBudget, req

CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
MAX_SEARCH_NAMES = 3


def _slim_mechanism(row: dict) -> dict[str, Any]:
    target = row.get("target_chembl_id") or ""
    return {
        "target_chembl_id": target,
        "target_pref_name": row.get("target_pref_name"),
        "mechanism_of_action": row.get("mechanism_of_action"),
        "action_type": row.get("action_type"),
        "direct_interaction": row.get("direct_interaction"),
    }


def _slim_activity(row: dict) -> dict[str, Any]:
    return {
        "activity_id": row.get("activity_id"),
        "assay_type": row.get("assay_type"),
        "assay_description": row.get("assay_description"),
        "target_pref_name": row.get("target_pref_name"),
        "standard_type": row.get("standard_type"),
        "standard_value": row.get("standard_value"),
        "standard_units": row.get("standard_units"),
        "pchembl_value": row.get("pchembl_value"),
        "molecule_pref_name": row.get("molecule_pref_name"),
    }


def fetch_chembl(query_names: list[str], fail: list, budget: CallBudget) -> dict[str, Any] | None:
    molecule_id: str | None = None
    pref_name: str | None = None
    search_used: str | None = None

    for name in query_names[:MAX_SEARCH_NAMES]:
        if not name.strip():
            continue
        url = f"{CHEMBL}/molecule/search.json?q={quote(name)}&limit=5"
        data = req(url, fail, f"chembl_search:{name}", budget, json_out=True)
        if not isinstance(data, dict):
            continue
        molecules = data.get("molecules") or []
        if not isinstance(molecules, list) or not molecules:
            continue
        best = molecules[0]
        if isinstance(best, dict):
            molecule_id = best.get("molecule_chembl_id")
            pref_name = best.get("pref_name")
            search_used = name
            if molecule_id:
                break

    if not molecule_id:
        return {
            "molecule_chembl_id": None,
            "pref_name": None,
            "mechanisms": [],
            "activities": [],
            "search_name_used": search_used,
        }

    mech_url = f"{CHEMBL}/mechanism.json?molecule_chembl_id={quote(molecule_id)}&limit=20"
    mech_data = req(mech_url, fail, "chembl_mechanisms", budget, json_out=True)
    mechanisms: list[dict] = []
    if isinstance(mech_data, dict):
        for row in mech_data.get("mechanisms") or []:
            if isinstance(row, dict):
                mechanisms.append(_slim_mechanism(row))

    act_url = f"{CHEMBL}/activity.json?molecule_chembl_id={quote(molecule_id)}&limit=25"
    act_data = req(act_url, fail, "chembl_activities", budget, json_out=True)
    activities: list[dict] = []
    if isinstance(act_data, dict):
        for row in act_data.get("activities") or []:
            if isinstance(row, dict):
                activities.append(_slim_activity(row))

    return {
        "molecule_chembl_id": molecule_id,
        "pref_name": pref_name,
        "mechanisms": mechanisms,
        "activities": activities,
        "search_name_used": search_used,
    }
