"""Enrich triaged claims with citation resolution, OpenAlex metadata, and LLM evidence grading.

Evidence grades include self_reported for Fact + Observational/Measurement claims in
abstract/results chunks with no inline external citations (the paper is the primary evidence).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests

import sys
from pathlib import Path as _Path
_ARTICLES = _Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))
from llm_env import LLM_API_KEY, LLM_BASE_URL  # noqa: E402
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")
MAX_RETRIES = 4
OPENALEX_EMAIL = os.environ.get("OPENALEX_EMAIL", "team@descai.org")
OPENALEX_SLEEP = 1.0 / 8.0
CLAIM_BORROW_MIN_LEN = 30
CLAIM_BORROW_PREFIX_MAX = 200
JSON_PARSE_ATTEMPTS = 3
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.I)

CLASSIFICATION_KEYS = (
    "claim_classification_1",
    "claim_classification_2",
    "claim_classification_3",
)

EMPIRICAL_UNREFERENCED_TAGS: frozenset[str] = frozenset(
    {"Causal", "Correlational", "Mechanistic", "Performance"}
)

SELF_REPORTED_SEMANTIC: frozenset[str] = frozenset({"abstract", "result", "discussion"})
SELF_REPORTED_FACT_TAGS: frozenset[str] = frozenset(
    {"Observational", "Measurement", "Causal", "Comparative",
     "Correlational", "Methodological", "Benchmark", "Performance"}
)
_SELF_REPORTED_CLAIM_TYPES: frozenset[str] = frozenset({"Fact", "Interpretation", "Result", "Assertion"})

_METHOD_HEADING_HINTS: frozenset[str] = frozenset([
    "method", "materials", "protocol", "husbandry", "toxicity",
    "sequencing", "replication", "preparation", "experimental",
    "sample preparation", "data processing",
])
_RESULT_HEADING_HINTS: frozenset[str] = frozenset([
    "result", "finding", "efficacy", "differentially expressed",
    "enrichment", "transcriptomic", "vs.", "versus", "control",
    "treated", "exposed", "induced", "affected",
])
_DISCUSSION_HEADING_HINTS: frozenset[str] = frozenset([
    "discussion", "interpretation", "implications", "conclusion",
])
_ABSTRACT_HEADING_HINTS: frozenset[str] = frozenset(["abstract", "key messages"])

EVIDENCE_AUDITOR_SYSTEM = """You are an evidence auditor. Given a scientific claim from a paper and the abstracts of its cited references, determine whether the cited evidence actually supports the specific claim as stated.

For EACH cited reference, output a JSON object with:
- "ref_number": int
- "support_verdict": one of "direct_support", "partial_support", "tangential", "overclaim", "not_relevant"
- "support_rationale": 1-2 sentences explaining why

Then provide an overall assessment:
- "evidence_grade": one of "strong", "moderate", "weak", "unsupported", "unverifiable"
- "evidence_summary": 1-2 sentences summarizing the overall evidence picture for this claim

