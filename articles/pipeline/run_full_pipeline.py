#!/usr/bin/env python3
"""
Full article review pipeline: PDF URL -> read -> chunk -> route -> sub-pipeline.

Supports staged execution for resource-constrained environments where
different vLLM models must be swapped between steps.

  Stage A (OCR model):
    python run_full_pipeline.py https://example.com/paper.pdf --stop-after reader

  Stage B (text LLM):
    python run_full_pipeline.py https://example.com/paper.pdf --from-step add_data

Environment: LLM_* (review), TAGGER_* (claim tags), VISION_MODEL_* + READ_PAPER_MODEL (OCR).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from openai import OpenAI

_PIPELINE = Path(__file__).resolve().parent
_ARTICLES = _PIPELINE.parent
_REPO_ROOT = _ARTICLES.parent
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))

from llm_env import LLM_API_KEY, LLM_BASE_URL, pipeline_env, review_model  # noqa: E402
from run_layout import (  # noqa: E402
    find_run_dir,
    run_dir_for_stem,
    safe_stem,
    work_dir_for_run,
)

_CLAIM_EXTRACT = _PIPELINE / "claim-extract"
_EMPIRICAL = _PIPELINE / "empirical"
_THEORETICAL = _PIPELINE / "Theoretical-narrative"
_PROTOCOL = _PIPELINE / "Protocol-pre_results"
_PROMPTS = _PIPELINE / "prompts"

DEFAULT_OUTPUT_DIR = _REPO_ROOT / "reviews" / "articles"
PY = sys.executable

STEPS = ("fetch", "reader", "add_data", "route", "pipeline")
STEP_INDEX = {name: i for i, name in enumerate(STEPS)}


def _infer_pdf_filename(url: str, response: requests.Response) -> str:
    cd = response.headers.get("Content-Disposition", "")
    if "filename=" in cd:
        match = re.search(r'filename="?([^";]+)"?', cd)
        if match:
            return safe_stem(Path(match.group(1)).stem) + ".pdf"

    path = urlparse(url).path
    basename = unquote(Path(path).name)
    if basename.lower().endswith(".pdf"):
        return safe_stem(Path(basename).stem) + ".pdf"

    return "document.pdf"


def download_pdf(url: str, run_dir: Path) -> Path:
    """Download a PDF from a direct URL into run_dir. Returns path to saved file."""
    print(f"\n  Downloading PDF from: {url}")
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
        first_bytes = resp.content[:5]
        if first_bytes != b"%PDF-":
            print(
                f"  WARNING: Response does not appear to be a PDF (Content-Type: {content_type})",
                file=sys.stderr,
            )

    filename = _infer_pdf_filename(url, resp)
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = run_dir / filename
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"  Saved: {dest} ({dest.stat().st_size:,} bytes)")
    return dest


def run_step(label: str, cmd: list[str], *, env: dict | None = None) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  cmd: {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"  FAILED (exit {result.returncode})")
        sys.exit(result.returncode)
    print("  OK")


def extract_abstract_text(kb_path: Path) -> str | None:
    """Pull abstract chunks from the knowledge base JSONL."""
    abstract_parts = []
    with kb_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("semantic_category") == "abstract":
                text = rec.get("text", "").strip()
                if text:
                    abstract_parts.append(text)
    if abstract_parts:
        return "\n\n".join(abstract_parts)
    return None


def extract_fallback_text(fullmd_path: Path, max_chars: int = 6000) -> str:
    """First ~2000 tokens (approx max_chars characters) of full.md as fallback."""
    text = fullmd_path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"\n---\n", "\n", text)
    return text[:max_chars].strip()


def classify_article_type(
    text: str,
    model: str,
    base_url: str,
    api_key: str,
    max_retries: int = 3,
) -> str:
    """Use LLM to classify article as empirical/theoretical_narrative/protocol."""
    prompt_path = _PROMPTS / "article_router_prompt.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")

    client = OpenAI(base_url=base_url, api_key=api_key)
    valid_types = {"empirical", "theoretical_narrative", "protocol"}

    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=200,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Classify this document:\n\n{text}"},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            # Try to parse JSON from response
            json_match = re.search(r"\{[^}]+\}", raw)
            if json_match:
                parsed = json.loads(json_match.group())
                article_type = parsed.get("article_type", "").strip().lower()
                if article_type in valid_types:
                    confidence = parsed.get("confidence", "unknown")
                    reasoning = parsed.get("reasoning", "")
                    print(f"  Classification: {article_type} (confidence: {confidence})")
                    if reasoning:
                        print(f"  Reasoning: {reasoning}")
                    return article_type
            # Fallback: check if raw response contains a valid type directly
            for vt in valid_types:
                if vt in raw.lower():
                    print(f"  Classification (from raw): {vt}")
                    return vt
        except Exception as e:
            if attempt < max_retries:
                print(f"  Router attempt {attempt} failed: {e}. Retrying...")
                time.sleep(2**attempt)
            else:
                print(f"  Router failed after {max_retries} attempts: {e}", file=sys.stderr)

    print("  WARNING: Could not classify article type. Defaulting to 'empirical'.", file=sys.stderr)
    return "empirical"


def route_to_pipeline(
    article_type: str,
    work_dir: Path,
    kb_path: Path,
    model: str,
    skip_llm: bool,
    overwrite: bool,
    run_dir: Path,
) -> None:
    """Invoke the appropriate sub-pipeline orchestrator."""
    pipe_map = {
        "empirical": _EMPIRICAL / "empirical-pipe.py",
        "theoretical_narrative": _THEORETICAL / "theoretical-narrative-pipe.py",
        "protocol": _PROTOCOL / "protocol-pipe.py",
    }
    script = pipe_map[article_type]

    cmd = [
        PY,
        str(script),
        "--input-dir", str(work_dir),
        "--kb", str(kb_path),
        "--run-dir", str(run_dir),
        "--model", model,
    ]
    if skip_llm:
        cmd.append("--skip-llm")
    if overwrite:
        cmd.append("--overwrite")

    run_step(
        f"Sub-pipeline: {article_type}",
        cmd,
        env=pipeline_env(model=model),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full pipeline: PDF URL -> read -> chunk -> classify -> review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Steps in order: fetch -> reader -> add_data -> route -> pipeline

Examples:
  # Full run:
  python run_full_pipeline.py https://example.com/paper.pdf

  # OCR only (Nanonets model running):
  python run_full_pipeline.py https://example.com/paper.pdf --stop-after reader

  # Resume after model swap:
  python run_full_pipeline.py paper.pdf --from-step add_data
""",
    )
    parser.add_argument(
        "source",
        type=str,
        help="PDF URL (http/https) or local path to PDF / output folder",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Base output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Review LLM model id (default: LLM_MODEL or VALIDATOR_MODEL from env)",
    )
    parser.add_argument(
        "--from-step",
        type=str,
        default="fetch",
        choices=STEPS,
        help="Resume from this step (default: fetch)",
    )
    parser.add_argument(
        "--stop-after",
        type=str,
        default=None,
        choices=STEPS[:-1],
        help="Stop after this step (default: run all)",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Pass --skip-llm to the sub-pipeline",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output folder",
    )
    args = parser.parse_args()
    review_llm_model = args.model or review_model()

    output_dir = args.output_dir.expanduser().resolve()
    start_idx = STEP_INDEX[args.from_step]
    stop_idx = STEP_INDEX[args.stop_after] if args.stop_after else len(STEPS) - 1

    if stop_idx < start_idx:
        print("error: --stop-after must come after --from-step", file=sys.stderr)
        sys.exit(1)

    source = args.source
    is_url = source.startswith("http://") or source.startswith("https://")
    source_path = None if is_url else Path(source).expanduser().resolve()

    # Determine PDF path, run root, and steps working directory
    pdf_path: Path | None = None
    run_dir: Path | None = None
    work_dir: Path | None = None

    # --- FETCH ---
    if start_idx <= STEP_INDEX["fetch"]:
        if is_url:
            stem = safe_stem(Path(urlparse(source).path).stem or "document")
            run_dir = run_dir_for_stem(output_dir, stem)
            pdf_path = download_pdf(source, run_dir)
        elif source_path and source_path.is_file() and source_path.suffix.lower() == ".pdf":
            pdf_path = source_path
            run_dir = run_dir_for_stem(output_dir, pdf_path.stem)
            run_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n  Using local PDF: {pdf_path}")
        elif source_path and source_path.is_dir():
            run_dir = source_path
            if run_dir.name == "steps":
                run_dir = run_dir.parent
            work_dir = work_dir_for_run(run_dir)
            print(f"\n  Using existing run folder: {run_dir}")
        else:
            print(f"error: source is not a URL, PDF file, or directory: {source}", file=sys.stderr)
            sys.exit(1)
    else:
        # Resuming: locate existing run directory
        if source_path and source_path.is_dir():
            run_dir = source_path
            if run_dir.name == "steps":
                run_dir = run_dir.parent
            work_dir = work_dir_for_run(run_dir)
        elif source_path and source_path.is_file() and source_path.suffix.lower() == ".pdf":
            pdf_path = source_path
            run_dir = find_run_dir(output_dir, pdf_path.stem)
            if run_dir is None:
                print(
                    f"error: cannot find run folder for '{pdf_path.stem}' under {output_dir}",
                    file=sys.stderr,
                )
                sys.exit(1)
            work_dir = work_dir_for_run(run_dir)
        elif is_url:
            stem = safe_stem(Path(urlparse(source).path).stem or "document")
            run_dir = find_run_dir(output_dir, stem)
            if run_dir is None:
                print(
                    f"error: cannot find run folder for '{stem}' under {output_dir}. "
                    "Run fetch/reader first.",
                    file=sys.stderr,
                )
                sys.exit(1)
            work_dir = work_dir_for_run(run_dir)
        else:
            print(f"error: cannot resolve source: {source}", file=sys.stderr)
            sys.exit(1)

    if stop_idx <= STEP_INDEX["fetch"]:
        print(f"\n  Stopped after: fetch")
        if pdf_path:
            print(f"  PDF: {pdf_path}")
        if run_dir:
            print(f"  Run folder: {run_dir}")
        return

    # --- READER ---
    if start_idx <= STEP_INDEX["reader"] and stop_idx >= STEP_INDEX["reader"]:
        if pdf_path is None:
            print("error: no PDF available for reader step", file=sys.stderr)
            sys.exit(1)
        if run_dir is None:
            run_dir = run_dir_for_stem(output_dir, pdf_path.stem)
        steps_dir = run_dir / "steps"
        steps_dir.mkdir(parents=True, exist_ok=True)
        run_step(
            "Read paper (OCR -> full.md)",
            [
                PY,
                str(_PIPELINE / "read-paper.py"),
                "--pdf",
                str(pdf_path),
                "--out-dir",
                str(steps_dir),
            ],
        )
        work_dir = steps_dir
        if not (work_dir / "full.md").is_file():
            print(f"error: reader did not produce full.md in {work_dir}", file=sys.stderr)
            sys.exit(1)
        print(f"  Run directory: {run_dir}")
        print(f"  Work directory (steps): {work_dir}")

    if stop_idx <= STEP_INDEX["reader"]:
        print(f"\n  Stopped after: reader")
        if pdf_path:
            print(f"  PDF: {pdf_path}")
        if run_dir:
            print(f"  Run folder: {run_dir}")
        return

    # --- ADD_DATA ---
    if start_idx <= STEP_INDEX["add_data"] and stop_idx >= STEP_INDEX["add_data"]:
        if run_dir is None and pdf_path:
            run_dir = find_run_dir(output_dir, pdf_path.stem) or run_dir_for_stem(output_dir, pdf_path.stem)
        if work_dir is None and run_dir is not None:
            work_dir = work_dir_for_run(run_dir)
        if work_dir is None:
            print("error: cannot determine work directory for add_data step", file=sys.stderr)
            sys.exit(1)
        if run_dir is None:
            run_dir = work_dir.parent if work_dir.name == "steps" else work_dir
        if pdf_path is None:
            meta_path = work_dir / "metadata.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                candidate = Path(meta.get("pdf", ""))
                if candidate.is_file():
                    pdf_path = candidate
            if pdf_path is None:
                pdfs = list(run_dir.glob("*.pdf"))
                if len(pdfs) == 1:
                    pdf_path = pdfs[0]
                else:
                    print(
                        "error: cannot find PDF for add_data. Pass the PDF path as source.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        kb_path = work_dir / "text_knowledge_base.jsonl"
        run_step(
            "Chunk PDF (add_data -> knowledge base)",
            [
                PY,
                str(_CLAIM_EXTRACT / "add_data.py"),
                "--file", str(pdf_path),
                "-o", str(kb_path),
            ],
            env=pipeline_env(model=review_llm_model),
        )

    if stop_idx <= STEP_INDEX["add_data"]:
        print(f"\n  Stopped after: add_data")
        print(f"  Output folder: {work_dir}")
        return

    # --- ROUTE ---
    if work_dir is None:
        print("error: cannot determine work directory", file=sys.stderr)
        sys.exit(1)

    kb_path = work_dir / "text_knowledge_base.jsonl"
    fullmd_path = work_dir / "full.md"

    if not kb_path.is_file():
        print(f"error: missing knowledge base: {kb_path}", file=sys.stderr)
        sys.exit(1)
    if not fullmd_path.is_file():
        print(f"error: missing full.md: {fullmd_path}", file=sys.stderr)
        sys.exit(1)

    article_type: str | None = None
    route_file = work_dir / "article_type.json"

    if start_idx <= STEP_INDEX["route"] and stop_idx >= STEP_INDEX["route"]:
        print(f"\n{'='*60}")
        print("  Article type classification (LLM router)")
        print(f"{'='*60}")

        text = extract_abstract_text(kb_path)
        if text:
            print("  Using abstract from knowledge base")
        else:
            print("  No abstract chunks found; using first ~2000 tokens of full.md")
            text = extract_fallback_text(fullmd_path)

        article_type = classify_article_type(
            text,
            model=review_llm_model,
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
        )

        route_file.write_text(
            json.dumps({"article_type": article_type}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  Saved route decision: {route_file}")
    else:
        # Load previous routing decision
        if route_file.is_file():
            data = json.loads(route_file.read_text(encoding="utf-8"))
            article_type = data.get("article_type")
        if article_type is None:
            print("error: no routing decision found. Run the route step first.", file=sys.stderr)
            sys.exit(1)

    if stop_idx <= STEP_INDEX["route"]:
        print(f"\n  Stopped after: route")
        print(f"  Article type: {article_type}")
        print(f"  Output folder: {work_dir}")
        return

    # --- PIPELINE ---
    if start_idx <= STEP_INDEX["pipeline"] and stop_idx >= STEP_INDEX["pipeline"]:
        print(f"\n  Routing to: {article_type}")
        if run_dir is None:
            run_dir = work_dir.parent if work_dir.name == "steps" else work_dir
        route_to_pipeline(
            article_type=article_type,
            work_dir=work_dir,
            kb_path=kb_path,
            model=review_llm_model,
            skip_llm=args.skip_llm,
            overwrite=args.overwrite,
            run_dir=run_dir,
        )

    if stop_idx <= STEP_INDEX["pipeline"]:
        print(f"\n  Stopped after: pipeline")
        print(f"  Article type: {article_type}")
        print(f"  Work directory: {work_dir}")
        return

    print(f"\n{'='*60}")
    print("  Pipeline complete")
    print(f"{'='*60}")
    print(f"  Article type: {article_type}")
    print(f"  Work directory: {work_dir}")


if __name__ == "__main__":
    main()
