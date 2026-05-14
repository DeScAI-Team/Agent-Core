#!/usr/bin/env python3
"""
Sequential Arweave uploader for article review outputs.

Uploads evidence_audit.md, review.json, and overview.json in sequence,
automatically linking each subsequent file to the previous upload via transaction IDs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Locate the article_uploader directory
_UPLOADER_DIR = Path(__file__).resolve().parent
_UPLOAD_CLI = _UPLOADER_DIR / "upload_cli.js"
_REPO_ROOT = _UPLOADER_DIR.parent.parent


def _run_node_upload(
    file_path: Path,
    tags: list[dict[str, str]],
) -> dict[str, Any]:
    """
    Call the Node.js upload CLI with tags and return parsed result.
    
    Args:
        file_path: Path to file to upload
        tags: List of tag dictionaries with 'name' and 'value' keys
        
    Returns:
        Dict with 'success', 'txId', 'webUrl', and optionally 'error'
    """
    cmd = ["node", str(_UPLOAD_CLI), str(file_path)]
    
    if tags:
        tags_json = json.dumps(tags)
        cmd.extend(["--tags", tags_json])
    
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
            cwd=str(_UPLOADER_DIR),
        )
        
        # Parse JSON from stdout
        try:
            upload_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            # If stdout isn't valid JSON, capture whatever we got
            return {
                "success": False,
                "error": f"Invalid JSON response. stdout: {result.stdout[:200]}, stderr: {result.stderr[:200]}",
            }
        
        return upload_result
        
    except subprocess.SubprocessError as e:
        return {
            "success": False,
            "error": f"Subprocess error: {e}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {e}",
        }


def _build_tags(
    doctype: str,
    research_name: str | None = None,
    review_date: str | None = None,
) -> list[dict[str, str]]:
    """
    Build Arweave GraphQL tags for upload.
    
    Args:
        doctype: Type of document - 'evidence', 'review', or 'overview'
        research_name: Name of the research from JSON
        review_date: Date of review from JSON
        
    Returns:
        List of tag dictionaries
    """
    tags = [
        {"name": "doctype", "value": doctype},
        {"name": "platform", "value": ""},  # Reserved for future use
    ]
    
    if research_name:
        tags.append({"name": "research_name", "value": research_name})
    
    if review_date:
        tags.append({"name": "review_date", "value": review_date})
    
    return tags


def _create_modified_json_with_link(
    json_path: Path,
    append_text: str,
) -> Path:
    """
    Create a temporary modified version of a JSON file with appended text to review_statement.
    
    Args:
        json_path: Original JSON file path
        append_text: Text to append to review_statement field
        
    Returns:
        Path to temporary modified JSON file
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Append text to review_statement
    if "review_statement" in data:
        data["review_statement"] = data["review_statement"] + append_text
    else:
        raise ValueError(f"No 'review_statement' field found in {json_path}")
    
    # Create temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="upload_", text=True)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        os.close(temp_fd)
        raise
    
    return Path(temp_path)


def _save_upload_metadata(
    output_dir: Path,
    metadata: dict[str, Any],
) -> None:
    """
    Save upload metadata to upload_metadata.json in the output directory.
    
    Args:
        output_dir: Directory containing the uploaded files
        metadata: Metadata dictionary to save
    """
    metadata_path = output_dir / "upload_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print(f"\n  ✓ Saved upload metadata: {metadata_path}")


