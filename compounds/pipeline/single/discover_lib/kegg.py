"""KEGG REST drug/pathway lookup with multi-name fan-out."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin

from .http import CallBudget, req

KEGG = "https://rest.kegg.jp/"
KEGG_MAX = 50
FLAGS = [
    ("mTOR", "mtor"), ("autophagy", "autophagy"), ("AMPK", "ampk"), ("apoptosis", "apoptosis"),
    ("cell cycle", "cell cycle"), ("oxidative stress", "oxidative stress"), ("NAD", "nad"),
    ("sirtuin", "sirtuin"), ("insulin signaling", "insulin signaling"), ("senescence", "senescence"),
]


def fetch_kegg(query_names: list[str], fail: list, budget: CallBudget) -> dict[str, Any] | None:
    drs: list[str] = []
    seen_dr: set[str] = set()
    for name in query_names:
        if not name.strip():
            continue
        txt = req(
            urljoin(KEGG, f"find/drug/{quote(name, safe='')}"),
            fail,
            f"kegg_find:{name}",
            budget,
            json_out=False,
        )
        if txt is None:
            continue
        for ln in txt.strip().splitlines():
            if "\t" not in ln:
                continue
            dr = ln.split("\t", 1)[0]
            if dr.startswith("dr:") and dr not in seen_dr:
                seen_dr.add(dr)
                drs.append(dr)

    if not drs:
        return {
            "kegg_drug_ids": [],
            "pathways": [],
            "pathway_names": [],
            "longevity_pathway_flags": {a: False for a, _ in FLAGS},
            "truncated": False,
        }

    pids: set[str] = set()
    for dr in drs:
        lt = req(urljoin(KEGG, f"link/pathway/{dr}"), fail, f"kegg_link:{dr}", budget, json_out=False)
        if lt:
            for line in lt.strip().splitlines():
                pids.update(p[5:] for p in line.split("\t") if p.startswith("path:"))

    ordered = sorted(pids)
    ent = []
    for pid in ordered[:KEGG_MAX]:
        body = req(urljoin(KEGG, f"get/{pid}"), fail, f"kegg_get:{pid}", budget, json_out=False) or ""
        name = next((ln[4:].strip() or None for ln in body.splitlines() if ln.startswith("NAME")), None)
        m = re.search(r"^DESCRIPTION\s+(.+?)(?=^\w+\s+|\Z)", body, re.M | re.S)
        desc = (m.group(1).strip()[:2000] or None) if m else None
        ent.append({"pathway_id": pid, "name": name, "description_snippet": desc})

    blob = " ".join(f'{e.get("name") or ""} {e.get("description_snippet") or ""}' for e in ent).lower()
    return {
        "kegg_drug_ids": drs,
        "pathway_ids": ordered,
        "pathway_count": len(ordered),
        "pathways": ent,
        "pathway_names": [e["name"] for e in ent if e.get("name")],
        "longevity_pathway_flags": {a: (b in blob) for a, b in FLAGS},
        "truncated": len(ordered) > KEGG_MAX,
        "pathway_get_limit": KEGG_MAX,
    }
