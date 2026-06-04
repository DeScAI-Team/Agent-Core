#!/usr/bin/env python3
"""Fetch recent tweets from nitter.net profile links into tweets.json per IPNFT folder."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from crawl_common import (
    collect_urls_from_metadata,
    ensure_layout,
    is_nitter_url,
    iter_ipnft_folders,
    load_crawl_skip_folders,
    load_manifest,
    metadata_dir,
    output_dir,
    utc_now,
    write_manifest,
)

TWEETS_FILENAME = "tweets.json"
MANIFEST_FILENAME = "nitter-manifest.json"
DEFAULT_MAX_TWEETS = 20
DEFAULT_NITTER_BASE = "https://nitter.net"
USER_AGENT = "Review-Generator-NitterCrawl/1.0"
REQUEST_TIMEOUT_SEC = 30

STATUS_PATH_RE = re.compile(r"/status/\d+", re.I)


def nitter_handle_from_url(url: str) -> str | None:
    """Extract profile handle; None for status/search URLs."""
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host != "nitter.net":
            return None
        path = parsed.path.strip("/")
        if not path or STATUS_PATH_RE.search(f"/{path}"):
            return None
        if path.startswith("search") or path.startswith("hashtag"):
            return None
        parts = path.split("/")
        handle = parts[0].lstrip("@")
        if not handle or handle.lower() in ("i", "intent", "share"):
            return None
        return handle
    except Exception:
        return None


def collect_nitter_sources(ipnft_dir: Any) -> tuple[list[str], list[str]]:
    links = collect_urls_from_metadata(ipnft_dir, is_nitter_url)
    handles: list[str] = []
    urls: list[str] = []
    seen_h: set[str] = set()
    for item in links:
        url = item["url"]
        handle = nitter_handle_from_url(url)
        if not handle:
            continue
        norm = handle.lower()
        if norm in seen_h:
            continue
        seen_h.add(norm)
        handles.append(handle)
        urls.append(url.split("#")[0].rstrip("/") or f"{DEFAULT_NITTER_BASE}/{handle}")
    return handles, urls


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return unescape(re.sub(r"<[^>]+>", " ", text)).strip()


def _handle_from_href(href: str) -> str:
    m = re.search(r"nitter\.net/(@?)([^/]+)", href, re.I)
    if m:
        return m.group(2).lstrip("@")
    return ""


def parse_rss_tweets(xml_text: str, base: str, max_tweets: int) -> list[dict[str, Any]]:
    tweets: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return tweets

    channel = root.find("channel")
    items = list(root.findall(".//item"))
    if channel is not None:
        items = list(channel.findall("item")) or items

    for item in items[:max_tweets]:
        title = (item.findtext("title") or "").strip()
        desc = _strip_html(item.findtext("description") or "")
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or item.findtext("{http://www.w3.org/2005/Atom}published") or "").strip()
        guid = (item.findtext("guid") or link or "").strip()
        tweet_id = None
        m = re.search(r"/status/(\d+)", link or guid)
        if m:
            tweet_id = m.group(1)
        text = desc or title
        if not text and not link:
            continue
        handle = _handle_from_href(link or guid)
        tweets.append({
            "id": tweet_id,
            "url": link or None,
            "publishedAt": pub or None,
            "text": text,
            "author": handle or None,
            "stats": {"replies": 0, "retweets": 0, "likes": 0, "quotes": 0},
            "media": [],
        })
    return tweets


def parse_html_tweets(html: str, handle: str, base: str, max_tweets: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    tweets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in soup.select(".timeline-item, .tweet-body, article.tweet"):
        if len(tweets) >= max_tweets:
            break
        link_el = item.select_one("a.tweet-link, a[href*='/status/']")
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = f"{base.rstrip('/')}{href}"
        m = re.search(r"/status/(\d+)", href)
        tweet_id = m.group(1) if m else None
        if tweet_id and tweet_id in seen_ids:
            continue
        if tweet_id:
            seen_ids.add(tweet_id)

        content = item.select_one(".tweet-content, .tweet-body")
        text = content.get_text(" ", strip=True) if content else item.get_text(" ", strip=True)
        if not text:
            continue

        date_el = item.select_one(".tweet-date a, time")
        pub = None
        if date_el:
            pub = date_el.get("title") or date_el.get("datetime") or date_el.get_text(strip=True)

        stats = {"replies": 0, "retweets": 0, "likes": 0, "quotes": 0}
        for icon, key in (
            (".icon-comment", "replies"),
            (".icon-retweet", "retweets"),
            (".icon-heart", "likes"),
            (".icon-quote", "quotes"),
        ):
            el = item.select_one(icon)
            if el and el.parent:
                num_m = re.search(r"\d+", el.parent.get_text())
                if num_m:
                    stats[key] = int(num_m.group())

        media: list[dict[str, str]] = []
        for img in item.select(".attachment.image img, .still-image img"):
            src = img.get("src") or ""
            if src:
                if not src.startswith("http"):
                    src = f"{base.rstrip('/')}{src}"
                media.append({"type": "image", "url": src})

        tweets.append({
            "id": tweet_id,
            "url": href or f"{base}/{handle}/status/{tweet_id}" if tweet_id else None,
            "publishedAt": pub,
            "text": text,
            "author": handle,
            "stats": stats,
            "media": media,
        })
    return tweets


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[int, str]:
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC),
            allow_redirects=True,
        ) as resp:
            body = await resp.text(errors="replace")
            return resp.status, body
    except Exception:
        return 0, ""


async def fetch_handle_tweets(
    session: aiohttp.ClientSession,
    handle: str,
    bases: list[str],
    max_tweets: int,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Returns (tweets, status, error). status: success | rss_failed | html_failed."""
    for base in bases:
        base = base.rstrip("/")
        rss_url = f"{base}/{handle}/rss"
        status, body = await fetch_text(session, rss_url)
        if status == 200 and body.strip():
            tweets = parse_rss_tweets(body, base, max_tweets)
            if tweets:
                return tweets, "success", None

        profile_url = f"{base}/{handle}"
        status, body = await fetch_text(session, profile_url)
        if status == 200 and body.strip():
            tweets = parse_html_tweets(body, handle, base, max_tweets)
            if tweets:
                return tweets, "success", None

    return [], "html_failed", "rss and html returned no tweets"


