"""Shared LLM / tagger / vision env for the articles pipeline."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from openai import OpenAI

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

_ARTICLES_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ARTICLES_DIR.parent

if load_dotenv is not None:
    load_dotenv(_REPO_ROOT / ".env")

# Hugging Face Hub: HF_TOKEN is canonical; keep legacy alias in sync for older libraries.
_hf_token = os.environ.get("HF_TOKEN", "").strip()
_legacy_hf_token = os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
if _hf_token and not _legacy_hf_token:
    os.environ["HUGGING_FACE_HUB_TOKEN"] = _hf_token
elif _legacy_hf_token and not _hf_token:
    os.environ["HF_TOKEN"] = _legacy_hf_token

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

TAGGER_BASE_URL = os.environ.get("TAGGER_BASE_URL") or LLM_BASE_URL
TAGGER_API_KEY = os.environ.get("TAGGER_API_KEY") or LLM_API_KEY

VISION_BASE_URL = (
    os.environ.get("VISION_MODEL_URL")
    or os.environ.get("VISION_BASE_URL")
    or os.environ.get("VLLM_BASE_URL")
    or "http://localhost:8001/v1"
)
VISION_API_KEY = (
    os.environ.get("VISION_MODEL_API_KEY")
    or os.environ.get("VISION_API_KEY")
    or os.environ.get("VLLM_API_KEY")
    or "none"
)

READ_PAPER_MODEL = os.environ.get("READ_PAPER_MODEL", "nanonets/Nanonets-OCR2-3B")

# Deprecated aliases for scripts not yet migrated.
VLLM_BASE_URL = LLM_BASE_URL
VLLM_API_KEY = LLM_API_KEY


def make_client(*, tagger: bool = False, vision: bool = False) -> OpenAI:
    if vision:
        return OpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)
    if tagger:
        return OpenAI(base_url=TAGGER_BASE_URL, api_key=TAGGER_API_KEY)
    return OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


def resolve_model(
    client: OpenAI,
    env_var: str,
    fallback_envs: tuple[str, ...] = (),
) -> str:
    """Auto-discover model id, then fall back to env vars."""
    try:
        models = client.models.list()
        if models.data:
            return models.data[0].id
    except Exception as exc:  # noqa: BLE001
        print(f"[llm_env] model discovery failed: {exc}", file=sys.stderr)
    for name in (env_var, *fallback_envs):
        val = os.environ.get(name)
        if val:
            return val
    raise RuntimeError(
        f"No model available. Set {env_var} (or {fallback_envs}) "
        f"or run a server reachable at {getattr(client, 'base_url', '?')}."
    )


def review_model(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for name in ("LLM_MODEL", "VALIDATOR_MODEL"):
        val = os.environ.get(name)
        if val:
            return val
    return resolve_model(make_client(), "LLM_MODEL", ("VALIDATOR_MODEL",))


def tagger_model(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for name in ("TAGGER_MODEL", "CLASSIFIER_MODEL", "VALIDATOR_MODEL"):
        val = os.environ.get(name)
        if val:
            return val
    return resolve_model(
        make_client(tagger=True),
        "TAGGER_MODEL",
        ("CLASSIFIER_MODEL", "VALIDATOR_MODEL"),
    )


def pipeline_env(*, model: str | None = None) -> dict[str, str]:
    """Environment dict for subprocess steps (includes legacy VLLM_* aliases)."""
    m = model or review_model()
    return {
        **os.environ,
        "LLM_BASE_URL": LLM_BASE_URL,
        "LLM_API_KEY": LLM_API_KEY,
        "TAGGER_BASE_URL": TAGGER_BASE_URL,
        "TAGGER_API_KEY": TAGGER_API_KEY,
        "VISION_MODEL_URL": VISION_BASE_URL,
        "VISION_MODEL_API_KEY": VISION_API_KEY,
        "VLLM_BASE_URL": LLM_BASE_URL,
        "VLLM_API_KEY": LLM_API_KEY,
        "VALIDATOR_MODEL": m,
        "LLM_MODEL": m,
    }
