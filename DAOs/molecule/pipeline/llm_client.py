"""Shared LLM client + env loading for the DAO review pipeline."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

from openai import OpenAI

_PIPELINE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PIPELINE_DIR.parent.parent.parent

if load_dotenv is not None:
    env_path = _REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

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

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
THINK_TRAIL_RE = re.compile(r"<think>.*", re.DOTALL)
FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def make_client(*, tagger: bool = False) -> OpenAI:
    if tagger:
        return OpenAI(base_url=TAGGER_BASE_URL, api_key=TAGGER_API_KEY)
    return OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


def discover_model(client: OpenAI, *, env_var: str, fallback_envs: tuple[str, ...] = ()) -> str:
    """Auto-discover a model id, then fall back to env vars."""
    try:
        models = client.models.list()
        if models.data:
            return models.data[0].id
    except Exception as exc:  # noqa: BLE001
        print(f"[llm] model discovery failed: {exc}", file=sys.stderr)
    for name in (env_var, *fallback_envs):
        val = os.environ.get(name)
        if val:
            return val
    raise RuntimeError(
        f"No model available. Set {env_var} (or one of {fallback_envs}) or run a server reachable at {LLM_BASE_URL}."
    )


def strip_thinking(text: str) -> str:
    if not text:
        return ""
    text = THINK_RE.sub("", text)
    text = THINK_TRAIL_RE.sub("", text)
    return text.strip()


def call(
    client: OpenAI,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    enable_thinking: bool = False,
    extra_body: dict[str, Any] | None = None,
) -> str:
    body = dict(extra_body or {})
    body.setdefault("chat_template_kwargs", {"enable_thinking": enable_thinking})
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=body,
    )
    raw = resp.choices[0].message.content or ""
    return strip_thinking(raw)


def parse_json_object(raw: str) -> dict[str, Any] | list[Any] | None:
    """Best-effort JSON parsing tolerant to fences and surrounding prose."""
    if not raw:
        return None
    text = raw.strip()
    m = FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def load_prompt(name: str) -> str:
    path = _PIPELINE_DIR / "prompts" / name
    return path.read_text(encoding="utf-8")
