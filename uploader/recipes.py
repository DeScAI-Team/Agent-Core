from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class UploadStep:
    """One step in a sequential upload recipe."""

    metadata_key: str
    label: str
    doctype: str
    file_key: str
    append_template: str | None = None
    modified: bool = False


@dataclass
class Recipe:
    name: str
    steps: list[UploadStep]
    tag_builder: Callable[[str, dict[str, Any]], list[dict[str, str]]]
    file_map: dict[str, str]
    review_json_key: str
    metadata_extra: Callable[[dict[str, Any], str], dict[str, Any]] | None = None


def _article_tags(doctype: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
    tags = [
        {"name": "doctype", "value": doctype},
        {"name": "platform", "value": "ResearchHub"},
        {"name": "category", "value": "Article"},
    ]
    if ctx.get("research_name"):
        tags.append({"name": "research_name", "value": ctx["research_name"]})
    if ctx.get("review_date"):
        tags.append({"name": "review_date", "value": ctx["review_date"]})
    return tags


def _proposal_tags(doctype: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
    tags = [
        {"name": "platform", "value": "ResearchHub"},
        {"name": "category", "value": "Proposal"},
        {"name": "doctype", "value": doctype},
    ]
    if ctx.get("name"):
        tags.append({"name": "name", "value": ctx["name"]})
    if ctx.get("review_date"):
        tags.append({"name": "date", "value": ctx["review_date"]})
    return tags


def _dao_tags(doctype: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
    tags = [
        {"name": "doctype", "value": doctype},
        {"name": "DaoName", "value": ctx.get("dao_name") or ""},
        {"name": "platform", "value": "Molecule"},
        {"name": "category", "value": "ResearchDAO"},
    ]
    if ctx.get("review_date"):
        tags.append({"name": "date", "value": ctx["review_date"]})
    return tags


def _compounds_tags(doctype: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
    tags = [
        {"name": "doctype", "value": doctype},
        {"name": "platform", "value": "PumpScience"},
        {"name": "category", "value": "compounds"},
    ]
    if ctx.get("compound_name"):
        tags.append({"name": "compounds", "value": ctx["compound_name"]})
    if ctx.get("review_date"):
        tags.append({"name": "date", "value": ctx["review_date"]})
    return tags


def _article_metadata_extra(_ctx: dict[str, Any], input_dir: str) -> dict[str, Any]:
    return {"output_dir": input_dir}


def _dao_metadata_extra(_ctx: dict[str, Any], input_dir: str) -> dict[str, Any]:
    return {"synthesis_dir": input_dir}


def _compounds_metadata_extra(ctx: dict[str, Any], input_dir: str) -> dict[str, Any]:
    return {
        "input_dir": input_dir,
        "review_file": ctx.get("review_file", ""),
        "evidence_file": ctx.get("evidence_file", ""),
    }


RECIPES: dict[str, Recipe] = {
    "article": Recipe(
        name="article",
        file_map={
            "evidence": "evidence_audit.md",
            "review": "review.json",
            "overview": "overview.json",
        },
        review_json_key="review",
        tag_builder=_article_tags,
        metadata_extra=_article_metadata_extra,
        steps=[
            UploadStep("evidence_audit", "Step 1/3: Upload evidence_audit.md", "evidence", "evidence"),
            UploadStep(
                "review",
                "Step 2/3: Upload review.json (with evidence link)",
                "review",
                "review",
                append_template=" Full evidence audit is available at arweave.net/{evidence_txid}",
                modified=True,
            ),
            UploadStep(
                "overview",
                "Step 3/3: Upload overview.json (with review link)",
                "overview",
                "overview",
                append_template=" Full review available at descai.net/review/{review_txid}",
                modified=True,
            ),
        ],
    ),
    "proposal": Recipe(
        name="proposal",
        file_map={
            "evidence": "evidence_audit.md",
            "review": "review.json",
        },
        review_json_key="review",
        tag_builder=_proposal_tags,
        metadata_extra=_article_metadata_extra,
        steps=[
            UploadStep(
                "evidence_audit",
                "Step 1/2: Upload evidence_audit.md",
                "EvidenceAudit",
                "evidence",
            ),
            UploadStep(
                "review",
                "Step 2/2: Upload review.json (with evidence link)",
                "review",
                "review",
                append_template=" Evidence bundle available at arweave.net/{evidence_txid}",
                modified=True,
            ),
        ],
    ),
    "dao": Recipe(
        name="dao",
        file_map={
            "evidence": "dao_evidence_audit.md",
            "review": "dao_review.json",
            "overview": "overview.json",
        },
        review_json_key="review",
        tag_builder=_dao_tags,
        metadata_extra=_dao_metadata_extra,
        steps=[
            UploadStep(
                "evidence_audit",
                "Step 1/3: Upload dao_evidence_audit.md",
                "evidence",
                "evidence",
            ),
            UploadStep(
                "review",
                "Step 2/3: Upload dao_review.json (with evidence link)",
                "review",
                "review",
                append_template=" Find evidence audit at arweave.net/{evidence_txid}",
                modified=True,
            ),
            UploadStep(
                "overview",
                "Step 3/3: Upload overview.json (with review link)",
                "overview",
                "overview",
                append_template=" Find full review at descai.net/review/{review_txid}",
                modified=True,
            ),
        ],
    ),
    "compounds": Recipe(
        name="compounds",
        file_map={
            "evidence": "evidence_audit.md",
            "review": "__review_json__",
        },
        review_json_key="review",
        tag_builder=_compounds_tags,
        metadata_extra=_compounds_metadata_extra,
        steps=[
            UploadStep(
                "evidence_audit",
                "Step 1/2: Upload evidence_audit.md",
                "evidence",
                "evidence",
            ),
            UploadStep(
                "review",
                "Step 2/2: Upload review (with evidence link)",
                "review",
                "review",
                append_template=" You can find evidence audit at arweave.net/{evidence_txid}",
                modified=True,
            ),
        ],
    ),
}


def get_recipe(name: str) -> Recipe:
    key = name.lower().strip()
    if key not in RECIPES:
        known = ", ".join(sorted(RECIPES))
        raise ValueError(f"Unknown recipe {name!r}. Choose from: {known}, crawl-log")
    return RECIPES[key]
