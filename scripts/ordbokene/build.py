from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .extract import extract_cross_reference, extract_definitions
from .settings import KNOWN_POS

MORPHOLOGY_TAGS = {
    "Masc": "Masc",
    "mask": "Masc",
    "Fem": "Fem",
    "fem": "Fem",
    "Neut": "Neuter",
    "Neuter": "Neuter",
    "Nøyt": "Neuter",
    "nøyt": "Neuter",
    "Masc/Fem": "Masc/Fem",
}


def build_lemma(
    raw_dict: dict[str, Any], llm_result: dict[str, Any] | str, article_id: int
) -> dict[str, Any]:
    lemmas_raw = [lemma for lemma in raw_dict.get("lemmas", []) if isinstance(lemma, dict)]
    is_sub_article = (
        raw_dict.get("_parent_article_id") is not None
        or raw_dict.get("article_type") == "SUB_ARTICLE"
    )
    cross_reference = extract_cross_reference(raw_dict)

    definitions: list[dict[str, Any]] = []
    primary_translation: str | None = None

    if cross_reference is None and isinstance(llm_result, dict):
        llm_queue: dict[int, deque[str]] = defaultdict(deque)
        for llm_definition in llm_result.get("definitions", []):
            if isinstance(llm_definition, dict) and llm_definition.get("source_id") is not None:
                llm_queue[llm_definition["source_id"]].append(llm_definition.get("translation", ""))

        for definition in extract_definitions(raw_dict):
            source_id = definition.get("source_id")
            queue = llm_queue.get(source_id)
            definitions.append(
                {
                    "text": definition["text"],
                    "translation": queue.popleft() if queue else "",
                    "examples": definition.get("examples", []),
                }
            )

        lemma_primary = llm_result.get("lemma_primary")
        if isinstance(lemma_primary, str) and lemma_primary.strip():
            primary_translation = lemma_primary.strip()

    lemma_entries: list[dict[str, Any]] = []
    for lemma in lemmas_raw:
        tags: list[str] = []
        for paradigm in lemma.get("paradigm_info", []):
            if isinstance(paradigm, dict):
                tags.extend(str(tag) for tag in paradigm.get("tags", []))

        pos = next((tag for tag in tags if tag in KNOWN_POS), None)
        is_expression = "EXPR" in tags
        word_forms = [
            {"word_form": form, "tags_json": form_tags}
            for form, form_tags in _collect_word_forms(lemma).items()
        ]
        lemma_entries.append(
            {
                "lemma": lemma.get("lemma", ""),
                "hgno": lemma.get("hgno") if lemma.get("hgno") is not None else 1,
                "pos": pos or "UNKNOWN",
                "source_lemma_id": lemma.get("id"),
                "is_sub_article": is_sub_article or is_expression,
                "primary_translation": None if is_expression else primary_translation,
                "word_forms": word_forms,
            }
        )

    if not lemma_entries and lemmas_raw:
        lemma_entries.append(
            {
                "lemma": lemmas_raw[0].get("lemma", ""),
                "hgno": 1,
                "pos": "UNKNOWN",
                "source_lemma_id": lemmas_raw[0].get("id"),
                "is_sub_article": is_sub_article,
                "primary_translation": primary_translation,
                "word_forms": [],
            }
        )

    return {
        "source_article_id": article_id,
        "lemmas": lemma_entries,
        "cross_reference": cross_reference,
        "definitions": definitions,
    }


def _collect_word_forms(lemma_data: dict[str, Any]) -> dict[str, list[str]]:
    forms: dict[str, list[str]] = {}
    lemma_morph_tags: list[str] = []

    for paradigm in lemma_data.get("paradigm_info", []):
        if not isinstance(paradigm, dict):
            continue
        paradigm_tags = _source_morphology_tags(paradigm.get("tags", []))
        inflection_class = lemma_data.get("inflection_class")
        if inflection_class:
            paradigm_tags = _merge_tags(paradigm_tags, [inflection_class])
        lemma_morph_tags = _merge_tags(lemma_morph_tags, paradigm_tags)

        for inflection in paradigm.get("inflection", []):
            if not isinstance(inflection, dict):
                continue
            word_form = inflection.get("word_form")
            if not word_form:
                continue
            tags = _merge_tags(paradigm_tags, list(inflection.get("tags", [])))
            existing = forms.get(word_form)
            forms[word_form] = tags if existing is None else _merge_tags(existing, tags)

    forms.setdefault(lemma_data.get("lemma", ""), _merge_tags(lemma_morph_tags, ["Inf"]))
    return forms


def _source_morphology_tags(tags: list[str]) -> list[str]:
    return [MORPHOLOGY_TAGS.get(tag, tag) for tag in tags if tag not in KNOWN_POS]


def _merge_tags(*tag_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for tags in tag_groups:
        for tag in tags:
            if tag not in seen:
                merged.append(tag)
                seen.add(tag)
    return merged
