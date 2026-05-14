# Article Uploader

Sequential Arweave uploader for DeScAi article review outputs with automatic transaction ID linking and GraphQL tagging support.

## Overview

This tool uploads article review outputs to Arweave in a specific sequence:
1. Upload `evidence_audit.md` → get txid1
2. Create modified `review.json` with link to evidence audit → upload → get txid2
3. Create modified `overview.json` with link to review → upload → get txid3

All transaction IDs and metadata are saved to `upload_metadata.json` in the output directory.

## Setup

### Prerequisites
- Node.js modules installed in `../../Arweave-Cli/node_modules/`
- Arweave wallet configured in `../../Arweave-Cli/.env`

### Installation
```bash
# Install Node.js dependencies (if not already installed in Arweave-Cli)
cd ../../Arweave-Cli
npm install

# Return to article_uploader
cd ../../articles/article_uploader
npm install
```

## Usage

### Standalone
```bash
python uploader.py --output-dir "path/to/article/output"
```

### From Pipeline
The uploader is automatically called from `run_full_pipeline.py` unless `--skip-upload` is specified.

## Features

- **Sequential uploads**: Ensures proper ordering and dependency linking
- **GraphQL tags**: Adds metadata tags for querying via Arweave GraphQL
  - `doctype`: evidence, review, or overview
  - `research_name`: Study name from JSON
  - `review_date`: Review date from JSON
  - `platform`: (reserved for future use)
- **Original file preservation**: Never modifies original files
- **Transaction tracking**: Saves all txids and URLs to `upload_metadata.json`
- **Error recovery**: Stops on failure and supports `--resume` flag

## Output

Creates `upload_metadata.json` in the output directory:
```json
{
  "upload_date": "2026-05-14T12:28:00Z",
  "evidence_audit": {
    "txid": "abc123...",
    "url": "https://arweave.net/abc123...",
    "tags": {...}
  },
  "review": {
    "txid": "def456...",
    "url": "https://arweave.net/def456...",
    "descai_url": "https://descai.net/review/def456...",
    "tags": {...}
  },
  "overview": {
    "txid": "ghi789...",
    "url": "https://arweave.net/ghi789...",
    "tags": {...}
  }
}
```

## Architecture

- `upload_service.js`: Core Arweave upload logic with tag support
- `upload_cli.js`: CLI wrapper for the upload service
- `uploader.py`: Python orchestrator for sequential uploads
- Uses Turbo SDK from `@ardrive/turbo-sdk`
- Reuses wallet configuration from `Arweave-Cli/.env`
