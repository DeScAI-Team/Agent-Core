from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from uploader.recipes import Recipe

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CRAWL_LOG_PATH = _REPO_ROOT / "crawlers" / "output" / "crawl-log.json"
MOLECULE_IPNFTS_DIR = _REPO_ROOT / "crawlers" / "output" / "molecule" / "ipnfts"
PAPERS_DIR = _REPO_ROOT / "crawlers" / "output" / "researchhub" / "papers"

REVIEW_RECIPES = frozenset({"article", "proposal", "dao", "compounds"})
REVIEWED_VALUE = "reviewed"

ARWEAVE_GQL = os.environ.get("ARWEAVE_GRAPHQL_URL", "https://arweave.net/graphql")

GQL_CRAWL_LOG = """
query CrawlLogTxs($owners: [String!]!) {
  transactions(
    owners: $owners
    tags: [{ name: "doctype", values: ["crawllog"] }]
    first: 50
  ) {
    edges {
      node {
        id
        block {
          timestamp
        }
      }
    }
  }
}
"""

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


@dataclass(frozen=True)
class CrawlLogMark:
    section: str
    key: str


def _safe_stem(stem: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._-") or "document"
    if len(s) > 120:
        s = s[:120]
    if s.upper() in _WIN_RESERVED:
        s = f"_{s}_"
    return s


def _article_pdf_stem(pdf_url: str) -> str:
    return _safe_stem(Path(urlparse(pdf_url).path).stem or "document")


def load_crawl_log(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected crawl-log object in {path}")
    return data


def save_crawl_log(path: Path, data: dict[str, Any]) -> None:
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _entry_key(section: str, entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry.strip() or None
    if not isinstance(entry, dict):
        return None
    if section == "researchhub":
        return str(entry.get("path", "")).strip() or None
    if section == "molecule":
        return str(entry.get("name", "")).strip() or None
    if section == "pumpScience":
        return str(entry.get("ticker", "")).strip() or None
    return None


def _is_reviewed(entry: Any) -> bool:
    if isinstance(entry, str):
        return False
    if not isinstance(entry, dict):
        return False
    return entry.get("reviewed") == REVIEWED_VALUE


def _make_entry(section: str, key: str, reviewed: bool = False) -> dict[str, Any]:
    if section == "researchhub":
        entry: dict[str, Any] = {"path": key}
    elif section == "molecule":
        entry = {"name": key}
    elif section == "pumpScience":
        entry = {"ticker": key}
    else:
        raise ValueError(f"Unknown section {section!r}")
    if reviewed:
        entry["reviewed"] = REVIEWED_VALUE
    return entry


def _normalize_entries(section: str, entries: list[Any]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for raw in entries:
        key = _entry_key(section, raw)
        if not key:
            continue
        reviewed = _is_reviewed(raw)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = _make_entry(section, key, reviewed=reviewed)
        elif reviewed:
            existing["reviewed"] = REVIEWED_VALUE
    return [by_key[k] for k in sorted(by_key)]


def mark_reviewed(data: dict[str, Any], *, section: str, key: str) -> bool:
    """Set reviewed on matching entry; append if missing. Returns True if changed."""
    key = key.strip()
    if not key:
        return False

    if section == "researchhub":
        block_key = "files"
        parent_key = "researchhub"
    elif section == "molecule":
        block_key = "folders"
        parent_key = "molecule"
    elif section == "pumpScience":
        block_key = "tickers"
        parent_key = "pumpScience"
    else:
        raise ValueError(f"Unknown section {section!r}")

    parent = data.setdefault(parent_key, {})
    entries = parent.setdefault(block_key, [])
    if not isinstance(entries, list):
        entries = []
        parent[block_key] = entries

    normalized = _normalize_entries(section, entries)
    found = False
    for entry in normalized:
        if _entry_key(section, entry) == key:
            entry["reviewed"] = REVIEWED_VALUE
            found = True
            break

    if not found:
        normalized.append(_make_entry(section, key, reviewed=True))
        normalized.sort(key=lambda e: _entry_key(section, e) or "")

    parent[block_key] = normalized
    data["version"] = 2
    return True


def _resolve_dao_folder(tag_ctx: dict[str, Any], input_dir: Path) -> str | None:
    dao_name = (
        tag_ctx.get("dao_name")
        or tag_ctx.get("research_name")
        or ""
    ).strip()
    if dao_name and MOLECULE_IPNFTS_DIR.is_dir():
        for folder in sorted(MOLECULE_IPNFTS_DIR.iterdir()):
            if not folder.is_dir():
                continue
            if folder.name == dao_name:
                return folder.name
            for profile_path in (
                folder / "profile.json",
                folder / "metadata" / "profile.json",
            ):
                if not profile_path.is_file():
                    continue
                try:
                    profile = json.loads(profile_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                symbol = profile.get("symbol") or (profile.get("ipnft") or {}).get(
                    "initialSymbol"
                )
                if symbol and str(symbol).strip() == dao_name:
                    return folder.name

    fallback = input_dir.parent.name.strip()
    return fallback or None


def _resolve_article_paper_path(input_dir: Path) -> str | None:
    stem = input_dir.parent.name.strip()
    if not stem or not PAPERS_DIR.is_dir():
        return None
    for paper_path in sorted(PAPERS_DIR.glob("PaperRecord_*.json")):
        try:
            data = json.loads(paper_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pdf_url = data.get("pdf_url")
        if not pdf_url:
            continue
        if _article_pdf_stem(pdf_url) == stem:
            return f"papers/{paper_path.name}"
    return None


def resolve_crawl_log_mark(
    recipe: Recipe,
    input_dir: Path,
    tag_ctx: dict[str, Any],
) -> CrawlLogMark | None:
    input_dir = input_dir.resolve()
    name = recipe.name

    if name == "proposal":
        proposal_dir = input_dir.parent.name
        if not proposal_dir.startswith("proposal_"):
            return None
        return CrawlLogMark("researchhub", f"proposals/{proposal_dir}.json")

    if name == "compounds":
        ticker = (tag_ctx.get("compound_name") or input_dir.parent.name).strip()
        return CrawlLogMark("pumpScience", ticker) if ticker else None

    if name == "dao":
        folder = _resolve_dao_folder(tag_ctx, input_dir)
        return CrawlLogMark("molecule", folder) if folder else None

    if name == "article":
        path = _resolve_article_paper_path(input_dir)
        return CrawlLogMark("researchhub", path) if path else None

    return None


def apply_reviewed_mark(
    recipe: Recipe,
    input_dir: Path,
    tag_ctx: dict[str, Any],
    crawl_log_path: Path | None = None,
) -> bool:
    """Mark crawl-log entry reviewed after successful review upload. Returns True if marked."""
    if recipe.name not in REVIEW_RECIPES:
        return False

    path = (crawl_log_path or DEFAULT_CRAWL_LOG_PATH).resolve()
    mark = resolve_crawl_log_mark(recipe, input_dir, tag_ctx)
    if mark is None:
        print(
            f"\n  Warning: could not resolve crawl-log entry for {recipe.name} upload",
            file=sys.stderr,
        )
        return False

    if not path.is_file():
        print(
            f"\n  Warning: crawl-log not found at {path}; skipping reviewed mark",
            file=sys.stderr,
        )
        return False

    data = load_crawl_log(path)
    mark_reviewed(data, section=mark.section, key=mark.key)
    save_crawl_log(path, data)
    print(f"\n  Crawl-log: marked reviewed — {mark.section} {mark.key!r}")
    return True


def fetch_latest_crawl_log(agent_wallet: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return (log, tx_id) for newest crawllog tx owned by agent_wallet."""
    body = json.dumps(
        {"query": GQL_CRAWL_LOG, "variables": {"owners": [agent_wallet]}},
    ).encode("utf-8")
    req = urllib.request.Request(
        ARWEAVE_GQL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None, None

    if parsed.get("errors"):
        return None, None
    edges = parsed.get("data", {}).get("transactions", {}).get("edges", [])
    if not edges:
        return None, None

    best_id: str | None = None
    best_ts = -1
    for edge in edges:
        node = edge.get("node") or {}
        tx_id = node.get("id")
        if not tx_id:
            continue
        ts = (node.get("block") or {}).get("timestamp") or 0
        if ts >= best_ts:
            best_ts = ts
            best_id = tx_id

    if not best_id:
        best_id = edges[0].get("node", {}).get("id")
    if not best_id:
        return None, None

    try:
        with urllib.request.urlopen(f"https://arweave.net/{best_id}", timeout=60) as resp:
            log = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None, None

    if not isinstance(log, dict):
        return None, None
    if not isinstance(log.get("researchhub", {}).get("files"), list):
        return None, None
    if not isinstance(log.get("molecule", {}).get("folders"), list):
        return None, None
    return log, best_id


def _load_root_env() -> None:
    env_path = _REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def sync_crawl_log_from_chain(
    path: Path | None = None,
    *,
    agent_wallet: str | None = None,
) -> bool:
    """Fetch latest on-chain crawl-log and write local file. Returns True if synced."""
    _load_root_env()
    wallet = (agent_wallet or os.environ.get("AGENT_WALLET", "")).strip()
    if not wallet:
        print("\n  Warning: AGENT_WALLET unset; skipping crawl-log sync", file=sys.stderr)
        return False

    log, tx_id = fetch_latest_crawl_log(wallet)
    if log is None:
        print("\n  Warning: no crawl-log on chain to sync", file=sys.stderr)
        return False

    out = (path or DEFAULT_CRAWL_LOG_PATH).resolve()
    log = dict(log)
    log["version"] = 2
    log["previousArweaveTxId"] = tx_id
    if "researchhub" in log:
        log["researchhub"]["files"] = _normalize_entries(
            "researchhub", log["researchhub"].get("files", []),
        )
    if "molecule" in log:
        log["molecule"]["folders"] = _normalize_entries(
            "molecule", log["molecule"].get("folders", []),
        )
    if "pumpScience" in log:
        log["pumpScience"]["tickers"] = _normalize_entries(
            "pumpScience", log["pumpScience"].get("tickers", []),
        )
    save_crawl_log(out, log)
    print(f"\n  Synced crawl-log from tx {tx_id} → {out}")
    return True