Return ONLY valid JSON with keys: "per_reference" (array), "evidence_grade" (string), "evidence_summary" (string).
"""

_REFERENCES_HEADER = re.compile(r"^\s*\*{0,2}\s*REFERENCES\s*\*{0,2}\s*$", re.I)
_REF_LINE = re.compile(r"^\s*(\d+)\.\s+(.+)$")
_PAREN_GROUPS = re.compile(r"\(([^)]+)\)")
_BARE_CITE_TAIL = re.compile(
    r"(?<=[a-zà-ž\)])\s+(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)\s*([.,])(?=\s|$)",
    re.UNICODE,
)


def reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """Rebuild abstract text from OpenAlex abstract_inverted_index."""
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


def _expand_citation_token_segment(segment: str) -> list[int]:
    segment = segment.strip()
    if not segment:
        return []
    for sep in ("–", "-", "—"):
        if sep in segment:
            parts = [p.strip() for p in segment.split(sep, 1)]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                a, b = int(parts[0]), int(parts[1])
                if a <= b:
                    return list(range(a, b + 1))
                return list(range(b, a + 1))
            break
    if segment.isdigit():
        return [int(segment)]
    return []


def _parse_numeric_citation_inner(inner: str) -> list[int]:
    inner = inner.replace("，", ",")
    inner = re.sub(r"\s+", " ", inner.strip())
    if not inner:
        return []
    out: list[int] = []
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        out.extend(_expand_citation_token_segment(part))
    return out


def extract_citation_numbers(text: str, max_ref: int | None = None) -> list[int]:
    """Parenthetical numeric-only cites plus Docling-style bare numeric tails."""
    seen: set[int] = set()
    ordered: list[int] = []

    def add(n: int) -> None:
        if max_ref is not None and (n < 1 or n > max_ref):
            return
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    for m in _PAREN_GROUPS.finditer(text):
        inner = m.group(1).strip()
        if not re.fullmatch(r"[\d\s,，\-–—]+", inner):
            continue
        for n in _parse_numeric_citation_inner(inner):
            add(n)

    for m in _BARE_CITE_TAIL.finditer(text):
        cluster = m.group(1)
        inner = cluster.replace("，", ",")
        for part in re.split(r"\s*,\s*", inner):
            part = part.strip()
            if not part:
                continue
            for n in _expand_citation_token_segment(part):
                add(n)

    return sorted(ordered)


def _extract_doi_from_raw(raw_text: str) -> str | None:
    s = re.sub(
        r"https://doi\.org/https://doi\.org/",
        "https://doi.org/",
        raw_text,
        flags=re.I,
    )
    m = re.search(r"https://doi\.org/(10\.\d+[^\s\]\}<>,;]+)", s, re.I)
    if m:
        return m.group(1).rstrip(".,;)")
    m = re.search(r"(?i)doi:\s*(10\.\d+\S+)", s)
    if m:
        return m.group(1).rstrip(".,;)")
    m = re.search(r"(?i)\bdoi\s+(10\.\d+\S+)", s)
    if m:
        return m.group(1).rstrip(".,;)")
    return None


def parse_reference_index(full_md_text: str, stderr: Any) -> dict[int, dict[str, Any]]:
    lines = full_md_text.splitlines()
    start_idx: int | None = None
    for i, line in enumerate(lines):
        if _REFERENCES_HEADER.match(line):
            start_idx = i + 1
            break
    if start_idx is None:
        start_idx = int(len(lines) * 0.6)
        print(
            "retrieve_compare.py: no **REFERENCES** header; scanning from 60% of file",
            file=stderr,
        )

    index: dict[int, dict[str, Any]] = {}
    for line in lines[start_idx:]:
        m = _REF_LINE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        raw = line.strip()
        doi = _extract_doi_from_raw(raw)
        index[n] = {"raw_text": raw, "doi": doi}

    if not index:
        print("retrieve_compare.py: warning: empty reference index", file=stderr)
    else:
        print(
            f"retrieve_compare.py: parsed {len(index)} reference line(s), "
            f"with DOI: {sum(1 for v in index.values() if v.get('doi'))}",
            file=stderr,
        )
    return index


def load_kb_indices(kb_path: Path, stderr: Any) -> tuple[dict[int, str], dict[str, list[tuple[int, str]]]]:
    """chunk_id -> text, and doc_name -> [(chunk_id, text), ...] in file order."""
    out: dict[int, str] = {}
    by_doc: dict[str, list[tuple[int, str]]] = {}
    dup_warned = False
    for line in kb_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        cid = rec.get("chunk_id")
        text = rec.get("text")
        if cid is None or text is None:
            continue
        try:
            k = int(cid)
        except (TypeError, ValueError):
            continue
        if k in out and not dup_warned:
            print(
                "retrieve_compare.py: warning: duplicate chunk_id in KB (last wins)",
                file=stderr,
            )
            dup_warned = True
        t = str(text)
        out[k] = t
        dn = str(rec.get("doc_name") or "").strip()
        if dn:
            by_doc.setdefault(dn, []).append((k, t))
    print(f"retrieve_compare.py: loaded {len(out)} chunk(s) from KB", file=stderr)
    return out, by_doc


def load_chunk_index(kb_path: Path, stderr: Any) -> dict[int, str]:
    """Backward-compatible: only chunk_id -> text."""
    chunk_index, _ = load_kb_indices(kb_path, stderr)
    return chunk_index


def _iter_claim_dicts(triaged: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for dim_key, dim_val in triaged.items():
        if not isinstance(dim_val, dict):
            continue
        buckets = dim_val.get("buckets") or {}
        if isinstance(buckets, dict):
            for bname, arr in buckets.items():
                if not isinstance(arr, list):
                    continue
                for rec in arr:
                    if isinstance(rec, dict):
                        yield dim_key, str(bname), rec
        noise = dim_val.get("noise") or []
        if isinstance(noise, list):
            for rec in noise:
                if isinstance(rec, dict):
                    yield dim_key, "noise", rec


def _collect_doc_names(triaged: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for _, _, rec in _iter_claim_dicts(triaged):
        dn = rec.get("doc_name")
        if dn:
            names.add(str(dn).strip())
    return names


def _default_kb_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "text_knowledge_base.jsonl"


def _default_fullmd_for_doc(doc_name: str) -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / doc_name / "full.md"


def _normalize_for_match(s: str) -> str:
    return re.sub(r"\s+", " ", s.casefold().strip())


def _strip_model_noise(text: str) -> str:
    """Strip markdown fences and common think / reasoning wrappers."""
    t = text.strip()
    fm = _FENCE_BLOCK.search(t)
    if fm:
        t = fm.group(1).strip()
    # Qwen / common "think" wrappers (avoid raw `<...>` in source for XML-safe tools)
    t = re.sub(
        r"\x3cthink\x3e[\s\S]*?\x3c/think\x3e",
        "",
        t,
        flags=re.DOTALL,
    ).strip()
    t = re.sub(r"\x3cthink\x3e[\s\S]*", "", t, flags=re.DOTALL).strip()
    t = re.sub(
        r"\x3creasoning\x3e[\s\S]*?\x3c/reasoning\x3e",
        "",
        t,
        flags=re.DOTALL,
    ).strip()
    t = re.sub(r"\x3creasoning\x3e[\s\S]*", "", t, flags=re.DOTALL).strip()
    t = re.sub(
        r"\x3credacted_reasoning\x3e[\s\S]*?\x3c/redacted_reasoning\x3e",
        "",
        t,
        flags=re.DOTALL,
    ).strip()
    t = re.sub(r"\x3credacted_reasoning\x3e[\s\S]*", "", t, flags=re.DOTALL).strip()
    t = re.sub(
        r"<think>.*?</think>", "", t, flags=re.DOTALL
    ).strip()
    t = re.sub(r"<think>.*", "", t, flags=re.DOTALL).strip()
    return t


def _extract_first_json_object(text: str) -> str | None:
    """First top-level `{...}` span with string-aware brace matching."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    return None


