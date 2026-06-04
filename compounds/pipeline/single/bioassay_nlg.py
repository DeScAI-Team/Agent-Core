#!/usr/bin/env python3
"""Thin wrapper around DeScAI BioAssay-NLG for single-CID assay prose summaries."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import requests

_COMPOUNDS_DIR = Path(__file__).resolve().parents[2]
_BIOASSAY_EXTRACTOR = _COMPOUNDS_DIR / "BioAssay-NLG" / "assay-extractor"
PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUG_TIMEOUT = 20

_cid_sids_cache: dict[int, set[int]] = {}


def _ensure_extractor_path() -> bool:
    if not _BIOASSAY_EXTRACTOR.is_dir():
        return False
    path = str(_BIOASSAY_EXTRACTOR)
    if path not in sys.path:
        sys.path.insert(0, path)
    return True


def fetch_sids_for_cid(cid: int, *, fail: list | None = None) -> set[int]:
    """Fetch all PubChem SIDs for a CID via PUG REST (cached per process)."""
    if cid in _cid_sids_cache:
        return _cid_sids_cache[cid]

    url = f"{PUG}/compound/cid/{cid}/sids/JSON"
    try:
        r = requests.get(url, timeout=PUG_TIMEOUT)
        if not r.ok:
            if fail is not None:
                fail.append({"step": f"pubchem_cid_sids:{cid}", "reason": f"HTTP {r.status_code}: {r.text[:300]}"})
            _cid_sids_cache[cid] = set()
            return set()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        if fail is not None:
            fail.append({"step": f"pubchem_cid_sids:{cid}", "reason": str(exc)})
        _cid_sids_cache[cid] = set()
        return set()

    sids: set[int] = set()
    infos = (data.get("InformationList") or {}).get("Information") or []
    if isinstance(infos, list):
        for info in infos:
            if not isinstance(info, dict):
                continue
            raw = info.get("SID") or []
            if not isinstance(raw, list):
                raw = [raw]
            for sid in raw:
                try:
                    sids.add(int(sid))
                except (TypeError, ValueError):
                    pass

    _cid_sids_cache[cid] = sids
    return sids


def summarize_assay_for_cid(
    assay_json: dict[str, Any],
    cid: int,
    *,
    cid_sids: set[int] | None = None,
    fail: list | None = None,
) -> str | None:
    """Return natural-language prose for one compound in one PubChem assay JSON."""
    if not _ensure_extractor_path():
        return None

    from aggregator import (  # noqa: WPS433
        IndividualTestAggregator,
        build_tid_lookup,
        extract_full_metadata,
    )
    from templates import render_natural_language  # noqa: WPS433

    aggregator = IndividualTestAggregator(assay_json)
    tid_lookup = build_tid_lookup(assay_json)
    metadata = extract_full_metadata(assay_json)

    all_sids = list(aggregator.get_all_sids())
    if not all_sids:
        return None

    known = cid_sids if cid_sids is not None else fetch_sids_for_cid(cid, fail=fail)
    if not known:
        return None

    target_sids = [sid for sid in all_sids if sid in known]
    if not target_sids:
        return None

    aggregated = aggregator.aggregate_for_sids(target_sids)
    return render_natural_language(cid, target_sids, metadata, tid_lookup, aggregated)
