#!/usr/bin/env python3
"""Research DAO review pipeline orchestrator for Molecule IP-NFTs.

Two-phase pipeline:
  Phase 1: Per-PDF article processing (reuses articles/pipeline/run_full_pipeline.py)
  Phase 2: DAO-level synthesis (aggregate, enrich, review, score, evidence doc)

Supports staged execution for single-model environments:

  Stage A (vision/OCR model):
    python run_dao_pipeline.py --ipnft-dir .../CLAW --stop-after ocr --model /vision-model

  Stage B (text LLM — swap model, then resume):
    python run_dao_pipeline.py --ipnft-dir .../CLAW --from-step llm --model /text-model

Usage:
  Single IPNFT (full run, both models available):
    python run_dao_pipeline.py --ipnft-dir crawlers/output/molecule/ipnfts/CLAW \\
      --output-dir DAOs/molecule/output/CLAW --model /model

  Batch (all IPNFTs):
    python run_dao_pipeline.py --batch crawlers/output/molecule/ipnfts \\
      --output-dir DAOs/molecule/output --model /model

Environment: VLLM_BASE_URL, VLLM_API_KEY, VALIDATOR_MODEL, READ_PAPER_MODEL, WHISPER_CPP_BIN, WHISPER_MODEL_PATH
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

_PIPELINE_DIR = Path(__file__).resolve().parent
_DAO_ROOT = _PIPELINE_DIR.parent
_REPO_ROOT = _DAO_ROOT.parent.parent
_ARTICLE_PIPELINE = _REPO_ROOT / "articles" / "pipeline"

PY = sys.executable

STEPS = ("multimedia", "ocr", "llm", "aggregate", "review", "score", "evidence", "upload")
STEP_INDEX = {name: i for i, name in enumerate(STEPS)}
# Backward compat
STEP_INDEX["filter"] = STEP_INDEX["multimedia"]
STEP_INDEX["process"] = STEP_INDEX["ocr"]


def _load_env() -> None:
    if load_dotenv:
        env_path = _REPO_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)


def _resolve_model(args_model: str | None) -> str:
    if args_model:
        return args_model
    return os.environ.get("VALIDATOR_MODEL", "/model")


def _run_article_pipeline(
    pdf_path: Path,
    output_dir: Path,
    model: str,
    skip_llm: bool = False,
    overwrite: bool = False,
    stop_after: str | None = None,
    from_step: str | None = None,
) -> bool:
    """Run the article pipeline on a single PDF. Returns True on success.

    Args:
        stop_after: Article pipeline step to stop after (e.g. "reader" for OCR-only)
        from_step: Article pipeline step to resume from (e.g. "add_data" for text LLM)
    """
    cmd = [
        PY,
        str(_ARTICLE_PIPELINE / "run_full_pipeline.py"),
        str(pdf_path),
        "--output-dir", str(output_dir),
        "--model", model,
        "--skip-upload",
    ]
    if skip_llm:
        cmd.append("--skip-llm")
    if overwrite:
        cmd.append("--overwrite")
    if stop_after:
        cmd.extend(["--stop-after", stop_after])
    if from_step:
        cmd.extend(["--from-step", from_step])

    stage_label = ""
    if stop_after == "reader":
        stage_label = " (OCR only)"
    elif from_step == "add_data":
        stage_label = " (text LLM)"

    print(f"  [article-pipeline{stage_label}] Processing: {pdf_path.name}")
    print(f"  [article-pipeline{stage_label}] Output: {output_dir}")
    t0 = time.time()

    result = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        env={**os.environ, "VALIDATOR_MODEL": model},
        capture_output=False,
    )

    elapsed = time.time() - t0
    if result.returncode == 0:
        print(f"  [article-pipeline{stage_label}] Done in {elapsed:.1f}s")
        return True
    else:
        print(f"  [article-pipeline{stage_label}] FAILED (exit {result.returncode}) after {elapsed:.1f}s")
        return False


def _find_review_jsons(docs_dir: Path) -> list[Path]:
    """Find all review.json files produced by article pipeline runs."""
    results = []
    if not docs_dir.exists():
        return results
    for output_dir in docs_dir.rglob("output"):
        review_path = output_dir / "review.json"
        if review_path.exists():
            results.append(review_path)
    return sorted(results)


def _ensure_multimedia_bundle(
    ipnft_dir: Path,
    output_dir: Path,
    model: str,
    start_idx: int,
    stop_idx: int,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    """Run multimedia step when requested or when bundle is missing."""
    bundle_manifest = output_dir / "bundle" / "manifest.json"
    should_run = (
        start_idx <= STEP_INDEX["multimedia"] and stop_idx >= STEP_INDEX["multimedia"]
    ) or (not bundle_manifest.exists())

    if not should_run:
        return None

    if bundle_manifest.exists() and not args.overwrite and start_idx > STEP_INDEX["multimedia"]:
        print(f"\n[multimedia] Using existing bundle: {output_dir / 'bundle'}")
        return None

    return run_multimedia(
        ipnft_dir=ipnft_dir,
        output_dir=output_dir,
        model=model,
        skip_llm=args.skip_llm,
        skip_vision=args.skip_vision,
        keep_temp=args.keep_temp,
        overwrite=args.overwrite,
    )


def run_multimedia(
    ipnft_dir: Path,
    output_dir: Path,
    model: str,
    *,
    skip_llm: bool = False,
    skip_vision: bool = False,
    keep_temp: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build multimedia bundle from IPNFT dataroom files."""
    from multimedia_processor import process_ipnft

    bundle_dir = output_dir / "bundle"
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.exists() and not overwrite:
        print(f"\n[multimedia] Reusing existing bundle: {bundle_dir}")
        results_path = output_dir / "bundle_results.json"
        if results_path.exists():
            return json.loads(results_path.read_text(encoding="utf-8"))
        return {"reused": True, "bundle_dir": str(bundle_dir)}

    print(f"\n[multimedia] Building bundle for {ipnft_dir.name}")
    results = process_ipnft(
        ipnft_dir,
        bundle_dir,
        text_model=model,
        skip_llm=skip_llm,
        skip_vision=skip_vision,
        keep_temp=keep_temp,
        overwrite=overwrite,
    )
    bundle_results_path = output_dir / "bundle_results.json"
    bundle_results_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def run_phase1(
    ipnft_dir: Path,
    output_dir: Path,
    model: str,
    skip_llm: bool = False,
    overwrite: bool = False,
    stage: str = "full",
) -> dict[str, Any]:
    """Phase 1: Run article pipeline on bundle/pdf/articles/ PDFs.

    Args:
        stage: One of "full" (default), "ocr" (vision model only, stops after reader),
               or "llm" (text model only, resumes from add_data).
    """
    from multimedia_processor import list_article_pdfs

    docs_output = output_dir / "docs"
    docs_output.mkdir(parents=True, exist_ok=True)

    candidates = list_article_pdfs(output_dir / "bundle")

    stage_label = {"full": "", "ocr": " (OCR only)", "llm": " (text LLM)"}
    print(f"\n[Phase 1{stage_label.get(stage, '')}] Processing {len(candidates)} article PDFs from bundle")

    results = {"processed": [], "failed": [], "skipped_count": 0, "stage": stage}

    if not candidates:
        print("  No processable PDFs found.")
        return results

    stop_after = "reader" if stage == "ocr" else None
    from_step = "add_data" if stage == "llm" else None

    for i, doc in enumerate(candidates, 1):
        print(f"\n  [{i}/{len(candidates)}] {doc['filename']} ({doc['reason']})")
        success = _run_article_pipeline(
            pdf_path=doc["path"],
            output_dir=docs_output,
            model=model,
            skip_llm=skip_llm,
            overwrite=overwrite,
            stop_after=stop_after,
            from_step=from_step,
        )
        entry = {"filename": doc["filename"], "reason": doc["reason"]}
        if success:
            results["processed"].append(entry)
        else:
            results["failed"].append(entry)

    return results