def _strip_thinking_tags(text: str) -> str:
    """Legacy name; delegates to full model noise strip."""
    return _strip_model_noise(text)


def _llm_json_call(
    client: Any,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int = 2048,
    use_json_response_format: bool = False,
) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": MODEL,
                "max_tokens": max_tokens,
                "temperature": 0,
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            }
            if use_json_response_format:
                kwargs["response_format"] = {"type": "json_object"}
                try:
                    response = client.chat.completions.create(**kwargs)
                except Exception:
                    kwargs.pop("response_format", None)
                    response = client.chat.completions.create(**kwargs)
            else:
                response = client.chat.completions.create(**kwargs)
            text = (response.choices[0].message.content or "").strip()
            return _strip_model_noise(text)
        except Exception as exc:
            last_exc = exc
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
    raise RuntimeError(str(last_exc))


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    for candidate in (_strip_model_noise(raw), raw.strip()):
        if not candidate:
            continue
        try:
            o = json.loads(candidate)
            if isinstance(o, dict):
                return o
        except json.JSONDecodeError:
            pass
        extracted = _extract_first_json_object(candidate)
        if extracted:
            try:
                o = json.loads(extracted)
                if isinstance(o, dict):
                    return o
            except json.JSONDecodeError:
                continue
    return None


def _tags_for_claim(rec: dict[str, Any]) -> set[str]:
    tags: set[str] = set()
    for key in CLASSIFICATION_KEYS:
        part = rec.get(key) or []
        if isinstance(part, list):
            for x in part:
                tags.add(str(x).strip())
    return tags


def _unreferenced_summary(rec: dict[str, Any]) -> str:
    ct = str(rec.get("claim_type") or "").strip()
    tags = _tags_for_claim(rec)
    if ct == "Fact" and (tags & EMPIRICAL_UNREFERENCED_TAGS):
        return "Empirical claim with no cited references."
    return "No inline citations in source chunk."


