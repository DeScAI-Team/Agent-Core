from __future__ import annotations

from pathlib import Path
from typing import Any

from uploader.core import run_crawl_log_upload, run_sequential_upload
from uploader.recipes import get_recipe

RECIPE_NAMES = ("article", "proposal", "dao", "compounds", "crawl-log")


def run_recipe(
    recipe_name: str,
    *,
    dir: Path | str | None = None,
    output_dir: Path | str | None = None,
    file: Path | str | None = None,
    crawl_date: str | None = None,
    resume: bool = False,
    crawl_log_path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Run an upload recipe.

    Review recipes (article, proposal, dao, compounds) require ``dir``.
    crawl-log requires ``file``.
    ``output_dir`` defaults to ``dir`` (or parent of ``file`` for crawl-log).
    """
    name = recipe_name.lower().strip()

    if name == "crawl-log":
        if file is None:
            raise ValueError("crawl-log recipe requires file=...")
        file_path = Path(file)
        out = Path(output_dir) if output_dir is not None else file_path.parent
        return run_crawl_log_upload(
            file_path,
            out,
            crawl_date=crawl_date,
            resume=resume,
        )

    if dir is None:
        raise ValueError(f"Recipe {recipe_name!r} requires dir=...")
    input_dir = Path(dir)
    out_dir = Path(output_dir) if output_dir is not None else input_dir
    recipe = get_recipe(name)
    log_path = Path(crawl_log_path) if crawl_log_path is not None else None
    return run_sequential_upload(
        recipe,
        input_dir,
        out_dir,
        resume=resume,
        crawl_log_path=log_path,
    )
