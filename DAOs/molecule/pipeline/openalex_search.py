"""Minimal OpenAlex search wrapper for DAO claim validation.

Adapted from compounds/pipeline/single/discover_lib/openalex.py. We only need
keyword searches with abstract reconstruction.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import requests

OPENALEX_EMAIL = os.environ.get("OPENALEX_EMAIL", "team@descai.org")
OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
OPENALEX_SLEEP = 1.0 / 8.0
DEFAULT_PER_PAGE = 5


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


def search(
    query: str,
    *,
    session: requests.Session | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    log_prefix: str = "openalex",
) -> list[dict[str, Any]]:
    sess = session or requests.Session()
    params = {
        "search": query,
        "per-page": str(per_page),
        "mailto": OPENALEX_EMAIL,
        "select": "id,doi,title,type,publication_year,cited_by_count,abstract_inverted_index",
    }
    headers = {
        "User-Agent": f"DAO-Review/1.0 (mailto:{OPENALEX_EMAIL})",
        "Accept": "application/json",
    }
    time.sleep(OPENALEX_SLEEP)
    try:
        r = sess.get(OPENALEX_SEARCH_URL, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        print(f"  [{log_prefix}] request error {query!r}: {exc}", file=sys.stderr)
        return []
    if r.status_code != 200:
        print(f"  [{log_prefix}] HTTP {r.status_code} for {query!r}", file=sys.stderr)
        return []

    works: list[dict[str, Any]] = []
    for item in r.json().get("results") or []:
        if not isinstance(item, dict):
            continue
        inv = item.get("abstract_inverted_index")
        abstract = _reconstruct_abstract(inv) if isinstance(inv, dict) else None
        oid_full = item.get("id") or ""
        oid = oid_full.rsplit("/", 1)[-1] if oid_full else ""
        works.append({
            "openalex_id": oid,
            "openalex_url": oid_full,
            "doi": item.get("doi") or "",
            "title": item.get("title") or item.get("display_name") or "",
            "year": item.get("publication_year"),
            "cited_by_count": item.get("cited_by_count"),
            "abstract": abstract,
        })
    return works


def search_many(
    queries: list[str],
    *,
    per_page: int = DEFAULT_PER_PAGE,
    max_total: int = 8,
) -> list[dict[str, Any]]:
    """Run multiple queries, dedupe by openalex_id/doi, cap output size."""
    sess = requests.Session()
    seen: set[str] = set()
    works: list[dict[str, Any]] = []
    for q in queries:
        for w in search(q, session=sess, per_page=per_page, log_prefix="openalex"):
            key = w.get("openalex_id") or w.get("doi") or w.get("title") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            entry = dict(w)
            entry["search_term"] = q
            works.append(entry)
            if len(works) >= max_total:
                return works
    return works