def _effective_semantic_category(rec: dict[str, Any]) -> str:
    """Resolve semantic_category, falling back to section_heading keywords when 'other'."""
    sem = str(rec.get("semantic_category") or "").strip().lower()
    if sem and sem != "other":
        return sem
    heading = str(rec.get("section_heading") or "").strip().lower()
    if not heading:
        return sem or "other"
    if any(kw in heading for kw in _ABSTRACT_HEADING_HINTS):
        return "abstract"
    if any(kw in heading for kw in _METHOD_HEADING_HINTS):
        return "method"
    if any(kw in heading for kw in _RESULT_HEADING_HINTS):
        return "result"
    if any(kw in heading for kw in _DISCUSSION_HEADING_HINTS):
        return "discussion"
    return sem or "other"


def _is_self_reported_fact_claim(rec: dict[str, Any]) -> bool:
    """Primary findings stated in abstract/results/discussion.

    Matches when the claim is in a self-reported section AND either:
      - has matching empirical tags, OR
      - has an allowed claim_type with no tags (KB didn't populate them)
    """
    sem = _effective_semantic_category(rec)
    if sem not in SELF_REPORTED_SEMANTIC:
        return False
    ct = str(rec.get("claim_type") or "").strip()
    if ct and ct not in _SELF_REPORTED_CLAIM_TYPES:
        return False
    tags = _tags_for_claim(rec)
    if tags:
        return bool(tags & SELF_REPORTED_FACT_TAGS)
    return ct in _SELF_REPORTED_CLAIM_TYPES


def _is_self_reported_method_claim(rec: dict[str, Any]) -> bool:
    """Protocol/procedure claims from method/result/discussion sections."""
    sem = _effective_semantic_category(rec)
    if sem == "method":
        return True
    if sem in ("result", "discussion"):
        tags = _tags_for_claim(rec)
        return "Methodological" in tags if tags else False
    return False


def _self_reported_summary(rec: dict[str, Any], *, skip_llm: bool, method: bool = False) -> str:
    loc = _effective_semantic_category(rec) or "this section"
    if method:
        base = (
            f"Procedural description in {loc} section: the manuscript describes "
            "its own methodology; no external citation expected."
        )
    else:
        base = (
            f"Self-reported empirical finding in {loc} text: the manuscript describes "
            "this as its own experimental or analytic outcome; external references are "
            "not cited inline in this chunk."
        )
    if skip_llm:
        return base + " LLM evidence check skipped."
    return base


def _citation_numbers_for_claim(
    rec: dict[str, Any],
    chunk_index: dict[int, str],
    kb_by_doc: dict[str, list[tuple[int, str]]],
    max_ref: int | None,
) -> tuple[list[int], list[int], bool]:
    """(citation ints, source chunk_ids, borrowed_from_other_chunk)."""
    claim = str(rec.get("claim") or "").strip()
    doc = str(rec.get("doc_name") or "").strip()
    try:
        home = int(rec["chunk_id"]) if rec.get("chunk_id") is not None else None
    except (TypeError, ValueError, KeyError):
        home = None

    home_text = chunk_index.get(home, "") if home is not None else ""
    primary = extract_citation_numbers(home_text, max_ref=max_ref)
    if primary:
        return primary, ([home] if home is not None else []), False

    if not doc or home is None or len(claim) < CLAIM_BORROW_MIN_LEN:
        return [], [], False

    claim_norm = _normalize_for_match(claim)
    prefix = claim_norm[: min(CLAIM_BORROW_PREFIX_MAX, len(claim_norm))]

    seen_nums: set[int] = set()
    ordered: list[int] = []
    contributing: set[int] = set()

    for cid, text in kb_by_doc.get(doc, []):
        text_norm = _normalize_for_match(text)
        if claim_norm not in text_norm and (
            len(prefix) < 20 or prefix not in text_norm
        ):
            continue
        nums = extract_citation_numbers(text, max_ref=max_ref)
        if not nums:
            continue
        contributing.add(cid)
        for n in nums:
            if n not in seen_nums:
                seen_nums.add(n)
                ordered.append(n)

    out_cites = sorted(ordered)
    src = sorted(contributing)
    borrowed = bool(out_cites) and bool(src) and (src != [home])
    return out_cites, src, borrowed


