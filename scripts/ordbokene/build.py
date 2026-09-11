from __future__ import annotations

import copy
from typing import Any

from .constants import KNOWN_POS, UD_TAG_ALIASES, UD_UPOS
from .embed import embed_translations
from .extract import (
    _example_text,
    extract_cross_reference,
    extract_see_also,
    iter_definition_cores,
)

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
    see_also = extract_see_also(raw_dict)

    definitions: list[dict[str, Any]] = []
    primary_translation: str | None = None
    article_is_expression = raw_dict.get("word_class") == "EXPR"
    article_has_expression_tag = any(
        any(
            isinstance(tag, str) and tag == "EXPR"
            for paradigm in lemma.get("paradigm_info", [])
            if isinstance(paradigm, dict)
            for tag in paradigm.get("tags", [])
        )
        for lemma in lemmas_raw
    )

    if cross_reference is None and isinstance(llm_result, dict):
        # Reuse the same checked merge as article writes, without mutating the source.
        enriched = copy.deepcopy(raw_dict)
        embed_translations(enriched, llm_result)
        definitions = [
            {
                "text": core["text"],
                "translation": core["explanation_element"].get("translation", ""),
                "examples": [
                    {"no": _example_text(el), "en": el.get("en", "")}
                    for el in core["example_elements"]
                ],
            }
            for core in iter_definition_cores(enriched)
        ]

        lemma_primary = next(
            (
                lm.get("primary_translation")
                for lm in enriched.get("lemmas", [])
                if isinstance(lm, dict) and lm.get("primary_translation")
            ),
            None,
        )
        if isinstance(lemma_primary, str) and lemma_primary.strip():
            primary_translation = lemma_primary.strip()
        else:
            # Older snapshots may have sense English but no primary memory hook.
            primary_translation = next(
                (
                    definition["translation"].strip()
                    for definition in definitions
                    if isinstance(definition.get("translation"), str)
                    and definition["translation"].strip()
                ),
                None,
            )

    lemma_entries: list[dict[str, Any]] = []
    for lemma in lemmas_raw:
        tags: list[str] = []
        for paradigm in lemma.get("paradigm_info", []):
            if isinstance(paradigm, dict):
                tags.extend(str(tag) for tag in paradigm.get("tags", []))

        normalized_tags = [UD_TAG_ALIASES.get(tag, tag) for tag in tags]
        is_expression = "EXPR" in tags or (article_is_expression and not article_has_expression_tag)
        pos = (
            "EXPR"
            if is_expression
            else next((tag for tag in normalized_tags if tag in KNOWN_POS), None)
        )
        word_forms = [
            {"word_form": form, "tags_json": tags, "pronunciation": pron}
            for form, (tags, pron) in _collect_word_forms(lemma).items()
        ]
        if is_expression and lemma.get("lemma"):
            self_form = lemma["lemma"]
            for word_form in word_forms:
                if word_form["word_form"] == self_form and "EXPR" not in word_form["tags_json"]:
                    word_form["tags_json"].append("EXPR")
        entry: dict[str, Any] = {
            "lemma": lemma.get("lemma", ""),
            "hgno": lemma.get("hgno") if lemma.get("hgno") is not None else 1,
            "pos": pos or "UNKNOWN",
            "source_lemma_id": lemma.get("id"),
            "is_sub_article": is_sub_article or is_expression,
            "primary_translation": primary_translation,
            "word_forms": word_forms,
        }
        # Audio is round-tripped through the raw article lemma (embedded by the
        # audio step), so a re-export reproduces it losslessly instead of wiping
        # in-place lemma audio. Mirrors the example/definition translation flow.
        audio = lemma.get("audio")
        if audio:
            entry["audio"] = audio
        lemma_entries.append(entry)

    return {
        "source_article_id": article_id,
        "lemmas": lemma_entries,
        "cross_reference": cross_reference,
        "definitions": definitions,
        "see_also": see_also,
    }


def _collect_word_forms(
    lemma_data: dict[str, Any],
) -> dict[str, tuple[list[str], list[dict[str, Any]]]]:
    """Return {word_form: (tags, pronunciation)} for every inflected form."""
    forms: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
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
            pron: list[dict[str, Any]] = inflection.get("pronunciation") or []
            existing = forms.get(word_form)
            if existing is None:
                forms[word_form] = (tags, pron)
            else:
                forms[word_form] = (_merge_tags(existing[0], tags), existing[1] or pron)

    lemma_word = lemma_data.get("lemma", "")
    if lemma_word and lemma_word not in forms:
        lemma_pron: list[dict[str, Any]] = lemma_data.get("pronunciation") or []
        forms[lemma_word] = (lemma_morph_tags, lemma_pron)
    return forms


_POS_TAGS = KNOWN_POS | UD_UPOS


def _source_morphology_tags(tags: list[str]) -> list[str]:
    normalized = [UD_TAG_ALIASES.get(tag, tag) for tag in tags]
    return [MORPHOLOGY_TAGS.get(tag, tag) for tag in normalized if tag not in _POS_TAGS]


def _merge_tags(*tag_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for tags in tag_groups:
        for tag in tags:
            if tag not in seen:
                merged.append(tag)
                seen.add(tag)
    return merged
