"""Shared LLM / tagger env for the proposals pipeline (re-exports articles/llm_env)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ARTICLES = _REPO_ROOT / "articles"
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))

from llm_env import (  # noqa: E402, F401
    LLM_API_KEY,
    LLM_BASE_URL,
    TAGGER_API_KEY,
    TAGGER_BASE_URL,
    make_client,
    review_model,
    tagger_model,
)
