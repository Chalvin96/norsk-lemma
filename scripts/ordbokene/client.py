from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Protocol

import requests

from .extract import (
    extract_definitions,
    extract_existing_translations,
    is_expression,
    source_packet,
)
from .io import ExplodedEntry, write_text_atomically
from .llm import PROVIDERS, LlmConfig, LlmError, is_quota_error
from .prompt import build_prompt, response_schema
from .settings import logger

# Bump when extraction, prompting or acceptance changes.
PIPELINE_VERSION = "dictionary-definitions-v3"

_LITERAL_ANNOTATION_RE = re.compile(
    r"(?:\(\s*(?:lit\.|literally\b)[^)]*\)|\[\s*(?:lit\.|literally\b)[^]]*\]|"
    r"\blit\.|\bliterally\s*:|\bliteral\s+translation\s*:)",
    re.I,
)
_LITERAL_ONLY_RE = re.compile(
    r"^\s*(?:(?:\(\s*)?(?:lit\.|literally\s*:|literal\s+translation\s*:)|"
    r"\(\s*literally\b|\[\s*(?:lit\.|literally\b))",
    re.I,
)

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
    harness = getattr(config, "harness", "openrouter")
    provider = PROVIDERS.get(harness)
    if provider is None:
        return {index: f"unknown_harness: {harness}" for index in range(len(batch))}
    if getattr(config, "reasoning_effort", None) and harness in _EFFORT_IGNORED:
        logger.warning("--reasoning-effort is ignored by the %s harness", harness)
    if len({aid for aid, _ in batch}) != len(batch):
        return {index: "duplicate_article_id" for index in range(len(batch))}

    cache_dir = getattr(config, "cache_dir", None)
    cache = Path(cache_dir) if cache_dir else None
    results: dict[int, Any] = {}
    pending: dict[int, dict] = {}
    paths: dict[int, Path] = {}
    for index, (article_id, raw) in enumerate(batch):
        try:
            packet = source_packet(article_id, raw, getattr(config, "articles_dir", None))
        except ValueError as exc:
            results[index] = str(exc)
            continue
        if cache:
            identity = (
                PIPELINE_VERSION,
                config.model,
                harness,
                getattr(config, "reasoning_effort", None),
                build_prompt([packet]),
            )
            fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            paths[index] = cache / str(article_id) / f"{fingerprint}.json"
            if getattr(config, "reuse_cached_translations", True):
                try:
                    cached = json.loads(paths[index].read_text(encoding="utf-8"))
                    if validate_translation(packet, cached) is None:
                        results[index] = cached
                        continue
                except (OSError, ValueError):
                    pass
        pending[index] = packet

    # Own a single retry budget here; transports perform one attempt. Successful
    # records leave pending immediately and are durable before the caller writes.
    for attempt in range(config.max_retries):
        if not pending:
            break
        packets = list(pending.values())
        llm_config = LlmConfig(
            model=config.model,
            harness=harness,
            max_retries=1,
            retry_delay=config.retry_delay,
            reasoning_effort=getattr(config, "reasoning_effort", None),
            response_schema=response_schema(packets),
        )
        prompt = build_prompt(packets)
        max_tokens = max(
            1024,
            len(packets) * 400 + sum(len(d["text"]) for p in packets for d in p["definitions"]),
        )
        try:
            content = provider(session, llm_config, prompt, max_tokens=max_tokens)
            parsed = _parse_json_response(content, [p["article_id"] for p in packets])
        except LlmError as exc:
            if exc.code == "missing_api_key":
                results.update({index: str(exc) for index in pending})
                break
            parsed = {i: str(exc) for i in range(len(packets))}
        stopped = False
        for local_index, (index, packet) in enumerate(list(pending.items())):
            result = parsed.get(local_index, "missing_result")
            error = result if isinstance(result, str) else validate_translation(packet, result)
            if error:
                results[index] = error
                stopped = stopped or is_quota_error(error)
                continue
            results[index] = result
            if index in paths:
                write_text_atomically(paths[index], json.dumps(result, ensure_ascii=False))
            del pending[index]
        if stopped:
            break
        if pending and attempt + 1 < config.max_retries:
            time.sleep(config.retry_delay)
    return results


def _parse_json_response(content: str, article_ids: list[int]) -> dict[int, Any]:
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate_json_key")
            obj[key] = value
        return obj

    try:
        parsed = json.loads(content, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        return {index: "json_parse_failed" for index in range(len(article_ids))}
    if not isinstance(parsed, dict):
        return {index: "json_parse_failed" for index in range(len(article_ids))}
    if set(parsed) - {str(aid) for aid in article_ids}:
        return {index: "unexpected_article_id" for index in range(len(article_ids))}
    return {
        index: parsed.get(str(article_id), "missing_entry")
        for index, article_id in enumerate(article_ids)
    }


def validate_translation(packet: dict, result: Any) -> str | None:
    """Fail closed before caching/embedding, retaining repeated-id occurrence order.

    This checks structure and detectable literal-only failures, not arbitrary
    semantic equivalence. Dictionary fidelity still requires linguistic review.
    """
    if not isinstance(result, dict) or set(result) != {"definitions", "lemma_primary"}:
        return "invalid_record_shape"
    if not isinstance(result["lemma_primary"], str) or not result["lemma_primary"].strip():
        return "blank_primary"
    if _LITERAL_ANNOTATION_RE.search(result["lemma_primary"]):
        return "literal_in_primary"
    definitions = result["definitions"]
    if not isinstance(definitions, list) or len(definitions) != len(packet["definitions"]):
        return "definition_cardinality_mismatch"
    for source, translated in zip(packet["definitions"], definitions, strict=True):
        if not isinstance(translated, dict) or set(translated) != {"source_id", "translation"}:
            return "invalid_definition_shape"
        if (
            type(translated["source_id"]) is not int
            or translated["source_id"] != source["source_id"]
        ):
            return "source_id_mismatch"
        meaning = translated["translation"]
        if not isinstance(meaning, str) or not meaning.strip():
            return "blank_definition"
        if packet.get("is_expression") and _LITERAL_ONLY_RE.search(meaning):
            return "literal_only_expression"
    return None


def complete_existing_translation(raw: dict) -> dict | None:
    """Reuse only complete definitions; embedded example alignment is source-derived."""
    result = extract_existing_translations(raw)
    if result is None:
        return None
    definitions = [
        {"source_id": d["source_id"], "translation": d["translation"]}
        for d in result["definitions"]
    ]
    packet = {"definitions": extract_definitions(raw), "is_expression": is_expression(raw)}
    if validate_translation(packet, {**result, "definitions": definitions}) is None:
        return result
    return None