def run_phase2(
    ipnft_dir: Path,
    output_dir: Path,
    model: str,
    skip_llm: bool = False,
    skip_upload: bool = False,
    from_step: str = "aggregate",
) -> None:
    """Phase 2: DAO-level synthesis."""
    from aggregate import aggregate_reviews
    from enrich_context import enrich_context
    from dao_review import run_dao_review
    from dao_score import run_dao_score
    from dao_evidence_doc import generate_evidence_doc, generate_overview

    synthesis_dir = output_dir / "synthesis"
    synthesis_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = output_dir / "docs"

    start_idx = STEP_INDEX.get(from_step, 2)

    print(f"\n[Phase 2] DAO synthesis for {ipnft_dir.name}")

    if start_idx <= STEP_INDEX["aggregate"]:
        print("\n  [aggregate] Merging per-document reviews (type-aware)...")
        review_paths = _find_review_jsons(docs_dir)
        mappings = json.loads((_PIPELINE_DIR / "dao_mappings.json").read_text(encoding="utf-8"))
        type_dimension_weights = mappings.get("type_dimension_weights")
        aggregated = aggregate_reviews(review_paths, type_dimension_weights=type_dimension_weights)
        agg_path = synthesis_dir / "aggregated.json"
        agg_path.write_text(json.dumps(aggregated, indent=2), encoding="utf-8")
        print(f"  [aggregate] Merged {len(review_paths)} reviews -> {agg_path.name}")
    else:
        agg_path = synthesis_dir / "aggregated.json"
        aggregated = json.loads(agg_path.read_text(encoding="utf-8"))

    if start_idx <= STEP_INDEX["review"]:
        print("\n  [enrich] Extracting structured context...")
        enriched = enrich_context(
            ipnft_dir=ipnft_dir,
            aggregated=aggregated,
            molecule_docs_path=_DAO_ROOT / "molecule_docs.json",
            context_index_path=_PIPELINE_DIR / "context_index.json",
        )
        enriched_path = synthesis_dir / "enriched.json"
        enriched_path.write_text(json.dumps(enriched, indent=2), encoding="utf-8")
        print(f"  [enrich] Context enriched -> {enriched_path.name}")

        if not skip_llm:
            print("\n  [review] Running DAO-level LLM review...")
            dao_review_result = run_dao_review(
                enriched=enriched,
                model=model,
                prompts_dir=_PIPELINE_DIR / "prompts",
            )
            review_path = synthesis_dir / "dao_review_raw.json"
            review_path.write_text(json.dumps(dao_review_result, indent=2), encoding="utf-8")
            print(f"  [review] DAO review -> {review_path.name}")
        else:
            review_path = synthesis_dir / "dao_review_raw.json"
            if review_path.exists():
                dao_review_result = json.loads(review_path.read_text(encoding="utf-8"))
            else:
                dao_review_result = enriched
    else:
        enriched_path = synthesis_dir / "enriched.json"
        enriched = json.loads(enriched_path.read_text(encoding="utf-8"))
        review_path = synthesis_dir / "dao_review_raw.json"
        if review_path.exists():
            dao_review_result = json.loads(review_path.read_text(encoding="utf-8"))
        else:
            dao_review_result = enriched

    if start_idx <= STEP_INDEX["score"]:
        print("\n  [score] Computing DAO-weighted composite score...")
        scored = run_dao_score(
            dao_review=dao_review_result,
            mappings_path=_PIPELINE_DIR / "dao_mappings.json",
        )
        scored_path = synthesis_dir / "dao_review.json"
        scored_path.write_text(json.dumps(scored, indent=2), encoding="utf-8")
        print(f"  [score] Scored -> {scored_path.name} (composite: {scored.get('composite_score', 'N/A')})")
    else:
        scored_path = synthesis_dir / "dao_review.json"
        scored = json.loads(scored_path.read_text(encoding="utf-8"))

    if start_idx <= STEP_INDEX["evidence"]:
        print("\n  [evidence] Generating evidence audit document...")
        audit_path = synthesis_dir / "dao_evidence_audit.md"
        generate_evidence_doc(
            scored_review=scored,
            output_path=audit_path,
        )
        overview_path = synthesis_dir / "overview.json"
        generate_overview(
            scored_review=scored,
            output_path=overview_path,
        )
        print(f"  [evidence] Audit -> {audit_path.name}")
        print(f"  [evidence] Overview -> {overview_path.name}")

    if start_idx <= STEP_INDEX["upload"] and not skip_upload:
        sys.path.insert(0, str(_DAO_ROOT))
        from uploader import run_upload_sequence

        print("\n  [upload] Uploading to Arweave...")
        upload_result = run_upload_sequence(synthesis_dir=synthesis_dir)
        if upload_result.get("success"):
            print("  [upload] Upload complete.")
        else:
            print(f"  [upload] Upload failed: {upload_result.get('error', 'unknown')}")

    print(f"\n[Done] DAO review complete for {ipnft_dir.name}")
    print(f"  Output: {synthesis_dir}")


