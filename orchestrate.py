#!/usr/bin/env python3
"""
Top-level on-chain agent orchestrator.

Step 1: Crawl (node crawlers/full-crawl.mjs, uploads crawl-log to Arweave)
Steps 2–5: Review every item under crawlers/output (articles, DAOs, proposals, compounds)
Per item: run the route pipeline, then upload the review bundle (python -m uploader --resume)
Step 6: Snapshot crawlers/output + reviews/ (python -m snapshotter → snapshot.tar.zst + R2)

Crawl output: crawlers/output/
Review output: reviews/{articles,DAOs,proposals,compounds}/

Usage:
  python orchestrate.py                   # same as --all (default)
  python orchestrate.py --all             # crawl, review all items, upload each
  python orchestrate.py --test            # crawl, one sample per route, upload each
  python orchestrate.py --dry-run         # print commands only
  python orchestrate.py --skip-crawl      # use existing crawlers/output
  python orchestrate.py --skip-upload     # pipelines only (crawl-log still uploads on crawl)
  python orchestrate.py --skip-snapshot   # skip step 6 R2 snapshot
  python orchestrate.py --just-snapshot   # only step 6 (verify snapshotter)
  python orchestrate.py --no-vis          # dev: skip vision/LLM in pipelines

Exit 1 if crawl fails or any pipeline/upload/snapshot step failed.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

PDF_HEAD_TIMEOUT_SEC = 30

REPO_ROOT = Path(__file__).resolve().parent

CRAWL_ROOT = REPO_ROOT / "crawlers" / "output"
PAPERS_DIR = CRAWL_ROOT / "researchhub" / "papers"
PROPOSALS_DIR = CRAWL_ROOT / "researchhub" / "proposals"
MOLECULE_INPUT_DIR = CRAWL_ROOT / "molecule" / "ipnfts"
COMPOUND_TOKENS_FILE = CRAWL_ROOT / "pump.science" / "compound-tokens.json"

ARTICLE_OUTPUT_DIR = REPO_ROOT / "reviews" / "articles"
DAO_OUTPUT_DIR = REPO_ROOT / "reviews" / "DAOs"
PROPOSAL_OUTPUT_DIR = REPO_ROOT / "reviews" / "proposals"
COMPOUND_OUTPUT_DIR = REPO_ROOT / "reviews" / "compounds"

PIPELINE_SCRIPT = REPO_ROOT / "articles" / "pipeline" / "run_full_pipeline.py"
CRAWL_SCRIPT = REPO_ROOT / "crawlers" / "full-crawl.mjs"
DAO_PIPELINE_SCRIPT = REPO_ROOT / "DAOs" / "molecule" / "pipeline" / "run_dao_review.py"
PROPOSAL_PIPELINE_SCRIPT = REPO_ROOT / "proposals" / "pipeline" / "proposal_pipe.py"
COMPOUND_ORCHESTRATOR = REPO_ROOT / "compounds" / "orchestrate.py"

_ARTICLES_DIR = REPO_ROOT / "articles"
if str(_ARTICLES_DIR) not in sys.path:
    sys.path.insert(0, str(_ARTICLES_DIR))
from pipeline.run_layout import find_run_dir, review_dir_for_run, run_dir_for_stem, safe_stem  # noqa: E402

PY = sys.executable


def _banner(msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}\n", flush=True)


def _print_summary(title: str, results: list[tuple[str, bool, str]]) -> int:
    """Print pass/fail table; return failure count."""
    _banner(title)
    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - ok_count
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}  {detail}")
    print(f"\n  Total: {len(results)}  |  passed: {ok_count}  |  failed: {fail_count}")
    if fail_count:
        print("  Some items failed. See logs above.", file=sys.stderr)
    return fail_count


# ── Publish / upload helpers ─────────────────────────────────

def is_publishable_bundle(review_dir: Path) -> bool:
    """True when review_dir has evidence_audit.md, overview.json, and a review JSON."""
    if not review_dir.is_dir():
        return False
    if not (review_dir / "evidence_audit.md").is_file():
        return False
    if not (review_dir / "overview.json").is_file():
        return False
    if (review_dir / "review.json").is_file():
        return True
    return bool(list(review_dir.glob("*-review.json")))


def upload_bundle(
    recipe: str,
    review_dir: Path,
    *,
    dry_run: bool,
    skip_upload: bool,
) -> bool:
    """Upload review bundle; return True on success or skip."""
    if skip_upload:
        return True

    cmd = [
        PY,
        "-m",
        "uploader",
        "--recipe",
        recipe,
        "--dir",
        str(review_dir),
        "--resume",
    ]
    print(f"  upload: {' '.join(cmd)}")

    if dry_run:
        print("  [dry-run] upload skipped")
        return True

    if not is_publishable_bundle(review_dir):
        print(f"  ! upload skipped — incomplete bundle: {review_dir}", file=sys.stderr)
        return False

    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        print(f"  upload FAILED (exit {result.returncode})", file=sys.stderr)
        return False
    print("  upload OK")
    return True


def _article_pdf_stem(pdf_url: str) -> str:
    """Match run_full_pipeline.py fetch stem (raw urlparse path, no unquote)."""
    return safe_stem(Path(urlparse(pdf_url).path).stem or "document")


def resolve_article_review_dir(pdf_url: str) -> Path | None:
    """Locate reviews/articles/<stem>/review after run_full_pipeline."""
    stem = _article_pdf_stem(pdf_url)
    run_dir = find_run_dir(ARTICLE_OUTPUT_DIR, stem)
    if run_dir is None:
        candidate = run_dir_for_stem(ARTICLE_OUTPUT_DIR, stem)
        if candidate.is_dir():
            run_dir = candidate
    if run_dir is None:
        return None
    review_dir = review_dir_for_run(run_dir)
    return review_dir if review_dir.is_dir() else None


def expected_article_review_dir(pdf_url: str) -> Path:
    """Predicted upload path from PDF URL (used for dry-run)."""
    return review_dir_for_run(run_dir_for_stem(ARTICLE_OUTPUT_DIR, _article_pdf_stem(pdf_url)))


def _dao_symbol(ipnft_dir: Path) -> str:
    for profile_path in (ipnft_dir / "profile.json", ipnft_dir / "metadata" / "profile.json"):
        if not profile_path.is_file():
            continue
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        symbol = profile.get("symbol") or (profile.get("ipnft") or {}).get("initialSymbol")
        if symbol:
            return str(symbol).strip()
    return ipnft_dir.name


def compound_review_dir(ticker: str) -> Path:
    return COMPOUND_OUTPUT_DIR / ticker / "review"


# ── Test-mode sampling (one item per route) ───────────────────

def _pdf_content_length(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=PDF_HEAD_TIMEOUT_SEC) as resp:
            length = resp.headers.get("Content-Length")
            if length is not None:
                return int(length)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        pass
    return None


def pick_test_paper() -> tuple[Path, str]:
    """Smallest PDF (by Content-Length) among papers with pdf_url."""
    rows: list[tuple[Path, str, int]] = []
    for path in sorted(PAPERS_DIR.glob("PaperRecord_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skipping {path.name}: {exc}")
            continue
        url = data.get("pdf_url")
        if not url:
            continue
        size = _pdf_content_length(url)
        if size is None:
            abstract_len = len((data.get("abstract") or "").strip())
            size = abstract_len if abstract_len > 0 else 10**12
        rows.append((path, url, size))
    if not rows:
        raise RuntimeError(f"No papers with pdf_url under {PAPERS_DIR}")
    path, url, _ = min(rows, key=lambda row: (row[2], row[0].name.lower()))
    return path, url


def pick_test_proposal() -> Path:
    rows: list[tuple[Path, int]] = []
    for path in sorted(PROPOSALS_DIR.glob("proposal_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            body = data.get("proposal-body") or {}
            if isinstance(body, dict):
                length = len((body.get("text") or "").strip())
            else:
                length = len(str(body).strip())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skipping {path.name}: {exc}")
            continue
        if length > 0:
            rows.append((path, length))
    if not rows:
        raise RuntimeError(f"No proposals with text under {PROPOSALS_DIR}")
    return min(rows, key=lambda row: (row[1], row[0].name.lower()))[0]


def pick_test_ipnft(*, seed: int | None) -> Path:
    dirs = _collect_ipnft_dirs()
    if not dirs:
        raise RuntimeError(f"No IPNFT folders with profile.json under {MOLECULE_INPUT_DIR}")
    return random.Random(seed).choice(dirs)


def pick_test_compound() -> tuple[str, list[str]]:
    entries = _collect_compound_tokens()
    if not entries:
        raise RuntimeError(f"No compound tokens under {COMPOUND_TOKENS_FILE}")
    singles = [row for row in entries if len(row[1]) == 1]
    pool = singles if singles else entries
    return min(
        pool,
        key=lambda row: (len(row[1]), sum(len(n) for n in row[1]), row[0].lower()),
    )


# ── Step 1: Crawl ────────────────────────────────────────────

def run_crawl(*, dry_run: bool) -> None:
    _banner("Step 1 — Crawl (full-crawl.mjs)")
    cmd = ["node", str(CRAWL_SCRIPT)]
    print(f"  cmd: {' '.join(cmd)}")

    if dry_run:
        print("  [dry-run] skipped")
        return

    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        print(f"\n  FAILED (exit {result.returncode}). Stopping.", file=sys.stderr)
        sys.exit(result.returncode)
    print("  OK")


# ── Step 2: Article reviews ──────────────────────────────────

def _collect_papers() -> list[tuple[Path, str]]:
    """Return [(json_path, pdf_url), ...] for every paper with a usable pdf_url."""
    if not PAPERS_DIR.is_dir():
        return []
    pairs: list[tuple[Path, str]] = []
    for p in sorted(PAPERS_DIR.glob("PaperRecord_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skipping {p.name}: {exc}")
            continue
        url = data.get("pdf_url")
        if not url:
            print(f"  ! skipping {p.name}: no pdf_url")
            continue
        pairs.append((p, url))
    return pairs


def run_article_reviews(
    *,
    dry_run: bool,
    no_vis: bool,
    skip_upload: bool,
    test_mode: bool,
) -> int:
    _banner("Step 2 — Article reviews")

    if test_mode:
        try:
            papers = [pick_test_paper()]
        except RuntimeError as exc:
            print(f"  {exc}")
            return 1
        print(f"  Test mode: 1 paper — {papers[0][0].name}\n")
    else:
        papers = _collect_papers()
        if not papers:
            print("  No papers found in", PAPERS_DIR)
            return 0
        print(f"  Found {len(papers)} paper(s) with pdf_url\n")
    ARTICLE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, bool, str]] = []

    for i, (json_path, pdf_url) in enumerate(papers, 1):
        label = json_path.stem
        _banner(f"[{i}/{len(papers)}] {label}")

        cmd = [
            PY,
            str(PIPELINE_SCRIPT),
            pdf_url,
            "--output-dir",
            str(ARTICLE_OUTPUT_DIR),
        ]
        if no_vis:
            cmd += ["--stop-after", "add_data"]

        print(f"  cmd: {' '.join(cmd)}")

        if dry_run:
            upload_bundle(
                "article",
                expected_article_review_dir(pdf_url),
                dry_run=True,
                skip_upload=skip_upload,
            )
            results.append((label, True, "dry-run"))
            continue

        t0 = time.monotonic()
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            detail = f"pipeline exit {result.returncode} ({elapsed:.0f}s)"
            print(f"  FAILED — {detail}")
            results.append((label, False, detail))
            continue

        review_dir = resolve_article_review_dir(pdf_url)
        if review_dir is None:
            detail = f"pipeline ok but review dir not found ({elapsed:.0f}s)"
            print(f"  FAILED — {detail}", file=sys.stderr)
            results.append((label, False, detail))
            continue

        upload_ok = upload_bundle(
            "article", review_dir, dry_run=False, skip_upload=skip_upload
        )
        if upload_ok:
            results.append((label, True, f"ok ({elapsed:.0f}s)"))
        else:
            results.append((label, False, f"upload failed ({elapsed:.0f}s)"))

    return _print_summary("Article review summary", results)


# ── Step 3: DAO reviews ──────────────────────────────────────

def _ipnft_has_profile(ipnft_dir: Path) -> bool:
    """Molecule crawler uses metadata/profile.json; legacy layout uses root profile.json."""
    return (ipnft_dir / "profile.json").is_file() or (
        ipnft_dir / "metadata" / "profile.json"
    ).is_file()


def _collect_ipnft_dirs() -> list[Path]:
    if not MOLECULE_INPUT_DIR.is_dir():
        return []
    return sorted(
        d for d in MOLECULE_INPUT_DIR.iterdir()
        if d.is_dir() and _ipnft_has_profile(d)
    )


def run_dao_reviews(
    *,
    dry_run: bool,
    no_vis: bool,
    skip_upload: bool,
    test_mode: bool,
    test_seed: int | None,
) -> int:
    _banner("Step 3 — DAO reviews (Molecule IP-NFTs)")

    if test_mode:
        try:
            ipnft_dirs = [pick_test_ipnft(seed=test_seed)]
        except RuntimeError as exc:
            print(f"  {exc}")
            return 1
        print(f"  Test mode: 1 IPNFT — {ipnft_dirs[0].name}\n")
    else:
        ipnft_dirs = _collect_ipnft_dirs()
        if not ipnft_dirs:
            print("  No IPNFT folders found in", MOLECULE_INPUT_DIR)
            return 0
        print(f"  Found {len(ipnft_dirs)} IPNFT(s)\n")
    DAO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, bool, str]] = []

    for i, ipnft_dir in enumerate(ipnft_dirs, 1):
        label = ipnft_dir.name
        symbol = _dao_symbol(ipnft_dir)
        review_dir = DAO_OUTPUT_DIR / symbol / "review"
        _banner(f"[{i}/{len(ipnft_dirs)}] {label} ({symbol})")

        cmd = [PY, str(DAO_PIPELINE_SCRIPT), "--ipnft-dir", str(ipnft_dir)]
        if no_vis:
            cmd.append("--skip-vision")

        print(f"  cmd: {' '.join(cmd)}")

        if dry_run:
            upload_bundle("dao", review_dir, dry_run=True, skip_upload=skip_upload)
            results.append((label, True, "dry-run"))
            continue

        t0 = time.monotonic()
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            detail = f"pipeline exit {result.returncode} ({elapsed:.0f}s)"
            print(f"  FAILED — {detail}")
            results.append((label, False, detail))
            continue

        if not review_dir.is_dir():
            review_dir = DAO_OUTPUT_DIR / label / "review"

        upload_ok = upload_bundle("dao", review_dir, dry_run=False, skip_upload=skip_upload)
        if upload_ok:
            results.append((label, True, f"ok ({elapsed:.0f}s)"))
        else:
            results.append((label, False, f"upload failed ({elapsed:.0f}s)"))

    return _print_summary("DAO review summary", results)


# ── Step 4: Proposal reviews ─────────────────────────────────

def _collect_proposals() -> list[Path]:
    if not PROPOSALS_DIR.is_dir():
        return []
    return sorted(PROPOSALS_DIR.glob("proposal_*.json"))


def run_proposal_reviews(
    *,
    dry_run: bool,
    no_vis: bool,
    skip_upload: bool,
    test_mode: bool,
) -> int:
    _banner("Step 4 — Proposal reviews")

    if test_mode:
        try:
            proposals = [pick_test_proposal()]
        except RuntimeError as exc:
            print(f"  {exc}")
            return 1
        print(f"  Test mode: 1 proposal — {proposals[0].name}\n")
    else:
        proposals = _collect_proposals()
        if not proposals:
            print("  No proposals found in", PROPOSALS_DIR)
            return 0
        print(f"  Found {len(proposals)} proposal(s)\n")
    PROPOSAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, bool, str]] = []

    for i, json_path in enumerate(proposals, 1):
        label = json_path.stem
        proposal_out = PROPOSAL_OUTPUT_DIR / label
        review_dir = proposal_out / "review"
        _banner(f"[{i}/{len(proposals)}] {label}")

        cmd = [
            PY,
            str(PROPOSAL_PIPELINE_SCRIPT),
            "--input-json",
            str(json_path),
            "--output-dir",
            str(proposal_out),
        ]
        if no_vis:
            cmd.append("--skip-llm")

        print(f"  cmd: {' '.join(cmd)}")

        if dry_run:
            upload_bundle("proposal", review_dir, dry_run=True, skip_upload=skip_upload)
            results.append((label, True, "dry-run"))
            continue

        t0 = time.monotonic()
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            detail = f"pipeline exit {result.returncode} ({elapsed:.0f}s)"
            print(f"  FAILED — {detail}")
            results.append((label, False, detail))
            continue

        upload_ok = upload_bundle(
            "proposal", review_dir, dry_run=False, skip_upload=skip_upload
        )
        if upload_ok:
            results.append((label, True, f"ok ({elapsed:.0f}s)"))
        else:
            results.append((label, False, f"upload failed ({elapsed:.0f}s)"))

    return _print_summary("Proposal review summary", results)


# ── Step 5: Compound reviews ─────────────────────────────────

def _parse_intervention(intervention: str) -> list[str]:
    if re.search(r"compound\s+\d+\s*:", intervention, re.IGNORECASE):
        parts = re.split(r";\s*", intervention)
        names = []
        for part in parts:
            m = re.match(r"compound\s+\d+\s*:\s*(.+)", part.strip(), re.IGNORECASE)
            if m:
                names.append(m.group(1).strip())
        return names
    return [intervention.strip()] if intervention.strip() else []


def _collect_compound_tokens() -> list[tuple[str, list[str]]]:
    if not COMPOUND_TOKENS_FILE.is_file():
        return []
    try:
        tokens = json.loads(COMPOUND_TOKENS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! failed to read {COMPOUND_TOKENS_FILE.name}: {exc}")
        return []
    entries: list[tuple[str, list[str]]] = []
    for t in tokens:
        ticker = str(t.get("ticker", "")).strip()
        intervention = t.get("intervention", "")
        names = _parse_intervention(str(intervention))
        if names and ticker:
            entries.append((ticker, names))
        elif ticker:
            print(f"  ! skipping {ticker}: empty intervention")
    return entries


def run_compound_reviews(
    *,
    dry_run: bool,
    no_vis: bool,
    skip_upload: bool,
    test_mode: bool,
) -> int:
    _banner("Step 5 — Compound reviews (pump.science)")

    if no_vis:
        print("  Note: --no-vis has no effect on compound pipeline", flush=True)

    if test_mode:
        try:
            tokens = [pick_test_compound()]
        except RuntimeError as exc:
            print(f"  {exc}")
            return 1
        ticker, compounds = tokens[0]
        print(f"  Test mode: 1 token — {ticker} ({', '.join(compounds)})\n")
    else:
        tokens = _collect_compound_tokens()
        if not tokens:
            print("  No compound tokens found in", COMPOUND_TOKENS_FILE)
            return 0
        print(f"  Found {len(tokens)} token(s)\n")
    results: list[tuple[str, bool, str]] = []

    for i, (ticker, compounds) in enumerate(tokens, 1):
        label = f"{ticker} ({', '.join(compounds)})"
        review_dir = compound_review_dir(ticker)
        _banner(f"[{i}/{len(tokens)}] {label}")

        cmd = [PY, str(COMPOUND_ORCHESTRATOR), "--compounds", *compounds]
        print(f"  cmd: {' '.join(cmd)}")

        if dry_run:
            upload_bundle("compounds", review_dir, dry_run=True, skip_upload=skip_upload)
            results.append((label, True, "dry-run"))
            continue

        t0 = time.monotonic()
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            detail = f"pipeline exit {result.returncode} ({elapsed:.0f}s)"
            print(f"  FAILED — {detail}")
            results.append((label, False, detail))
            continue

        upload_ok = upload_bundle(
            "compounds", review_dir, dry_run=False, skip_upload=skip_upload
        )
        if upload_ok:
            results.append((label, True, f"ok ({elapsed:.0f}s)"))
        else:
            results.append((label, False, f"upload failed ({elapsed:.0f}s)"))

    return _print_summary("Compound review summary", results)


# ── Step 6: Snapshot (crawlers/output + reviews → R2) ────────

def run_snapshot(*, dry_run: bool, skip_snapshot: bool) -> int:
    _banner("Step 6 — Snapshot (python -m snapshotter)")
    if skip_snapshot:
        print("  Skipped (--skip-snapshot)")
        return 0

    cmd = [PY, "-m", "snapshotter"]
    if dry_run:
        cmd.append("--dry-run")
    print(f"  cmd: {' '.join(cmd)}")

    if dry_run:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        return 0 if result.returncode == 0 else 1

    t0 = time.monotonic()
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        print(f"  FAILED (exit {result.returncode}) after {elapsed:.0f}s", file=sys.stderr)
        return 1
    print(f"  OK ({elapsed:.0f}s)")
    return 0


# ── Main ─────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "On-chain agent: crawl (crawlers/output) → review all routes → "
            "per-item Arweave upload → snapshot to R2."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--all",
        action="store_true",
        help="Crawl, review every item in crawlers/output, upload each (default)",
    )
    mode.add_argument(
        "--test",
        action="store_true",
        help="Crawl, then one sample per route (shortest paper/proposal/compound; random DAO)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for --test DAO sample selection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands but don't execute anything",
    )
    parser.add_argument(
        "--no-vis",
        action="store_true",
        help="Dev: skip vision/LLM (articles stop after add_data; DAO --skip-vision; proposals --skip-llm)",
    )
    parser.add_argument(
        "--skip-crawl",
        action="store_true",
        help="Skip crawl; process existing crawlers/output",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip per-item review uploads (crawl-log still uploads when crawl runs)",
    )
    parser.add_argument(
        "--skip-snapshot",
        action="store_true",
        help="Skip step 6 (python -m snapshotter)",
    )
    parser.add_argument(
        "--just-snapshot",
        action="store_true",
        help="Run only python -m snapshotter (skip crawl and all review routes)",
    )
    args = parser.parse_args()

    if args.just_snapshot:
        _banner("Mode: JUST SNAPSHOT")
        failures = run_snapshot(dry_run=args.dry_run, skip_snapshot=False)
        _banner("Orchestrator finished")
        if failures:
            print("  Snapshot step failed.", file=sys.stderr)
            sys.exit(1)
        print("  Snapshot completed successfully.")
        return

    test_mode = args.test
    if test_mode:
        _banner("Mode: TEST (one sample per route)")
    else:
        _banner("Mode: ALL (every item in crawlers/output)")

    failures = 0

    if not args.skip_crawl:
        run_crawl(dry_run=args.dry_run)

    failures += run_article_reviews(
        dry_run=args.dry_run,
        no_vis=args.no_vis,
        skip_upload=args.skip_upload,
        test_mode=test_mode,
    )
    failures += run_dao_reviews(
        dry_run=args.dry_run,
        no_vis=args.no_vis,
        skip_upload=args.skip_upload,
        test_mode=test_mode,
        test_seed=args.seed,
    )
    failures += run_proposal_reviews(
        dry_run=args.dry_run,
        no_vis=args.no_vis,
        skip_upload=args.skip_upload,
        test_mode=test_mode,
    )
    failures += run_compound_reviews(
        dry_run=args.dry_run,
        no_vis=args.no_vis,
        skip_upload=args.skip_upload,
        test_mode=test_mode,
    )

    failures += run_snapshot(
        dry_run=args.dry_run,
        skip_snapshot=args.skip_snapshot,
    )

    _banner("Orchestrator finished")
    if failures:
        print(f"  {failures} item(s) failed across review routes or snapshot.", file=sys.stderr)
        sys.exit(1)
    print("  All routes and snapshot completed successfully.")


if __name__ == "__main__":
    main()
