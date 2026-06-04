from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SNAPSHOTTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SNAPSHOTTER_DIR.parent

DEFAULT_CRAWL_DIR = REPO_ROOT / "crawlers" / "output"
DEFAULT_REVIEWS_DIR = REPO_ROOT / "reviews"
DEFAULT_ARCHIVE = REPO_ROOT / "snapshot.tar.zst"
DEFAULT_RECEIPT = REPO_ROOT / "snapshot-receipt.json"
TMP_TAR = REPO_ROOT / ".snapshot-tmp.tar"

DEFAULT_PRESIGN_EXPIRY_SEC = 7 * 24 * 3600  # 7 days


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


@dataclass(frozen=True)
class R2Config:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    prefix: str
    region: str
    presign_expiry_sec: int


def require_bundle_dirs(crawl_dir: Path, reviews_dir: Path) -> None:
    for label, path in (("crawl", crawl_dir), ("reviews", reviews_dir)):
        if not path.is_dir():
            raise FileNotFoundError(f"Missing {label} directory: {path}")


def _pick(override: str | None, env_name: str, default: str = "") -> str:
    if override is not None and str(override).strip():
        return str(override).strip()
    return os.environ.get(env_name, default).strip()


def load_r2_config(
    *,
    endpoint: str | None = None,
    bucket: str | None = None,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
    prefix: str | None = None,
    region: str | None = None,
    presign_expiry_sec: int | None = None,
) -> R2Config:
    """Load R2 settings from CLI overrides (when set) then repo-root .env."""
    load_root_env()

    endpoint_val = _pick(endpoint, "SNAPSHOT_R2_ENDPOINT")
    bucket_val = _pick(bucket, "SNAPSHOT_R2_BUCKET")
    access_key_id_val = _pick(access_key_id, "SNAPSHOT_R2_ACCESS_KEY_ID")
    secret_access_key_val = _pick(secret_access_key, "SNAPSHOT_R2_SECRET_ACCESS_KEY")
    prefix_val = _pick(prefix, "SNAPSHOT_R2_PREFIX", "crawl-snapshots").strip("/")
    region_val = _pick(region, "SNAPSHOT_R2_REGION", "auto")

    presign_raw = presign_expiry_sec
    if presign_raw is None:
        env_presign = os.environ.get("SNAPSHOT_PRESIGN_EXPIRY_SEC", "").strip()
        presign_raw = int(env_presign) if env_presign else DEFAULT_PRESIGN_EXPIRY_SEC

    missing = [
        name
        for name, val in (
            ("SNAPSHOT_R2_ENDPOINT", endpoint_val),
            ("SNAPSHOT_R2_BUCKET", bucket_val),
            ("SNAPSHOT_R2_ACCESS_KEY_ID", access_key_id_val),
            ("SNAPSHOT_R2_SECRET_ACCESS_KEY", secret_access_key_val),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required R2 setting(s). Set in .env or pass flags: {', '.join(missing)}"
        )

    if presign_raw < 0:
        raise RuntimeError("SNAPSHOT_PRESIGN_EXPIRY_SEC must be >= 0 (0 disables presigned URLs)")

    return R2Config(
        endpoint=endpoint_val,
        bucket=bucket_val,
        access_key_id=access_key_id_val,
        secret_access_key=secret_access_key_val,
        prefix=prefix_val or "crawl-snapshots",
        region=region_val or "auto",
        presign_expiry_sec=presign_raw,
    )