def should_skip_folder(
    out_dir: Path,
    handles: list[str],
    force: bool,
) -> bool:
    if force or not handles:
        return False
    manifest = load_manifest(out_dir, MANIFEST_FILENAME)
    if not manifest or manifest.get("status") != "success":
        return False
    prev = sorted(manifest.get("handles") or [])
    return prev == sorted(handles)


async def crawl_ipnft_nitter(
    ipnft_dir: Path,
    *,
    bases: list[str],
    max_tweets: int,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    folder = ipnft_dir.name
    ensure_layout(ipnft_dir)
    meta = metadata_dir(ipnft_dir)
    content = output_dir(ipnft_dir)
    handles, source_urls = collect_nitter_sources(ipnft_dir)

    if not handles:
        entry = {
            "ipnftFolder": folder,
            "status": "no_links",
            "handles": [],
            "sourceUrls": [],
            "generatedAt": utc_now(),
        }
        if not dry_run:
            write_manifest(meta, MANIFEST_FILENAME, entry)
        return entry

    if should_skip_folder(meta, handles, force):
        print(f"  [{folder}] skip nitter (unchanged handles)", flush=True)
        return {"ipnftFolder": folder, "skipped": True, "reason": "unchanged"}

    if dry_run:
        return {
            "ipnftFolder": folder,
            "dry_run": True,
            "handles": handles,
            "sourceUrls": source_urls,
        }

    all_tweets: list[dict[str, Any]] = []
    handle_results: list[dict[str, Any]] = []

    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
    ) as session:
        for handle in handles:
            print(f"  [{folder}] nitter @{handle}", flush=True)
            tweets, status, err = await fetch_handle_tweets(
                session, handle, bases, max_tweets,
            )
            handle_results.append({
                "handle": handle,
                "status": status,
                "tweetCount": len(tweets),
                "error": err,
            })
            for t in tweets:
                if len(all_tweets) >= max_tweets:
                    break
                all_tweets.append(t)

    all_tweets = all_tweets[:max_tweets]
    overall = "success" if all_tweets else (
        "no_links" if not handles else handle_results[0]["status"]
    )

    manifest = {
        "generatedAt": utc_now(),
        "ipnftFolder": folder,
        "status": overall,
        "handles": handles,
        "sourceUrls": source_urls,
        "basesTried": bases,
        "handleResults": handle_results,
        "tweetCount": len(all_tweets),
    }
    write_manifest(meta, MANIFEST_FILENAME, manifest)

    if all_tweets:
        tweets_doc = {
            "generatedAt": utc_now(),
            "handles": handles,
            "sourceUrls": source_urls,
            "tweetCount": len(all_tweets),
            "tweets": all_tweets,
        }
        (content / TWEETS_FILENAME).write_text(
            json.dumps(tweets_doc, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        f"  [{folder}] nitter done: {len(all_tweets)} tweet(s), status={overall}",
        flush=True,
    )
    return manifest


async def run_all(args: argparse.Namespace) -> int:
    ipnfts_dir = args.ipnfts_dir.resolve()
    if not ipnfts_dir.is_dir():
        print(f"Error: not a directory: {ipnfts_dir}", file=sys.stderr)
        return 1

    skip = load_crawl_skip_folders(args.crawl_skip_file)
    try:
        folders = iter_ipnft_folders(
            ipnfts_dir,
            folder=args.folder,
            max_folders=args.max,
            skip_folders=skip,
            require_links=False,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not folders:
        print("No IPNFT folders with JSON sources to process.", file=sys.stderr)
        return 0

    bases = [args.nitter_base.rstrip("/")]
    if args.nitter_fallback_bases:
        for b in args.nitter_fallback_bases.split(","):
            b = b.strip().rstrip("/")
            if b and b not in bases:
                bases.append(b)

    print(
        f"Nitter crawl: {len(folders)} folder(s), concurrency={args.concurrency}",
        flush=True,
    )

    sem = asyncio.Semaphore(args.concurrency)

    async def bounded(folder: Any) -> None:
        async with sem:
            await crawl_ipnft_nitter(
                folder,
                bases=bases,
                max_tweets=args.max_tweets,
                force=args.force,
                dry_run=args.dry_run,
            )

    results = await asyncio.gather(*(bounded(f) for f in folders), return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    for err in failures:
        print(f"Error: {err}", file=sys.stderr)
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from crawl_common import add_base_crawl_args

    p = argparse.ArgumentParser(description="Fetch nitter timelines into tweets.json")
    add_base_crawl_args(p)
    p.add_argument("--max-tweets", type=int, default=DEFAULT_MAX_TWEETS)
    p.add_argument("--nitter-base", type=str, default=DEFAULT_NITTER_BASE)
    p.add_argument(
        "--nitter-fallback-bases",
        type=str,
        default="",
        help="Comma-separated alternate Nitter instances",
    )
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run_all(args)))


if __name__ == "__main__":
    main()
