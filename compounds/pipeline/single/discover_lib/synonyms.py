"""PubChem PUG REST synonym and CID resolution."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .http import CallBudget, req

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
MAX_SYNONYMS = 8
_CAS_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")


def _is_cas_like(name: str) -> bool:
    return bool(_CAS_RE.match(name.strip()))


def _score_synonym(name: str, primary_token: str) -> tuple[int, int]:
    lower = name.lower()
    token = primary_token.lower()
    contains = 0 if token and token in lower else 1
    cas_penalty = 1 if _is_cas_like(name) else 0
    length = len(name)
    return (contains, cas_penalty, length)


def _rank_synonyms(names: list[str], primary: str) -> list[str]:
    token = primary.split()[0] if primary.split() else primary
    unique: dict[str, str] = {}
    for n in names:
        s = n.strip()
        if not s:
            continue
        key = s.lower()
        if key not in unique:
            unique[key] = s
    ranked = sorted(unique.values(), key=lambda x: _score_synonym(x, token))
    return ranked[:MAX_SYNONYMS]


def resolve_compound_identity(compound: str, fail: list, budget: CallBudget) -> dict[str, Any]:
    """Resolve PubChem CID and query names for fan-out searches."""
    primary = compound.strip()
    out: dict[str, Any] = {
        "primary_cid": None,
        "synonyms": [],
        "query_names": [primary] if primary else [],
    }
    if not primary:
        return out

    cid_url = f"{PUG}/compound/name/{quote(primary, safe='')}/cids/JSON"
    cid_data = req(cid_url, fail, "pubchem_cids", budget, json_out=True)
    cid: int | None = None
    if isinstance(cid_data, dict):
        block = cid_data.get("IdentifierList") or {}
        cids = block.get("CID") or []
        if isinstance(cids, list) and cids:
            try:
                cid = int(cids[0])
            except (TypeError, ValueError):
                pass
    out["primary_cid"] = cid

    syn_url = f"{PUG}/compound/name/{quote(primary, safe='')}/synonyms/JSON"
    syn_data = req(syn_url, fail, "pubchem_synonyms", budget, json_out=True)
    raw_syns: list[str] = []
    if isinstance(syn_data, dict):
        block = syn_data.get("InformationList") or {}
        infos = block.get("Information") or []
        if isinstance(infos, list) and infos:
            syns = infos[0].get("Synonym") if isinstance(infos[0], dict) else None
            if isinstance(syns, list):
                raw_syns = [str(s) for s in syns if s]

    ranked = _rank_synonyms([primary, *raw_syns], primary)
    out["synonyms"] = ranked
    out["query_names"] = ranked if ranked else [primary]
    return out