def run_single(args: argparse.Namespace) -> None:
    """Run pipeline for a single IPNFT."""
    ipnft_dir = args.ipnft_dir.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else _DAO_ROOT / "molecule" / "output" / ipnft_dir.name

    model = _resolve_model(args.model)
    from_step = args.from_step or "multimedia"
    stop_after = args.stop_after
    start_idx = STEP_INDEX.get(from_step, 0)
    stop_idx = STEP_INDEX.get(stop_after, len(STEPS) - 1) if stop_after else len(STEPS) - 1

    _ensure_multimedia_bundle(
        ipnft_dir, output_dir, model, start_idx, stop_idx, args,
    )

    if stop_after and stop_idx <= STEP_INDEX["multimedia"]:
        print(f"\n[Stopped after '{stop_after}'] — resume with --from-step ocr")
        return

    # Phase 1: OCR stage (vision model)
    if start_idx <= STEP_INDEX["ocr"] and stop_idx >= STEP_INDEX["ocr"]:
        stage = "ocr" if stop_idx == STEP_INDEX["ocr"] else "full" if start_idx <= STEP_INDEX["ocr"] and stop_idx >= STEP_INDEX["llm"] else "ocr"
        if from_step == "llm":
            stage = "llm"
        elif stop_after == "ocr":
            stage = "ocr"

        phase1_results = run_phase1(
            ipnft_dir=ipnft_dir,
            output_dir=output_dir,
            model=model,
            skip_llm=args.skip_llm,
            overwrite=args.overwrite,
            stage=stage,
        )
        results_path = output_dir / "phase1_results.json"
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(phase1_results, indent=2), encoding="utf-8")

    # Phase 1: LLM stage (text model) — only if resuming from llm step
    elif start_idx == STEP_INDEX["llm"]:
        phase1_results = run_phase1(
            ipnft_dir=ipnft_dir,
            output_dir=output_dir,
            model=model,
            skip_llm=args.skip_llm,
            overwrite=args.overwrite,
            stage="llm",
        )
        results_path = output_dir / "phase1_results.json"
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(phase1_results, indent=2), encoding="utf-8")

    if stop_after and stop_idx <= STEP_INDEX["llm"]:
        print(f"\n[Stopped after '{stop_after}'] — swap models and resume with --from-step {STEPS[stop_idx + 1]}")
        return

    # Phase 2: DAO synthesis
    if start_idx <= STEP_INDEX["aggregate"] or (stop_after is None or stop_idx >= STEP_INDEX["aggregate"]):
        phase2_from = from_step if start_idx >= STEP_INDEX["aggregate"] else "aggregate"
        phase2_stop = stop_after if stop_after and stop_idx >= STEP_INDEX["aggregate"] else None

        run_phase2(
            ipnft_dir=ipnft_dir,
            output_dir=output_dir,
            model=model,
            skip_llm=args.skip_llm,
            skip_upload=args.skip_upload,
            from_step=phase2_from,
        )