JSON_RETRY_SUFFIX = (
    "\n\nYour previous response was not valid JSON. Output ONLY one JSON object "
    'with keys "per_reference", "evidence_grade", "evidence_summary". '
    "No markdown code fences, no preamble, no commentary."
)


def _normalize_doi_key(doi: str) -> str:
    d = doi.strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    return d.lower()


def _fetch_one_openalex(
    doi: str,
    session: requests.Session,
    email: str,
    stderr: Any,
) -> dict[str, Any]:
    url = (
        f"https://api.openalex.org/works/https://doi.org/{doi}"
        f"?mailto={quote(email)}"
    )
    headers = {
        "User-Agent": f"Claim-extractor/1.0 (mailto:{email})",
        "Accept": "application/json",
    }
    time.sleep(OPENALEX_SLEEP)
    try:
        r = session.get(url, headers=headers, timeout=60)
    except requests.RequestException as exc:
        print(f"retrieve_compare.py: OpenAlex request error for {doi!r}: {exc}", file=stderr)
        return {"doi": doi, "status": "not_found", "error": str(exc)}

    if r.status_code == 404:
        return {"doi": doi, "status": "not_found"}

    if r.status_code != 200:
        print(
            f"retrieve_compare.py: OpenAlex HTTP {r.status_code} for {doi!r}",
            file=stderr,
        )
        return {"doi": doi, "status": "not_found", "http_status": r.status_code}

    data = r.json()
    title = data.get("title") or data.get("display_name")
    wtype = data.get("type")
    cited_by = data.get("cited_by_count")
    year = data.get("publication_year")
    inv = data.get("abstract_inverted_index")
    abstract = reconstruct_abstract(inv) if isinstance(inv, dict) else None
    base: dict[str, Any] = {
        "doi": doi,
        "title": title,
        "type": wtype,
        "cited_by_count": cited_by,
        "publication_year": year,
        "status": "ok",
    }
    if abstract:
        base["abstract"] = abstract
        return base
    base["status"] = "no_abstract"
    return base


def ensure_openalex_cache(
    dois_needed: set[str],
    cache_path: Path,
    email: str,
    stderr: Any,
) -> dict[str, Any]:
    cache: dict[str, Any] = {}
    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cache = raw
        except (json.JSONDecodeError, OSError) as exc:
            print(
                f"retrieve_compare.py: warning: could not read cache {cache_path}: {exc}",
                file=stderr,
            )
            cache = {}

    session = requests.Session()
    hits = misses = 0
    not_found = 0
    for doi in sorted(dois_needed):
        key = _normalize_doi_key(doi)
        if key in cache:
            hits += 1
            continue
        misses += 1
        rec = _fetch_one_openalex(doi, session, email, stderr)
        cache[key] = rec
        if rec.get("status") == "not_found":
            not_found += 1

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"retrieve_compare.py: OpenAlex cache hits={hits} new_fetches={misses} "
        f"not_found_like={not_found} -> {cache_path}",
        file=stderr,
    )
    return cache


