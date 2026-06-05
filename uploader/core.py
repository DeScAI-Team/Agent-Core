from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uploader.crawl_log import DEFAULT_CRAWL_LOG_PATH, apply_reviewed_mark
from uploader.recipes import Recipe, UploadStep, get_recipe

UPLOADER_DIR = Path(__file__).resolve().parent
REPO_ROOT = UPLOADER_DIR.parent
NODE_CLI = UPLOADER_DIR / "arweaveServiceCLI.js"
METADATA_FILENAME = "upload_metadata.json"


def repo_root() -> Path:
    return REPO_ROOT


def load_root_env() -> None:
    """Load repo-root .env without overriding existing shell variables."""
    env_path = REPO_ROOT / ".env"
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


def require_wallet_env() -> tuple[str, Path]:
    """Validate AGENT_WALLET and PATH_TO_KEYFILE from repo-root .env."""
    load_root_env()
    agent_wallet = os.environ.get("AGENT_WALLET", "").strip()
    keyfile_raw = os.environ.get("PATH_TO_KEYFILE", "").strip()
    if not agent_wallet:
        raise RuntimeError("Missing AGENT_WALLET in repo-root .env")
    if not keyfile_raw:
        raise RuntimeError("Missing PATH_TO_KEYFILE in repo-root .env")
    keyfile_path = Path(keyfile_raw)
    if not keyfile_path.is_absolute():
        keyfile_path = REPO_ROOT / keyfile_path
    keyfile_path = keyfile_path.resolve()
    if not keyfile_path.is_file():
        raise RuntimeError(f"Keyfile not found: {keyfile_path}")
    os.environ["PATH_TO_KEYFILE"] = str(keyfile_path)
    return agent_wallet, keyfile_path


def run_node_upload(file_path: Path, tags: list[dict[str, str]]) -> dict[str, Any]:
    """Call arweaveServiceCLI.js with --json and return parsed result."""
    cmd = ["node", str(NODE_CLI), str(file_path), "--json"]
    for tag in tags:
        cmd.extend(["--tag", f"{tag['name']}={tag['value']}"])

    print(f"\n  Uploading: {file_path.name}")
    if tags:
        print(f"  Tags: {json.dumps(tags, indent=2)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(UPLOADER_DIR),
            env=os.environ.copy(),
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            return {
                "success": False,
                "error": f"Empty CLI response. stderr: {(result.stderr or '')[:200]}",
            }
        try:
            upload_result = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": (
                    f"Invalid JSON response. stdout: {stdout[:200]}, "
                    f"stderr: {(result.stderr or '')[:200]}"
                ),
            }
        if not upload_result.get("success") and result.returncode != 0:
            upload_result.setdefault("error", upload_result.get("error") or "Upload failed")
        return upload_result
    except subprocess.SubprocessError as e:
        return {"success": False, "error": f"Subprocess error: {e}"}


def _insert_after_key(data: dict[str, Any], after: str, key: str, value: str) -> dict[str, Any]:
    """Return new dict with key/value inserted immediately after after (or at end)."""
    if key in data:
        data = {k: v for k, v in data.items() if k != key}
    ordered: dict[str, Any] = {}
    inserted = False
    for k, v in data.items():
        ordered[k] = v
        if k == after:
            ordered[key] = value
            inserted = True
    if not inserted:
        ordered[key] = value
    return ordered


def inject_evidence_audit(json_path: Path, evidence_txid: str) -> Path:
    """Return a temp copy of json_path with evidence_audit inserted after review_date."""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {json_path}")

    url = f"https://arweave.net/{evidence_txid}"
    if "review_date" in data:
        data = _insert_after_key(data, "review_date", "evidence_audit", url)
    elif "compound_token" in data:
        data = _insert_after_key(data, "compound_token", "evidence_audit", url)
    elif "research_name" in data:
        data = _insert_after_key(data, "research_name", "evidence_audit", url)
    else:
        first_key = next(iter(data), None)
        if first_key is None:
            data["evidence_audit"] = url
        else:
            data = _insert_after_key(data, first_key, "evidence_audit", url)

    temp_fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="upload_", text=True)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        os.close(temp_fd)
        raise
    return Path(temp_path)


