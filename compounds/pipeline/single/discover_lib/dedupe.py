"""Canonical dedupe keys for discover material rows and downstream grouping."""

from __future__ import annotations

import hashlib
import re
from typing import Any


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())[:120]


def dedupe_key(source_type: str, content: Any) -> str | None:
    """Stable key for a material row; None skips dedupe (meta rows)."""
    st = (source_type or "").strip()
    if not st or st.endswith("_meta") or st in {
        "compound_name",
        "discover_metadata",
        "discover_round_meta",
        "europe_pmc_query_stats",
        "pubchem_bioassays_meta",
        "clinical_trials_meta",
        "openalex_grounding_meta",
        "openalex_risk_meta",
    }:
        return None
    if not isinstance(content, dict):
        content = {}

    for field, prefix in (
        ("pmid", "pmid"),
        ("pmcid", "pmcid"),
        ("doi", "doi"),
        ("openalex_id", "openalex"),
        ("nct_id", "nct"),
        ("aid", "aid"),
        ("activity_id", "activity"),
        ("pathway_id", "kegg"),
    ):
        val = content.get(field)
        if val is not None and str(val).strip():
            return f"{prefix}:{str(val).strip().lower()}"

    if st == "openfda_faers":
        return "faers:aggregate"

    if st == "openfda_drug_label":
        for key in ("set_id", "id", "spl_id"):
            val = content.get(key)
            if val is not None and str(val).strip():
                return f"label:{str(val).strip().lower()}"
        title = content.get("title") or content.get("brand_name")
        if isinstance(title, str) and title.strip():
            return f"label:title:{_norm_title(title)}"

    if st == "chembl_mechanism":
        parts = [
            str(content.get("target_chembl_id") or ""),
            str(content.get("mechanism_of_action") or ""),
            str(content.get("action_type") or ""),
        ]
        digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
        return f"chembl_mech:{digest}"

    title = content.get("title") or content.get("brief_title") or content.get("official_title")
    if isinstance(title, str) and title.strip():
        digest = hashlib.sha256(_norm_title(title).encode()).hexdigest()[:16]
        return f"{st}:title:{digest}"

    excerpt = str(content.get("assay_description") or content.get("prose") or "")[:200]
    if excerpt.strip():
        digest = hashlib.sha256(excerpt.encode()).hexdigest()[:16]
        return f"{st}:hash:{digest}"

    return None


def dedupe_key_for_row(row: dict[str, Any]) -> str | None:
    return dedupe_key(str(row.get("source_type") or ""), row.get("content"))


class DiscoverRegistry:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.count_by_source: dict[str, int] = {}

    def add_row(self, row: dict[str, Any]) -> bool:
        """Return True if row is new and should be kept."""
        key = dedupe_key_for_row(row)
        if key is None:
            return True
        if key in self.seen:
            return False
        self.seen.add(key)
        st = str(row.get("source_type") or "unknown")
        self.count_by_source[st] = self.count_by_source.get(st, 0) + 1
        return True

    def filter_new_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            if self.add_row(row):
                out.append(row)
        return out

    def load_keys(self, keys: set[str]) -> None:
        self.seen.update(keys)

    def keys_from_material_rows(self, rows: list[dict[str, Any]]) -> set[str]:
        keys: set[str] = set()
        for row in rows:
            key = dedupe_key_for_row(row)
            if key:
                keys.add(key)
                self.seen.add(key)
        return keys
