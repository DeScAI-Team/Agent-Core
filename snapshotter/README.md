# Pipeline snapshotter

Bundles **`crawlers/output`** and **`reviews/`** into `snapshot.tar.zst`, then uploads to a **private** Cloudflare R2 bucket.

## Setup

From repo root with the Agent venv active:

```bash
source Agent/bin/activate
pip install -r requirements.txt   # includes boto3, zstd
```

Copy snapshot variables from [`env-example.txt`](../env-example.txt) into repo-root `.env` and create an [R2 API token](https://developers.cloudflare.com/r2/api/s3/tokens/) with read/write on your bucket. The bucket does **not** need public access.

## Usage

```bash
python -m snapshotter --dry-run
python -m snapshotter --no-upload          # build snapshot.tar.zst only
python -m snapshotter                      # build + upload
python -m snapshotter --upload-only snapshot.tar.zst
```

Override any R2 setting via flags (useful in CI without editing `.env`):

```bash
python -m snapshotter \
  --r2-endpoint "$SNAPSHOT_R2_ENDPOINT" \
  --r2-bucket "$SNAPSHOT_R2_BUCKET" \
  --r2-access-key-id "$SNAPSHOT_R2_ACCESS_KEY_ID" \
  --r2-secret-access-key "$SNAPSHOT_R2_SECRET_ACCESS_KEY"
```

Defaults:

| Item | Path |
|------|------|
| Crawl input | `crawlers/output/` |
| Reviews input | `reviews/` |
| Local archive | `snapshot.tar.zst` |
| Receipt | `snapshot-receipt.json` |

Archive layout inside `snapshot.tar.zst`:

```
crawlers/output/...
reviews/...
```

After upload, stdout prints:

1. `s3://<bucket>/<object_key>` — stable object reference
2. Presigned HTTPS URL (if `SNAPSHOT_PRESIGN_EXPIRY_SEC` > 0) — temporary download link for private buckets

R2 keys look like `crawl-snapshots/<UTC-timestamp>/snapshot.tar.zst`.

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `SNAPSHOT_R2_ENDPOINT` | yes | `https://<account_id>.r2.cloudflarestorage.com` |
| `SNAPSHOT_R2_BUCKET` | yes | Bucket name |
| `SNAPSHOT_R2_ACCESS_KEY_ID` | yes | R2 access key |
| `SNAPSHOT_R2_SECRET_ACCESS_KEY` | yes | R2 secret |
| `SNAPSHOT_R2_REGION` | no | boto3 region (default `auto`) |
| `SNAPSHOT_R2_PREFIX` | no | Key prefix (default `crawl-snapshots`) |
| `SNAPSHOT_PRESIGN_EXPIRY_SEC` | no | Presigned GET TTL in seconds (default `604800`; `0` = upload only) |

CLI flags `--r2-endpoint`, `--r2-bucket`, `--r2-access-key-id`, `--r2-secret-access-key`, `--r2-prefix`, `--r2-region`, and `--presign-expiry-sec` override the corresponding env vars.

`snapshot-receipt.json` records `bucket`, `object_key`, `s3_uri`, `endpoint`, optional `presigned_url`, and `presigned_expires_at`.

`zstd` may require system `libzstd` (libzstd-dev on Debian/Ubuntu).
