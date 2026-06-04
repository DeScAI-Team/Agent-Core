"""Shared LLM server env for compounds pipeline scripts."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_COMPOUNDS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _COMPOUNDS_DIR.parent

if load_dotenv is not None:
    load_dotenv(_REPO_ROOT / ".env")

LLM_BASE_URL = (
    os.environ.get("LLM_BASE_URL")
    or os.environ.get("VLLM_BASE_URL")
    or "http://localhost:8000/v1"
)
LLM_API_KEY = (
    os.environ.get("LLM_API_KEY")
    or os.environ.get("VLLM_API_KEY")
    or "none"
)

# Optional dedicated tagger server (falls back to LLM_* when unset).
TAGGER_BASE_URL = (
    os.environ.get("TAGGER_BASE_URL")
    or LLM_BASE_URL
)
TAGGER_API_KEY = (
    os.environ.get("TAGGER_API_KEY")
    or LLM_API_KEY
)