def save_metadata(output_dir: Path, metadata: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / METADATA_FILENAME
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved upload metadata: {metadata_path}")


def resolve_review_json(input_dir: Path) -> Path:
    """Find review JSON in compounds layout: *-review.json or review.json."""
    candidates = sorted(input_dir.glob("*-review.json"))
    if candidates:
        return candidates[0]
    review = input_dir / "review.json"
    if review.is_file():
        return review
    raise FileNotFoundError(
        f"No review JSON found in {input_dir} (expected *-review.json or review.json)"
    )


def resolve_input_files(recipe: Recipe, input_dir: Path) -> dict[str, Path]:
    """Map recipe file keys to resolved paths under input_dir."""
    resolved: dict[str, Path] = {}
    for key, filename in recipe.file_map.items():
        if filename == "__review_json__":
            path = resolve_review_json(input_dir)
        else:
            path = input_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing required file: {path}")
        resolved[key] = path
    return resolved


def read_tag_context(recipe: Recipe, files: dict[str, Path]) -> dict[str, Any]:
    review_path = files[recipe.review_json_key]
    with open(review_path, encoding="utf-8") as f:
        data = json.load(f)
    ctx: dict[str, Any] = {
        "review_date": data.get("review_date", ""),
        "research_name": data.get("research_name", ""),
    }
    if recipe.name == "proposal":
        ctx["name"] = data.get("research_name", data.get("proposal_name", ""))
    elif recipe.name == "dao":
        ctx["dao_name"] = (
            data.get("research_dao")
            or data.get("ipnft_symbol")
            or data.get("research_name", "")
        )
    elif recipe.name == "compounds":
        ctx["compound_name"] = data.get("compound_token") or data.get("research_name", "")
        ctx["review_file"] = str(review_path)
        ctx["evidence_file"] = str(files["evidence"])
    return ctx


def _step_entry(result: dict[str, Any], tags: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "txid": result["txId"],
        "url": result["webUrl"],
        "tags": tags,
    }


def _resume_start_step(existing: dict[str, Any], steps: list[UploadStep]) -> int:
    for idx, step in enumerate(steps):
        block = existing.get(step.metadata_key, {})
        if not block.get("txid"):
            return idx
    return len(steps)


def run_sequential_upload(
    recipe: Recipe,
    input_dir: Path,
    output_dir: Path,
    *,
    resume: bool = False,
    crawl_log_path: Path | None = None,
) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    require_wallet_env()

    try:
        files = resolve_input_files(recipe, input_dir)
    except FileNotFoundError as e:
        msg = str(e)
        print(f"\n  Error: {msg}", file=sys.stderr)
        return {"success": False, "error": msg}

    try:
        tag_ctx = read_tag_context(recipe, files)
    except Exception as e:
        msg = f"Failed to read review JSON for tags: {e}"
        print(f"\n  Error: {msg}", file=sys.stderr)
        return {"success": False, "error": msg}

    metadata: dict[str, Any] = {
        "upload_date": datetime.now(timezone.utc).isoformat(),
        **(recipe.metadata_extra(tag_ctx, str(input_dir)) if recipe.metadata_extra else {}),
    }
    for step in recipe.steps:
        metadata[step.metadata_key] = {}

    metadata_path = output_dir / METADATA_FILENAME
    start_step = 0
    if resume and metadata_path.is_file():
        try:
            with open(metadata_path, encoding="utf-8") as f:
                existing = json.load(f)
            start_step = _resume_start_step(existing, recipe.steps)
            for step in recipe.steps:
                if existing.get(step.metadata_key, {}).get("txid"):
                    metadata[step.metadata_key] = existing[step.metadata_key]
                    print(f"\n  Resuming: {step.metadata_key} already uploaded")
        except Exception as e:
            print(f"\n  Warning: Could not load existing metadata: {e}")

    txids: dict[str, str] = {}
    if metadata.get("evidence_audit", {}).get("txid"):
        txids["evidence_txid"] = metadata["evidence_audit"]["txid"]
    if metadata.get("review", {}).get("txid"):
        txids["review_txid"] = metadata["review"]["txid"]

    print(f"\n{'=' * 60}")
    print(f"  Arweave Upload — {recipe.name}")
    print(f"{'=' * 60}")
    print(f"  Input dir:  {input_dir}")
    print(f"  Output dir: {output_dir}")

    for step_idx, step in enumerate(recipe.steps):
        if step_idx < start_step:
            continue

        print(f"\n{'=' * 60}")
        print(f"  {step.label}")
        print(f"{'=' * 60}")

        source_path = files[step.file_key]
        upload_path = source_path
        temp_path: Path | None = None

        if step.inject_evidence_audit:
            evidence_txid = txids.get("evidence_txid", "")
            if not evidence_txid:
                msg = "Missing evidence txid for evidence_audit field injection"
                print(f"\n  Error: {msg}", file=sys.stderr)
                metadata[step.metadata_key] = {"error": msg}
                save_metadata(output_dir, metadata)
                return {"success": False, "error": msg, "metadata": metadata}
            try:
                temp_path = inject_evidence_audit(source_path, evidence_txid)
                upload_path = temp_path
            except Exception as e:
                msg = f"Failed to create modified JSON: {e}"
                print(f"\n  Error: {msg}", file=sys.stderr)
                metadata[step.metadata_key] = {"error": msg}
                save_metadata(output_dir, metadata)
                return {"success": False, "error": msg, "metadata": metadata}

        tags = recipe.tag_builder(step.doctype, tag_ctx)
        result = run_node_upload(upload_path, tags)

        if temp_path and temp_path.exists():
            temp_path.unlink()

        if not result.get("success"):
            err = result.get("error", "Unknown error")
            print(f"\n  Upload failed: {err}", file=sys.stderr)
            metadata[step.metadata_key] = {"error": err}
            save_metadata(output_dir, metadata)
            return {"success": False, "error": err, "metadata": metadata}

        entry = _step_entry(result, tags)
        if step.metadata_key == "review":
            entry["descai_url"] = f"https://descai.net/review/{result['txId']}"
        metadata[step.metadata_key] = entry

        if step.metadata_key == "evidence_audit":
            txids["evidence_txid"] = result["txId"]
        elif step.metadata_key == "review":
            txids["review_txid"] = result["txId"]

        print(f"\n  Upload successful!")
        print(f"    TX ID: {result['txId']}")
        print(f"    URL:   {result['webUrl']}")
        if entry.get("descai_url"):
            print(f"    DeScAi: {entry['descai_url']}")

        save_metadata(output_dir, metadata)

    print(f"\n{'=' * 60}")
    print(f"  Upload Sequence Complete!")
    print(f"{'=' * 60}")

    crawl_log_marked = apply_reviewed_mark(
        recipe,
        input_dir,
        tag_ctx,
        crawl_log_path=crawl_log_path or DEFAULT_CRAWL_LOG_PATH,
    )
    return {
        "success": True,
        "metadata": metadata,
        "crawl_log_marked": crawl_log_marked,
    }


def run_crawl_log_upload(
    file_path: Path,
    output_dir: Path,
    *,
    crawl_date: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    file_path = file_path.resolve()
    output_dir = output_dir.resolve()
    require_wallet_env()

    if not file_path.is_file():
        msg = f"File not found: {file_path}"
        print(f"\n  Error: {msg}", file=sys.stderr)
        return {"success": False, "error": msg}

    if crawl_date is None:
        try:
            with open(file_path, encoding="utf-8") as f:
                log_data = json.load(f)
            crawl_date = log_data.get("updatedAt") or datetime.now(timezone.utc).isoformat()
        except Exception:
            crawl_date = datetime.now(timezone.utc).isoformat()

    metadata_path = output_dir / METADATA_FILENAME
    prior_metadata: dict[str, Any] = {}
    if metadata_path.is_file():
        try:
            with open(metadata_path, encoding="utf-8") as f:
                prior_metadata = json.load(f)
        except Exception as e:
            print(f"\n  Warning: Could not load existing metadata: {e}")

    if resume and prior_metadata.get("crawl_log", {}).get("txid"):
        print("\n  Resuming: crawl-log already uploaded")
        return {"success": True, "metadata": prior_metadata}

    tags = [
        {"name": "doctype", "value": "crawllog"},
        {"name": "Crawl-Date", "value": crawl_date},
        {"name": "Content-Type", "value": "application/json"},
    ]

    print(f"\n{'=' * 60}")
    print("  Arweave Upload — crawl-log")
    print(f"{'=' * 60}")
    print(f"  File:       {file_path}")
    print(f"  Output dir: {output_dir}")

    result = run_node_upload(file_path, tags)
    if not result.get("success"):
        err = result.get("error", "Unknown error")
        metadata = {
            "upload_date": datetime.now(timezone.utc).isoformat(),
            "uploaded_file": str(file_path),
            "crawl_log": {"error": err},
        }
        save_metadata(output_dir, metadata)
        return {"success": False, "error": err, "metadata": metadata}

    checkpoint_entry = {
        "txid": result["txId"],
        "url": result["webUrl"],
        "tags": tags,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    checkpoints = list(prior_metadata.get("crawl_log_checkpoints", []))
    checkpoints.append(checkpoint_entry)
    metadata = {
        **{k: v for k, v in prior_metadata.items() if k not in ("crawl_log", "crawl_log_checkpoints")},
        "upload_date": datetime.now(timezone.utc).isoformat(),
        "uploaded_file": str(file_path),
        "crawl_log": {
            "txid": result["txId"],
            "url": result["webUrl"],
            "tags": tags,
        },
        "crawl_log_checkpoints": checkpoints,
    }
    save_metadata(output_dir, metadata)
    print(f"\n  Upload successful!")
    print(f"    TX ID: {result['txId']}")
    print(f"    URL:   {result['webUrl']}")
    return {"success": True, "metadata": metadata}
