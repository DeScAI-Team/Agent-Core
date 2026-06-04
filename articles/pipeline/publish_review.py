"""Shape review.json for publication: 0–100 int scores, slim categories."""

from __future__ import annotations

import math
from typing import Any, Callable


def score_0_1_to_pct(score: float) -> int:
    return int(math.ceil(float(score) * 100))


def _has_rationale(entry: dict[str, Any]) -> bool:
    return bool((entry.get("rationale") or "").strip())


def filter_scored_categories(
    all_scores: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Categories with non-empty rationale (still 0–1 floats + internal fields)."""
    return {k: v for k, v in all_scores.items() if _has_rationale(v)}


def publish_categories(
    all_scores: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Emit {score: int, rationale: str} only; omit empty rationales."""
    published: dict[str, dict[str, Any]] = {}
    for dim_key, info in all_scores.items():
        if not _has_rationale(info):
            continue
        published[dim_key] = {
            "score": score_0_1_to_pct(info["score"]),
            "rationale": (info.get("rationale") or "").strip(),
        }
    return published


def publish_composite(
    all_scores: dict[str, dict[str, Any]],
    dimension_weights: dict[str, float],
    compute_composite: Callable[[dict[str, dict[str, Any]], dict[str, float]], float],
) -> int:
    """Weighted composite on categories with rationales, then ceil to 0–100 int."""
    filtered = filter_scored_categories(all_scores)
    composite_0_1 = compute_composite(filtered, dimension_weights)
    return score_0_1_to_pct(composite_0_1)
