"""Incremental discover session: batched fetch, registry dedupe, per-source caps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chembl import fetch_chembl
from .clinical_trials import slim_study
from .dedupe import DiscoverRegistry
from .epmc import L, _build_queries
from .http import CallBudget, req
from .kegg import fetch_kegg
from .limits import DiscoverLimits, load_limits
from .openalex import (
    grounding_search_terms,
    risk_search_terms,
    search_openalex,
)
from .openfda import fetch_openfda
from .pubchem import fetch_pubchem_bioassays
from .synonyms import resolve_compound_identity

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _emit(source_type: str, content: Any) -> dict[str, Any]:
    return {"source_type": source_type, "content": content}


@dataclass
class DiscoverSession:
    compound: str
    query_names: list[str]
    primary_cid: int | None
    limits: DiscoverLimits = field(default_factory=load_limits)
    registry: DiscoverRegistry = field(default_factory=DiscoverRegistry)
    fail: list = field(default_factory=list)
    ver: dict[str, Any] = field(default_factory=dict)
    budget: CallBudget | None = None
    round_num: int = 0

    # Per-source cursor / cache state
    epmc_cursors: dict[str, str | None] = field(default_factory=dict)
    epmc_query_order: list[str] = field(default_factory=list)
    epmc_query_idx: int = 0
    epmc_stats: dict[str, Any] = field(default_factory=dict)

    ct_alias_idx: int = 0
    ct_done: bool = False

    chembl_cache: dict[str, Any] | None = None
    chembl_mech_idx: int = 0
    chembl_act_idx: int = 0
    chembl_exhausted: bool = False

    pubchem_cache: dict[str, Any] | None = None
    pubchem_aid_idx: int = 0
    pubchem_exhausted: bool = False

    openfda_faers_done: bool = False
    openfda_labels: list[dict[str, Any]] | None = None
    openfda_label_idx: int = 0
    openfda_exhausted: bool = False
    label_filter_dropped: int = 0
    aliases_tried: dict[str, Any] = field(default_factory=dict)

    kegg_cache: dict[str, Any] | None = None
    kegg_path_idx: int = 0
    kegg_exhausted: bool = False

    oa_grounding_term_idx: int = 0
    oa_risk_term_idx: int = 0
    oa_grounding_exhausted: bool = False
    oa_risk_exhausted: bool = False

    source_exhausted: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def create(cls, compound: str) -> DiscoverSession:
        fail: list = []
        budget = CallBudget(limit=load_limits().http_calls_one_shot)
        identity = resolve_compound_identity(compound, fail, budget)
        query_names = identity.get("query_names") or [compound]
        session = cls(
            compound=compound,
            query_names=query_names,
            primary_cid=identity.get("primary_cid"),
            fail=fail,
            ver={
                "openfda": None,
                "clinical_trials_gov": None,
                "kegg": "KEGG REST",
                "europe_pmc": None,
                "pubchem": "PUG REST",
                "chembl": "ChEMBL REST API",
                "openalex": "OpenAlex REST API",
            },
            budget=budget,
        )
        session.epmc_query_order = [qn for qn, _, _ in _build_queries(compound, query_names)]
        return session

    def new_round_budget(self) -> None:
        self.round_num += 1
        self.budget = CallBudget(limit=self.limits.http_calls_per_round)

    def _total(self, source: str) -> int:
        return self.registry.count_by_source.get(source, 0)

    def _at_total_cap(self, source: str, cap: int) -> bool:
        return self._total(source) >= cap

    def _batch_epmc(self) -> list[dict[str, Any]]:
        caps = self.limits.europe_pmc
        if self.source_exhausted.get("europe_pmc") or self._at_total_cap("europe_pmc", caps.total):
            return []
        out: list[dict[str, Any]] = []
        queries = _build_queries(self.compound, self.query_names)
        if not self.epmc_query_order:
            self.epmc_query_order = [qn for qn, _, _ in queries]
        qmap = {qn: (q, ps) for qn, q, ps in queries}
        budget = self.budget
        if budget is None:
            return []

        round_budget = caps.batch
        while round_budget > 0 and self.epmc_query_idx < len(self.epmc_query_order):
            qn = self.epmc_query_order[self.epmc_query_idx]
            if self.epmc_cursors.get(qn) is False:
                self.epmc_query_idx += 1
                continue
            q, page_size = qmap.get(qn, (None, 30))
            if not q:
                self.epmc_query_idx += 1
                continue
            page_size = min(page_size, round_budget, caps.batch)
            params: dict[str, Any] = {
                "query": q,
                "format": "json",
                "pageSize": page_size,
                "resultType": "core",
            }
            cursor = self.epmc_cursors.get(qn)
            if cursor:
                params["cursorMark"] = cursor
            d = req(
                EPMC,
                self.fail,
                f"europe_pmc:{qn}",
                budget,
                params=params,
                json_out=True,
            )
            if self.ver.get("europe_pmc") is None and isinstance(d, dict):
                self.ver["europe_pmc"] = {k: d.get(k) for k in ("version", "release", "hitCount")}
            self.epmc_stats[qn] = {
                "query": q,
                "hitCount": d.get("hitCount") if isinstance(d, dict) else None,
                "pageSize": page_size,
            }
            if not isinstance(d, dict):
                self.epmc_cursors[qn] = False
                self.epmc_query_idx += 1
                continue
            next_cursor = d.get("nextCursorMark")
            rl = d.get("resultList")
            hits = L(rl.get("result")) if isinstance(rl, dict) else []
            if not hits:
                self.epmc_cursors[qn] = False
                self.epmc_query_idx += 1
                continue
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                if self._at_total_cap("europe_pmc", caps.total):
                    self.source_exhausted["europe_pmc"] = True
                    break
                key = None
                for pref, fld in (("pmid", "pmid"), ("pmcid", "pmcid"), ("doi", "doi"), ("id", "id")):
                    v = hit.get(fld)
                    if v:
                        key = f"{pref}:{v}"
                        break
                if not key:
                    continue
                s = hit.get("authorString")
                auth = [x.strip() for x in s.split(",") if x.strip()] if isinstance(s, str) else None
                rec = {
                    "title": hit.get("title"),
                    "authors": auth,
                    "journal": hit.get("journalTitle") or hit.get("journal"),
                    "year": hit.get("pubYear"),
                    "doi": hit.get("doi"),
                    "pmid": hit.get("pmid"),
                    "abstract": hit.get("abstractText") or hit.get("abstract"),
                    "citedByCount": hit.get("citedByCount"),
                    "source_queries": [qn],
                }
                row = _emit("europe_pmc", rec)
                if self.registry.add_row(row):
                    out.append(row)
                    round_budget -= 1
                    if round_budget <= 0:
                        break
            if next_cursor and next_cursor != cursor:
                self.epmc_cursors[qn] = next_cursor
            else:
                self.epmc_cursors[qn] = False
                self.epmc_query_idx += 1
            if self.source_exhausted.get("europe_pmc"):
                break
        if self.epmc_query_idx >= len(self.epmc_query_order):
            self.source_exhausted["europe_pmc"] = True
        return out

    def _batch_openalex(self, axis: str) -> list[dict[str, Any]]:
        caps = (
            self.limits.openalex_grounding
            if axis == "openalex_grounding"
            else self.limits.openalex_risk
        )
        st = axis
        if self.source_exhausted.get(st) or self._at_total_cap(st, caps.total):
            return []
        terms = (
            grounding_search_terms(self.compound)
            if axis == "openalex_grounding"
            else risk_search_terms(self.compound)
        )
        idx_attr = "oa_grounding_term_idx" if axis == "openalex_grounding" else "oa_risk_term_idx"
        exhausted_attr = "oa_grounding_exhausted" if axis == "openalex_grounding" else "oa_risk_exhausted"
        if getattr(self, exhausted_attr):
            return []
        out: list[dict[str, Any]] = []
        import requests

        session_http = requests.Session()
        idx = getattr(self, idx_attr)
        remaining = caps.batch
        while remaining > 0 and idx < len(terms):
            term = terms[idx]
            per_term = min(caps.batch, remaining, 6)
            works = search_openalex(
                term, session_http, per_term, log_prefix=axis.replace("_", "-"),
            )
            for work in works:
                if self._at_total_cap(st, caps.total):
                    setattr(self, exhausted_attr, True)
                    self.source_exhausted[st] = True
                    break
                entry = dict(work)
                entry["search_term"] = term
                row = _emit(st, entry)
                if self.registry.add_row(row):
                    out.append(row)
                    remaining -= 1
                    if remaining <= 0:
                        break
            idx += 1
            if getattr(self, exhausted_attr):
                break
        setattr(self, idx_attr, idx)
        if idx >= len(terms):
            setattr(self, exhausted_attr, True)
            self.source_exhausted[st] = True
        return out

    def _batch_clinical_trials(self) -> list[dict[str, Any]]:
        caps = self.limits.clinical_trials
        if self.ct_done or self.source_exhausted.get("clinical_trials") or self._at_total_cap(
            "clinical_trials", caps.total
        ):
            return []
        names = [n for n in self.query_names if n.strip()]
        if not names:
            return []
        out: list[dict[str, Any]] = []
        budget = self.budget
        if budget is None:
            return []
        remaining = caps.batch
        vh_box: list[str | None] = [None]
        from .clinical_trials import CT

        while remaining > 0 and self.ct_alias_idx < len(names):
            name = names[self.ct_alias_idx]
            page_size = min(100, remaining, caps.batch)
            ct_raw = req(
                CT,
                self.fail,
                f"clinical_trials:{name}",
                budget,
                params={"query.term": name, "pageSize": page_size, "format": "json"},
                json_out=True,
            )
            if isinstance(ct_raw, dict):
                studies = ct_raw.get("studies")
                if isinstance(studies, list):
                    for s in studies:
                        if not isinstance(s, dict):
                            continue
                        if self._at_total_cap("clinical_trials", caps.total):
                            self.ct_done = True
                            self.source_exhausted["clinical_trials"] = True
                            break
                        slim = slim_study(s, vh_box)
                        row = _emit("clinical_trials", slim)
                        if self.registry.add_row(row):
                            out.append(row)
                            remaining -= 1
                            if remaining <= 0:
                                break
            self.ct_alias_idx += 1
            if self.source_exhausted.get("clinical_trials"):
                break
        if self.ct_alias_idx >= len(names):
            self.ct_done = True
            self.source_exhausted["clinical_trials"] = True
        if vh_box[0]:
            self.ver["clinical_trials_gov"] = {"api": "v2", "data_version_holder": vh_box[0]}
        return out

    def _batch_chembl(self) -> list[dict[str, Any]]:
        if self.chembl_exhausted:
            return []
        out: list[dict[str, Any]] = []
        budget = self.budget
        if budget is None:
            return []
        if self.chembl_cache is None:
            self.chembl_cache = fetch_chembl(self.query_names, self.fail, budget) or {}
            self.chembl_mech_idx = 0
            self.chembl_act_idx = 0
            header = {
                k: self.chembl_cache.get(k)
                for k in ("molecule_chembl_id", "pref_name", "search_name_used")
                if self.chembl_cache.get(k) is not None
            }
            if header:
                row = _emit("chembl_molecule", header)
                if self.registry.add_row(row):
                    out.append(row)

        mechs = self.chembl_cache.get("mechanisms") or []
        acts = self.chembl_cache.get("activities") or []
        mech_cap = self.limits.chembl_mechanism
        act_cap = self.limits.chembl_activity

        mech_batch = mech_cap.batch
        while mech_batch > 0 and self.chembl_mech_idx < len(mechs):
            if self._at_total_cap("chembl_mechanism", mech_cap.total):
                self.source_exhausted["chembl_mechanism"] = True
                break
            row = _emit("chembl_mechanism", mechs[self.chembl_mech_idx])
            self.chembl_mech_idx += 1
            if self.registry.add_row(row):
                out.append(row)
                mech_batch -= 1

        act_batch = act_cap.batch
        while act_batch > 0 and self.chembl_act_idx < len(acts):
            if self._at_total_cap("chembl_activity", act_cap.total):
                self.source_exhausted["chembl_activity"] = True
                break
            row = _emit("chembl_activity", acts[self.chembl_act_idx])
            self.chembl_act_idx += 1
            if self.registry.add_row(row):
                out.append(row)
                act_batch -= 1

        if (
            self.chembl_mech_idx >= len(mechs)
            and self.chembl_act_idx >= len(acts)
        ):
            self.chembl_exhausted = True
            self.source_exhausted["chembl_mechanism"] = True
            self.source_exhausted["chembl_activity"] = True
        return out

    def _batch_pubchem(self) -> list[dict[str, Any]]:
        caps = self.limits.pubchem_bioassay
        if self.pubchem_exhausted or self._at_total_cap("pubchem_bioassay", caps.total):
            self.source_exhausted["pubchem_bioassay"] = True
            return []
        out: list[dict[str, Any]] = []
        budget = self.budget
        if budget is None:
            return []
        if self.pubchem_cache is None:
            self.pubchem_cache = fetch_pubchem_bioassays(self.primary_cid, self.fail, budget)
            meta = {
                k: self.pubchem_cache.get(k)
                for k in ("cid", "assay_count", "nlg_skipped_reason")
                if k in self.pubchem_cache
            }
            if meta:
                row = _emit("pubchem_bioassays_meta", meta)
                if self.registry.add_row(row):
                    out.append(row)
        summaries = self.pubchem_cache.get("summaries") or []
        batch = caps.batch
        while batch > 0 and self.pubchem_aid_idx < len(summaries):
            if self._at_total_cap("pubchem_bioassay", caps.total):
                self.pubchem_exhausted = True
                self.source_exhausted["pubchem_bioassay"] = True
                break
            row = _emit("pubchem_bioassay", summaries[self.pubchem_aid_idx])
            self.pubchem_aid_idx += 1
            if self.registry.add_row(row):
                out.append(row)
                batch -= 1
        if self.pubchem_aid_idx >= len(summaries):
            self.pubchem_exhausted = True
            self.source_exhausted["pubchem_bioassay"] = True
        return out

    def _batch_openfda(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        budget = self.budget
        if budget is None:
            return []
        faers_cap = self.limits.openfda_faers
        if not self.openfda_faers_done and not self._at_total_cap("openfda_faers", faers_cap.total):
            block, _, dropped, aliases = fetch_openfda(
                self.query_names, self.fail, budget, self.ver,
            )
            self.label_filter_dropped = dropped
            self.aliases_tried = aliases
            ae = (block or {}).get("adverse_events")
            if isinstance(ae, dict):
                row = _emit("openfda_faers", ae)
                if self.registry.add_row(row):
                    out.append(row)
            self.openfda_faers_done = True
            self.source_exhausted["openfda_faers"] = True
            raw_labels = (block or {}).get("drug_labels") or []
            self.openfda_labels = raw_labels if isinstance(raw_labels, list) else []

        label_cap = self.limits.openfda_label
        labels = self.openfda_labels or []
        batch = label_cap.batch
        while batch > 0 and self.openfda_label_idx < len(labels):
            if self._at_total_cap("openfda_drug_label", label_cap.total):
                self.openfda_exhausted = True
                self.source_exhausted["openfda_drug_label"] = True
                break
            row = _emit("openfda_drug_label", labels[self.openfda_label_idx])
            self.openfda_label_idx += 1
            if self.registry.add_row(row):
                out.append(row)
                batch -= 1
        if self.openfda_label_idx >= len(labels):
            self.openfda_exhausted = True
            self.source_exhausted["openfda_drug_label"] = True
        return out

    def _batch_kegg(self) -> list[dict[str, Any]]:
        caps = self.limits.kegg_pathway
        if self.kegg_exhausted or self._at_total_cap("kegg_pathway", caps.total):
            self.source_exhausted["kegg_pathway"] = True
            return []
        out: list[dict[str, Any]] = []
        budget = self.budget
        if budget is None:
            return []
        if self.kegg_cache is None:
            self.kegg_cache = fetch_kegg(self.query_names, self.fail, budget) or {}
            summary = {
                k: self.kegg_cache.get(k)
                for k in (
                    "kegg_drug_ids",
                    "pathway_ids",
                    "pathway_count",
                    "pathway_names",
                    "longevity_pathway_flags",
                    "truncated",
                    "pathway_get_limit",
                )
                if self.kegg_cache.get(k) is not None
            }
            if summary:
                row = _emit("kegg_summary", summary)
                if self.registry.add_row(row):
                    out.append(row)
        pathways = self.kegg_cache.get("pathways") or []
        batch = caps.batch
        while batch > 0 and self.kegg_path_idx < len(pathways):
            if self._at_total_cap("kegg_pathway", caps.total):
                self.kegg_exhausted = True
                self.source_exhausted["kegg_pathway"] = True
                break
            row = _emit("kegg_pathway", pathways[self.kegg_path_idx])
            self.kegg_path_idx += 1
            if self.registry.add_row(row):
                out.append(row)
                batch -= 1
        if self.kegg_path_idx >= len(pathways):
            self.kegg_exhausted = True
            self.source_exhausted["kegg_pathway"] = True
        return out

    def fetch_round_rows(self) -> list[dict[str, Any]]:
        """Fetch one incremental round; return new deduped material rows."""
        self.new_round_budget()
        rows: list[dict[str, Any]] = []
        for batch_fn in (
            self._batch_epmc,
            lambda: self._batch_openalex("openalex_grounding"),
            lambda: self._batch_openalex("openalex_risk"),
            self._batch_clinical_trials,
            self._batch_chembl,
            self._batch_pubchem,
            self._batch_openfda,
            self._batch_kegg,
        ):
            rows.extend(batch_fn())
        return rows

    def all_sources_exhausted(self) -> bool:
        keys = (
            "europe_pmc",
            "openalex_grounding",
            "openalex_risk",
            "clinical_trials",
            "chembl_mechanism",
            "chembl_activity",
            "pubchem_bioassay",
            "openfda_faers",
            "openfda_drug_label",
            "kegg_pathway",
        )
        return all(self.source_exhausted.get(k, False) for k in keys)

    def build_final_report(self) -> dict[str, Any]:
        """Rebuild nested report from registry counts + metadata (for one-shot compat)."""
        return {
            "compound_name": self.compound,
            "metadata": {
                "incremental": True,
                "rounds": self.round_num,
                "registry_counts": dict(self.registry.count_by_source),
                "failures": list(self.fail),
                "api_versions": self.ver,
                "http_calls": self.budget.count if self.budget else 0,
                "label_filter_dropped": self.label_filter_dropped,
                "openfda_aliases_tried": self.aliases_tried,
            },
        }


def rows_from_one_shot_report(report: dict[str, Any], registry: DiscoverRegistry) -> list[dict[str, Any]]:
    """Convert full discover report to material rows with dedupe at emit time."""
    from .material import iter_material_records

    out: list[dict[str, Any]] = []
    for row in iter_material_records(report):
        if registry.add_row(row):
            out.append(row)
    return out
