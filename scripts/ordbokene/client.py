from __future__ import annotations

import json
import re
from typing import Any, Protocol

import requests

from .constants import KNOWN_POS
from .extract import extract_definitions
from .io import ExplodedEntry
from .llm import PROVIDERS, LlmConfig, LlmError
from .prompt import build_prompt
from .settings import MAX_EXAMPLES, logger

# Harnesses whose transport does not thread a reasoning-effort knob.
_EFFORT_IGNORED = frozenset({"openrouter", "claude"})


class TranslationConfig(Protocol):
    model: str
    max_retries: int
    retry_delay: int


def request_translations(
    session: requests.Session,
    config: TranslationConfig,
    batch: list[ExplodedEntry],
) -> dict[int, Any]:
    prompt_entries: list[dict[str, Any]] = []
    for _, raw_dict in batch:
        lemmas = [lemma for lemma in raw_dict.get("lemmas", []) if isinstance(lemma, dict)]
        first_lemma = lemmas[0] if lemmas else {}
        tags: list[str] = []
        for paradigm in first_lemma.get("paradigm_info", []):
            if isinstance(paradigm, dict):
                tags.extend(str(tag) for tag in paradigm.get("tags", []))
        pos = next((tag for tag in tags if tag in KNOWN_POS), "")
        prompt_entries.append(
            {
                "article_id": raw_dict.get("article_id"),
                "lemmas": [str(lemma.get("lemma", "")) for lemma in lemmas],
                "hgno": first_lemma.get("hgno"),
                "pos": pos,
                "tags": tags,
                "is_expression": "EXPR" in tags,
                "definitions": extract_definitions(raw_dict),
            }
        )

    # Example strings ride along in the prompt on this branch, so their length
    # feeds the token budget alongside each definition's text.
    max_tokens = max(
        len(batch) * 400
        + sum(
            len(definition["text"])
            + sum(
                len(ex)
                for ex in definition.get("examples", [])[:MAX_EXAMPLES]
                if isinstance(ex, str)
            )
            for entry in prompt_entries
            for definition in entry["definitions"]
        )
        // 2,
        1024,
    )

    llm_config = LlmConfig(
        model=config.model,
        harness=getattr(config, "harness", "openrouter"),
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
        reasoning_effort=getattr(config, "reasoning_effort", None),
    )
    if llm_config.reasoning_effort and llm_config.harness in _EFFORT_IGNORED:
        logger.warning("--reasoning-effort is ignored by the %s harness", llm_config.harness)

    provider = PROVIDERS.get(llm_config.harness)
    if provider is None:
        return {index: f"unknown_harness: {llm_config.harness}" for index in range(len(batch))}

    try:
        content = provider(session, llm_config, build_prompt(prompt_entries), max_tokens=max_tokens)
    except LlmError as exc:
        return {index: str(exc) for index in range(len(batch))}

    article_ids = [
        int(entry["article_id"]) for entry in prompt_entries if entry["article_id"] is not None
    ]
    return _parse_json_response(content, article_ids)


def _parse_json_response(content: str, article_ids: list[int]) -> dict[int, Any]:
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
        if match is not None:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

    if not isinstance(parsed, dict):
        return {index: "json_parse_failed" for index in range(len(article_ids))}

    return {
        index: parsed.get(str(article_id), "missing_entry")
        for index, article_id in enumerate(article_ids)
    }
