"""Embed LLM translation results back into article JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .extract import iter_definition_cores
from .io import write_text_atomically


def embed_translations(article: dict[str, Any], llm_result: dict[str, Any]) -> None:
    """Mutate *article_copy* in-place to store translation fields from *llm_result*.

    Callers must pass a deep copy — this function mutates its argument.
    """
    cores = iter_definition_cores(article)
    definitions = llm_result.get("definitions", [])
    if [d.get("source_id") for d in definitions] != [c["source_id"] for c in cores]:
        raise ValueError("source_id_or_cardinality_mismatch")
    for core, definition in zip(cores, definitions, strict=True):
        if "examples" in definition:
            examples = definition["examples"]
            if (
                not isinstance(examples, list)
                or len(examples) != len(core["example_elements"])
                or any(not isinstance(en, str) for en in examples)
            ):
                raise ValueError("example_cardinality_mismatch")
    primary = llm_result.get("lemma_primary") or ""
    for lemma in article.get("lemmas", []):
        if isinstance(lemma, dict) and isinstance(primary, str) and primary.strip():
            lemma["primary_translation"] = primary

    for core, definition in zip(cores, definitions, strict=True):
        translation = definition.get("translation", "")
        if isinstance(translation, str) and translation.strip():
            core["explanation_element"]["translation"] = translation
        if "examples" in definition:
            for element, english in zip(
                core["example_elements"], definition["examples"], strict=True
            ):
                if english.strip():
                    element["en"] = english


def write_article(articles_dir: Path, article_id: int, data: dict[str, Any]) -> None:
    output_path = articles_dir / f"{article_id}.json"
    write_text_atomically(output_path, json.dumps(data, ensure_ascii=False))
