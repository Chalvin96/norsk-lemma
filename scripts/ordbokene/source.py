from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

import requests

from .settings import REQUEST_TIMEOUT, logger

ARTICLES_ARCHIVE_URL = "https://ord.uib.no/bm/fil/article.tar.gz"


def ensure_articles_dir(articles_dir: Path) -> None:
    if _has_article_json(articles_dir):
        return

    articles_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Articles directory is empty, downloading source archive from %s",
        ARTICLES_ARCHIVE_URL,
    )
    _download_and_extract_articles(articles_dir)


def _has_article_json(articles_dir: Path) -> bool:
    return articles_dir.exists() and any(articles_dir.glob("*.json"))


def _download_and_extract_articles(articles_dir: Path) -> None:
    with requests.get(ARTICLES_ARCHIVE_URL, stream=True, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp_file:
            archive_path = Path(tmp_file.name)
            total = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp_file.write(chunk)
                    total += len(chunk)
        logger.info("Downloaded %.1f MB", total / 1024 / 1024)

    # The archive nests files under an article/ subdirectory (article/1.json, …).
    # Extract to a temp dir, then flatten into articles_dir so explode() finds *.json
    # at the top level.
    logger.info("Extracting archive to %s", articles_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(tmp_dir, filter="data")
        finally:
            archive_path.unlink(missing_ok=True)

        _flatten_into(Path(tmp_dir), articles_dir)

    logger.info(
        "Articles directory ready with %d files", sum(1 for _ in articles_dir.glob("*.json"))
    )


def _flatten_into(src: Path, dst: Path) -> None:
    """Move all *.json files found anywhere under src into dst (flat)."""
    moved = 0
    for json_file in src.rglob("*.json"):
        shutil.move(str(json_file), dst / json_file.name)
        moved += 1
    if moved == 0:
        logger.warning("No JSON files found in extracted archive — download may have failed")
