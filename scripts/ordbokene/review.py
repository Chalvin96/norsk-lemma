from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

from .llm import PROVIDERS, LlmConfig, LlmError


@dataclass(frozen=True)
class TranslationReviewItem:
    article_id: int
    lemma: str
    primary_translation: str
    definitions: list[dict[str, Any]]


class ReviewConfig(Protocol):
    review_model: str
    max_retries: int
    retry_delay: int


def collect_translation_reviews(
    lemma_dir: Path,
    *,
    limit: int | None = None,
) -> list[TranslationReviewItem]:
    items: list[TranslationReviewItem] = []
    for lemma_path in sorted(lemma_dir.glob("*.json")):
        if not lemma_path.stem.isdigit():
            continue
        data = json.loads(lemma_path.read_text(encoding="utf-8"))
        definitions = [
            {
                "index": index,
                "text": str(definition.get("text") or ""),
                "translation": str(definition.get("translation") or ""),
                "examples": [
                    _coerce_example(example)
                    for example in definition.get("examples", [])
                ],
            }
            for index, definition in enumerate(data.get("definitions", []))
            if isinstance(definition, dict)
        ]
        if not definitions:
            continue
        items.append(
            TranslationReviewItem(
                article_id=int(lemma_path.stem),
                lemma=_primary_lemma(data),
                primary_translation=_primary_translation(data),
                definitions=definitions,
            )
        )
        if limit is not None and len(items) >= limit:
            return items
    return items


def build_review_prompt(items: list[TranslationReviewItem]) -> str:
    payload = [
        {
            "id": index,
            "article_id": item.article_id,
            "lemma": item.lemma,
            "primary_translation": item.primary_translation,
            "definitions": item.definitions,
        }
        for index, item in enumerate(items)
    ]
    return f"""Review Norwegian Bokmål dictionary translations into English.

Review `primary_translation`, definition `translation`, and example `en` fields.
Use lemma and definition text as sense context. Flag only real issues:
- mistranslation: meaning is wrong or important content is missing
- too_literal: understandable but unnatural English
- wrong_sense: translation ignores the listed dictionary sense
- ordering_mismatch: example English belongs to a different Norwegian example
- empty_translation: required English field is blank
- register: tone/register is materially off
- primary_mismatch: primary_translation does not fit the definitions

Return ONLY JSON:
{{
  "issues": [
    {{
      "id": 0,
      "field": "primary_translation|definitions[0].translation|definitions[0].examples[0].en",
      "severity": "low|medium|high",
      "category": "mistranslation|too_literal|wrong_sense|ordering_mismatch|empty_translation|register|primary_mismatch",
      "problem": "short explanation",
      "suggested_en": "better English"
    }}
  ]
}}

If an item is acceptable, omit it from `issues`.

Items:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def request_translation_review(
    session: requests.Session,
    config: ReviewConfig,
    items: list[TranslationReviewItem],
) -> dict[str, Any] | str:
    if not items:
        return {"issues": []}

    llm_config = LlmConfig(
        model=config.review_model,
        harness=getattr(config, "harness", "openrouter"),
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
        reasoning_effort=getattr(config, "reasoning_effort", None),
    )
    provider = PROVIDERS.get(llm_config.harness)
    if provider is None:
        return f"unknown_harness: {llm_config.harness}"

    try:
        content = provider(
            session,
            llm_config,
            build_review_prompt(items),
            max_tokens=max(2048, len(items) * 350),
            temperature=0.0,
        )
    except LlmError as exc:
        return str(exc)

    parsed = parse_review_response(content)
    return _enrich_with_article_ids(parsed, items)


def parse_review_response(content: str) -> dict[str, Any] | str:
    content = content.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match is None:
            return "json_parse_failed"
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return "json_parse_failed"

    if not isinstance(parsed, dict) or not isinstance(parsed.get("issues"), list):
        return "invalid_review_shape"
    return parsed


def _primary_lemma(data: dict[str, Any]) -> str:
    for lemma in data.get("lemmas", []):
        if isinstance(lemma, dict) and lemma.get("lemma"):
            return str(lemma["lemma"])
    return ""


def _coerce_example(example: Any) -> dict[str, str]:
    """Coerce a raw example value to ``{"no": ..., "en": ...}``.

    Handles three shapes emitted across schema versions:
      - ``str``  (pre-v3 bare string) → ``{"no": str, "en": ""}``
      - ``dict`` with ``no``/``en`` keys (v3) → normalised strings
      - anything else → empty no/en
    """
    if isinstance(example, str):
        return {"no": example, "en": ""}
    if isinstance(example, dict):
        return {
            "no": str(example.get("no") or ""),
            "en": str(example.get("en") or ""),
        }
    return {"no": "", "en": ""}


def _enrich_with_article_ids(
    result: dict[str, Any] | str,
    items: list[TranslationReviewItem],
) -> dict[str, Any] | str:
    """Add ``article_id`` to each issue based on the batch ``id`` mapping."""
    if not isinstance(result, dict):
        return result
    id_to_article_id = {i: item.article_id for i, item in enumerate(items)}
    for issue in result.get("issues", []):
        if isinstance(issue, dict) and isinstance(issue.get("id"), (int, float)):
            issue.setdefault("article_id", id_to_article_id.get(int(issue["id"])))
    return result


def _primary_translation(data: dict[str, Any]) -> str:
    for lemma in data.get("lemmas", []):
        if isinstance(lemma, dict) and lemma.get("primary_translation"):
            return str(lemma["primary_translation"])
    return ""
