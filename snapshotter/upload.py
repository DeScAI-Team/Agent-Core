from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from snapshotter.env import R2Config


def object_key(prefix: str, timestamp: str | None = None) -> str:
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{prefix}/{ts}/snapshot.tar.zst"


def s3_uri(config: R2Config, key: str) -> str:
    return f"s3://{config.bucket}/{key}"


def make_s3_client(config: R2Config):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=Config(signature_version="s3v4"),
        region_name=config.region,
    )


def upload_to_r2(archive_path: Path, config: R2Config, key: str) -> int:
    """Upload archive to private R2. Returns uploaded byte size."""
    client = make_s3_client(config)
    body = archive_path.read_bytes()
    client.put_object(
        Bucket=config.bucket,
        Key=key,
        Body=body,
        ContentType="application/zstd",
    )
    return len(body)


def presigned_download_url(config: R2Config, key: str) -> tuple[str, str] | tuple[None, None]:
    """Return (url, expires_at_iso) or (None, None) when presign is disabled."""
    if config.presign_expiry_sec == 0:
        return None, None
    client = make_s3_client(config)
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.bucket, "Key": key},
        ExpiresIn=config.presign_expiry_sec,
    )
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=config.presign_expiry_sec)
    ).isoformat()
    return url, expires_at


def upload_result(
    archive_path: Path,
    config: R2Config,
    key: str,
) -> dict[str, Any]:
    """Upload and return receipt fields for a private bucket."""
    uploaded_bytes = upload_to_r2(archive_path, config, key)
    presigned_url, presigned_expires_at = presigned_download_url(config, key)
    return {
        "bucket": config.bucket,
        "object_key": key,
        "s3_uri": s3_uri(config, key),
        "endpoint": config.endpoint,
        "region": config.region,
        "bytes": uploaded_bytes,
        "presigned_url": presigned_url,
        "presigned_expires_at": presigned_expires_at,
    }
