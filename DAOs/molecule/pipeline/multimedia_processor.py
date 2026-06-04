#!/usr/bin/env python3
"""Multimedia router: inventory dataroom files and build a JSON+MD bundle.

Reads manifest.json + downloaded files from an IPNFT directory, routes each file
by type, and writes structured outputs under {output_dir}/bundle/:

  bundle/pdf/<stem>.json         — sectioned OCR text + metadata
  bundle/images/<stem>.json      — vision caption + labels
  bundle/videos/<stem>/frames.jsonl — per-frame caption + audio transcript
  bundle/text/<stem>.md          — extracted plaintext

Crawler-produced *.md files in the IPNFT directory itself are NOT copied here;
the chunk step reads them in place.

Env:
  VLLM_BASE_URL, VLLM_API_KEY      — vision model endpoint
  READ_PAPER_MODEL                 — vision/OCR model (default: nanonets/Nanonets-OCR2-3B)
  WHISPER_CPP_BIN, WHISPER_MODEL_PATH — whisper.cpp for video audio
  ffmpeg / ffprobe on PATH

Usage:
  python multimedia_processor.py \\
    --ipnft-dir output/molecule/ipnfts/BeeARD \\
    --output-dir reviews/DAOs/BeeARD
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

_PIPELINE_DIR = Path(__file__).resolve().parent
_DAO_ROOT = _PIPELINE_DIR.parent
_REPO_ROOT = _DAO_ROOT.parent.parent

if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from file_inventory import inventory_files  # noqa: E402
from routes import process_image, process_pdf, process_text, process_video  # noqa: E402

if load_dotenv:
    _env_path = _REPO_ROOT / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)

VLLM_BASE_URL = (
    os.environ.get("VISION_MODEL_URL")
    or os.environ.get("VLLM_BASE_URL")
    or "http://localhost:8000/v1"
)
VLLM_API_KEY = (
    os.environ.get("VISION_MODEL_API_KEY")
    or os.environ.get("VLLM_API_KEY")
    or "none"
)
READ_PAPER_MODEL = os.environ.get("READ_PAPER_MODEL", "nanonets/Nanonets-OCR2-3B")


def _load_env() -> None:
    if load_dotenv:
        env_path = _REPO_ROOT / ".env"
        if load_dotenv and env_path.exists():
            load_dotenv(env_path)


def _ensure_bundle_dirs(bundle_dir: Path) -> None:
    for sub in ("pdf", "images", "videos", "text"):
        (bundle_dir / sub).mkdir(parents=True, exist_ok=True)


def process_ipnft(
    ipnft_dir: Path,
    bundle_dir: Path,
    *,
    vision_model: str | None = None,
    vision_base_url: str | None = None,
    vision_api_key: str | None = None,
    skip_vision: bool = False,
    keep_temp: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Process all inventory files into bundle_dir."""
    ipnft_dir = ipnft_dir.resolve()
    bundle_dir = bundle_dir.resolve()
    _ensure_bundle_dirs(bundle_dir)

    v_model = vision_model or READ_PAPER_MODEL
    v_url = vision_base_url or VLLM_BASE_URL
    v_key = vision_api_key or VLLM_API_KEY

    vision_client = OpenAI(base_url=v_url, api_key=v_key)

    files = inventory_files(ipnft_dir)
    results: dict[str, Any] = {
        "ipnft_dir": str(ipnft_dir),
        "bundle_dir": str(bundle_dir),
        "processed": [],
        "failed": [],
        "skipped_vision": [],
        "total_inventory": len(files),
    }

    for i, entry in enumerate(files, 1):
        source: Path = entry["path"]
        route = entry["route"]
        print(f"  [{i}/{len(files)}] {source.name} ({route})")

        t0 = time.time()
        try:
            if route == "pdf":
                if skip_vision:
                    results["skipped_vision"].append({"filename": source.name, "route": route})
                    continue
                out = process_pdf(
                    source,
                    bundle_dir,
                    vision_client=vision_client,
                    vision_model=v_model,
                    overwrite=overwrite,
                )
            elif route == "image":
                if skip_vision:
                    results["skipped_vision"].append({"filename": source.name, "route": route})
                    continue
                out = process_image(
                    source,
                    bundle_dir,
                    vision_client=vision_client,
                    vision_model=v_model,
                    overwrite=overwrite,
                )
            elif route == "video":
                if skip_vision:
                    results["skipped_vision"].append({"filename": source.name, "route": route})
                    continue
                out = process_video(
                    source,
                    bundle_dir,
                    vision_client=vision_client,
                    vision_model=v_model,
                    keep_temp=keep_temp,
                    overwrite=overwrite,
                )
            elif route == "text":
                out = process_text(
                    source,
                    bundle_dir,
                    content_type=entry.get("content_type", ""),
                    overwrite=overwrite,
                )
            else:
                continue

            elapsed = time.time() - t0
            results["processed"].append({
                "filename": source.name,
                "route": route,
                "elapsed_sec": round(elapsed, 1),
                **out,
            })
            if out.get("skipped"):
                print(f"    skipped (exists): {out.get('output_path')}")
            else:
                print(f"    -> {out.get('output_path', out)}")

        except Exception as exc:
            elapsed = time.time() - t0
            err = str(exc)
            results["failed"].append({
                "filename": source.name,
                "route": route,
                "error": err,
                "elapsed_sec": round(elapsed, 1),
            })
            print(f"    FAILED: {err}")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ipnft_dir": str(ipnft_dir),
        "bundle_dir": str(bundle_dir),
        "summary": {
            "inventory": len(files),
            "processed": len(results["processed"]),
            "failed": len(results["failed"]),
            "skipped_vision": len(results["skipped_vision"]),
        },
        "entries": results["processed"] + [
            {**f, "status": "failed"} for f in results["failed"]
        ],
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser(description="Multimedia router for IPNFT dataroom files")
    parser.add_argument("--ipnft-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="DAO output dir; bundle written to {output-dir}/bundle/",
    )
    parser.add_argument("--vision-model", type=str, default=None)
    parser.add_argument("--skip-vision", action="store_true", help="Skip PDF/image/video vision routes")
    parser.add_argument("--keep-temp", action="store_true", help="Keep video frame temp files")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    ipnft_dir = args.ipnft_dir.resolve()
    if not ipnft_dir.is_dir():
        parser.error(f"Not a directory: {ipnft_dir}")

    bundle_dir = args.output_dir.resolve() / "bundle"
    print(f"\n[multimedia] {ipnft_dir.name} -> {bundle_dir}")

    results = process_ipnft(
        ipnft_dir,
        bundle_dir,
        vision_model=args.vision_model,
        skip_vision=args.skip_vision,
        keep_temp=args.keep_temp,
        overwrite=args.overwrite,
    )

    print(
        f"\n[multimedia] Done: {len(results['processed'])} processed, "
        f"{len(results['failed'])} failed"
    )
    out_path = args.output_dir.resolve() / "bundle_results.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"[multimedia] Wrote {out_path}")


if __name__ == "__main__":
    main()
