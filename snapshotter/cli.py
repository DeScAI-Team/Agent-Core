from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from snapshotter.compress import build_snapshot
from snapshotter.env import (
    DEFAULT_ARCHIVE,
    DEFAULT_CRAWL_DIR,
    DEFAULT_RECEIPT,
    DEFAULT_REVIEWS_DIR,
    REPO_ROOT,
    load_r2_config,
    require_bundle_dirs,
)
from snapshotter.upload import object_key, s3_uri, upload_result


def _r2_overrides_from_args(args: argparse.Namespace) -> dict:
    return {
        "endpoint": args.r2_endpoint,
        "bucket": args.r2_bucket,
        "access_key_id": args.r2_access_key_id,
        "secret_access_key": args.r2_secret_access_key,
        "prefix": args.r2_prefix,
        "region": args.r2_region,
        "presign_expiry_sec": args.presign_expiry_sec,
    }


def _try_r2_config(args: argparse.Namespace):
    try:
        return load_r2_config(**_r2_overrides_from_args(args))
    except (RuntimeError, ValueError):
        return None


def _add_r2_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "R2 (private bucket)",
        "Optional overrides; defaults come from SNAPSHOT_R2_* in repo-root .env",
    )
    group.add_argument("--r2-endpoint", default=None, help="SNAPSHOT_R2_ENDPOINT")
    group.add_argument("--r2-bucket", default=None, help="SNAPSHOT_R2_BUCKET")
    group.add_argument("--r2-access-key-id", default=None, help="SNAPSHOT_R2_ACCESS_KEY_ID")
    group.add_argument(
        "--r2-secret-access-key",
        default=None,
        help="SNAPSHOT_R2_SECRET_ACCESS_KEY",
    )
    group.add_argument("--r2-prefix", default=None, help="SNAPSHOT_R2_PREFIX")
    group.add_argument("--r2-region", default=None, help="SNAPSHOT_R2_REGION (default auto)")
    group.add_argument(
        "--presign-expiry-sec",
        type=int,
        default=None,
        metavar="SEC",
        help="Presigned GET URL lifetime; 0 disables (SNAPSHOT_PRESIGN_EXPIRY_SEC)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bundle crawlers/output + reviews into snapshot.tar.zst and upload to a private Cloudflare R2 bucket.",
    )
    parser.add_argument(
        "--crawl-dir",
        type=Path,
        default=DEFAULT_CRAWL_DIR,
        help=f"Crawl output directory (default: {DEFAULT_CRAWL_DIR})",
    )
    parser.add_argument(
        "--reviews-dir",
        type=Path,
        default=DEFAULT_REVIEWS_DIR,
        help=f"Reviews directory (default: {DEFAULT_REVIEWS_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ARCHIVE,
        help=f"Output archive path (default: {DEFAULT_ARCHIVE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print paths and R2 target only; do not build or upload",
    )
    parser.add_argument(
        "--upload-only",
        type=Path,
        metavar="ARCHIVE",
        help="Upload an existing .tar.zst without rebuilding",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Build snapshot.tar.zst locally only",
    )
    _add_r2_args(parser)
    args = parser.parse_args(argv)

    crawl_dir = args.crawl_dir.resolve()
    reviews_dir = args.reviews_dir.resolve()
    output_path = args.output.resolve()
    config = _try_r2_config(args)
    prefix = config.prefix if config else "crawl-snapshots"
    key = object_key(prefix)

    if args.dry_run:
        print(f"repo_root:     {REPO_ROOT}")
        print(f"crawl_dir:     {crawl_dir}")
        print(f"reviews_dir:   {reviews_dir}")
        print(f"output:        {output_path}")
        print(f"object_key:    {key}")
        if config:
            print(f"bucket:        {config.bucket}")
            print(f"s3_uri:        {s3_uri(config, key)}")
            print(f"endpoint:      {config.endpoint}")
            if config.presign_expiry_sec:
                print(f"presign_sec:   {config.presign_expiry_sec}")
            else:
                print("presign:       disabled (upload only; use AWS CLI/SDK to download)")
        else:
            print("r2_config:     (set SNAPSHOT_R2_* in .env or pass --r2-* flags)")
        return 0

    tar_size: int | None = None

    if args.upload_only is not None:
        archive_path = args.upload_only.resolve()
        if not archive_path.is_file():
            print(f"Error: archive not found: {archive_path}", file=sys.stderr)
            return 1
    else:
        try:
            require_bundle_dirs(crawl_dir, reviews_dir)
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        try:
            tar_size = build_snapshot(
                crawl_dir=crawl_dir,
                reviews_dir=reviews_dir,
                output_path=output_path,
            )
        except ImportError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        archive_path = output_path
        print(f"Wrote {archive_path} (uncompressed tar ~{tar_size} bytes)")

        if args.no_upload:
            return 0

    if args.no_upload:
        return 0

    try:
        config = load_r2_config(**_r2_overrides_from_args(args))
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        result = upload_result(archive_path, config, key)
    except Exception as exc:
        print(f"Error: R2 upload failed: {exc}", file=sys.stderr)
        return 1

    receipt = {
        "archive_path": str(archive_path),
        "crawl_dir": str(crawl_dir),
        "reviews_dir": str(reviews_dir),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "uncompressed_tar_bytes": tar_size,
        **result,
    }
    DEFAULT_RECEIPT.write_text(
        json.dumps(receipt, indent=2) + "\n",
        encoding="utf-8",
    )

    print(result["s3_uri"])
    if result.get("presigned_url"):
        print(result["presigned_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
