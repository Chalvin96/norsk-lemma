"""Embed LLM translation results back into article JSON files."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from .extract import iter_definition_cores
from .io import write_text_atomically


def embed_translations(article: dict[str, Any], llm_result: dict[str, Any]) -> None:
    """Mutate *article_copy* in-place to store translation fields from *llm_result*.

    Callers must pass a deep copy — this function mutates its argument.
    """
    primary = llm_result.get("lemma_primary") or ""
    for lemma in article.get("lemmas", []):
        if isinstance(lemma, dict):
            lemma["primary_translation"] = primary

    by_source: dict[int, deque[dict[str, Any]]] = {}
    for defn in llm_result.get("definitions", []):
        sid = defn.get("source_id")
        if sid is not None:
            examples = defn.get("examples", [])
            by_source.setdefault(sid, deque()).append(
                {
                    "translation": defn.get("translation", ""),
                    "examples": examples if isinstance(examples, list) else [],
                }
            )

    for core in iter_definition_cores(article):
        queue = by_source.get(core["source_id"])
        if not queue:
            continue
        entry = queue.popleft()
        core["explanation_element"]["translation"] = entry["translation"]
        english_examples = entry["examples"]
        for index, element in enumerate(core["example_elements"]):
            translation = english_examples[index] if index < len(english_examples) else ""
            element["en"] = translation if isinstance(translation, str) else ""


def write_article(articles_dir: Path, article_id: int, data: dict[str, Any]) -> None:
    output_path = articles_dir / f"{article_id}.json"
    write_text_atomically(output_path, json.dumps(data, ensure_ascii=False))