def run_upload_sequence(
    output_dir: Path | str,
    resume: bool = False,
) -> dict[str, Any]:
    """
    Run the full upload sequence for an article output directory.
    
    Args:
        output_dir: Path to the article output directory containing evidence_audit.md,
                   review.json, and overview.json
        resume: If True, attempt to resume from a failed upload using existing metadata
        
    Returns:
        Dictionary containing upload results and metadata
    """
    output_dir = Path(output_dir).resolve()
    
    # Check required files exist
    evidence_path = output_dir / "evidence_audit.md"
    review_path = output_dir / "review.json"
    overview_path = output_dir / "overview.json"
    metadata_path = output_dir / "upload_metadata.json"
    
    missing_files = []
    if not evidence_path.is_file():
        missing_files.append("evidence_audit.md")
    if not review_path.is_file():
        missing_files.append("review.json")
    if not overview_path.is_file():
        missing_files.append("overview.json")
    
    if missing_files:
        error_msg = f"Missing required files in {output_dir}: {', '.join(missing_files)}"
        print(f"\n  ✗ Error: {error_msg}", file=sys.stderr)
        return {"success": False, "error": error_msg}
    
    # Initialize metadata structure
    metadata = {
        "upload_date": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "evidence_audit": {},
        "review": {},
        "overview": {},
    }
    
    # Load review.json to get research_name and review_date
    try:
        with open(review_path, "r", encoding="utf-8") as f:
            review_data = json.load(f)
        research_name = review_data.get("research_name", "")
        review_date = review_data.get("review_date", "")
    except Exception as e:
        error_msg = f"Failed to read review.json: {e}"
        print(f"\n  ✗ Error: {error_msg}", file=sys.stderr)
        return {"success": False, "error": error_msg}
    
    # Check if we're resuming from a failed upload
    start_step = 0
    if resume and metadata_path.is_file():
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                existing_metadata = json.load(f)
            
            # Determine where to resume
            if existing_metadata.get("evidence_audit", {}).get("txid"):
                start_step = 1
                metadata["evidence_audit"] = existing_metadata["evidence_audit"]
                print(f"\n  → Resuming: evidence_audit already uploaded")
            
            if existing_metadata.get("review", {}).get("txid"):
                start_step = 2
                metadata["review"] = existing_metadata["review"]
                print(f"\n  → Resuming: review already uploaded")
            
            if existing_metadata.get("overview", {}).get("txid"):
                start_step = 3
                metadata["overview"] = existing_metadata["overview"]
                print(f"\n  → Resuming: overview already uploaded")
        except Exception as e:
            print(f"\n  ! Warning: Could not load existing metadata: {e}")
    
    print(f"\n{'='*60}")
    print(f"  Arweave Upload Sequence")
    print(f"{'='*60}")
    print(f"  Output directory: {output_dir}")
    print(f"  Research: {research_name}")
    print(f"  Date: {review_date}")
    
    # Step 1: Upload evidence_audit.md
    if start_step <= 0:
        print(f"\n{'='*60}")
        print(f"  Step 1/3: Upload evidence_audit.md")
        print(f"{'='*60}")
        
        tags = _build_tags("evidence", research_name, review_date)
        result = _run_node_upload(evidence_path, tags)
        
        if not result.get("success"):
            error_msg = result.get("error", "Unknown error")
            print(f"\n  ✗ Upload failed: {error_msg}", file=sys.stderr)
            metadata["evidence_audit"] = {"error": error_msg}
            _save_upload_metadata(output_dir, metadata)
            return {"success": False, "error": error_msg, "metadata": metadata}
        
        evidence_txid = result["txId"]
        evidence_url = result["webUrl"]
        
        metadata["evidence_audit"] = {
            "txid": evidence_txid,
            "url": evidence_url,
            "tags": tags,
        }
        
        print(f"\n  ✓ Evidence audit uploaded successfully!")
        print(f"    TX ID: {evidence_txid}")
        print(f"    URL: {evidence_url}")
        
        # Save intermediate metadata
        _save_upload_metadata(output_dir, metadata)
    else:
        evidence_txid = metadata["evidence_audit"]["txid"]
        evidence_url = metadata["evidence_audit"]["url"]
    
    # Step 2: Upload modified review.json with evidence link
    if start_step <= 1:
        print(f"\n{'='*60}")
        print(f"  Step 2/3: Upload review.json (with evidence link)")
        print(f"{'='*60}")
        
        # Create modified review with evidence link
        append_text = f" Full evidence audit is available at arweave.net/{evidence_txid}"
        try:
            modified_review_path = _create_modified_json_with_link(review_path, append_text)
        except Exception as e:
            error_msg = f"Failed to create modified review.json: {e}"
            print(f"\n  ✗ Error: {error_msg}", file=sys.stderr)
            metadata["review"] = {"error": error_msg}
            _save_upload_metadata(output_dir, metadata)
            return {"success": False, "error": error_msg, "metadata": metadata}
        
        try:
            tags = _build_tags("review", research_name, review_date)
            result = _run_node_upload(modified_review_path, tags)
            
            if not result.get("success"):
                error_msg = result.get("error", "Unknown error")
                print(f"\n  ✗ Upload failed: {error_msg}", file=sys.stderr)
                metadata["review"] = {"error": error_msg}
                _save_upload_metadata(output_dir, metadata)
                return {"success": False, "error": error_msg, "metadata": metadata}
            
            review_txid = result["txId"]
            review_url = result["webUrl"]
            
            metadata["review"] = {
                "txid": review_txid,
                "url": review_url,
                "descai_url": f"https://descai.net/review/{review_txid}",
                "tags": tags,
            }
            
            print(f"\n  ✓ Review uploaded successfully!")
            print(f"    TX ID: {review_txid}")
            print(f"    URL: {review_url}")
            print(f"    DeScAi URL: https://descai.net/review/{review_txid}")
            
            # Save intermediate metadata
            _save_upload_metadata(output_dir, metadata)
        finally:
            # Clean up temporary file
            if modified_review_path.exists():
                modified_review_path.unlink()
    else:
        review_txid = metadata["review"]["txid"]
        review_url = metadata["review"]["url"]
    
    # Step 3: Upload modified overview.json with review link
    if start_step <= 2:
        print(f"\n{'='*60}")
        print(f"  Step 3/3: Upload overview.json (with review link)")
        print(f"{'='*60}")
        
        # Create modified overview with review link
        append_text = f" Full review available at descai.net/review/{review_txid}"
        try:
            modified_overview_path = _create_modified_json_with_link(overview_path, append_text)
        except Exception as e:
            error_msg = f"Failed to create modified overview.json: {e}"
            print(f"\n  ✗ Error: {error_msg}", file=sys.stderr)
            metadata["overview"] = {"error": error_msg}
            _save_upload_metadata(output_dir, metadata)
            return {"success": False, "error": error_msg, "metadata": metadata}
        
        try:
            tags = _build_tags("overview", research_name, review_date)
            result = _run_node_upload(modified_overview_path, tags)
            
            if not result.get("success"):
                error_msg = result.get("error", "Unknown error")
                print(f"\n  ✗ Upload failed: {error_msg}", file=sys.stderr)
                metadata["overview"] = {"error": error_msg}
                _save_upload_metadata(output_dir, metadata)
                return {"success": False, "error": error_msg, "metadata": metadata}
            
            overview_txid = result["txId"]
            overview_url = result["webUrl"]
            
            metadata["overview"] = {
                "txid": overview_txid,
                "url": overview_url,
                "tags": tags,
            }
            
            print(f"\n  ✓ Overview uploaded successfully!")
            print(f"    TX ID: {overview_txid}")
            print(f"    URL: {overview_url}")
            
            # Save final metadata
            _save_upload_metadata(output_dir, metadata)
        finally:
            # Clean up temporary file
            if modified_overview_path.exists():
                modified_overview_path.unlink()
    
    print(f"\n{'='*60}")
    print(f"  Upload Sequence Complete!")
    print(f"{'='*60}")
    print(f"  Evidence: {metadata['evidence_audit']['url']}")
    print(f"  Review: {metadata['review']['descai_url']}")
    print(f"  Overview: {metadata['overview']['url']}")
    
    return {"success": True, "metadata": metadata}


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Upload article review outputs to Arweave in sequence with automatic linking.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Path to article output directory containing evidence_audit.md, review.json, and overview.json",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from a failed upload using existing upload_metadata.json",
    )
    args = parser.parse_args()
    
    result = run_upload_sequence(args.output_dir, resume=args.resume)
    
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
