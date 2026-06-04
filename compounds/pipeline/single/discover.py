#!/usr/bin/env python3
"""Fetch compound evidence from public APIs → ``material.json`` (JSONL rows).

Each line: ``{"source_type":"...","content":{...}}`` — same format as export JSONL.

Modes:
- **Incremental (default in run_review):** batched fetch with per-source caps, collection-time
  dedupe, delta-tag between rounds.
- **One-shot:** parallel full fetch (--one-shot), dedupe at serialize time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discover_lib.chembl import fetch_chembl
from discover_lib.clinical_trials import fetch_clinical_trials
from discover_lib.dedupe import DiscoverRegistry
from discover_lib.epmc import fetch_europe_pmc
from discover_lib.http import CallBudget, MAX_HTTP_CALLS
from discover_lib.kegg import fetch_kegg
from discover_lib.material import (
    MATERIAL_FILENAME,
    append_material_rows,
    init_material_file,
    load_material_records,
    serialize_material_jsonl,
)
from discover_lib.openalex import fetch_openalex_grounding, fetch_openalex_risk
from discover_lib.openfda import fetch_openfda
from discover_lib.pubchem import fetch_pubchem_bioassays
from discover_lib.session import DiscoverSession, rows_from_one_shot_report
from discover_lib.synonyms import resolve_compound_identity

_SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


class _SyncCallBudget:
    def __init__(self, limit: int = MAX_HTTP_CALLS) -> None:
        self.limit = limit
        self.count = 0
        self.exhausted = False
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            if self.count >= self.limit:
                self.exhausted = True
                return False
            self.count += 1
            return True


class _SyncFailList(list):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def append(self, item: dict[str, str]) -> None:
        with self._lock:
            super().append(item)


class _DiscoverState:
    def __init__(self) -> None:
        self.budget = _SyncCallBudget()
        self.fail: _SyncFailList = _SyncFailList()
        self.ver: dict[str, Any] = {
            "openfda": None,
            "clinical_trials_gov": None,
            "kegg": "KEGG REST",
            "europe_pmc": None,
            "pubchem": "PUG REST",
            "chembl": "ChEMBL REST API",
            "openalex": "OpenAlex REST API",
        }


def safe_compound_dir(compound: str) -> str:
    safe = re.sub(r"[^\w\-.]+", "_", compound, flags=re.UNICODE).strip("._- ")[:80] or "compound"
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe


def compound_output_dir(compound: str, compound_dir: Path | None = None) -> Path:
    if compound_dir is not None:
        return compound_dir.resolve()
    raise ValueError(
        "No output directory specified. Pass --compound-dir or an absolute --output path."
    )


def output_file_path(compound: str, explicit: str | None, compound_dir: Path | None = None) -> Path:
    if explicit is not None:
        raw = Path(explicit.strip()).expanduser()
        if raw.is_absolute():
            return raw.resolve()
        folder = compound_output_dir(compound, compound_dir)
        target = (folder / raw).resolve()
        try:
            target.relative_to(folder)
        except ValueError:
            print(f"Refusing --output outside compound folder: {explicit!r}", file=sys.stderr)
            raise SystemExit(2) from None
        return target
    folder = compound_output_dir(compound, compound_dir)
    return folder / MATERIAL_FILENAME


def serialize_report_deduped(report: dict[str, Any]) -> str:
    registry = DiscoverRegistry()
    rows = rows_from_one_shot_report(report, registry)
    return "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)


def run(c: str) -> dict[str, Any]:
    """Fetch all sources in parallel; return nested discover report dict."""
    state = _DiscoverState()
    identity = resolve_compound_identity(c, state.fail, state.budget)
    query_names = identity.get("query_names") or [c]
    primary_cid = identity.get("primary_cid")

    results: dict[str, Any] = {}

    def _openfda() -> None:
        block, _, dropped, aliases = fetch_openfda(query_names, state.fail, state.budget, state.ver)
        results["openfda"] = block
        results["label_filter_dropped"] = dropped
        results["aliases_tried"] = aliases

    def _clinical_trials() -> None:
        results["clinical_trials"] = fetch_clinical_trials(query_names, state.fail, state.budget, state.ver)

    def _kegg() -> None:
        results["kegg"] = fetch_kegg(query_names, state.fail, state.budget)

    def _epmc() -> None:
        results["europe_pmc"] = fetch_europe_pmc(c, query_names, state.fail, state.budget, state.ver)

    def _chembl() -> None:
        results["chembl"] = fetch_chembl(query_names, state.fail, state.budget)

    def _pubchem() -> None:
        results["pubchem_bioassays"] = fetch_pubchem_bioassays(primary_cid, state.fail, state.budget)

    def _openalex_grounding() -> None:
        results["openalex_grounding"] = fetch_openalex_grounding(c)

    def _openalex_risk() -> None:
        results["openalex_risk"] = fetch_openalex_risk(c)

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="discover") as pool:
        futures = [
            pool.submit(_openfda),
            pool.submit(_clinical_trials),
            pool.submit(_kegg),
            pool.submit(_epmc),
            pool.submit(_chembl),
            pool.submit(_pubchem),
            pool.submit(_openalex_grounding),
            pool.submit(_openalex_risk),
        ]
        for fut in as_completed(futures):
            fut.result()

    if state.budget.exhausted:
        state.fail.append({"step": "discover", "reason": f"HTTP call budget exhausted at {state.budget.count} calls"})

    return {
        "compound_name": c,
        "openfda": results.get("openfda"),
        "clinical_trials": results.get("clinical_trials"),
        "kegg": results.get("kegg"),
        "europe_pmc": results.get("europe_pmc"),
        "chembl": results.get("chembl"),
        "pubchem_bioassays": results.get("pubchem_bioassays"),
        "openalex_grounding": results.get("openalex_grounding"),
        "openalex_risk": results.get("openalex_risk"),
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api_versions": state.ver,
            "failures": list(state.fail),
            "label_filter_dropped": results.get("label_filter_dropped", 0),
            "resolved_identity": identity,
            "query_names": query_names,
            "openfda_aliases_tried": results.get("aliases_tried"),
            "http_calls": state.budget.count,
        },
    }


def run_material(c: str) -> dict[str, Any]:
    """Fetch all sources and return in-memory report (for tests)."""
    return run(c)


def _run_delta_tag(
    material_path: Path,
    out_dir: Path,
    *,
    model: str | None,
    include_risk_severity: bool,
) -> None:
    cmd: list[str] = [
        sys.executable,
        str(_SCRIPT_DIR / "tag-group-filter.py"),
        str(material_path),
        "--out-dir",
        str(out_dir),
        "--incremental",
        "--tagged-output",
        str(out_dir / "material_tagged.jsonl"),
    ]
    if model:
        cmd += ["--model", model]
    if include_risk_severity:
        cmd.append("--include-risk-severity")
    subprocess.run(cmd, check=True)


def run_incremental(
    c: str,
    compound_dir: Path,
    *,
    max_rounds: int | None = None,
    fresh_material: bool = False,
    model: str | None = None,
    include_risk_severity: bool = False,
    skip_tag: bool = False,
) -> Path:
    """Incremental discover: batch fetch → append → delta-tag until caps or no new rows."""
    compound_dir = compound_dir.resolve()
    material_path = compound_dir / MATERIAL_FILENAME
    session = DiscoverSession.create(c)
    limits = session.limits
    max_r = max_rounds if max_rounds is not None else limits.max_rounds

    if fresh_material or not material_path.is_file():
        init_material_file(material_path, c, fresh=True)
    else:
        existing = load_material_records(material_path)
        session.registry.keys_from_material_rows(existing)

    print(f"Incremental discover: max_rounds={max_r}", file=sys.stderr)

    for _ in range(max_r):
        new_rows = session.fetch_round_rows()
        meta_row = {
            "source_type": "discover_round_meta",
            "content": {
                "round": session.round_num,
                "new_rows": len(new_rows),
                "http_calls": session.budget.count if session.budget else 0,
                "counts_by_source": dict(session.registry.count_by_source),
                "exhausted": dict(session.source_exhausted),
            },
        }
        append_material_rows(material_path, [meta_row])
        if new_rows:
            append_material_rows(material_path, new_rows)
            print(
                f"  round {session.round_num}: +{len(new_rows)} rows "
                f"(total keys {len(session.registry.seen)})",
                file=sys.stderr,
            )
            if not skip_tag:
                _run_delta_tag(
                    material_path,
                    compound_dir,
                    model=model,
                    include_risk_severity=include_risk_severity,
                )
        else:
            print(f"  round {session.round_num}: no new rows", file=sys.stderr)
            break
        if session.all_sources_exhausted():
            print("  all sources exhausted", file=sys.stderr)
            break

    discover_meta = {
        "source_type": "discover_metadata",
        "content": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "incremental": True,
            "rounds": session.round_num,
            "failures": list(session.fail),
            "api_versions": session.ver,
            "registry_counts": dict(session.registry.count_by_source),
        },
    }
    append_material_rows(material_path, [discover_meta])
    return material_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compound intel from public APIs → material.json.",
        epilog=f"Default incremental when --incremental; use --one-shot for legacy parallel fetch.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--compound", required=True)
    ap.add_argument("--compound-dir", type=Path, default=None, metavar="DIR")
    ap.add_argument("--output", metavar="PATH", default=None)
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument(
        "--incremental",
        action="store_true",
        help="Incremental batched discover with delta-tag between rounds.",
    )
    ap.add_argument(
        "--one-shot",
        action="store_true",
        help="Legacy single parallel fetch (no incremental loop).",
    )
    ap.add_argument("--max-rounds", type=int, default=None, metavar="N")
    ap.add_argument("--fresh-material", action="store_true", help="Truncate material.json before run.")
    ap.add_argument("--model", default=None, help="LLM model for delta-tag passes.")
    ap.add_argument("--skip-tag", action="store_true", help="Incremental discover only; no tag-group-filter.")
    ap.add_argument("--include-risk-severity", action="store_true")
    ns = ap.parse_args()
    c = ns.compound.strip()
    compound_dir = ns.compound_dir.expanduser().resolve() if ns.compound_dir else None

    use_incremental = bool(ns.incremental) and not ns.one_shot

    try:
        if use_incremental:
            if compound_dir is None:
                print("discover.py: --compound-dir required for incremental mode", file=sys.stderr)
                return 1
            path = run_incremental(
                c,
                compound_dir,
                max_rounds=ns.max_rounds,
                fresh_material=ns.fresh_material,
                model=ns.model,
                include_risk_severity=ns.include_risk_severity,
                skip_tag=ns.skip_tag,
            )
            if ns.stdout:
                out = path.read_text(encoding="utf-8")
                n_records = out.count("\n") if out else 0
            else:
                path = output_file_path(c, ns.output, compound_dir=compound_dir)
                n_records = path.read_text(encoding="utf-8").count("\n") if path.is_file() else 0
        else:
            report = run(c)
            out = serialize_report_deduped(report)
            n_records = out.count("\n") if out else 0
            if ns.stdout:
                pass
            else:
                path = output_file_path(c, ns.output, compound_dir=compound_dir)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(out, encoding="utf-8")
    except Exception as e:
        report = {
            "compound_name": c or None,
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "failures": [{"step": "fatal", "reason": str(e)}],
            },
        }
        out = serialize_report_deduped(report)
        n_records = out.count("\n") if out else 0
        if not ns.stdout and compound_dir:
            path = output_file_path(c, ns.output, compound_dir=compound_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(out, encoding="utf-8")

    if ns.stdout:
        sys.stdout.buffer.write(out.encode("utf-8"))
    elif not use_incremental:
        print(f"Wrote material: {path} ({n_records} records)", file=sys.stderr)
    else:
        print(f"Wrote material: {path} ({n_records} records)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
