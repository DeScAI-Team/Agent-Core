#!/usr/bin/env python3
"""Research DAO review pipeline orchestrator.

Stages: process -> chunk -> extract -> validate -> group -> review -> overview -> evidence

Default output layout (mirrors compounds/<TICKER> conventions):

  reviews/DAOs/<SYMBOL>/
    review/
      review.json
      overview.json
      evidence_audit.md
    steps/
      bundle/
      chunks.jsonl
      extracted.jsonl
      validated.jsonl
      groups/<category>.json
      group_scores.json

Usage:
  python run_dao_review.py --ipnft-dir output/molecule/ipnfts/BeeARD
  python run_dao_review.py --ipnft-dir output/molecule/ipnfts/BeeARD \\
      --output-dir reviews/DAOs/BeeARD --reuse-bundle
  python run_dao_review.py --batch output/molecule/ipnfts --skip-vision
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_PIPELINE_DIR = Path(__file__).resolve().parent
_DAO_ROOT = _PIPELINE_DIR.parent
_REPO_ROOT = _DAO_ROOT.parent.parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import chunk as chunk_mod  # noqa: E402
import evidence_audit as evidence_mod  # noqa: E402
import extract_tag as extract_mod  # noqa: E402
import group_score as group_mod  # noqa: E402
import overview as overview_mod  # noqa: E402
import review as review_mod  # noqa: E402
import validate as validate_mod  # noqa: E402
from llm_client import LLM_BASE_URL  # noqa: E402  (forces .env load)
from multimedia_processor import process_ipnft  # noqa: E402

STAGES = ("process", "chunk", "extract", "validate", "group", "review", "overview", "evidence")
STAGE_INDEX = {name: i for i, name in enumerate(STAGES)}


def _resolve_output_dir(ipnft_dir: Path, override: Path | None) -> Path:
    if override is not None:
        return override.resolve()
    symbol = _detect_symbol(ipnft_dir) or ipnft_dir.name
    return (_REPO_ROOT / "reviews" / "DAOs" / symbol).resolve()


def _detect_symbol(ipnft_dir: Path) -> str | None:
    import json as _json

    for path in (ipnft_dir / "profile.json", ipnft_dir / "metadata" / "profile.json"):
        if path.exists():
            try:
                profile = _json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            return profile.get("symbol") or (profile.get("ipnft", {}) or {}).get("initialSymbol")
    return None


def _stage_active(name: str, start_idx: int, stop_idx: int) -> bool:
    idx = STAGE_INDEX[name]
    return start_idx <= idx <= stop_idx


def run_single(args: argparse.Namespace) -> None:
    ipnft_dir = args.ipnft_dir.resolve()
    output_dir = _resolve_output_dir(ipnft_dir, args.output_dir)
    review_dir = output_dir / "review"
    steps_dir = output_dir / "steps"
    bundle_dir = steps_dir / "bundle"
    chunks_path = steps_dir / "chunks.jsonl"
    extracted_path = steps_dir / "extracted.jsonl"
    validated_path = steps_dir / "validated.jsonl"
    groups_dir = steps_dir / "groups"
    scores_path = steps_dir / "group_scores.json"
    review_path = review_dir / "review.json"
    overview_path = review_dir / "overview.json"
    audit_path = review_dir / "evidence_audit.md"

    review_dir.mkdir(parents=True, exist_ok=True)
    steps_dir.mkdir(parents=True, exist_ok=True)

    start_idx = STAGE_INDEX[args.from_step]
    stop_idx = STAGE_INDEX[args.stop_after] if args.stop_after else len(STAGES) - 1

    print(f"[orchestrator] {ipnft_dir.name} -> {output_dir}")
    print(f"[orchestrator] stages {STAGES[start_idx]}..{STAGES[stop_idx]}")

    if _stage_active("process", start_idx, stop_idx):
        bundle_manifest = bundle_dir / "manifest.json"
        if args.reuse_bundle and bundle_manifest.exists() and not args.overwrite_bundle:
            print(f"[process] reusing bundle at {bundle_dir}")
        else:
            print(f"[process] building bundle at {bundle_dir}")
            process_ipnft(
                ipnft_dir,
                bundle_dir,
                vision_model=args.vision_model,
                skip_vision=args.skip_vision,
                keep_temp=args.keep_temp,
                overwrite=args.overwrite_bundle,
            )

    if _stage_active("chunk", start_idx, stop_idx):
        with chunks_path.open("w", encoding="utf-8") as fh:
            count = 0
            by_kind: dict[str, int] = {}
            for ch in chunk_mod.collect_chunks(ipnft_dir, bundle_dir, max_chars=args.max_chars):
                fh.write(_dump_jsonl(ch))
                count += 1
                by_kind[ch["source_kind"]] = by_kind.get(ch["source_kind"], 0) + 1
        print(f"[chunk] wrote {count} chunks to {chunks_path}")
        for k, v in sorted(by_kind.items()):
            print(f"  {k}: {v}")

    if _stage_active("extract", start_idx, stop_idx):
        extract_mod.run(
            chunks_path=chunks_path,
            ipnft_dir=ipnft_dir,
            output_path=extracted_path,
            extract_model=args.model,
            tagger_model=args.tagger_model,
        )

    if _stage_active("validate", start_idx, stop_idx):
        validate_mod.run(
            extracted_path=extracted_path,
            ipnft_dir=ipnft_dir,
            output_path=validated_path,
            model=args.model,
            skip_openalex=args.skip_openalex,
        )

    if _stage_active("group", start_idx, stop_idx):
        group_mod.run(
            validated_path=validated_path,
            out_groups_dir=groups_dir,
            out_scores_path=scores_path,
        )

    if _stage_active("review", start_idx, stop_idx):
        review_mod.run(
            groups_dir=groups_dir,
            scores_path=scores_path,
            ipnft_dir=ipnft_dir,
            output_path=review_path,
            model=args.model,
            extracted_path=extracted_path,
            chunks_path=chunks_path,
        )

    if _stage_active("overview", start_idx, stop_idx):
        if review_path.exists():
            overview_mod.run(
                review_path=review_path,
                output_path=overview_path,
                model=args.model,
            )
        else:
            print("[overview] skipped — review.json missing")

    if _stage_active("evidence", start_idx, stop_idx):
        evidence_mod.build(
            ipnft_dir=ipnft_dir,
            steps_dir=steps_dir,
            review_dir=review_dir,
            output_path=audit_path,
        )

    print(f"\n[orchestrator] done. Output: {output_dir}")


def _dump_jsonl(row: dict[str, Any]) -> str:
    import json as _json

    return _json.dumps(row, ensure_ascii=False) + "\n"


def run_batch(args: argparse.Namespace) -> None:
    batch_dir = args.batch.resolve()
    candidates = [
        d for d in sorted(batch_dir.iterdir())
        if d.is_dir() and (d / "profile.json").exists()
    ]
    print(f"[batch] {len(candidates)} IPNFTs in {batch_dir}")
    for i, ipnft_dir in enumerate(candidates, 1):
        print(f"\n{'=' * 60}\n[{i}/{len(candidates)}] {ipnft_dir.name}\n{'=' * 60}")
        single_args = argparse.Namespace(**vars(args))
        single_args.ipnft_dir = ipnft_dir
        single_args.output_dir = None if args.output_dir is None else args.output_dir / ipnft_dir.name
        try:
            run_single(single_args)
        except Exception as exc:  # noqa: BLE001
            print(f"[batch] {ipnft_dir.name} FAILED: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Research DAO review pipeline orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ipnft-dir", type=Path)
    group.add_argument("--batch", type=Path)

    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--from-step",
        choices=list(STAGES),
        default="process",
    )
    parser.add_argument(
        "--stop-after",
        choices=list(STAGES),
        default=None,
    )
    parser.add_argument("--reuse-bundle", action="store_true",
                        help="If steps/bundle/manifest.json exists, skip the process stage")
    parser.add_argument("--overwrite-bundle", action="store_true")
    parser.add_argument("--skip-vision", action="store_true",
                        help="Skip PDF/image/video processing in the process stage")
    parser.add_argument("--skip-openalex", action="store_true",
                        help="Mark all scientific lines inconclusive without hitting OpenAlex")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--max-chars", type=int, default=chunk_mod.DEFAULT_MAX_CHARS)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--tagger-model", type=str, default=None)
    parser.add_argument("--vision-model", type=str, default=None)
    args = parser.parse_args()

    if args.ipnft_dir:
        if not args.ipnft_dir.is_dir():
            parser.error(f"Not a directory: {args.ipnft_dir}")
        run_single(args)
    else:
        if not args.batch.is_dir():
            parser.error(f"Not a directory: {args.batch}")
        run_batch(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