def _truncate(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    if len(s) <= n:
        return s
    return s[:n]


def _lookup_ox(cache: dict[str, Any], doi: str | None) -> dict[str, Any] | None:
    if not doi:
        return None
    key = _normalize_doi_key(doi)
    rec = cache.get(key)
    return rec if isinstance(rec, dict) else None


def _build_citation_rows(
    citation_numbers: list[int],
    ref_index: dict[int, dict[str, Any]],
    openalex_cache: dict[str, Any],
    *,
    truncate_abstract: int | None,
    verdicts: dict[int, dict[str, str]] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref_n in citation_numbers:
        ref_meta = ref_index.get(ref_n) or {}
        doi = ref_meta.get("doi")
        ox = _lookup_ox(openalex_cache, str(doi) if doi else None)
        title = type_ = year = cited_by = abstract = None
        if ox:
            title = ox.get("title")
            type_ = ox.get("type")
            year = ox.get("publication_year")
            cited_by = ox.get("cited_by_count")
            abstract = ox.get("abstract")
        sv = sr = None
        if verdicts and ref_n in verdicts:
            sv = verdicts[ref_n].get("support_verdict")
            sr = verdicts[ref_n].get("support_rationale")
        ab_out = abstract
        if truncate_abstract is not None and ab_out is not None:
            ab_out = _truncate(ab_out, truncate_abstract)
        rows.append(
            {
                "ref_number": ref_n,
                "doi": doi,
                "title": title,
                "type": type_,
                "cited_by_count": cited_by,
                "publication_year": year,
                "abstract": ab_out,
                "support_verdict": sv,
                "support_rationale": sr,
            }
        )
    return rows


def _refs_with_usable_abstracts(
    citation_numbers: list[int],
    ref_index: dict[int, dict[str, Any]],
    openalex_cache: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ref_n in citation_numbers:
        ref_meta = ref_index.get(ref_n) or {}
        doi = ref_meta.get("doi")
        ox = _lookup_ox(openalex_cache, str(doi) if doi else None)
        if not ox or ox.get("status") != "ok":
            continue
        ab = ox.get("abstract")
        if not ab:
            continue
        out.append(
            {
                "ref_number": ref_n,
                "doi": doi,
                "title": ox.get("title"),
                "type": ox.get("type"),
                "publication_year": ox.get("publication_year"),
                "cited_by_count": ox.get("cited_by_count"),
                "abstract": ab,
            }
        )
    return out


def _run_evidence_llm(
    client: Any,
    rec: dict[str, Any],
    refs_with_abstracts: list[dict[str, Any]],
    *,
    debug_llm: bool = False,
    use_json_response_format: bool = False,
) -> tuple[dict[int, dict[str, str]], str, str]:
    lines = [
        f"claim_type: {rec.get('claim_type')}",
        f"claim: {rec.get('claim')}",
    ]
    for k in CLASSIFICATION_KEYS:
        lines.append(f"{k}: {json.dumps(rec.get(k), ensure_ascii=False)}")
    lines.append("\nCited references (with abstracts):\n")
    for r in refs_with_abstracts:
        lines.append(
            f"\n--- ref {r['ref_number']} ---\n"
            f"title: {r.get('title')}\n"
            f"type: {r.get('type')}\n"
            f"year: {r.get('publication_year')}\n"
            f"abstract:\n{r.get('abstract') or ''}\n"
        )
    base_user = "\n".join(lines)
    user_text = base_user
    parsed: dict[str, Any] | None = None
    last_raw = ""

    for parse_attempt in range(JSON_PARSE_ATTEMPTS):
        max_tok = 2048 + 256 * parse_attempt
        use_jrf = use_json_response_format and parse_attempt == 0
        raw = _llm_json_call(
            client,
            EVIDENCE_AUDITOR_SYSTEM,
            user_text,
            max_tokens=max_tok,
            use_json_response_format=use_jrf,
        )
        last_raw = raw
        if debug_llm:
            print(
                f"retrieve_compare.py: LLM raw (trunc repr): {repr(raw)[:4000]}",
                file=sys.stderr,
            )
        parsed = _parse_json_object(raw)
        if debug_llm:
            print(
                f"retrieve_compare.py: JSON parse {'OK' if parsed else 'FAIL'} "
                f"(attempt {parse_attempt + 1}/{JSON_PARSE_ATTEMPTS})",
                file=sys.stderr,
            )
        if parsed:
            break
        user_text = base_user + JSON_RETRY_SUFFIX

    if not parsed:
        if debug_llm and last_raw:
            print(
                f"retrieve_compare.py: last raw (full repr, capped 8000): "
                f"{repr(last_raw)[:8000]}",
                file=sys.stderr,
            )
        return (
            {},
            "unverifiable",
            "LLM returned JSON that could not be parsed after retries.",
        )
    per: dict[int, dict[str, str]] = {}
    for item in parsed.get("per_reference") or []:
        if not isinstance(item, dict):
            continue
        try:
            rn = int(item.get("ref_number"))
        except (TypeError, ValueError):
            continue
        per[rn] = {
            "support_verdict": str(item.get("support_verdict") or ""),
            "support_rationale": str(item.get("support_rationale") or ""),
        }
    grade = str(parsed.get("evidence_grade") or "unverifiable")
    summary = str(parsed.get("evidence_summary") or "")
    return per, grade, summary


def enrich_triaged(
    triaged: dict[str, Any],
    *,
    chunk_index: dict[int, str],
    kb_by_doc: dict[str, list[tuple[int, str]]],
    ref_index: dict[int, dict[str, Any]],
    openalex_cache: dict[str, Any],
    client: Any,
    skip_llm: bool,
    debug_llm: bool,
    use_json_response_format: bool,
    stderr: Any,
) -> dict[str, Any]:
    max_ref = max(ref_index.keys()) if ref_index else None
    out = copy.deepcopy(triaged)
    borrowed = 0

    for _, _, rec in _iter_claim_dicts(out):
        try:
            ck = int(rec["chunk_id"]) if rec.get("chunk_id") is not None else None
        except (TypeError, ValueError):
            ck = None
        if ck is not None and ck not in chunk_index:
            print(
                f"retrieve_compare.py: warning: chunk_id={ck} not in KB",
                file=stderr,
            )

        cites, cite_sources, did_borrow = _citation_numbers_for_claim(
            rec, chunk_index, kb_by_doc, max_ref
        )
        rec["citation_numbers"] = cites
        rec["citation_source_chunk_ids"] = cite_sources
        if did_borrow:
            borrowed += 1
        if skip_llm:
            rec["citations"] = _build_citation_rows(
                cites,
                ref_index,
                openalex_cache,
                truncate_abstract=500,
                verdicts=None,
            )
            for c in rec["citations"]:
                c["support_verdict"] = None
                c["support_rationale"] = None
            if not cites and _is_self_reported_fact_claim(rec):
                rec["evidence_grade"] = "self_reported"
                rec["evidence_summary"] = _self_reported_summary(rec, skip_llm=True)
            elif not cites and _is_self_reported_method_claim(rec):
                rec["evidence_grade"] = "self_reported_method"
                rec["evidence_summary"] = _self_reported_summary(rec, skip_llm=True, method=True)
            else:
                rec["evidence_grade"] = "pending"
                rec["evidence_summary"] = "LLM check skipped."
            continue

        if not cites:
            rec["citations"] = []
            if _is_self_reported_fact_claim(rec):
                rec["evidence_grade"] = "self_reported"
                rec["evidence_summary"] = _self_reported_summary(rec, skip_llm=False)
            elif _is_self_reported_method_claim(rec):
                rec["evidence_grade"] = "self_reported_method"
                rec["evidence_summary"] = _self_reported_summary(rec, skip_llm=False, method=True)
            else:
                rec["evidence_grade"] = "unreferenced"
                rec["evidence_summary"] = _unreferenced_summary(rec)
            continue

        usable = _refs_with_usable_abstracts(cites, ref_index, openalex_cache)
        if not usable:
            rec["citations"] = _build_citation_rows(
                cites,
                ref_index,
                openalex_cache,
                truncate_abstract=500,
                verdicts=None,
            )
            rec["evidence_grade"] = "unverifiable"
            rec["evidence_summary"] = (
                "All cited references could not be retrieved from OpenAlex."
            )
            continue

        if client is None:
            rec["citations"] = _build_citation_rows(
                cites,
                ref_index,
                openalex_cache,
                truncate_abstract=500,
                verdicts=None,
            )
            rec["evidence_grade"] = "unverifiable"
            rec["evidence_summary"] = "LLM client not configured."
            continue

        verdicts, grade, summary = _run_evidence_llm(
            client,
            rec,
            usable,
            debug_llm=debug_llm,
            use_json_response_format=use_json_response_format,
        )
        rec["citations"] = _build_citation_rows(
            cites,
            ref_index,
            openalex_cache,
            truncate_abstract=500,
            verdicts=verdicts,
        )

        if grade in ("unsupported", "unreferenced") and (
            _is_self_reported_fact_claim(rec) or _is_self_reported_method_claim(rec)
        ):
            is_method = _is_self_reported_method_claim(rec)
            grade = "self_reported_method" if is_method else "self_reported"
            summary = _self_reported_summary(rec, skip_llm=False, method=is_method)

        rec["evidence_grade"] = grade
        rec["evidence_summary"] = summary

    print(
        f"retrieve_compare.py: cross-chunk citation borrow used for {borrowed} claim(s)",
        file=stderr,
    )
    return out


def _collect_dois_from_triaged(
    triaged: dict[str, Any],
    chunk_index: dict[int, str],
    kb_by_doc: dict[str, list[tuple[int, str]]],
    ref_index: dict[int, dict[str, Any]],
) -> set[str]:
    max_ref = max(ref_index.keys()) if ref_index else None
    dois: set[str] = set()
    for _, _, rec in _iter_claim_dicts(triaged):
        cites, _, _ = _citation_numbers_for_claim(rec, chunk_index, kb_by_doc, max_ref)
        for n in cites:
            meta = ref_index.get(n) or {}
            d = meta.get("doi")
            if d:
                dois.add(str(d))
    return dois


def main() -> None:
    default_kb = _default_kb_path()
    p = argparse.ArgumentParser(
        description="Enrich triaged.json with citations, OpenAlex metadata, and evidence grades."
    )
    p.add_argument("triaged_json", type=Path, help="Input triaged.json")
    p.add_argument(
        "--kb",
        type=Path,
        default=default_kb,
        help="text_knowledge_base.jsonl (default: ../../data/text_knowledge_base.jsonl)",
    )
    p.add_argument(
        "--fullmd",
        type=Path,
        default=None,
        help="full.md (default: ../../data/{doc_name}/full.md from triaged claims)",
    )
    p.add_argument(
        "--openalex-cache",
        type=Path,
        default=None,
        help="OpenAlex JSON cache path (default: next to -o output, or triaged dir)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write JSON here (default: stdout)",
    )
    p.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip LLM phase; evidence_grade=pending",
    )
    p.add_argument(
        "--debug-llm",
        action="store_true",
        help="Log truncated LLM response repr and parse status to stderr",
    )
    args = p.parse_args()

    triaged = json.loads(args.triaged_json.read_text(encoding="utf-8"))
    if not isinstance(triaged, dict):
        print("retrieve_compare.py: error: triaged JSON must be an object", file=sys.stderr)
        raise SystemExit(1)

    doc_names = _collect_doc_names(triaged)
    fullmd_path = args.fullmd
    if fullmd_path is None:
        if len(doc_names) == 1:
            fullmd_path = _default_fullmd_for_doc(next(iter(doc_names)))
            print(f"retrieve_compare.py: using --fullmd {fullmd_path}", file=sys.stderr)
        elif len(doc_names) == 0:
            print(
                "retrieve_compare.py: error: no doc_name on claims; pass --fullmd",
                file=sys.stderr,
            )
            raise SystemExit(1)
        else:
            print(
                "retrieve_compare.py: error: multiple doc_name values "
                f"{sorted(doc_names)!r}; pass --fullmd",
                file=sys.stderr,
            )
            raise SystemExit(1)

    cache_path = args.openalex_cache
    if cache_path is None:
        if args.output is not None:
            cache_path = args.output.parent / "openalex_cache.json"
        else:
            cache_path = args.triaged_json.parent / "openalex_cache.json"
        print(f"retrieve_compare.py: using --openalex-cache {cache_path}", file=sys.stderr)

    full_text = fullmd_path.read_text(encoding="utf-8")
    ref_index = parse_reference_index(full_text, sys.stderr)
    chunk_index, kb_by_doc = load_kb_indices(args.kb, sys.stderr)

    debug_llm = args.debug_llm or os.environ.get(
        "RETRIEVE_COMPARE_DEBUG_LLM", ""
    ).strip().lower() in ("1", "true", "yes")
    use_json_response_format = os.environ.get(
        "RETRIEVE_COMPARE_JSON_RESPONSE_FORMAT", ""
    ).strip().lower() in ("1", "true", "yes")

    dois_needed = _collect_dois_from_triaged(
        triaged, chunk_index, kb_by_doc, ref_index
    )
    openalex_cache = ensure_openalex_cache(
        dois_needed, cache_path, OPENALEX_EMAIL, sys.stderr
    )

    client: Any = None
    if not args.skip_llm:
        from openai import OpenAI as _OpenAI

        client = _OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    enriched = enrich_triaged(
        triaged,
        chunk_index=chunk_index,
        kb_by_doc=kb_by_doc,
        ref_index=ref_index,
        openalex_cache=openalex_cache,
        client=client,
        skip_llm=args.skip_llm,
        debug_llm=debug_llm,
        use_json_response_format=use_json_response_format,
        stderr=sys.stderr,
    )

    text = json.dumps(enriched, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
