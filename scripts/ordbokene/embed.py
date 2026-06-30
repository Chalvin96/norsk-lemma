"""Embed LLM translation results back into article JSON files."""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any


def embed_translations(article: dict[str, Any], llm_result: dict[str, Any]) -> None:
    """Mutate *article_copy* in-place to store translation fields from *llm_result*.

    Callers must pass a deep copy — this function mutates its argument.
    """
    primary = llm_result.get("lemma_primary") or ""
    for lemma in article.get("lemmas", []):
        if isinstance(lemma, dict):
            lemma["primary_translation"] = primary

    by_source: dict[int, deque[str]] = {}
    for defn in llm_result.get("definitions", []):
        sid = defn.get("source_id")
        if sid is not None:
            by_source.setdefault(sid, deque()).append(defn.get("translation", ""))

    def walk(elements: list[Any], source_id: int | None) -> None:
        for el in elements:
            if not isinstance(el, dict):
                continue
            t = el.get("type_")
            if t == "explanation" and source_id in by_source:
                q = by_source[source_id]
                if q:
                    el["translation"] = q.popleft()
            elif t == "definition" and not el.get("sub_definition"):
                walk(el.get("elements", []), el.get("id"))

    walk(article.get("body", {}).get("definitions", []), None)


def write_article(articles_dir: Path, article_id: int, data: dict[str, Any]) -> None:
    tmp = articles_dir / f"{article_id}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(articles_dir / f"{article_id}.json")
