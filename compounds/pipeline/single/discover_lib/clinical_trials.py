"""ClinicalTrials.gov v2 with multi-alias term merge."""

from __future__ import annotations

from typing import Any

from .http import CallBudget, req

CT = "https://clinicaltrials.gov/api/v2/studies"


def phases(dm: Any) -> Any:
    if not isinstance(dm, dict):
        return None
    for d in (dm, dm.get("designInfo") or {}):
        if isinstance(d, dict):
            for k in ("phases", "phase", "phasesList"):
                if d.get(k) is not None:
                    return d[k]
    return None


def slim_study(study: dict, vh: list) -> dict:
    ps = study.get("protocolSection")
    ps = ps if isinstance(ps, dict) else {}
    sm, cm, om, scm = (ps.get(k) or {} for k in ("statusModule", "conditionsModule", "outcomesModule", "sponsorCollaboratorsModule"))
    dm, idm = ps.get("designModule") or {}, ps.get("identificationModule") or {}
    misc = (study.get("derivedSection") or {}).get("miscInfoModule") or {}
    if not vh[0] and isinstance(misc.get("versionHolder"), str):
        vh[0] = misc["versionHolder"]
    lead = (scm.get("leadSponsor") or {})
    po, so = om.get("primary_outcomes"), om.get("secondary_outcomes")
    rs = study.get("resultsSection")
    om_mod = rs.get("outcomeMeasuresModule", {}) if isinstance(rs, dict) else {}
    ms = om_mod.get("outcomeMeasures")
    rsum = None
    if isinstance(rs, dict):
        rsum = {"has_outcome_measures_module": bool(om_mod), "outcome_measures_count": len(ms) if isinstance(ms, list) else None}
    os = ("measure", "description", "timeFrame")
    return {
        "nct_id": idm.get("nctId"),
        "brief_title": idm.get("briefTitle"),
        "phases": phases(dm),
        "conditions": cm.get("conditions"),
        "primary_outcomes": [{k: x.get(k) for k in os} for x in po if isinstance(x, dict)] if isinstance(po, list) else None,
        "secondary_outcomes": [{k: x.get(k) for k in os} for x in so if isinstance(x, dict)] if isinstance(so, list) else None,
        "overall_status": sm.get("overallStatus"),
        "start_date": sm.get("startDateStruct"),
        "primary_completion_date": sm.get("primaryCompletionDateStruct"),
        "completion_date": sm.get("completionDateStruct"),
        "study_first_submit_date": sm.get("studyFirstSubmitDate"),
        "has_results": study.get("hasResults"),
        "results_summary": rsum,
        "lead_sponsor_name": lead.get("name"),
        "lead_sponsor_class": lead.get("class"),
    }


def fetch_clinical_trials(query_names: list[str], fail: list, budget: CallBudget, ver: dict[str, Any]) -> dict[str, Any] | None:
    by_nct: dict[str, dict] = {}
    vh_box: list[str | None] = [None]

    for name in query_names:
        if not name.strip():
            continue
        ct_raw = req(
            CT,
            fail,
            f"clinical_trials:{name}",
            budget,
            params={"query.term": name, "pageSize": 100, "format": "json"},
            json_out=True,
        )
        if not isinstance(ct_raw, dict):
            continue
        studies = ct_raw.get("studies")
        if not isinstance(studies, list):
            continue
        for s in studies:
            if not isinstance(s, dict):
                continue
            slim = slim_study(s, vh_box)
            nct = slim.get("nct_id")
            if nct:
                by_nct[str(nct)] = slim

    ct_out = {"studies": list(by_nct.values()), "study_count": len(by_nct), "version_holder": vh_box[0]}
    ver["clinical_trials_gov"] = {"api": "v2", "data_version_holder": ct_out.get("version_holder")}
    return ct_out
