#!/usr/bin/env python3
"""Partition validated lines by category and compute per-category scores.

Two correctness layers sit between raw verdicts and the final score:

1. **Tag-based weighting** — each line carries
   `weight = source_weight[source_kind] * line_type_weight[category][line_type]`,
   so high-trust evidence (on-chain facts, PDFs) and category-relevant
   line types (mission lines for `mission_clarity`, fact lines for
   `team_credibility`, etc.) count more. Both maps live in `dao_mappings.json`
   so the rubric can be retuned without code changes.

2. **Near-duplicate clustering** — within a category, lines that say
   essentially the same thing (Jaccard >= 0.55 on normalized tokens) are
   merged into a cluster. Each line's `effective_weight = weight / sqrt(cluster_size)`,
   so a DAO that repeats one mission across six docs no longer eats six
   penalties (or six rewards). Cluster of 1 → 1.0× weight; cluster of 4 →
   0.5× per line (2× total); cluster of 9 → 0.33× per line (3× total).

Score formula (per category):

    weighted_numerator   = Σ effective_weight(line) for lines with verdict in {valid, positive}
    weighted_denominator = Σ effective_weight(line) for lines with verdict in
                              {valid, positive, invalid, negative}
    score_pct            = round(100 * weighted_numerator / weighted_denominator)

`raw_numerator` / `raw_denominator` / `raw_score_pct` (un-weighted line counts,
no clustering) are written alongside for transparency.

Reads:  steps/validated.jsonl + dao_mappings.json
Writes:
  - steps/groups/<category>.json   (one JSON per category, list of lines, each
                                    annotated with `weight`, `cluster_id`,
                                    `cluster_size`, `effective_weight`)
  - steps/group_scores.json        (per-category aggregates: weighted + raw)
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

_PIPELINE_DIR = Path(__file__).resolve().parent
_DEFAULT_MAPPINGS = _PIPELINE_DIR / "dao_mappings.json"

_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "this", "that", "these",
    "those", "are", "was", "were", "will", "have", "has", "had", "but",
    "not", "its", "our", "their", "your", "any", "all", "out", "via",
    "such", "they", "them", "his", "her", "him", "she", "you", "who",
    "what", "when", "where", "while", "also",
}
CLUSTER_THRESHOLD = 0.55


def _load_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _normalize_tokens(text: str) -> set[str]:
    cleaned = _NORMALIZE_RE.sub(" ", (text or "").lower())
    return {t for t in cleaned.split() if len(t) > 2 and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def _cluster_lines(rows: list[dict[str, Any]], *, threshold: float = CLUSTER_THRESHOLD) -> None:
    """Greedy single-link clustering on normalized token Jaccard. Mutates rows."""
    representatives: list[set[str]] = []
    cluster_of: list[int] = []
    for r in rows:
        toks = _normalize_tokens(r.get("text") or "")
        best_idx = -1
        best_sim = 0.0
        for ci, rep in enumerate(representatives):
            sim = _jaccard(toks, rep)
            if sim > best_sim:
                best_sim = sim
                best_idx = ci
        if best_idx >= 0 and best_sim >= threshold:
            cluster_of.append(best_idx)
            representatives[best_idx] = representatives[best_idx] | toks
        else:
            cluster_of.append(len(representatives))
            representatives.append(toks)

    sizes: dict[int, int] = defaultdict(int)
    for cid in cluster_of:
        sizes[cid] += 1
    for r, cid in zip(rows, cluster_of):
        size = sizes[cid]
        r["cluster_id"] = cid
        r["cluster_size"] = size
        damp = 1.0 / math.sqrt(size) if size > 0 else 1.0
        base_weight = float(r.get("weight") or 0.0)
        r["effective_weight"] = round(base_weight * damp, 4)


def _line_weight(
    *,
    category: str,
    source_kind: str,
    line_type: str,
    source_weights: dict[str, float],
    line_type_weights: dict[str, dict[str, float]],
    default_source: float,
    default_line_type: float,
) -> float:
    sw = float(source_weights.get(source_kind, default_source))
    cat_map = line_type_weights.get(category) or {}
    lw = float(cat_map.get(line_type, default_line_type))
    return round(sw * lw, 4)


def run(
    *,
    validated_path: Path,
    out_groups_dir: Path,
    out_scores_path: Path,
    mappings_path: Path = _DEFAULT_MAPPINGS,
) -> dict[str, Any]:
    mappings = json.loads(mappings_path.read_text(encoding="utf-8"))
    categories: list[str] = mappings.get("categories") or []
    pos = set(mappings.get("score_method", {}).get("positive_verdicts", []))
    neg = set(mappings.get("score_method", {}).get("negative_verdicts", []))
    ignore = set(mappings.get("score_method", {}).get("ignored_verdicts", []))

    source_weights: dict[str, float] = mappings.get("source_weights") or {}
    line_type_weights: dict[str, dict[str, float]] = mappings.get("line_type_weights") or {}
    default_source = float(mappings.get("default_source_weight", 1.0))
    default_line_type = float(mappings.get("default_line_type_weight", 1.0))

    out_groups_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in categories}
    sources_per_category: dict[str, set[str]] = {c: set() for c in categories}

    for row in _load_jsonl(validated_path):
        cat = row.get("category")
        if cat not in grouped:
            continue
        weight = _line_weight(
            category=cat,
            source_kind=row.get("source_kind") or "",
            line_type=row.get("line_type") or "",
            source_weights=source_weights,
            line_type_weights=line_type_weights,
            default_source=default_source,
            default_line_type=default_line_type,
        )
        annotated = dict(row)
        annotated["weight"] = weight
        grouped[cat].append(annotated)
        sp = row.get("source_path") or row.get("bundle_path") or ""
        if sp:
            sources_per_category[cat].add(sp)

    # Cluster within each category and stamp effective_weight onto every row.
    for cat, rows in grouped.items():
        _cluster_lines(rows)

    aggregates: dict[str, dict[str, Any]] = {}
    for cat in categories:
        rows = grouped[cat]
        w_num = 0.0
        w_den = 0.0
        r_num = 0
        r_den = 0
        verdict_counts: dict[str, int] = {
            "valid": 0, "invalid": 0, "positive": 0, "negative": 0,
            "neutral": 0, "inconclusive": 0,
        }
        verdict_weight: dict[str, float] = {k: 0.0 for k in verdict_counts}
        cluster_ids = {r["cluster_id"] for r in rows}

        for r in rows:
            v = r.get("verdict")
            ew = float(r.get("effective_weight") or 0.0)
            if v in verdict_counts:
                verdict_counts[v] += 1
                verdict_weight[v] = round(verdict_weight[v] + ew, 4)
            if v in ignore or v is None:
                continue
            r_den += 1
            w_den += ew
            if v in pos:
                r_num += 1
                w_num += ew

        score_pct = round(100.0 * w_num / w_den) if w_den > 0 else None
        raw_score_pct = round(100.0 * r_num / r_den) if r_den > 0 else None

        aggregates[cat] = {
            "score_pct": score_pct,
            "numerator": round(w_num, 4),
            "denominator": round(w_den, 4),
            "raw_score_pct": raw_score_pct,
            "raw_numerator": r_num,
            "raw_denominator": r_den,
            "line_count": len(rows),
            "cluster_count": len(cluster_ids),
            "source_count": len(sources_per_category[cat]),
            "verdict_breakdown": verdict_counts,
            "verdict_weight": verdict_weight,
        }

        out_path = out_groups_dir / f"{cat}.json"
        out_path.write_text(
            json.dumps(
                {
                    "category": cat,
                    "score_pct": score_pct,
                    "numerator": round(w_num, 4),
                    "denominator": round(w_den, 4),
                    "raw_score_pct": raw_score_pct,
                    "raw_numerator": r_num,
                    "raw_denominator": r_den,
                    "line_count": len(rows),
                    "cluster_count": len(cluster_ids),
                    "lines": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    out_scores_path.parent.mkdir(parents=True, exist_ok=True)
    out_scores_path.write_text(
        json.dumps(aggregates, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[group-score] wrote group files to {out_groups_dir}")
    for cat, agg in aggregates.items():
        s = agg["score_pct"]
        rs = agg["raw_score_pct"]
        s_str = "-" if s is None else f"{s}"
        rs_str = "-" if rs is None else f"{rs}"
        print(
            f"  {cat:26s}  weighted={s_str:>3}  raw={rs_str:>3}  "
            f"weighted=({agg['numerator']:.2f}/{agg['denominator']:.2f})  "
            f"raw=({agg['raw_numerator']}/{agg['raw_denominator']})  "
            f"lines={agg['line_count']}  clusters={agg['cluster_count']}"
        )
    return aggregates


def main() -> int:
    parser = argparse.ArgumentParser(description="Group validated lines and compute per-category scores")
    parser.add_argument("--validated", type=Path, required=True)
    parser.add_argument("--groups-dir", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--mappings", type=Path, default=_DEFAULT_MAPPINGS)
    args = parser.parse_args()

    run(
        validated_path=args.validated.resolve(),
        out_groups_dir=args.groups_dir.resolve(),
        out_scores_path=args.scores.resolve(),
        mappings_path=args.mappings.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
