#!/usr/bin/env python3
"""
Quick pipeline re-run for document (10).

Skips PDF read / add_data.py — uses existing text_knowledge_base.jsonl.
Runs: spaCy tag → LLM extract → validate → classify → group → triage →
      retrieve_compare → prep evidence → generate review → originality →
      screener → unified scoring → evidence audit Markdown.

Output: articles/data/document (10)/pipe-test2/

Usage:
  python run_pipe2.py                  # full run (steps 1-13), LLM enabled
  python run_pipe2.py --from-step 4    # resume from classify (needs validated_claims.jsonl)
  python run_pipe2.py --from-step 8    # just prep + review + originality + screener + scoring
  python run_pipe2.py --from-step 12   # unified scoring + evidence audit document
  python run_pipe2.py --from-step 13   # evidence audit only (needs review.json + retrieve_compare output + screener.json)
  python run_pipe2.py --skip-llm       # run retrieve_compare WITHOUT LLM evidence grading
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
PIPELINE = REPO / "articles" / "pipeline"
CLAIM_EXTRACT = PIPELINE / "claim-extract"
EMPIRICAL = PIPELINE / "empirical"
MAPPINGS = PIPELINE / "mappings.json"

DOC_DIR = REPO / "articles" / "data" / "document (10)"
FULL_MD = DOC_DIR / "full.md"

# Source KB — full document KB (all chunks)
SOURCE_KB = REPO / "articles" / "data" / "text_knowledge_base.jsonl"

# Output directory
OUT = DOC_DIR / "pipe-test2"

PY = sys.executable


def run(label: str, cmd: list[str], *, env: dict | None = None, cwd: Path | None = None):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  cmd: {' '.join(cmd[:3])} ...")
    result = subprocess.run(cmd, env=env, cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        print(f"  FAILED (exit {result.returncode})")
        sys.exit(result.returncode)
    print(f"  OK")


def main():
    parser = argparse.ArgumentParser(description="Run pipe-test2 pipeline for document (10).")
    parser.add_argument(
        "--from-step", type=int, default=1, choices=range(1, 14),
        help="Start from this step (1=spacy, 2=extract, 3=validate, 4=classify, 5=group, 6=triage, 7=retrieve_compare, 8=prep_evidence, 9=review, 10=originality_check, 11=screener, 12=score, 13=evidence-doc)",
    )
    parser.add_argument(
        "--model", type=str, default="/model",
        help="Model name as served by vLLM (default: /model)",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip LLM evidence grading in retrieve_compare (default: LLM enabled)",
    )
    args = parser.parse_args()
    start = args.from_step

    OUT.mkdir(parents=True, exist_ok=True)

    # Copy the full KB into our output dir so intermediate files land there
    kb_dest = OUT / "text_knowledge_base.jsonl"
    if not kb_dest.exists() or start == 1:
        shutil.copy2(SOURCE_KB, kb_dest)
        print(f"Copied KB ({SOURCE_KB.name}) → {kb_dest}")

    base_env = {**os.environ, "VALIDATOR_MODEL": args.model}
    ce_env = {**base_env, "CLAIM_EXTRACT_DATA_DIR": str(OUT)}

    # --- Step 1: spaCy tagging ---
    if start <= 1:
        _spacy_in = CLAIM_EXTRACT / "text_knowledge_base.jsonl"
        _spacy_out = CLAIM_EXTRACT / "test_output_tagged.jsonl"
        shutil.copy2(kb_dest, _spacy_in)
        run(
            "Step 1/13 — spaCy tagging (text_knowledge_base → test_output_tagged)",
            [PY, str(CLAIM_EXTRACT / "spacy_test.py")],
        )
        shutil.move(str(_spacy_out), str(OUT / "test_output_tagged.jsonl"))
        _spacy_in.unlink(missing_ok=True)

    # --- Step 2: LLM claim extraction ---
    if start <= 2:
        run(
            "Step 2/13 — LLM claim extraction (test_output_tagged → final_claims_for_audit)",
            [PY, str(CLAIM_EXTRACT / "LLM_extract.py")],
            env=ce_env,
        )

    # --- Step 3: LLM validation ---
    if start <= 3:
        run(
            "Step 3/13 — LLM validation (final_claims → validated_claims)",
            [PY, str(CLAIM_EXTRACT / "claim_validator.py")],
            env=ce_env,
        )

    # --- Step 4: Classification ---
    validated = OUT / "validated_claims.jsonl"
    classified = OUT / "classified_claims.jsonl"
    if start <= 4:
        run(
            "Step 4/13 — Classify claims",
            [PY, str(PIPELINE / "classify_claims.py"), "-i", str(validated), "-o", str(classified)],
            env=base_env,
        )

    # --- Step 5: Group by dimension ---
    grouped = OUT / "grouped.json"
    if start <= 5:
        run(
            "Step 5/13 — Group by dimension",
            [PY, str(PIPELINE / "group.py"), str(classified), "-o", str(grouped), "--mappings", str(MAPPINGS)],
            env=base_env,
        )

    # --- Step 6: Triage ---
    triaged = OUT / "triaged.json"
    if start <= 6:
        run(
            "Step 6/13 — Triage into buckets",
            [PY, str(EMPIRICAL / "triage.py"), str(grouped), "-o", str(triaged), "--mappings", str(MAPPINGS)],
            env=base_env,
        )

    # --- Step 7: Retrieve + Compare (evidence grading) ---
    if start <= 7:
        use_llm = not args.skip_llm
        rc_out = OUT / ("retrieve_compare_llm.json" if use_llm else "retrieve_compare_out.json")
        rc_cmd = [
            PY, str(EMPIRICAL / "retrieve_compare.py"),
            str(triaged),
            "--kb", str(kb_dest),
            "--fullmd", str(FULL_MD),
            "--openalex-cache", str(OUT / "openalex_cache.json"),
            "-o", str(rc_out),
        ]
        if not use_llm:
            rc_cmd.append("--skip-llm")
        run(
            f"Step 7/13 — Retrieve & compare ({'WITH LLM' if use_llm else 'skip-llm'})",
            rc_cmd,
            env=base_env,
        )

    # --- Step 8: Prep evidence narratives ---
    rc_out = OUT / ("retrieve_compare_llm.json" if not args.skip_llm else "retrieve_compare_out.json")
    prepped_evidence = OUT / "prepped_evidence.json"
    if start <= 8:
        run(
            "Step 8/13 — Prep evidence narratives",
            [PY, str(EMPIRICAL / "prep.py"), str(rc_out), "-o", str(prepped_evidence)],
            env=base_env,
        )

    # --- Step 9: Generate review ---
    review_out = OUT / "review.json"
    if start <= 9:
        review_cmd = [
            PY, str(EMPIRICAL / "review.py"),
            str(prepped_evidence),
            "--mappings", str(MAPPINGS),
            "-o", str(review_out),
            "--pre-condensed-dump", str(OUT / "pre_condensed_rationales.json"),
        ]
        run(
            "Step 9/13 — Generate evidence-aware review",
            review_cmd,
            env=base_env,
        )

    # --- Step 10: Originality check ---
    if start <= 10:
        originality_out = OUT / "originality.json"
        originality_cmd = [
            PY, str(EMPIRICAL / "originality_check.py"),
            "--directory", str(OUT),
            "--fullmd", str(FULL_MD),
            "--kb", str(kb_dest),
            "--openalex-cache", str(OUT / "originality_openalex_cache.json"),
            "-o", str(originality_out),
            "--review", str(OUT / "review.json"),
        ]
        if args.skip_llm:
            originality_cmd.append("--skip-llm")
        run(
            f"Step 10/13 — Originality check ({'WITH LLM' if not args.skip_llm else 'skip-llm'})",
            originality_cmd,
            env=base_env,
        )

    # --- Step 11: Document screener ---
    if start <= 11:
        screener_out = OUT / "screener.json"
        screener_cmd = [
            PY, str(EMPIRICAL / "screener.py"),
            "--fullmd", str(FULL_MD),
            "--openalex-cache", str(OUT / "openalex_cache.json"),
            "--mappings", str(MAPPINGS),
            "--review", str(OUT / "review.json"),
            "-o", str(screener_out),
        ]
        if args.skip_llm:
            screener_cmd.append("--skip-llm")
        run(
            f"Step 11/13 — Document screener ({'WITH LLM' if not args.skip_llm else 'skip-llm'})",
            screener_cmd,
            env=base_env,
        )

    # --- Step 12: Unified scoring ---
    if start <= 12:
        screener_out = OUT / "screener.json"
        score_cmd = [
            PY, str(EMPIRICAL / "score.py"),
            "--review", str(review_out),
            "--prepped-evidence", str(prepped_evidence),
            "--originality", str(OUT / "originality.json"),
            "--screener", str(screener_out),
            "--mappings", str(MAPPINGS),
            "-o", str(review_out),
        ]
        if args.skip_llm:
            score_cmd.append("--skip-llm")
        run(
            f"Step 12/13 — Unified scoring ({'WITH LLM' if not args.skip_llm else 'skip-llm'})",
            score_cmd,
            env=base_env,
        )

    # --- Step 13: Evidence audit document ---
    if start <= 13:
        run(
            "Step 13/13 — Evidence audit document",
            [
                PY,
                str(EMPIRICAL / "evidence-doc.py"),
                "--directory",
                str(OUT),
                "-o",
                str(OUT / "evidence_audit.md"),
            ],
            env=base_env,
        )

    print(f"\n{'='*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"  Output dir: {OUT}")
    print(f"{'='*60}")

    if args.skip_llm:
        print()
        print("To re-run step 7+ with LLM evidence grading:")
        print(f"  python run_pipe2.py --from-step 7")


if __name__ == "__main__":
    main()
