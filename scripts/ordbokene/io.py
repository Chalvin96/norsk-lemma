from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .settings import logger

ExplodedEntry = tuple[int, dict[str, Any]]


def explode(articles_dir: Path) -> list[ExplodedEntry]:
    """Walk articles/*.json, explode inline-only sub-articles as peers."""
    results: list[ExplodedEntry] = []

    for file_path in sorted(
        articles_dir.glob("*.json"),
        key=lambda path: int(path.stem) if path.stem.isdigit() else path.stem,
    ):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping %s: %s", file_path.name, exc)
            continue

        if data.get("edit_state") == "På vent":
            continue

        article_id = data.get("article_id")
        if article_id is None:
            continue

        results.append((article_id, data))

        for sub_article in _find_inline_sub_articles(data):
            sub_id = sub_article.get("article_id")
            if sub_id is None:
                continue
            if (articles_dir / f"{sub_id}.json").exists():
                continue
            results.append((sub_id, _wrap_sub_as_article(article_id, sub_article)))

    return results


def _find_inline_sub_articles(data: dict[str, Any]) -> list[dict[str, Any]]:
    sub_articles: list[dict[str, Any]] = []

    def walk(elements: list[Any]) -> None:
        for element in elements:
            if not isinstance(element, dict):
                continue
            if element.get("type_") == "sub_article":
                article = element.get("article")
                if isinstance(article, dict):
                    sub_articles.append(article)
                continue
            walk(element.get("elements", []))

    walk(data.get("body", {}).get("definitions", []))
    return sub_articles


def _wrap_sub_as_article(parent_article_id: int, sub_article: dict[str, Any]) -> dict[str, Any]:
    return {
        "article_id": sub_article.get("article_id"),
        "_parent_article_id": parent_article_id,
        "lemmas": sub_article.get("lemmas", []),
        "body": sub_article.get("body", {}),
        "edit_state": (sub_article.get("properties") or {}).get("edit_state", "Eksisterende"),
        "article_type": sub_article.get("article_type", "SUB_ARTICLE"),
        "word_class": sub_article.get("word_class", ""),
    }


def collect_pending(
    exploded: list[ExplodedEntry],
    lemma_dir: Path,
    force: bool,
) -> list[ExplodedEntry]:
    if force:
        return exploded
    return [
        (article_id, raw)
        for article_id, raw in exploded
        if not (lemma_dir / f"{article_id}.json").exists()
    ]


def write_lemma(lemma_dir: Path, article_id: int, data: dict[str, Any]) -> Path:
    lemma_dir.mkdir(parents=True, exist_ok=True)
    output_path = lemma_dir / f"{article_id}.json"
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def write_error(error_log: Path, file_path: Path, word: str, error: str) -> None:
    error_log.parent.mkdir(parents=True, exist_ok=True)
    with error_log.open("a", encoding="utf-8") as handle:
        handle.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {file_path.name} | {word} | {error}\n"
        )