def run_batch(args: argparse.Namespace) -> None:
    """Run pipeline for all IPNFTs in a batch directory."""
    batch_dir = args.batch.resolve()
    base_output = args.output_dir.resolve() if args.output_dir else _DAO_ROOT / "molecule" / "output"
    model = _resolve_model(args.model)
    from_step = args.from_step or "multimedia"
    stop_after = args.stop_after
    start_idx = STEP_INDEX.get(from_step, 0)
    stop_idx = STEP_INDEX.get(stop_after, len(STEPS) - 1) if stop_after else len(STEPS) - 1

    ipnft_dirs = sorted(
        d for d in batch_dir.iterdir()
        if d.is_dir() and (d / "profile.json").exists()
    )

    print(f"[Batch] Found {len(ipnft_dirs)} IPNFTs in {batch_dir}")

    for i, ipnft_dir in enumerate(ipnft_dirs, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(ipnft_dirs)}] {ipnft_dir.name}")
        print(f"{'='*60}")

        output_dir = base_output / ipnft_dir.name

        try:
            _ensure_multimedia_bundle(
                ipnft_dir, output_dir, model, start_idx, stop_idx, args,
            )

            if stop_after and stop_idx <= STEP_INDEX["multimedia"]:
                continue

            if start_idx <= STEP_INDEX["llm"]:
                stage = "ocr" if stop_after == "ocr" else "llm" if from_step == "llm" else "full"
                run_phase1(
                    ipnft_dir=ipnft_dir,
                    output_dir=output_dir,
                    model=model,
                    skip_llm=args.skip_llm,
                    overwrite=args.overwrite,
                    stage=stage,
                )

            if stop_after and stop_idx <= STEP_INDEX["llm"]:
                continue

            if start_idx >= STEP_INDEX["aggregate"] or stop_idx >= STEP_INDEX["aggregate"]:
                phase2_from = from_step if start_idx >= STEP_INDEX["aggregate"] else "aggregate"
                run_phase2(
                    ipnft_dir=ipnft_dir,
                    output_dir=output_dir,
                    model=model,
                    skip_llm=args.skip_llm,
                    skip_upload=args.skip_upload,
                    from_step=phase2_from,
                )
        except Exception as exc:
            print(f"  [ERROR] {ipnft_dir.name}: {exc}")
            continue

    if stop_after and stop_idx <= STEP_INDEX["llm"]:
        print(f"\n[Batch] OCR complete. Swap models and resume with --from-step {STEPS[stop_idx + 1]}")
    else:
        print(f"\n[Batch] Complete. Processed {len(ipnft_dirs)} IPNFTs.")


