#!/usr/bin/env python3
"""
Crawl HTTP(S) links from each IPNFT folder's links.json using crawl4ai.

Reads links.json produced by aggregate-links, skips nitter and ipfs URLs,
saves section-deduped markdown under output/. Doc sites get merged deep
crawl; Molecule hub pages use single-fetch link extraction with bounded
off-site follows (no site-wide deep crawl on molecule.xyz).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import aiofiles
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.deep_crawling import BestFirstCrawlingStrategy
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from crawl_common import (
    ensure_layout,
    iter_ipnft_folders,
    links_json_path,
    load_crawl_skip_folders,
    load_manifest,
    metadata_dir,
    output_dir,
    write_manifest,
)

LINKS_FILENAME = "links.json"
MANIFEST_FILENAME = "crawl-manifest.json"
EXTRACTED_LINKS_FILENAME = "crawl-extracted-links.json"
DEFAULT_MIN_CONTENT_CHARS = 400
DEFAULT_MIN_SECTION_CHARS = 80
DEFAULT_HUB_MIN_CONTENT_CHARS = 150
DEFAULT_MAX_OUTBOUND_FOLLOWS = 12
DEFAULT_SPA_WAIT_SEC = 5.0

MOLECULE_HOSTS = frozenset({"molecule.xyz", "mint.molecule.to"})
HUB_BLOCKED_HOSTS = frozenset({
    "docs.molecule.xyz",
    "labs.molecule.xyz",
    "molecule.xyz",
    "mint.molecule.to",
})
# Same on every mint.molecule.to footer — not project-specific; wastes follow budget.
MOLECULE_FOOTER_HOSTS = frozenset({
    "peptai.xyz",
    "bio.xyz",
    "biofy.xyz",
    "ai.bio.xyz",
    "desci.codes",
})
HUB_BLOCKED_URLS = frozenset({
    "https://github.com/moleculeprotocol",
    "https://drive.google.com/drive/folders/1MBSVsumY2qfLNU9-pRERr5eq2f_ogjs8",
    "https://snapshot.box",
})
EXPLORER_HOSTS = frozenset({
    "etherscan.io", "basescan.org", "arbiscan.io", "polygonscan.com",
})
SOCIAL_DEFER_HOSTS = frozenset({
    "t.me", "telegram.me", "telegram.dog",
    "twitter.com", "x.com", "mobile.twitter.com", "nitter.net", "t.co",
    "discord.com", "discord.gg",
    "instagram.com", "facebook.com", "fb.com", "linkedin.com",
    "youtube.com", "youtu.be", "reddit.com", "threads.net", "tiktok.com",
})
SPA_HOSTS = MOLECULE_HOSTS | frozenset({"snapshot.box"})
MOLECULE_BLOCKED_PATH_PREFIXES = (
    "/discover",
    "/blog",
    "/resources",
    "/about",
    "/careers",
    "/app",
    "/brand",
)
BLOCKED_ANCHOR_RES = [
    re.compile(r"discover\s+projects?", re.I),
    re.compile(r"^launch\s+app$", re.I),
    re.compile(r"^connect$", re.I),
    re.compile(r"^subscribe$", re.I),
    re.compile(r"^sign\s+in$", re.I),
    re.compile(r"^log\s+in$", re.I),
]
ERROR_PAGE_PHRASES = (
    "page not found",
    "can't find the page",
    "cannot find the page",
)

CRAWLER_DIR = Path(__file__).resolve().parent

RESERVED_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+")
BOILERPLATE_LINE_RES = [
    re.compile(r"^\[Powered by GitBook\]", re.I),
    re.compile(r"^Last updated ", re.I),
    re.compile(r"^\[(Previous|Next)", re.I),
    re.compile(r"^Copy\s*$", re.I),
    re.compile(r"^`⌘Ctrl`", re.I),
    re.compile(r"^On this page\s*$", re.I),
    re.compile(r"^\[\]\(", re.I),
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_nitter_url(url: str) -> bool:
    return bare_host(url) == "nitter.net"


def is_social_url(url: str) -> bool:
    return bare_host(url) in SOCIAL_DEFER_HOSTS


def is_explorer_url(url: str) -> bool:
    host = bare_host(url)
    if host in EXPLORER_HOSTS:
        return True
    return any(
        host.endswith(suffix)
        for suffix in (".etherscan.io", ".basescan.org", ".arbiscan.io", ".polygonscan.com")
    )


def is_ipfs_url(url: str, site_name: str | None = None) -> bool:
    if url.strip().lower().startswith("ipfs://"):
        return True
    return (site_name or "").lower() == "ipfs"


def is_http_url(url: str) -> bool:
    return url.strip().lower().startswith(("http://", "https://"))


class CrawlMode(str, Enum):
    SINGLE = "single"
    DOCS_DEEP = "docs_deep"
    HUB = "hub"
    SPA_SINGLE = "spa_single"


def bare_host(url: str) -> str:
    try:
        host = urlparse(url.strip()).hostname or ""
        return host.lower().removeprefix("www.")
    except Exception:
        return ""


def is_molecule_host(url: str) -> bool:
    return bare_host(url) in MOLECULE_HOSTS


def molecule_project_id(url: str) -> str | None:
    """Extract ipnft token id from molecule.xyz or mint.molecule.to paths."""
    try:
        path = urlparse(url.strip()).path
        for pattern in (r"/ipnfts?/([^/?#]+)", r"/projects/([^/?#]+)"):
            m = re.search(pattern, path, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def molecule_xyz_to_mint(url: str) -> str | None:
    if bare_host(url) != "molecule.xyz":
        return None
    pid = molecule_project_id(url)
    if not pid:
        return None
    return f"https://mint.molecule.to/ipnft/{pid}"


def crawl_mode_for_url(url: str) -> CrawlMode:
    host = bare_host(url)
    if host in MOLECULE_HOSTS:
        return CrawlMode.HUB
    if host == "snapshot.box":
        return CrawlMode.SPA_SINGLE
    lower = url.lower()
    if "doc" in lower or "docs" in lower or "gitbook" in host:
        return CrawlMode.DOCS_DEEP
    return CrawlMode.SINGLE


def is_blocked_catalog_path(path: str) -> bool:
    lower = path.lower().rstrip("/") or "/"
    if lower == "/discover":
        return True
    for prefix in MOLECULE_BLOCKED_PATH_PREFIXES:
        if lower.startswith(prefix):
            return True
    # Project listing index (/projects with no id segment beyond listing)
    if lower == "/projects":
        return True
    return False


def is_blocked_anchor(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return any(p.search(t) for p in BLOCKED_ANCHOR_RES)


def is_global_footer_url(url: str) -> bool:
    """Molecule mint pages share the same footer/ecosystem links on every IPNFT."""
    try:
        base = url.split("#")[0].rstrip("/")
        if base in HUB_BLOCKED_URLS:
            return True
    except Exception:
        pass
    return bare_host(url) in MOLECULE_FOOTER_HOSTS


def is_blocked_follow_url(url: str, seed_url: str) -> str | None:
    """Return block reason, or None if external follow is allowed."""
    if not is_http_url(url):
        return "non_http"
    if is_nitter_url(url) or is_ipfs_url(url):
        return "deferred"
    if is_social_url(url):
        return "social_deferred"
    if is_explorer_url(url):
        return "explorer_deferred"
    if is_global_footer_url(url):
        return "molecule_footer_blocked"
    host = bare_host(url)
    if host in HUB_BLOCKED_HOSTS:
        return "molecule_ecosystem_blocked"
    if host in MOLECULE_HOSTS:
        seed_id = molecule_project_id(seed_url)
        cand_id = molecule_project_id(url)
        if seed_id and cand_id and seed_id == cand_id:
            return None
        return "molecule_catalog_blocked"
    try:
        if is_blocked_catalog_path(urlparse(url).path):
            return "catalog_blocked"
    except Exception:
        pass
    if url.rstrip("/") == seed_url.rstrip("/"):
        return "duplicate_seed"
    return None


def is_docs_url(url: str) -> bool:
    return crawl_mode_for_url(url) == CrawlMode.DOCS_DEEP


def sanitize_filename(name: str, max_len: int = 120) -> str:
    cleaned = RESERVED_CHARS.sub("_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._ ")
    return (cleaned[:max_len] if cleaned else "unnamed")


def site_name_from_url(url: str) -> str:
    try:
        host = urlparse(url.strip()).hostname or "unknown"
        return host.lower().removeprefix("www.")
    except Exception:
        return "unknown"


def page_slug_from_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        path = parsed.path.strip("/") or "index"
        path = path.replace("/", "__")
        return sanitize_filename(path, 80)
    except Exception:
        return "index"


def merged_docs_markdown_path(ipnft_dir: Path, site: str) -> Path:
    return output_dir(ipnft_dir) / f"{sanitize_filename(site)}.md"


def is_boilerplate_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(p.search(stripped) for p in BOILERPLATE_LINE_RES)


def strip_boilerplate_lines(md: str) -> str:
    kept = [line for line in md.splitlines() if not is_boilerplate_line(line)]
    return "\n".join(kept).strip()


def is_nav_boilerplate(body: str) -> bool:
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return True
    linkish = sum(
        1
        for line in lines
        if line.lstrip().startswith(("*", "-", "["))
        or "](http" in line
        or line.strip() in {"More", "Copy"}
    )
    return linkish / len(lines) >= 0.55


def heading_title(heading: str | None) -> str | None:
    if not heading:
        return None
    title = re.sub(r"^#+\s*", "", heading).strip()
    return title or None


def split_markdown_sections(md: str) -> list[tuple[str | None, str]]:
    """Split into (heading_line, body) sections. Preamble uses heading None."""
    sections: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_heading, current_lines
        body = "\n".join(current_lines).strip()
        if current_heading or body:
            sections.append((current_heading, body))
        current_heading = None
        current_lines = []

    for line in md.splitlines():
        if HEADING_LINE_RE.match(line):
            if not heading_title(line):
                current_lines.append(line)
                continue
            flush()
            current_heading = line.rstrip()
        else:
            current_lines.append(line)
    flush()
    return sections


def section_fingerprint(heading: str | None, body: str) -> str:
    label = heading or ""
    normalized = normalize_markdown_for_compare(f"{label}\n{body}")
    return hashlib.sha256(normalized.encode()).hexdigest()


def collect_unique_sections(
    md: str,
    seen_fps: set[str],
    *,
    min_section_chars: int,
) -> tuple[list[str], int, int]:
    """
    Return (section_blocks, kept_count, dropped_count).
    Each block is heading+body ready to join.
    """
    cleaned = strip_boilerplate_lines(md)
    blocks: list[str] = []
    kept = 0
    dropped = 0

    for heading, body in split_markdown_sections(cleaned):
        if not heading and is_nav_boilerplate(body):
            dropped += 1
            continue
        content = body if heading else body
        if meaningful_content_length(content) < min_section_chars:
            dropped += 1
            continue
        fp = section_fingerprint(heading, body)
        if fp in seen_fps:
            dropped += 1
            continue
        seen_fps.add(fp)
        if heading:
            blocks.append(f"{heading}\n\n{body}".strip() if body else heading)
        elif body:
            blocks.append(body)
        kept += 1

    return blocks, kept, dropped


def dedupe_markdown_document(
    md: str,
    seen_fps: set[str],
    *,
    min_section_chars: int,
) -> tuple[str, int, int]:
    """Section-dedupe a single document (within-file + global seen_fps)."""
    blocks, kept, dropped = collect_unique_sections(
        md, seen_fps, min_section_chars=min_section_chars,
    )
    return "\n\n".join(blocks), kept, dropped


def merge_docs_pages(
    pages: list[tuple[str, str]],
    seen_fps: set[str],
    *,
    min_section_chars: int,
) -> tuple[str, int, int, list[str]]:
    """
    Merge crawled doc pages into one markdown string.
    pages: list of (source_url, markdown)
    """
    all_blocks: list[str] = []
    total_kept = 0
    total_dropped = 0
    sources: list[str] = []

    for page_url, md in pages:
        if not md.strip():
            continue
        page_blocks, kept, dropped = collect_unique_sections(
            md, seen_fps, min_section_chars=min_section_chars,
        )
        if not page_blocks:
            total_dropped += dropped
            continue
        slug = page_slug_from_url(page_url)
        all_blocks.append(f"## Page: {slug}\n\nSource: {page_url}")
        all_blocks.extend(page_blocks)
        total_kept += kept
        total_dropped += dropped
        sources.append(page_url)

    merged = "\n\n---\n\n".join(all_blocks)
    return merged, total_kept, total_dropped, sources


def crawl_output_sites(urls: Iterable[str]) -> set[str]:
    return {site_name_from_url(u) for u in urls}


def cleanup_prior_crawl_outputs(
    ipnft_dir: Path,
    manifest: dict[str, Any] | None,
    crawl_urls: list[str],
) -> None:
    """Remove crawl artifacts under output/ before re-writing."""
    out = output_dir(ipnft_dir)
    sites = crawl_output_sites(crawl_urls)
    if manifest:
        for entry in manifest.get("entries", []):
            url = entry.get("url")
            if isinstance(url, str):
                sites.add(site_name_from_url(url))
            md = entry.get("markdown")
            if isinstance(md, str):
                stem = Path(md).name.removesuffix(".md")
                sites.add(stem)
            for page in entry.get("pages") or []:
                if isinstance(page, str) and "/" in page:
                    sites.add(Path(page).name.split(".")[0])

    for site in sites:
        sub = out / sanitize_filename(site)
        if sub.is_dir():
            shutil.rmtree(sub)
        stem = sanitize_filename(site)
        for path in out.glob(f"{stem}*.md"):
            if path.is_file():
                path.unlink()

    meta = metadata_dir(ipnft_dir)
    sidecar = meta / EXTRACTED_LINKS_FILENAME
    if sidecar.is_file():
        sidecar.unlink()

    media_dir = out / "media"
    if media_dir.is_dir():
        shutil.rmtree(media_dir)


def load_existing_manifest(ipnft_dir: Path) -> dict[str, Any] | None:
    return load_manifest(metadata_dir(ipnft_dir), MANIFEST_FILENAME)


def completed_urls(manifest: dict[str, Any] | None) -> set[str]:
    if not manifest:
        return set()
    urls: set[str] = set()
    for entry in manifest.get("entries", []):
        if entry.get("success"):
            url = entry.get("url")
            if isinstance(url, str):
                urls.add(url)
    return urls


def classify_link(link: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (url, skip_reason). url is set when crawlable."""
    url = str(link.get("url", "")).strip()
    if not url:
        return None, "empty_url"
    if is_nitter_url(url):
        return None, "nitter_deferred"
    if is_ipfs_url(url, link.get("siteName")):
        return None, "ipfs_deferred"
    if is_social_url(url):
        return None, "social_deferred"
    if is_explorer_url(url):
        return None, "explorer_deferred"
    if not is_http_url(url):
        return None, "non_http"
    if link.get("http_accessible") == "no":
        return None, "inaccessible"
    return url, None


