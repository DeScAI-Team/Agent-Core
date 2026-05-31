from __future__ import annotations

import argparse
import sys

from uploader.runner import RECIPE_NAMES, run_recipe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified Arweave uploader for Review-Generator pipeline outputs.",
    )
    parser.add_argument(
        "--recipe",
        required=True,
        choices=RECIPE_NAMES,
        help="Upload recipe matching the pipeline that produced the outputs",
    )
    parser.add_argument(
        "--dir",
        type=str,
        default=None,
        help="Input directory containing files to upload (review recipes)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory for upload_metadata.json (defaults to --dir or file parent)",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Input file to upload (crawl-log recipe)",
    )
    parser.add_argument(
        "--crawl-date",
        type=str,
        default=None,
        help="Crawl-Date tag override for crawl-log uploads",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip steps already recorded in output-dir upload_metadata.json",
    )
    args = parser.parse_args()

    try:
        result = run_recipe(
            args.recipe,
            dir=args.dir,
            output_dir=args.output_dir,
            file=args.file,
            crawl_date=args.crawl_date,
            resume=args.resume,
        )
    except (ValueError, RuntimeError) as e:
        print(f"\n  Error: {e}", file=sys.stderr)
        return 1

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
