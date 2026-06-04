"""Raw OpenAlex REST search (no LLM) for discover material."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

OPENALEX_EMAIL = os.environ.get("OPENALEX_EMAIL", "team@descai.org")
OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
OPENALEX_SLEEP = 1.0 / 8.0
OPENALEX_PER_PAGE = 6
OPENALEX_MAX_TERMS = 4


def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    if not inverted or not isinstance(inverted, dict):
        return None
    slots: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                slots.append((pos, str(word)))
    if not slots:
        return None
    slots.sort(key=lambda x: x[0])
    return " ".join(w for _, w in slots)


def search_openalex(query: str, session: requests.Session, n: int, *, log_prefix: str) -> list[dict[str, Any]]:
    params = {
        "search": query,
        "per-page": str(n),
        "mailto": OPENALEX_EMAIL,
        "select": "id,doi,title,type,publication_year,cited_by_count,abstract_inverted_index",
    }
    headers = {
        "User-Agent": f"Claim-extractor/1.0 (mailto:{OPENALEX_EMAIL})",
        "Accept": "application/json",
    }
    time.sleep(OPENALEX_SLEEP)
    try:
        r = session.get(OPENALEX_SEARCH_URL, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        print(f"  [{log_prefix}] request error {query!r}: {exc}", file=__import__("sys").stderr)
        return []
    if r.status_code != 200:
        print(f"  [{log_prefix}] HTTP {r.status_code} for {query!r}", file=__import__("sys").stderr)
        return []
    works: list[dict[str, Any]] = []
    for item in r.json().get("results") or []:
        if not isinstance(item, dict):
            continue
        inv = item.get("abstract_inverted_index")
        abstract = _reconstruct_abstract(inv) if isinstance(inv, dict) else None
        works.append({
            "openalex_id": item.get("id") or "",
            "doi": item.get("doi") or "",
            "title": item.get("title") or item.get("display_name") or "",
            "year": item.get("publication_year"),
            "cited_by_count": item.get("cited_by_count"),
            "abstract": abstract,
        })
    return works


def grounding_search_terms(compound: str) -> list[str]:
    return [
        f'"{compound}" aging longevity lifespan',
        f'"{compound}" healthspan senescence',
        f'"{compound}" lifespan extension',
    ][:OPENALEX_MAX_TERMS]


def risk_search_terms(compound: str) -> list[str]:
    return [
        f'"{compound}" adverse effects toxicity',
        f'"{compound}" contraindications safety',
        f'"{compound}" drug safety humans',
    ][:OPENALEX_MAX_TERMS]


def fetch_openalex_works(terms: list[str], *, log_prefix: str) -> dict[str, Any]:
    session = requests.Session()
    works: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in terms:
        for work in search_openalex(term, session, OPENALEX_PER_PAGE, log_prefix=log_prefix):
            oid = work.get("openalex_id") or work.get("doi") or ""
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            entry = dict(work)
            entry["search_term"] = term
            works.append(entry)
    return {"search_terms": terms, "work_count": len(works), "works": works}


def fetch_openalex_grounding(compound: str) -> dict[str, Any]:
    return fetch_openalex_works(grounding_search_terms(compound), log_prefix="openalex-grounding")


def fetch_openalex_risk(compound: str) -> dict[str, Any]:
    return fetch_openalex_works(risk_search_terms(compound), log_prefix="openalex-risk")
