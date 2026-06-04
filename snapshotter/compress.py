from __future__ import annotations

import tarfile
from pathlib import Path

from snapshotter.env import TMP_TAR, require_bundle_dirs

try:
    import zstd
except ImportError as exc:
    raise ImportError(
        "Python package 'zstd' is required. Activate Agent venv and run: pip install zstd"
    ) from exc

def build_snapshot(
    *,
    crawl_dir: Path,
    reviews_dir: Path,
    output_path: Path,
    tmp_tar: Path = TMP_TAR,
) -> int:
    """Create snapshot.tar.zst from crawl + reviews dirs. Returns uncompressed tar size."""
    require_bundle_dirs(crawl_dir, reviews_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(tmp_tar, "w") as tar:
            tar.add(crawl_dir, arcname="crawlers/output")
            tar.add(reviews_dir, arcname="reviews")

        tar_size = tmp_tar.stat().st_size
        raw_tar = tmp_tar.read_bytes()
        output_path.write_bytes(zstd.compress(raw_tar))
    finally:
        tmp_tar.unlink(missing_ok=True)

    return tar_size
