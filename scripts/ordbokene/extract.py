from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .constants import (
    ABBREVIATIONS,
    CONTEXT_LABEL_ITEM_TYPES,
    KNOWN_POS,
    PLACEHOLDER_ONLY_RE,
    REDIRECT_RE,
    SEE_ALSO_RE,
)


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
    if content == "$" and any(
        isinstance(item, dict) and item.get("type_") == "article_ref" for item in items
    ):
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


def _is_see_also_pointer(text: str, items: list[dict[str, Any]]) -> bool:
    # Broader than _is_redirect: also catches "Se:", "Sjå:", and "jamfør" forms that
    # carry an article_ref item. The \b in SEE_ALSO_RE prevents matching the real verb
    # "jamføre" (no word boundary between "r" and "e").
    if not SEE_ALSO_RE.match(text):
        return False
    return any(isinstance(item, dict) and item.get("type_") == "article_ref" for item in items)


def _walk_definition_cores(raw_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Core traversal returning ALL pre-filter definition cores.

    Each core is a plain dict:
      - ``source_id``: the parent definition node's ``id`` (or ``None``).
      - ``text``: the resolved, whitespace-normalized Norwegian definition text.
      - ``explanation_element``: the RAW ``explanation`` element dict (for reading
        ``translation``).
      - ``example_elements``: list of RAW ``example`` element dict references in
        the exact order the current flatten produces (including
        ``sub_definition`` children and the ``pending``/``flush`` mechanism).

    This is the SINGLE source of ordering for embedding, read-back, prompting,
    and ``build_lemma`` consumption.  Callers apply the final empty-text /
    null-source_id filter via :func:`iter_definition_cores`.
    """
    body = raw_dict.get("body", {})
    cores: list[dict[str, Any]] = []

    def walk(
        elements: list[Any], source_id: int | None = None, context: tuple[str, ...] = ()
    ) -> None:
        pending: list[dict[str, Any]] = []
        scope_start = len(cores)

        def flush_examples() -> None:
            if len(cores) > scope_start and pending:
                cores[-1]["example_elements"].extend(pending)
            pending.clear()

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
                    if resolved:
                        context = (*context, resolved.rstrip(":"))
                    continue
                if _is_redirect(resolved, raw_element.get("items", [])):
                    continue
                if _is_see_also_pointer(resolved, raw_element.get("items", [])):
                    continue

                flush_examples()
                cores.append(
                    {
                        "source_id": source_id,
                        "text": resolved,
                        "explanation_element": raw_element,
                        "example_elements": [],
                        **({"context": list(context)} if context else {}),
                    }
                )
                continue

            if type_ == "example":
                if _example_text(raw_element):
                    pending.append(raw_element)
                continue

            if type_ == "definition":
                if raw_element.get("sub_definition"):
                    for child in raw_element.get("elements", []):
                        if not isinstance(child, dict):
                            continue
                        if child.get("type_") == "example":
                            if _example_text(child):
                                pending.append(child)
                    continue

                flush_examples()
                walk(
                    raw_element.get("elements", []),
                    source_id=raw_element.get("id"),
                    context=context,
                )
                continue

            if type_ == "sub_article":
                continue

        flush_examples()

    walk(body.get("definitions", []))
    return cores


def iter_definition_cores(raw_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Return SURVIVING definition cores (post empty-text / null-source_id filter).

    Each core carries ``source_id``, ``text``, ``explanation_element`` (raw dict
    ref), and ``example_elements`` (list of raw ``example`` element dict refs).
    This is the single shared traversal used by embedding, read-back, prompting,
    and ``build_lemma`` so all orderings stay aligned by construction.
    """
    return [
        core
        for core in _walk_definition_cores(raw_dict)
        if core.get("text", "").strip() and core.get("source_id") is not None
    ]


def extract_definitions(raw_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract real definitions from the article body, skipping structural labels."""
    return [
        {
            "source_id": core["source_id"],
            "text": core["text"],
            "examples": [_example_text(el) for el in core["example_elements"]],
            **({"context": core["context"]} if core.get("context") else {}),
        }
        for core in iter_definition_cores(raw_dict)
    ]


def extract_senses(raw_dict: dict[str, Any]) -> list[dict[str, Any]]:
    return extract_definitions(raw_dict)


def extract_existing_translations(raw_dict: dict[str, Any]) -> dict[str, Any] | None:
    """Return a synthetic llm_result if the article already has embedded translations.

    This supports release rebuilds from enriched article snapshots without spending
    LLM calls on entries that already carry translation fields. Returns None if no
    translations are found, signalling that the LLM should be called.

    Uses the SAME §0 core traversal as :func:`extract_definitions` so that
    definition translations and example English are read from the exact same raw
    element refs that ``build_lemma`` will consume, keeping the positional pairing
    lossless (including same-``source_id`` siblings, ``sub_definition`` flattening,
    and the post-filter index space).

    Returns non-None when any definition translation OR any example ``en`` exists
    so that ``cmd_export`` does not skip articles that carry example English but
    no definition/primary translation.
    """
    lemmas = [lm for lm in raw_dict.get("lemmas", []) if isinstance(lm, dict)]
    primary = next(
        (lm.get("primary_translation") for lm in lemmas if lm.get("primary_translation")),
        None,
    )

    definitions: list[dict[str, Any]] = []
    has_example_en = False

    for core in iter_definition_cores(raw_dict):
        explanation = core["explanation_element"]
        translation = explanation.get("translation") or ""
        examples_en = [el.get("en") or "" for el in core["example_elements"]]
        if any(en for en in examples_en):
            has_example_en = True
        definitions.append(
            {
                "source_id": core["source_id"],
                "translation": translation,
                "examples": examples_en,
            }
        )

    has_translation = any(defn["translation"] for defn in definitions)

    if not primary and not has_translation and not has_example_en:
        return None

    return {"definitions": definitions, "lemma_primary": primary or ""}


def extract_cross_reference(raw_dict: dict[str, Any]) -> dict[str, Any] | None:
    # A see-also pointer on a contentful article must NOT wipe its definitions.
    # Only return a cross_reference when the article has no surviving real
    # definition core (i.e. it is a pure redirect).
    #
    # Derive the target from the SAME broad see-also detection used to build
    # ``see_also`` (SEE_ALSO_RE), not the narrow ``_is_redirect``. Otherwise a
    # pure redirect whose only pointer is a colon/capital ``Se: X`` or
    # ``jamfør X`` would be dropped from the definition cores (broad filter) yet
    # fail to produce a cross_reference (narrow filter) — yielding empty
    # definitions AND a null cross_reference, which fails import.
    if iter_definition_cores(raw_dict):
        return None

    see_also = extract_see_also(raw_dict)
    if not see_also:
        return None

    first = see_also[0]
    return {"article_id": first["article_id"], "lemma": first["lemma"]}


def extract_see_also(raw_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract see-also cross-reference links from the article body.

    Walks the article and collects one entry per ``article_ref`` item on any
    ``explanation`` whose resolved text matches ``SEE_ALSO_RE``. Returns a
    de-duplicated (by ``article_id``), first-seen-ordered list of::

        {"article_id": int, "lemma": str, "relation": "see" | "compare"}
    """
    entries: list[dict[str, Any]] = []
    seen: set[int] = set()

    def walk(elements: list[Any]) -> None:
        for element in elements:
            if not isinstance(element, dict):
                continue
            type_ = element.get("type_")
            if type_ == "explanation":
                resolved = _resolve_placeholders(
                    element.get("content", ""), element.get("items", [])
                )
                resolved = re.sub(r"\s+", " ", resolved).strip()
                if not _is_see_also_pointer(resolved, element.get("items", [])):
                    continue
                relation = "compare" if resolved.lower().startswith("jamfør") else "see"
                for item in element.get("items", []):
                    if not (isinstance(item, dict) and item.get("type_") == "article_ref"):
                        continue
                    article_id = item.get("article_id")
                    if article_id is None or article_id in seen:
                        continue
                    lemmas = item.get("lemmas", [])
                    first = lemmas[0] if lemmas else ""
                    lemma = first.get("lemma", "") if isinstance(first, dict) else str(first)
                    seen.add(article_id)
                    entries.append(
                        {
                            "article_id": article_id,
                            "lemma": lemma,
                            "relation": relation,
                        }
                    )
            elif type_ == "definition":
                walk(element.get("elements", []))

    walk(raw_dict.get("body", {}).get("definitions", []))
    return entries


def is_expression(raw: dict) -> bool:
    return raw.get("word_class") == "EXPR" or any(
        "EXPR" in paradigm.get("tags", [])
        for lemma in raw.get("lemmas", [])
        if isinstance(lemma, dict)
        for paradigm in lemma.get("paradigm_info", [])
        if isinstance(paradigm, dict)
    )


def source_packet(article_id: int, raw: dict, articles_dir: Path | None = None) -> dict:
    if (
        type(article_id) is not int
        or type(raw.get("article_id")) is not int
        or raw["article_id"] != article_id
    ):
        raise ValueError("article_id_mismatch")
    lemmas = [lemma for lemma in raw.get("lemmas", []) if isinstance(lemma, dict)]
    tags = list(
        dict.fromkeys(
            str(tag)
            for lemma in lemmas
            for paradigm in lemma.get("paradigm_info", [])
            if isinstance(paradigm, dict)
            for tag in paradigm.get("tags", [])
        )
    )

    elements = list(_source_elements(raw.get("body", {}).get("definitions", [])))
    cores = iter_definition_cores(raw)
    definitions = extract_definitions(raw)
    surviving_explanations = {id(core["explanation_element"]) for core in cores}
    for element in elements:
        if (
            element.get("type_") == "explanation"
            and element.get("content", "").strip() == "$"
            and any(
                isinstance(item, dict) and item.get("type_") == "article_ref"
                for item in element.get("items", [])
            )
            and id(element) not in surviving_explanations
        ):
            targets = [
                (item.get("article_id"), item.get("definition_id"))
                for item in element.get("items", [])
                if item.get("type_") == "article_ref"
            ]
            raise ValueError(
                f"unresolved_semantic_reference: filtered_source_reference article={article_id} targets={targets}"
            )
    if not definitions:
        raise ValueError("no_source_definitions")
    if [
        _example_text(el) for el in elements if el.get("type_") == "example" and _example_text(el)
    ] != [example for definition in definitions for example in definition["examples"]]:
        raise ValueError("unattached_or_reordered_source_examples")
    for definition, core in zip(definitions, cores, strict=True):
        if type(definition["source_id"]) is not int:
            raise ValueError("invalid_source_id")
        explanation = core["explanation_element"]
        refs = _semantic_references(explanation)
        if not refs:
            continue
        if len(refs) != 1:
            targets = [(r.get("article_id"), r.get("definition_id")) for r in refs]
            raise ValueError(f"unresolved_semantic_reference: multi_target targets={targets}")
        definition["text"], definition["references"] = _resolve_reference(refs[0], articles_dir)
        # Retain the referring core's source ID and examples; never inherit target examples.
    return {
        "article_id": article_id,
        "lemmas": [str(lemma.get("lemma", "")) for lemma in lemmas],
        "hgno": lemmas[0].get("hgno") if lemmas else None,
        "pos": next((tag for tag in tags if tag in KNOWN_POS and tag != "EXPR"), ""),
        "is_expression": is_expression(raw),
        "definitions": definitions,
    }


def _source_elements(elements):
    for element in elements:
        if isinstance(element, dict) and element.get("type_") != "sub_article":
            yield element
            yield from _source_elements(element.get("elements", []))


def _semantic_references(explanation: dict) -> list[dict]:
    items = explanation.get("items", [])
    # Typed contrast/opposite/abbreviation items are lexical context, not aliases.
    if not re.fullmatch(r"[\s$;,]*", explanation.get("content", "")) or any(
        item.get("type_") != "article_ref" for item in items
    ):
        return []
    return items


def _resolve_reference(ref: dict, articles_dir: Path | None, trail: tuple = ()) -> tuple[str, list]:
    target_id = ref.get("article_id")
    target_sid = ref.get("definition_id", ref.get("target_definition_id"))
    identity = (target_id, target_sid)

    def fail(cause):
        raise ValueError(f"unresolved_semantic_reference: {cause} target={target_id}:{target_sid}")

    if type(target_id) is not int or (target_sid is not None and type(target_sid) is not int):
        fail("invalid_target_id")
    if identity in trail:
        fail("cycle")
    if len(trail) >= 8:
        fail("depth_limit")
    if articles_dir is None:
        fail("missing_articles_dir")
    try:
        target = json.loads((articles_dir / f"{target_id}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail("missing_target_article")
    except (OSError, ValueError):
        fail("invalid_target_article")
    if (
        not isinstance(target, dict)
        or type(target.get("article_id")) is not int
        or target["article_id"] != target_id
    ):
        fail("target_article_id_mismatch")
    cores = iter_definition_cores(target)
    if target_sid is None:
        # One extracted explanation, not merely one node containing several meanings.
        if len(cores) != 1:
            fail("unscoped_target_not_single_definition")
        target_sid = cores[0]["source_id"]
    else:
        cores = [core for core in cores if core["source_id"] == target_sid]
        if not cores:
            nodes = [
                node
                for node in _source_elements(target.get("body", {}).get("definitions", []))
                if node.get("type_") == "definition" and node.get("id") == target_sid
            ]
            if len(nodes) != 1:
                fail("unmatched_definition_id")
            # Promote only the explicitly selected sub-definition for extraction.
            selected = {**nodes[0], "sub_definition": False}
            cores = [
                core
                for core in iter_definition_cores({"body": {"definitions": [selected]}})
                if core["source_id"] == target_sid
            ]
            if not cores or all(core["text"].endswith(":") for core in cores):
                fail("no_target_definition")
    if type(target_sid) is not int:
        fail("invalid_definition_id")
    local = [core for core in cores if not _semantic_references(core["explanation_element"])]
    provenance = [[target_id, target_sid]]
    if local:
        # A source-defined meaning is sufficient even if aliases would form a cycle.
        return "; ".join(
            (", ".join(c["context"]) + ": " if c.get("context") else "") + c["text"] for c in local
        ), provenance
    refs = [r for core in cores for r in _semantic_references(core["explanation_element"])]
    if len(refs) != 1:
        fail("multi_target")
    text, chain = _resolve_reference(refs[0], articles_dir, (*trail, identity))
    return text, provenance + chain