def dedupe_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for link in links:
        url = str(link.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(link)
    return out


def markdown_text(result: Any) -> str:
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    if isinstance(md, str):
        return md
    raw = getattr(md, "raw_markdown", None)
    if isinstance(raw, str) and raw.strip():
        return raw
    fit = getattr(md, "fit_markdown", None)
    if isinstance(fit, str):
        return fit
    return ""


def normalize_markdown_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def meaningful_content_length(text: str) -> int:
    """Length after stripping markdown links/images and collapsing whitespace."""
    stripped = MARKDOWN_IMAGE_RE.sub("", text)
    stripped = MARKDOWN_LINK_RE.sub("", stripped)
    stripped = re.sub(r"[#>*_`~\[\]|\\-]", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return len(stripped)


def is_too_short(text: str, min_chars: int) -> bool:
    return meaningful_content_length(text) < min_chars


def fetch_config(*, spa: bool = False, spa_wait_sec: float = DEFAULT_SPA_WAIT_SEC) -> CrawlerRunConfig:
    kwargs: dict[str, Any] = {
        "cache_mode": CacheMode.BYPASS,
        "exclude_all_images": True,
        "markdown_generator": DefaultMarkdownGenerator(
            options={"citations": True, "body_width": 0},
        ),
    }
    if spa:
        kwargs["delay_before_return_html"] = spa_wait_sec
        kwargs["scan_full_page"] = True
        kwargs["page_timeout"] = 90_000
    return CrawlerRunConfig(**kwargs)


def full_crawl_config(*, spa: bool = False, spa_wait_sec: float = DEFAULT_SPA_WAIT_SEC) -> CrawlerRunConfig:
    return fetch_config(spa=spa, spa_wait_sec=spa_wait_sec)


def prefetch_deep_config(max_depth: int, max_pages: int) -> CrawlerRunConfig:
    return CrawlerRunConfig(
        prefetch=True,
        cache_mode=CacheMode.BYPASS,
        deep_crawl_strategy=BestFirstCrawlingStrategy(
            max_depth=max_depth,
            max_pages=max_pages,
            include_external=False,
        ),
    )


async def collect_deep_crawl_results(
    crawler: AsyncWebCrawler,
    url: str,
    config: CrawlerRunConfig,
) -> list[Any]:
    container = await crawler.arun(url, config=config)
    if container is None:
        return []
    if hasattr(container, "__aiter__"):
        results = []
        async for item in container:
            results.append(item)
        return results
    if hasattr(container, "__iter__") and not isinstance(container, (str, bytes, dict)):
        try:
            return list(container)
        except TypeError:
            pass
    return [container]


def discovered_urls_from_results(results: list[Any], seed_url: str) -> list[str]:
    seed_host = urlparse(seed_url.strip()).netloc.lower().removeprefix("www.")
    seen: set[str] = {seed_url.rstrip("/")}
    ordered: list[str] = [seed_url]

    def maybe_add(raw: str) -> None:
        if not isinstance(raw, str) or not raw.strip().startswith(("http://", "https://")):
            return
        host = urlparse(raw.strip()).netloc.lower().removeprefix("www.")
        if host != seed_host:
            return
        norm = raw.rstrip("/")
        if norm not in seen:
            seen.add(norm)
            ordered.append(raw)

    for result in results:
        url = getattr(result, "url", None) or getattr(result, "redirected_url", None)
        if isinstance(url, str):
            maybe_add(url)
        links = getattr(result, "links", None) or {}
        for link in links.get("internal", []) or []:
            href = link.get("href") if isinstance(link, dict) else None
            maybe_add(href)
    return ordered


async def save_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(text)


def primary_result(result: Any) -> Any | None:
    if result is None:
        return None
    return result[0] if isinstance(result, list) else result


def is_error_page(result: Any) -> bool:
    status = getattr(result, "status_code", None)
    if status == 404:
        return True
    md = markdown_text(result).lower()
    html_snip = (getattr(result, "html", None) or "")[:8000].lower()
    combined = f"{md}\n{html_snip}"
    return any(phrase in combined for phrase in ERROR_PAGE_PHRASES)


def extract_links_from_result(result: Any, page_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    links = getattr(result, "links", None) or {}
    for bucket in ("internal", "external"):
        for item in links.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue
            href = item.get("href")
            if not isinstance(href, str) or not href.strip():
                continue
            href = href.strip()
            if href.startswith("/"):
                href = urljoin(page_url, href)
            if not href.startswith(("http://", "https://")):
                continue
            norm = href.rstrip("/")
            if norm in seen:
                continue
            seen.add(norm)
            text = str(item.get("text") or item.get("title") or "").strip()
            out.append({"url": href, "text": text, "kind": bucket})
    return out


def filter_follow_candidates(
    seed_url: str,
    extracted: list[dict[str, str]],
    *,
    seed_urls_from_links: set[str],
    already_planned: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    allowed: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for item in extracted:
        url = item["url"]
        if is_blocked_anchor(item.get("text", "")):
            rejected.append({**item, "reason": "blocked_anchor"})
            continue
        if url.rstrip("/") in {u.rstrip("/") for u in seed_urls_from_links}:
            rejected.append({**item, "reason": "already_in_links_json"})
            continue
        if url.rstrip("/") in {u.rstrip("/") for u in already_planned}:
            rejected.append({**item, "reason": "already_planned"})
            continue
        block = is_blocked_follow_url(url, seed_url)
        if block:
            rejected.append({**item, "reason": block})
            continue
        allowed.append(item)
    return allowed, rejected


class FollowState:
    """Tracks bounded off-site follows for one IPNFT folder."""

    def __init__(
        self,
        *,
        budget: int,
        seed_urls: set[str],
        done_urls: set[str],
    ) -> None:
        self.remaining = budget
        self.seed_urls = seed_urls
        self.done_urls = done_urls
        self.planned: set[str] = set()

    def can_follow(self, url: str) -> bool:
        norm = url.rstrip("/")
        if self.remaining <= 0:
            return False
        if norm in {u.rstrip("/") for u in self.done_urls}:
            return False
        if norm in {u.rstrip("/") for u in self.planned}:
            return False
        return True

    def mark_planned(self, url: str) -> None:
        self.planned.add(url.rstrip("/"))

    def consume(self) -> None:
        self.remaining -= 1


async def fetch_page(
    crawler: AsyncWebCrawler,
    url: str,
    *,
    spa: bool = False,
    spa_wait_sec: float = DEFAULT_SPA_WAIT_SEC,
) -> Any | None:
    result = await crawler.arun(url, config=fetch_config(spa=spa, spa_wait_sec=spa_wait_sec))
    return primary_result(result)


async def save_page_markdown(
    *,
    url: str,
    ipnft_dir: Path,
    raw_md: str,
    seen_section_fps: set[str],
    min_section_chars: int,
    min_chars: int,
) -> tuple[str | None, int, int, str | None]:
    site = site_name_from_url(url)
    md, sections_kept, sections_dropped = dedupe_markdown_document(
        raw_md, seen_section_fps, min_section_chars=min_section_chars,
    )
    if not md.strip():
        return None, sections_kept, sections_dropped, "empty_markdown"
    if is_too_short(md, min_chars):
        return None, sections_kept, sections_dropped, "too_short"
    md_path = merged_docs_markdown_path(ipnft_dir, site)
    await save_markdown(md_path, md)
    rel = md_path.relative_to(ipnft_dir).as_posix()
    return rel, sections_kept, sections_dropped, None


async def crawl_single_link(
    crawler: AsyncWebCrawler,
    url: str,
    ipnft_dir: Path,
    *,
    seen_section_fps: set[str],
    min_chars: int,
    min_section_chars: int,
    spa: bool = False,
    spa_wait_sec: float = DEFAULT_SPA_WAIT_SEC,
) -> dict[str, Any]:
    primary = await fetch_page(crawler, url, spa=spa, spa_wait_sec=spa_wait_sec)
    if primary is None:
        return {"url": url, "mode": "single", "success": False, "error": "no_result"}
    if not getattr(primary, "success", False):
        err = getattr(primary, "error_message", None) or "crawl_failed"
        return {"url": url, "mode": "single", "success": False, "error": str(err)}

    rel, kept, dropped, err = await save_page_markdown(
        url=url,
        ipnft_dir=ipnft_dir,
        raw_md=markdown_text(primary),
        seen_section_fps=seen_section_fps,
        min_section_chars=min_section_chars,
        min_chars=min_chars,
    )
    if err:
        return {
            "url": url,
            "mode": "single",
            "success": False,
            "error": err,
            "sections_kept": kept,
            "sections_dropped": dropped,
        }
    return {
        "url": url,
        "mode": "single",
        "success": True,
        "markdown": rel,
        "sections_kept": kept,
        "sections_dropped": dropped,
    }


async def crawl_hub_link(
    crawler: AsyncWebCrawler,
    url: str,
    ipnft_dir: Path,
    *,
    seen_section_fps: set[str],
    min_chars: int,
    hub_min_chars: int,
    min_section_chars: int,
    follow_state: FollowState,
    spa_wait_sec: float = DEFAULT_SPA_WAIT_SEC,
) -> dict[str, Any]:
    """Single fetch on Molecule hub; extract outbound links; bounded off-site follows."""
    fetch_url = url
    primary = await fetch_page(crawler, fetch_url, spa=True, spa_wait_sec=spa_wait_sec)
    fallback_url: str | None = None

    if primary is None or not getattr(primary, "success", False) or is_error_page(primary):
        fallback_url = molecule_xyz_to_mint(url)
        if fallback_url and fallback_url.rstrip("/") != url.rstrip("/"):
            primary = await fetch_page(crawler, fallback_url, spa=True, spa_wait_sec=spa_wait_sec)
            if primary is not None and getattr(primary, "success", False):
                fetch_url = fallback_url

    if primary is None:
        return {"url": url, "mode": "hub", "success": False, "error": "no_result"}
    if not getattr(primary, "success", False):
        err = getattr(primary, "error_message", None) or "crawl_failed"
        return {"url": url, "mode": "hub", "success": False, "error": str(err)}

    is_404 = is_error_page(primary)
    extracted = [] if is_404 else extract_links_from_result(primary, fetch_url)
    allowed, rejected = filter_follow_candidates(
        fetch_url,
        extracted,
        seed_urls_from_links=follow_state.seed_urls,
        already_planned=follow_state.planned,
    )

    sidecar = {
        "seedUrl": url,
        "fetchedUrl": fetch_url,
        "mintFallback": fallback_url,
        "isErrorPage": is_404,
        "extractedCount": len(extracted),
        "allowedFollows": allowed,
        "rejectedFollows": rejected,
    }
    sidecar_path = metadata_dir(ipnft_dir) / EXTRACTED_LINKS_FILENAME
    existing_sidecar: list[Any] = []
    if sidecar_path.is_file():
        try:
            existing_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if not isinstance(existing_sidecar, list):
                existing_sidecar = []
        except Exception:
            existing_sidecar = []
    existing_sidecar.append(sidecar)
    sidecar_path.write_text(json.dumps(existing_sidecar, indent=2) + "\n", encoding="utf-8")

    hub_md_rel: str | None = None
    hub_kept = hub_dropped = 0
    if not is_404:
        hub_md_rel, hub_kept, hub_dropped, md_err = await save_page_markdown(
            url=fetch_url,
            ipnft_dir=ipnft_dir,
            raw_md=markdown_text(primary),
            seen_section_fps=seen_section_fps,
            min_section_chars=min_section_chars,
            min_chars=hub_min_chars,
        )
        if md_err and md_err != "too_short":
            pass  # empty hub page is ok when we still have follows

    follow_entries: list[dict[str, Any]] = []
    followed_count = 0
    for item in allowed:
        follow_url = item["url"]
        if not follow_state.can_follow(follow_url):
            rejected.append({**item, "reason": "follow_budget_exhausted"})
            continue
        follow_state.mark_planned(follow_url)
        follow_state.consume()
        followed_count += 1
        print(f"    hub follow: {follow_url}", flush=True)
        child = await dispatch_crawl_url(
            crawler,
            follow_url,
            ipnft_dir,
            seen_section_fps=seen_section_fps,
            min_chars=min_chars,
            hub_min_chars=hub_min_chars,
            min_section_chars=min_section_chars,
            doc_max_depth=2,
            doc_max_pages=25,
            follow_state=follow_state,
            spa_wait_sec=spa_wait_sec,
            from_hub=True,
        )
        follow_entries.append(child)

    success = bool(hub_md_rel) or any(e.get("success") for e in follow_entries)
    entry: dict[str, Any] = {
        "url": url,
        "mode": "hub",
        "success": success,
        "fetchedUrl": fetch_url,
        "mintFallback": fallback_url,
        "isErrorPage": is_404,
        "markdown": hub_md_rel,
        "sections_kept": hub_kept,
        "sections_dropped": hub_dropped,
        "extractedCount": len(extracted),
        "allowedFollowCount": followed_count,
        "followEntries": follow_entries,
        "rejectedFollows": rejected[:30],
    }
    if not success:
        entry["error"] = "hub_empty_and_no_follows" if is_404 else "no_content"
    return entry


async def crawl_spa_single_link(
    crawler: AsyncWebCrawler,
    url: str,
    ipnft_dir: Path,
    *,
    seen_section_fps: set[str],
    min_chars: int,
    min_section_chars: int,
    spa_wait_sec: float = DEFAULT_SPA_WAIT_SEC,
) -> dict[str, Any]:
    return await crawl_single_link(
        crawler,
        url,
        ipnft_dir,
        seen_section_fps=seen_section_fps,
        min_chars=min_chars,
        min_section_chars=min_section_chars,
        spa=True,
        spa_wait_sec=spa_wait_sec,
    )


async def dispatch_crawl_url(
    crawler: AsyncWebCrawler,
    url: str,
    ipnft_dir: Path,
    *,
    seen_section_fps: set[str],
    min_chars: int,
    hub_min_chars: int,
    min_section_chars: int,
    doc_max_depth: int,
    doc_max_pages: int,
    follow_state: FollowState,
    spa_wait_sec: float = DEFAULT_SPA_WAIT_SEC,
    from_hub: bool = False,
) -> dict[str, Any]:
    mode = crawl_mode_for_url(url)
    if mode == CrawlMode.HUB and not from_hub:
        return await crawl_hub_link(
            crawler,
            url,
            ipnft_dir,
            seen_section_fps=seen_section_fps,
            min_chars=min_chars,
            hub_min_chars=hub_min_chars,
            min_section_chars=min_section_chars,
            follow_state=follow_state,
            spa_wait_sec=spa_wait_sec,
        )
    if mode == CrawlMode.DOCS_DEEP:
        if from_hub:
            return await crawl_single_link(
                crawler,
                url,
                ipnft_dir,
                seen_section_fps=seen_section_fps,
                min_chars=min_chars,
                min_section_chars=min_section_chars,
                spa=False,
                spa_wait_sec=spa_wait_sec,
            )
        return await crawl_docs_link(
            crawler,
            url,
            ipnft_dir,
            max_depth=doc_max_depth,
            max_pages=doc_max_pages,
            seen_section_fps=seen_section_fps,
            min_chars=min_chars,
            min_section_chars=min_section_chars,
        )
    if mode == CrawlMode.SPA_SINGLE:
        return await crawl_spa_single_link(
            crawler,
            url,
            ipnft_dir,
            seen_section_fps=seen_section_fps,
            min_chars=min_chars,
            min_section_chars=min_section_chars,
            spa_wait_sec=spa_wait_sec,
        )
    return await crawl_single_link(
        crawler,
        url,
        ipnft_dir,
        seen_section_fps=seen_section_fps,
        min_chars=min_chars,
        min_section_chars=min_section_chars,
        spa=bare_host(url) in SPA_HOSTS,
        spa_wait_sec=spa_wait_sec,
    )


async def crawl_docs_link(
    crawler: AsyncWebCrawler,
    url: str,
    ipnft_dir: Path,
    *,
    max_depth: int,
    max_pages: int,
    seen_section_fps: set[str],
    min_chars: int,
    min_section_chars: int,
) -> dict[str, Any]:
    site = site_name_from_url(url)
    discovery_config = prefetch_deep_config(max_depth, max_pages)

    try:
        discovery_results = await collect_deep_crawl_results(crawler, url, discovery_config)
    except Exception as exc:
        return {"url": url, "mode": "deep", "success": False, "error": f"discovery_failed: {exc}"}

    urls = discovered_urls_from_results(discovery_results, url)
    if not urls:
        urls = [url]
    urls = urls[:max_pages]

    full_config = fetch_config()
    crawled_pages: list[tuple[str, str]] = []
    dropped: list[dict[str, str]] = []
    errors: list[str] = []

    for page_url in urls:
        try:
            result = await crawler.arun(page_url, config=full_config)
            if result is None:
                errors.append(f"{page_url}: no_result")
                continue
            primary = result[0] if isinstance(result, list) else result
            if not getattr(primary, "success", False):
                err = getattr(primary, "error_message", None) or "failed"
                errors.append(f"{page_url}: {err}")
                continue
            md = markdown_text(primary)
            if not md.strip():
                dropped.append({"url": page_url, "reason": "empty_markdown"})
                continue
            crawled_pages.append((page_url, md))
        except Exception as exc:
            errors.append(f"{page_url}: {exc}")

    if not crawled_pages:
        return {
            "url": url,
            "mode": "deep",
            "success": False,
            "error": "; ".join(errors[:5]) or "no_pages_crawled",
            "dropped": dropped[:20],
        }

    merged, sections_kept, sections_dropped, source_pages = merge_docs_pages(
        crawled_pages,
        seen_section_fps,
        min_section_chars=min_section_chars,
    )

    if not merged.strip() or is_too_short(merged, min_chars):
        return {
            "url": url,
            "mode": "deep",
            "success": False,
            "error": "merged_too_short",
            "discovered_count": len(urls),
            "sections_kept": sections_kept,
            "sections_dropped": sections_dropped,
            "dropped": dropped[:20],
        }

    md_path = merged_docs_markdown_path(ipnft_dir, site)
    await save_markdown(md_path, merged)
    rel = md_path.relative_to(ipnft_dir).as_posix()

    return {
        "url": url,
        "mode": "deep",
        "success": True,
        "markdown": rel,
        "source_pages": source_pages,
        "discovered_count": len(urls),
        "sections_kept": sections_kept,
        "sections_dropped": sections_dropped,
        "dropped": dropped[:20],
        "errors": errors[:10] if errors else [],
    }


async def crawl_ipnft_folder(
    ipnft_dir: Path,
    *,
    force: bool,
    dry_run: bool,
    doc_max_depth: int,
    doc_max_pages: int,
    min_chars: int,
    min_section_chars: int,
    hub_min_chars: int,
    max_outbound_follows: int,
    spa_wait_sec: float,
) -> dict[str, Any]:
    folder_name = ipnft_dir.name
    ensure_layout(ipnft_dir)
    meta = metadata_dir(ipnft_dir)
    links_path = links_json_path(ipnft_dir)
    if not links_path.is_file():
        return {
            "ipnftFolder": folder_name,
            "skipped": True,
            "reason": "no_links_json",
        }

    raw_links = json.loads(links_path.read_text(encoding="utf-8"))
    if not isinstance(raw_links, list):
        return {"ipnftFolder": folder_name, "skipped": True, "reason": "invalid_links_json"}

    links = dedupe_links(raw_links)
    existing = load_existing_manifest(ipnft_dir)
    done = set() if force else completed_urls(existing)

    skipped: list[dict[str, str]] = []
    to_crawl: list[tuple[str, dict[str, Any]]] = []

    for link in links:
        url, reason = classify_link(link)
        if reason:
            skipped.append({"url": str(link.get("url", "")), "reason": reason})
            continue
        assert url is not None
        if url in done:
            skipped.append({"url": url, "reason": "already_crawled"})
            continue
        to_crawl.append((url, link))

    if dry_run:
        return {
            "ipnftFolder": folder_name,
            "dry_run": True,
            "would_crawl": [u for u, _ in to_crawl],
            "skipped": skipped,
        }

    if force and to_crawl:
        cleanup_prior_crawl_outputs(
            ipnft_dir,
            existing,
            [u for u, _ in to_crawl],
        )

    entries: list[dict[str, Any]] = []
    if existing and not force:
        entries = [e for e in existing.get("entries", []) if e.get("success")]

    seen_section_fps: set[str] = set()
    seed_url_set = {u.rstrip("/") for u, _ in to_crawl}
    follow_state = FollowState(
        budget=max_outbound_follows,
        seed_urls=seed_url_set,
        done_urls=done,
    )
    browser_config = BrowserConfig(headless=True, verbose=False)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for url, _link in to_crawl:
            print(f"  [{folder_name}] crawling {url} ({crawl_mode_for_url(url).value})", flush=True)
            entry = await dispatch_crawl_url(
                crawler,
                url,
                ipnft_dir,
                seen_section_fps=seen_section_fps,
                min_chars=min_chars,
                hub_min_chars=hub_min_chars,
                min_section_chars=min_section_chars,
                doc_max_depth=doc_max_depth,
                doc_max_pages=doc_max_pages,
                follow_state=follow_state,
                spa_wait_sec=spa_wait_sec,
            )
            entries.append(entry)

    manifest = {
        "generatedAt": _utc_now(),
        "ipnftFolder": folder_name,
        "source": LINKS_FILENAME,
        "minContentChars": min_chars,
        "hubMinContentChars": hub_min_chars,
        "minSectionChars": min_section_chars,
        "maxOutboundFollows": max_outbound_follows,
        "spaWaitSec": spa_wait_sec,
        "entries": entries,
        "skipped": skipped,
    }
    write_manifest(meta, MANIFEST_FILENAME, manifest)

    ok = sum(1 for e in entries if e.get("success"))
    print(f"  [{folder_name}] done: {ok}/{len(entries)} crawled, {len(skipped)} skipped", flush=True)
    return manifest


async def run_all(args: argparse.Namespace) -> int:
    ipnfts_dir = args.ipnfts_dir.resolve()
    if not ipnfts_dir.is_dir():
        print(f"Error: not a directory: {ipnfts_dir}", file=sys.stderr)
        return 1

    skip_folders = load_crawl_skip_folders(args.crawl_skip_file)

    try:
        folders = iter_ipnft_folders(
            ipnfts_dir,
            folder=args.folder,
            max_folders=args.max,
            skip_folders=skip_folders,
            require_links=True,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not folders:
        print("No IPNFT folders with links.json to process.", file=sys.stderr)
        return 0

    print(
        f"Crawling {len(folders)} IPNFT folder(s) under {ipnfts_dir} "
        f"(concurrency={args.concurrency})",
        flush=True,
    )

    sem = asyncio.Semaphore(args.concurrency)

    async def bounded(folder: Path) -> dict[str, Any]:
        async with sem:
            return await crawl_ipnft_folder(
                folder,
                force=args.force,
                dry_run=args.dry_run,
                doc_max_depth=args.doc_max_depth,
                doc_max_pages=args.doc_max_pages,
                min_chars=args.min_chars,
                min_section_chars=args.min_section_chars,
                hub_min_chars=args.hub_min_chars,
                max_outbound_follows=args.max_outbound_follows,
                spa_wait_sec=args.spa_wait_sec,
            )

    results = await asyncio.gather(*(bounded(f) for f in folders), return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    if failures:
        for err in failures:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Crawl links.json URLs for each IPNFT folder using crawl4ai.",
    )
    p.add_argument("--ipnfts-dir", type=Path, required=True)
    p.add_argument("--folder", type=str, default=None, help="Single IPNFT folder name")
    p.add_argument("--max", type=int, default=None, help="Max IPNFT folders to process")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel IPNFT folder crawls")
    p.add_argument("--doc-max-depth", type=int, default=2)
    p.add_argument("--doc-max-pages", type=int, default=25)
    p.add_argument(
        "--min-chars",
        type=int,
        default=DEFAULT_MIN_CONTENT_CHARS,
        help="Drop output with fewer meaningful characters after stripping links/images (default: 400)",
    )
    p.add_argument(
        "--min-section-chars",
        type=int,
        default=DEFAULT_MIN_SECTION_CHARS,
        help="Drop sections shorter than this when deduping (default: 80)",
    )
    p.add_argument(
        "--hub-min-chars",
        type=int,
        default=DEFAULT_HUB_MIN_CONTENT_CHARS,
        help="Min meaningful chars for Molecule hub pages (default: 150)",
    )
    p.add_argument(
        "--max-outbound-follows",
        type=int,
        default=DEFAULT_MAX_OUTBOUND_FOLLOWS,
        help="Max off-site URLs to crawl per IPNFT from hub extraction (default: 12)",
    )
    p.add_argument(
        "--spa-wait-sec",
        type=float,
        default=DEFAULT_SPA_WAIT_SEC,
        help="Extra wait before capturing SPA pages (default: 5)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Re-crawl URLs even if manifest exists")
    p.add_argument("--crawl-skip-file", type=Path, default=None)
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    code = asyncio.run(run_all(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
