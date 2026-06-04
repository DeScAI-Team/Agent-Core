#!/usr/bin/env python3
"""Chunk every bundle artifact + crawled markdown + on-chain facts into JSONL.

Reads:
  - {bundle_dir}/pdf/*.json            (sectioned OCR)
  - {bundle_dir}/images/*.json         (vision captions)
  - {bundle_dir}/videos/*/frames.jsonl (video frame + transcript rows)
  - {bundle_dir}/text/*.md             (extracted plaintext)
  - {ipnft_dir}/*.md and {ipnft_dir}/output/*.md (crawler-produced site pages)
  - {ipnft_dir}/profile.json           (on-chain facts)

Writes a single JSONL file with one chunk per row:

    {
      "chunk_id": "pdf:foo:s2:c0",
      "source_kind": "pdf|crawl_md|image_caption|video_transcript|video_frame|onchain_fact|text_doc",
      "source_path": "...",
      "doc_title": "...",
      "domain": "beeard.ai",
      "page": 3,
      "section": "Approach",
      "text": "..."
    }
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

DEFAULT_MAX_CHARS = 3200       # ~800 tokens at 4 chars/token
MIN_CHUNK_CHARS = 200
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
FRONT_MATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return WS_RE.sub(" ", text).strip()


def _greedy_pack(paragraphs: list[str], max_chars: int) -> list[str]:
    """Greedy bin-pack paragraphs into chunks up to max_chars; split oversized paragraphs by sentence."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_chars:
            if buf:
                chunks.append("\n\n".join(buf).strip())
                buf, size = [], 0
            sentences = SENTENCE_SPLIT_RE.split(p) or [p]
            sbuf: list[str] = []
            ssize = 0
            for s in sentences:
                if ssize + len(s) + 1 > max_chars and sbuf:
                    chunks.append(" ".join(sbuf).strip())
                    sbuf, ssize = [], 0
                sbuf.append(s)
                ssize += len(s) + 1
            if sbuf:
                chunks.append(" ".join(sbuf).strip())
            continue
        if size + len(p) + 2 > max_chars and buf:
            chunks.append("\n\n".join(buf).strip())
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 2
    if buf:
        chunks.append("\n\n".join(buf).strip())
    return [c for c in chunks if len(c) >= MIN_CHUNK_CHARS or len(chunks) == 1]


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text on blank lines, then pack into max_chars windows."""
    if not text or not text.strip():
        return []
    paragraphs = re.split(r"\n\s*\n", text)
    return _greedy_pack(paragraphs, max_chars)


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")[:60] or "x"


def _domain_from_path(path: Path) -> str:
    """Crawler files are named after the domain they came from (e.g. beeard.ai.md)."""
    name = path.stem
    if "." in name:
        return name
    return ""


def _load_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _strip_front_matter(text: str) -> str:
    return FRONT_MATTER_RE.sub("", text, count=1)


def _chunks_from_pdf_json(pdf_json_path: Path, max_chars: int) -> Iterator[dict[str, Any]]:
    payload = json.loads(pdf_json_path.read_text(encoding="utf-8"))
    title = payload.get("title") or pdf_json_path.stem
    sections = payload.get("sections") or []
    if not sections and payload.get("full_text"):
        sections = [{"heading": "Body", "text": payload["full_text"], "page_start": 1}]

    pdf_id = _safe_id(pdf_json_path.stem)
    for sidx, section in enumerate(sections):
        heading = section.get("heading") or f"Section {sidx + 1}"
        page_start = section.get("page_start")
        body = section.get("text") or ""
        for cidx, chunk_text in enumerate(_chunk_text(body, max_chars)):
            yield {
                "chunk_id": f"pdf:{pdf_id}:s{sidx}:c{cidx}",
                "source_kind": "pdf",
                "source_path": payload.get("source_path") or str(pdf_json_path),
                "bundle_path": str(pdf_json_path),
                "doc_title": title,
                "domain": "",
                "page": page_start,
                "section": heading,
                "text": chunk_text,
            }


def _chunks_from_image_json(image_json_path: Path) -> Iterator[dict[str, Any]]:
    payload = json.loads(image_json_path.read_text(encoding="utf-8"))
    description = (payload.get("description") or "").strip()
    transcribed = (payload.get("transcribed_text") or "").strip()
    labels = payload.get("labels") or []
    parts: list[str] = []
    if description:
        parts.append(f"Description: {description}")
    if transcribed:
        parts.append(f"Embedded text: {transcribed}")
    if labels:
        parts.append("Labels: " + ", ".join(str(l) for l in labels))
    text = "\n".join(parts).strip()
    if not text:
        return
    yield {
        "chunk_id": f"image:{_safe_id(image_json_path.stem)}:c0",
        "source_kind": "image_caption",
        "source_path": payload.get("source_path") or str(image_json_path),
        "bundle_path": str(image_json_path),
        "doc_title": payload.get("source_file") or image_json_path.stem,
        "domain": "",
        "page": None,
        "section": "image",
        "text": text,
    }


def _chunks_from_video(video_dir: Path, max_chars: int) -> Iterator[dict[str, Any]]:
    frames_path = video_dir / "frames.jsonl"
    if not frames_path.exists():
        return
    rows = list(_load_jsonl(frames_path))
    if not rows:
        return
    stem = video_dir.name
    src_id = _safe_id(stem)

    transcript_chunks: list[str] = []
    cur: list[str] = []
    for r in rows:
        seg = (r.get("audio_transcript") or "").strip()
        if seg:
            cur.append(seg)
        if cur and sum(len(s) for s in cur) >= max_chars // 2:
            transcript_chunks.append(" ".join(cur).strip())
            cur = []
    if cur:
        transcript_chunks.append(" ".join(cur).strip())

    for cidx, chunk in enumerate(transcript_chunks):
        if len(chunk) < MIN_CHUNK_CHARS // 2:
            continue
        yield {
            "chunk_id": f"video:{src_id}:t{cidx}",
            "source_kind": "video_transcript",
            "source_path": str(video_dir),
            "bundle_path": str(frames_path),
            "doc_title": stem,
            "domain": "",
            "page": None,
            "section": "audio",
            "text": chunk,
        }

    frame_lines: list[str] = []
    for r in rows:
        ts = r.get("timestamp_sec")
        desc = (r.get("description") or "").strip()
        embedded = (r.get("transcribed_text") or "").strip()
        if not desc and not embedded:
            continue
        line = f"[{ts}s] {desc}"
        if embedded:
            line += f" | embedded text: {embedded}"
        frame_lines.append(line)
    if frame_lines:
        text = "\n".join(frame_lines)
        for cidx, chunk in enumerate(_chunk_text(text, max_chars)):
            yield {
                "chunk_id": f"video:{src_id}:f{cidx}",
                "source_kind": "video_frame",
                "source_path": str(video_dir),
                "bundle_path": str(frames_path),
                "doc_title": stem,
                "domain": "",
                "page": None,
                "section": "frames",
                "text": chunk,
            }


def _chunks_from_text_md(md_path: Path, *, source_kind: str, domain: str, max_chars: int) -> Iterator[dict[str, Any]]:
    raw = md_path.read_text(encoding="utf-8", errors="replace")
    raw = _strip_front_matter(raw)
    if not raw.strip():
        return
    src_id = _safe_id(md_path.stem)

    matches = list(HEADING_RE.finditer(raw))
    if not matches:
        for cidx, chunk in enumerate(_chunk_text(raw, max_chars)):
            yield {
                "chunk_id": f"{source_kind}:{src_id}:c{cidx}",
                "source_kind": source_kind,
                "source_path": str(md_path),
                "bundle_path": str(md_path),
                "doc_title": md_path.stem,
                "domain": domain,
                "page": None,
                "section": "Body",
                "text": chunk,
            }
        return

    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        preface = raw[: matches[0].start()].strip()
        if preface:
            sections.append(("Header", preface))
    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections.append((heading, raw[body_start:body_end].strip()))

    for sidx, (heading, body) in enumerate(sections):
        if not body:
            continue
        for cidx, chunk in enumerate(_chunk_text(body, max_chars)):
            yield {
                "chunk_id": f"{source_kind}:{src_id}:s{sidx}:c{cidx}",
                "source_kind": source_kind,
                "source_path": str(md_path),
                "bundle_path": str(md_path),
                "doc_title": md_path.stem,
                "domain": domain,
                "page": None,
                "section": heading,
                "text": chunk,
            }


def _onchain_facts(profile_path: Path, links_path: Path | None) -> Iterator[dict[str, Any]]:
    """Emit one synthetic chunk per important fact from profile.json."""
    if not profile_path.exists():
        return
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    ipnft = profile.get("ipnft", {}) or {}

    def _emit(key: str, text: str) -> dict[str, Any]:
        return {
            "chunk_id": f"onchain:{_safe_id(key)}",
            "source_kind": "onchain_fact",
            "source_path": str(profile_path),
            "bundle_path": str(profile_path),
            "doc_title": "profile.json",
            "domain": "molecule.to",
            "page": None,
            "section": key,
            "text": text,
        }

    name = ipnft.get("name")
    symbol = profile.get("symbol") or ipnft.get("initialSymbol")
    org = ipnft.get("organization")
    topic = ipnft.get("topic")
    description = ipnft.get("description")
    if name or symbol:
        yield _emit("identity", f"IPNFT name: {name}. Symbol: {symbol}. Organization: {org}. Topic: {topic}.")
    if description:
        yield _emit("description", f"Project description (from on-chain metadata): {_norm(description)}")

    lead = ipnft.get("researchLead") or {}
    if lead.get("name") or lead.get("email"):
        yield _emit(
            "research_lead",
            f"Research lead listed on-chain: name={lead.get('name')}, email={lead.get('email')}.",
        )

    trl = ipnft.get("trlValue")
    trl_rationale = ipnft.get("trlRationale")
    if trl is not None or trl_rationale:
        yield _emit(
            "trl",
            f"Technology Readiness Level: {trl}. Rationale: {_norm(trl_rationale or 'not provided')}.",
        )

    funding_amount = ipnft.get("fundingAmountValue")
    if funding_amount is not None:
        decimals = ipnft.get("fundingAmountDecimals", 0) or 0
        currency = ipnft.get("fundingAmountCurrency", "")
        try:
            amount = int(funding_amount) / (10 ** decimals) if decimals else int(funding_amount)
        except (ValueError, TypeError):
            amount = funding_amount
        yield _emit(
            "funding",
            f"Funding amount on-chain: {amount} {currency}.",
        )

    agreements = ipnft.get("agreements") or []
    if agreements:
        types = ", ".join(a.get("type", "?") for a in agreements)
        yield _emit("agreements", f"On-chain agreements present ({len(agreements)}): {types}.")
    else:
        yield _emit("agreements_missing", "No on-chain legal agreements listed for this IPNFT.")

    ipt = ipnft.get("ipt") or {}
    if ipt:
        markets = ipt.get("markets") or []
        primary = markets[0] if markets else {}
        yield _emit(
            "tokenomics",
            (
                f"IPT symbol: {ipt.get('symbol')}. Holder count: {ipt.get('holderCount')}. "
                f"Total issued: {ipt.get('totalIssued')}. Markets: {len(markets)}. "
                f"Primary market liquidity (USD): {primary.get('liquidityUsd')}. "
                f"Primary market cap (USD): {primary.get('marketCapUsd')}. "
                f"Primary 24h volume (USD): {primary.get('tradingVolume24hr')}."
            ),
        )
    else:
        yield _emit("tokenomics_missing", "No IPT (IP Token) issued for this IPNFT.")

    created = ipnft.get("createdAt")
    minted = ipnft.get("mintedAt")
    updated = ipnft.get("updatedAt")
    if created or minted or updated:
        yield _emit(
            "timeline",
            f"Created: {created}. Minted: {minted}. Last updated on-chain: {updated}.",
        )

    if links_path and links_path.exists():
        try:
            links = json.loads(links_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            links = []
        urls = sorted({l.get("url") for l in links if isinstance(l, dict) and l.get("url")})
        if urls:
            yield _emit(
                "external_links",
                "External links registered with the IPNFT: " + "; ".join(urls[:30]),
            )


def collect_chunks(
    ipnft_dir: Path,
    bundle_dir: Path,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Iterator[dict[str, Any]]:
    pdf_dir = bundle_dir / "pdf"
    if pdf_dir.is_dir():
        for p in sorted(pdf_dir.glob("*.json")):
            yield from _chunks_from_pdf_json(p, max_chars)

    images_dir = bundle_dir / "images"
    if images_dir.is_dir():
        for p in sorted(images_dir.glob("*.json")):
            yield from _chunks_from_image_json(p)

    videos_dir = bundle_dir / "videos"
    if videos_dir.is_dir():
        for d in sorted(videos_dir.iterdir()):
            if d.is_dir():
                yield from _chunks_from_video(d, max_chars)

    text_bundle_dir = bundle_dir / "text"
    if text_bundle_dir.is_dir():
        for p in sorted(text_bundle_dir.glob("*.md")):
            yield from _chunks_from_text_md(
                p, source_kind="text_doc", domain="", max_chars=max_chars
            )

    skip = {"profile.json", "manifest.json", "dataroom.json", "links.json", "crawl-manifest.json", "crawl-extracted-links.json"}
    crawl_md_dirs = [ipnft_dir]
    out_dir = ipnft_dir / "output"
    if out_dir.is_dir():
        crawl_md_dirs.append(out_dir)
    seen_md: set[str] = set()
    for crawl_dir in crawl_md_dirs:
        for p in sorted(crawl_dir.glob("*.md")):
            if p.name in skip or p.name in seen_md:
                continue
            seen_md.add(p.name)
            domain = _domain_from_path(p)
            yield from _chunks_from_text_md(p, source_kind="crawl_md", domain=domain, max_chars=max_chars)

    profile_path = ipnft_dir / "profile.json"
    if not profile_path.exists():
        profile_path = ipnft_dir / "metadata" / "profile.json"
    links_path = ipnft_dir / "links.json"
    if not links_path.exists():
        links_path = ipnft_dir / "metadata" / "links.json"
    yield from _onchain_facts(profile_path, links_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Chunk DAO bundle + crawled MD + on-chain facts to JSONL")
    parser.add_argument("--ipnft-dir", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    by_kind: dict[str, int] = {}
    with args.output.open("w", encoding="utf-8") as fh:
        for chunk in collect_chunks(
            args.ipnft_dir.resolve(),
            args.bundle_dir.resolve(),
            max_chars=args.max_chars,
        ):
            fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")
            count += 1
            by_kind[chunk["source_kind"]] = by_kind.get(chunk["source_kind"], 0) + 1

    print(f"[chunk] wrote {count} chunks to {args.output}")
    for k, v in sorted(by_kind.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
