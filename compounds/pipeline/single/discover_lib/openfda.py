"""OpenFDA FAERS and drug label fetch with multi-alias queries."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from .http import CallBudget, req

FDA = "https://api.fda.gov/"
LABELS = (
    "adverse_reactions", "adverse_reactions_table", "boxed_warning", "boxed_warning_table",
    "contraindications", "clinical_pharmacology", "pharmacokinetics", "mechanism_of_action", "drug_interactions",
)


def L(x: Any) -> list[Any]:
    return [] if x is None else x if isinstance(x, list) else [x]


def _label_matches_alias(label: dict, aliases_lower: set[str]) -> bool:
    of = label.get("openfda") or {}
    if not isinstance(of, dict):
        return False
    fields = (
        of.get("generic_name") or [],
        of.get("substance_name") or [],
        of.get("brand_name") or [],
    )
    for field in fields:
        if not isinstance(field, list):
            continue
        for val in field:
            if not isinstance(val, str):
                continue
            vl = val.lower()
            for alias in aliases_lower:
                if alias in vl:
                    return True
    return False


def fetch_openfda(
    query_names: list[str],
    fail: list,
    budget: CallBudget,
    ver: dict[str, Any],
) -> tuple[dict[str, Any], list[dict], int, dict[str, Any]]:
    """Return openfda block, metadata about aliases tried."""
    aliases_tried: dict[str, Any] = {"faers": [], "labels": []}
    all_terms: set[str] = set()
    max_report_count = 0
    meta = None

    for name in query_names:
        if not name.strip():
            continue
        ev = req(
            urljoin(FDA, "drug/event.json"),
            fail,
            f"openfda_event:{name}",
            budget,
            params={"search": f"patient.drug.medicinalproduct:{name.upper()}", "limit": 100},
            json_out=True,
        )
        aliases_tried["faers"].append({"name": name, "hit": bool(isinstance(ev, dict) and ev.get("results"))})
        if isinstance(ev, dict) and ev.get("meta"):
            meta = ev["meta"]
            ver["openfda"] = meta
        if isinstance(ev, dict):
            rows = ev.get("results")
            if isinstance(rows, list):
                max_report_count = max(max_report_count, len(rows))
                for it in rows:
                    for p in L(it.get("patient")):
                        if isinstance(p, dict):
                            for rx in L(p.get("reaction")):
                                if isinstance(rx, dict):
                                    t = rx.get("reactionmeddrapt") or rx.get("reactionmeddraversionpt")
                                    if isinstance(t, str) and t.strip():
                                        all_terms.add(t.strip())

    ae = {
        "reaction_terms": sorted(all_terms),
        "report_count": max_report_count,
        "meta": meta,
    }

    raw_labels: list[dict] = []
    for name in query_names:
        if not name.strip():
            continue
        for field, fmt in (
            ("openfda.generic_name", name.title()),
            ("openfda.substance_name", name.title()),
            ("openfda.brand_name", name.title()),
        ):
            lb = req(
                urljoin(FDA, "drug/label.json"),
                fail,
                f"openfda_label:{field}:{name}",
                budget,
                params={"search": f"{field}:{fmt}", "limit": 10},
                json_out=True,
            )
            hit = bool(isinstance(lb, dict) and lb.get("results"))
            aliases_tried["labels"].append({"name": name, "field": field, "hit": hit})
            if ver["openfda"] is None and isinstance(lb, dict) and lb.get("meta"):
                ver["openfda"] = lb["meta"]
                meta = lb["meta"]
            if isinstance(lb, dict):
                rs = lb.get("results")
                if isinstance(rs, list):
                    raw_labels.extend([x for x in rs if isinstance(x, dict)])

    aliases_lower = {n.lower() for n in query_names if n.strip()}
    seen_ids: set[str] = set()
    filtered: list[dict] = []
    for x in raw_labels:
        key = str(x.get("set_id") or id(x))
        if key in seen_ids:
            continue
        if _label_matches_alias(x, aliases_lower):
            seen_ids.add(key)
            filtered.append({k: x.get(k) for k in LABELS})

    label_filter_dropped = len(raw_labels) - len(filtered)
    dl = filtered

    return {"adverse_events": ae, "drug_labels": dl}, dl, label_filter_dropped, aliases_tried
