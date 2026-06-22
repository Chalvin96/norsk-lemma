from __future__ import annotations

import re
from typing import Any

from .settings import ABBREVIATIONS, CONTEXT_LABEL_ITEM_TYPES, PLACEHOLDER_ONLY_RE, REDIRECT_RE


def _render_item(item: dict[str, Any]) -> str:
    if item.get("lemmas"):
        return " ".join(
            lemma.get("lemma", "") if isinstance(lemma, dict) else str(lemma)
            for lemma in item["lemmas"]
        )
    if item.get("text"):
        return _resolve_placeholders(item["text"], item.get("items", []))
    if item.get("numerator") is not None:
        return f"{item['numerator']}/{item['denominator']}"
    return ABBREVIATIONS.get(item.get("id", ""), "")


def _resolve_placeholders(content: str, items: list[dict[str, Any]]) -> str:
    for item in items:
        if "$" not in content:
            break
        content = content.replace("$", _render_item(item), 1)
    return content


def _example_text(element: dict[str, Any]) -> str:
    quote = element.get("quote", {})
    content = quote.get("content", "")
    return _resolve_placeholders(content, quote.get("items", [])) if content else ""


def _is_structural_context_label(element: dict[str, Any], resolved_text: str) -> bool:
    content = element.get("content", "").strip()
    if not content and not resolved_text.strip():
        return True
    items = element.get("items", [])
    if not items:
        return False
    if not all(
        isinstance(item, dict) and item.get("type_") in CONTEXT_LABEL_ITEM_TYPES for item in items
    ):
        return False
    return bool(PLACEHOLDER_ONLY_RE.match(content)) or content.endswith(":")


def _is_redirect(text: str, items: list[dict[str, Any]]) -> bool:
    # Real redirects use lowercase "se X" / "sjå X" (no colon). The old monolith checked
    # "Se:"/"Sjå:" which never matched real data — this regex is the corrected form.
    if not REDIRECT_RE.match(text):
        return False
    return any(isinstance(item, dict) and item.get("type_") == "article_ref" for item in items)


def extract_definitions(raw_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract real definitions from the article body, skipping structural labels."""
    body = raw_dict.get("body", {})
    definitions: list[dict[str, Any]] = []

    def walk(elements: list[Any], source_id: int | None = None) -> None:
        pending_examples: list[str] = []

        def flush_examples() -> None:
            if definitions and pending_examples:
                definitions[-1].setdefault("examples", []).extend(pending_examples)
            pending_examples.clear()

        for raw_element in elements:
            if not isinstance(raw_element, dict):
                continue
            type_ = raw_element.get("type_")

            if type_ == "explanation":
                resolved = _resolve_placeholders(
                    raw_element.get("content", ""), raw_element.get("items", [])
                )
                resolved = re.sub(r"\s+", " ", resolved).strip()

                if _is_structural_context_label(raw_element, resolved):
                    continue
                if _is_redirect(resolved, raw_element.get("items", [])):
                    continue

                flush_examples()
                definitions.append({"source_id": source_id, "text": resolved, "examples": []})
                continue

            if type_ == "example":
                if text := _example_text(raw_element):
                    pending_examples.append(text)
                continue

            if type_ == "definition":
                if raw_element.get("sub_definition"):
                    for child in raw_element.get("elements", []):
                        if not isinstance(child, dict):
                            continue
                        if child.get("type_") == "example":
                            if text := _example_text(child):
                                pending_examples.append(text)
                    continue

                flush_examples()
                walk(raw_element.get("elements", []), source_id=raw_element.get("id"))
                continue

            if type_ == "sub_article":
                continue

        flush_examples()

    walk(body.get("definitions", []))
    return [
        definition
        for definition in definitions
        if definition.get("text", "").strip() and definition.get("source_id") is not None
    ]


def extract_senses(raw_dict: dict[str, Any]) -> list[dict[str, Any]]:
    return extract_definitions(raw_dict)


def extract_existing_translations(raw_dict: dict[str, Any]) -> dict[str, Any] | None:
    """Return a synthetic llm_result if the article already has embedded translations.

    This supports release rebuilds from enriched article snapshots without spending
    LLM calls on entries that already carry translation fields. Returns None if no
    translations are found, signalling that the LLM should be called.
    """
    lemmas = [lm for lm in raw_dict.get("lemmas", []) if isinstance(lm, dict)]
    primary = next(
        (lm.get("primary_translation") for lm in lemmas if lm.get("primary_translation")),
        None,
    )

    definitions: list[dict[str, Any]] = []

    def walk(elements: list[Any], source_id: int | None) -> None:
        for el in elements:
            if not isinstance(el, dict):
                continue
            t = el.get("type_")
            if t == "explanation":
                translation = el.get("translation") or ""
                if translation:
                    definitions.append({"source_id": source_id, "translation": translation})
            elif t == "definition" and not el.get("sub_definition"):
                walk(el.get("elements", []), source_id=el.get("id"))

    walk(raw_dict.get("body", {}).get("definitions", []), source_id=None)

    if not primary and not definitions:
        return None

    return {"definitions": definitions, "lemma_primary": primary or ""}


def extract_cross_reference(raw_dict: dict[str, Any]) -> dict[str, Any] | None:
    def walk(elements: list[Any]) -> dict[str, Any] | None:
        for element in elements:
            if not isinstance(element, dict):
                continue
            type_ = element.get("type_")
            if type_ == "explanation":
                content = element.get("content", "").strip()
                items = element.get("items", [])
                if _is_redirect(content, items):
                    for item in items:
                        if isinstance(item, dict) and item.get("type_") == "article_ref":
                            lemmas = item.get("lemmas", [])
                            first = lemmas[0] if lemmas else ""
                            return {
                                "article_id": item.get("article_id"),
                                "lemma": first.get("lemma", "")
                                if isinstance(first, dict)
                                else str(first),
                            }
            elif type_ == "definition" and not element.get("sub_definition"):
                result = walk(element.get("elements", []))
                if result:
                    return result
        return None

    return walk(raw_dict.get("body", {}).get("definitions", []))