def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser(
        description="Research DAO review pipeline for Molecule IP-NFTs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ipnft-dir", type=Path, help="Single IPNFT directory to process")
    group.add_argument("--batch", type=Path, help="Directory containing multiple IPNFT folders")

    parser.add_argument("--output-dir", type=Path, help="Output directory (default: DAOs/molecule/output/<SYMBOL>)")
    parser.add_argument("--model", type=str, help="LLM model path (default: $VALIDATOR_MODEL or /model)")
    parser.add_argument(
        "--from-step",
        choices=list(STEPS) + ["filter", "process"],
        default=None,
        help="Resume from this step (default: multimedia). Steps: multimedia, ocr, llm, aggregate, ...",
    )
    parser.add_argument(
        "--stop-after",
        choices=list(STEPS) + ["filter"],
        default=None,
        help="Stop after this step (e.g. 'multimedia' for bundle only, 'ocr' for vision article OCR)",
    )
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM calls in synthesis")
    parser.add_argument("--skip-vision", action="store_true", help="Skip image/video routes in multimedia step")
    parser.add_argument("--keep-temp", action="store_true", help="Keep video frame temp files in bundle")
    parser.add_argument("--skip-upload", action="store_true", help="Skip Arweave upload after synthesis")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")

    args = parser.parse_args()

    if args.ipnft_dir:
        if not args.ipnft_dir.resolve().is_dir():
            parser.error(f"Not a directory: {args.ipnft_dir}")
        run_single(args)
    else:
        if not args.batch.resolve().is_dir():
            parser.error(f"Not a directory: {args.batch}")
        run_batch(args)


if __name__ == "__main__":
    main()
