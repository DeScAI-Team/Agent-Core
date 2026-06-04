"""Per-source batch and total caps for incremental discover."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class SourceCaps:
    batch: int
    total: int


@dataclass
class DiscoverLimits:
    europe_pmc: SourceCaps = field(default_factory=lambda: SourceCaps(20, 200))
    openalex_grounding: SourceCaps = field(default_factory=lambda: SourceCaps(4, 24))
    openalex_risk: SourceCaps = field(default_factory=lambda: SourceCaps(4, 24))
    clinical_trials: SourceCaps = field(default_factory=lambda: SourceCaps(30, 100))
    chembl_mechanism: SourceCaps = field(default_factory=lambda: SourceCaps(10, 20))
    chembl_activity: SourceCaps = field(default_factory=lambda: SourceCaps(15, 50))
    pubchem_bioassay: SourceCaps = field(default_factory=lambda: SourceCaps(2, 8))
    openfda_label: SourceCaps = field(default_factory=lambda: SourceCaps(5, 30))
    openfda_faers: SourceCaps = field(default_factory=lambda: SourceCaps(1, 1))
    kegg_pathway: SourceCaps = field(default_factory=lambda: SourceCaps(10, 40))
    max_rounds: int = 5
    http_calls_per_round: int = 40
    http_calls_one_shot: int = int(os.environ.get("DISCOVER_MAX_HTTP_CALLS", "80"))


def load_limits() -> DiscoverLimits:
    raw = os.environ.get("DISCOVER_LIMITS_JSON")
    if not raw:
        limits = DiscoverLimits()
        limits.max_rounds = int(os.environ.get("DISCOVER_MAX_ROUNDS", str(limits.max_rounds)))
        limits.http_calls_per_round = int(
            os.environ.get("DISCOVER_HTTP_PER_ROUND", str(limits.http_calls_per_round))
        )
        return limits
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return DiscoverLimits()

    def _cap(key: str, default: SourceCaps) -> SourceCaps:
        block = data.get(key) if isinstance(data, dict) else None
        if not isinstance(block, dict):
            return default
        return SourceCaps(
            int(block.get("batch", default.batch)),
            int(block.get("total", default.total)),
        )

    base = DiscoverLimits()
    return DiscoverLimits(
        europe_pmc=_cap("europe_pmc", base.europe_pmc),
        openalex_grounding=_cap("openalex_grounding", base.openalex_grounding),
        openalex_risk=_cap("openalex_risk", base.openalex_risk),
        clinical_trials=_cap("clinical_trials", base.clinical_trials),
        chembl_mechanism=_cap("chembl_mechanism", base.chembl_mechanism),
        chembl_activity=_cap("chembl_activity", base.chembl_activity),
        pubchem_bioassay=_cap("pubchem_bioassay", base.pubchem_bioassay),
        openfda_label=_cap("openfda_label", base.openfda_label),
        openfda_faers=_cap("openfda_faers", base.openfda_faers),
        kegg_pathway=_cap("kegg_pathway", base.kegg_pathway),
        max_rounds=int(data.get("max_rounds", base.max_rounds)) if isinstance(data, dict) else base.max_rounds,
        http_calls_per_round=int(data.get("http_calls_per_round", base.http_calls_per_round))
        if isinstance(data, dict)
        else base.http_calls_per_round,
        http_calls_one_shot=base.http_calls_one_shot,
    )
