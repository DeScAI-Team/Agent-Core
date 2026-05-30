"""Image routing: vision description → bundle/images/{stem}.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

_PIPELINE = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PIPELINE.parent.parent.parent
_ARTICLE_PIPELINE = _REPO_ROOT / "articles" / "pipeline"
if str(_ARTICLE_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_ARTICLE_PIPELINE))

from vision_client import load_image, vision_describe  # noqa: E402

from ._utils import parse_json_response, safe_stem

_PROMPTS = _PIPELINE / "prompts"


def process_image(
    source: Path,
    bundle_dir: Path,
    *,
    vision_client: OpenAI,
    vision_model: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    out_dir = bundle_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_stem(source.name)}.json"

    if out_path.exists() and not overwrite:
        return {"route": "image", "output_path": str(out_path), "skipped": True}

    prompt = (_PROMPTS / "image_description_prompt.md").read_text(encoding="utf-8")
    pil = load_image(source)
    raw = vision_describe(vision_client, vision_model, pil, prompt, max_tokens=4096)

    try:
        parsed = parse_json_response(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = {
            "description": raw,
            "transcribed_text": "",
            "labels": [],
        }

    payload = {
        "source_file": source.name,
        "source_path": str(source),
        **parsed,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"route": "image", "output_path": str(out_path)}
