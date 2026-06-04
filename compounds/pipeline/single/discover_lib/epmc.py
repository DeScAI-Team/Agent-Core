"""Europe PMC literature search with expanded query templates."""

from __future__ import annotations

from typing import Any

from .http import CallBudget, req

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def L(x: Any) -> list[Any]:
    return [] if x is None else x if isinstance(x, list) else [x]


def _cite(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        try:
            return int(float(str(x)))
        except (TypeError, ValueError):
            return 0


def _pick(a: dict, b: dict) -> dict:
    la = len((a.get("abstract") or "") or "")
    lb = len((b.get("abstract") or "") or "")
    if lb > la or (la == lb and _cite(b.get("citedByCount")) > _cite(a.get("citedByCount"))):
        return b
    return a


def _quote_name(name: str) -> str:
    return f'"{name}"'


def _build_queries(primary: str, query_names: list[str]) -> list[tuple[str, str, int]]:
    """Return (key, query_string, page_size) tuples."""
    names = query_names[:4] if query_names else [primary]
    or_parts = " OR ".join(_quote_name(n) for n in names if n.strip())
    aging_or = (
        f"({or_parts}) AND (longevity OR aging OR lifespan OR healthspan OR senescence)"
        if or_parts
        else f'{_quote_name(primary)} AND (longevity OR aging OR lifespan OR healthspan OR senescence)'
    )
    queries: list[tuple[str, str, int]] = [
        ("longevity", f'{_quote_name(primary)} AND longevity', 50),
        ("aging", f'{_quote_name(primary)} AND aging', 50),
        ("lifespan", f'{_quote_name(primary)} AND lifespan', 50),
        ("healthspan", f'{_quote_name(primary)} AND healthspan', 50),
        ("senescence", f'{_quote_name(primary)} AND senescence', 50),
        ("aging_or", aging_or, 50),
    ]
    return queries


def fetch_europe_pmc(
    primary: str,
    query_names: list[str],
    fail: list,
    budget: CallBudget,
    ver: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, dict] = {}
    query_stats: dict[str, Any] = {}

    for qn, q, page_size in _build_queries(primary, query_names):
        d = req(
            EPMC,
            fail,
            f"europe_pmc:{qn}",
            budget,
            params={"query": q, "format": "json", "pageSize": page_size, "resultType": "core"},
            json_out=True,
        )
        if ver.get("europe_pmc") is None and isinstance(d, dict):
            ver["europe_pmc"] = {k: d.get(k) for k in ("version", "release", "hitCount")}
        hit_count = d.get("hitCount") if isinstance(d, dict) else None
        query_stats[qn] = {"query": q, "hitCount": hit_count, "pageSize": page_size}

        rl = d.get("resultList") if isinstance(d, dict) else None
        if not isinstance(rl, dict):
            continue
        for hit in L(rl.get("result")):
            if not isinstance(hit, dict):
                continue
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
            if auth is None and isinstance(hit.get("authorList"), dict):
                auth = [x.get("fullName") for x in L(hit["authorList"].get("author")) if isinstance(x, dict)]
                auth = [n for n in auth if n] or None
            rec = {
                "title": hit.get("title"),
                "authors": auth,
                "journal": hit.get("journalTitle") or hit.get("journal"),
                "year": hit.get("pubYear"),
                "doi": hit.get("doi"),
                "pmid": hit.get("pmid"),
                "abstract": hit.get("abstractText") or hit.get("abstract"),
                "citedByCount": hit.get("citedByCount"),
                "source_queries": [],
            }
            if key not in merged:
                rec["source_queries"] = [qn]
                merged[key] = rec
            else:
                prev = merged[key]
                w = _pick(prev, rec)
                w["source_queries"] = sorted(set(prev.get("source_queries") or []) | {qn})
                merged[key] = w

    return {
        "articles": list(merged.values()),
        "unique_count": len(merged),
        "query_stats": query_stats,
    }
