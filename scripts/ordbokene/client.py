from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Protocol

import requests

from .extract import extract_definitions
from .io import ExplodedEntry
from .prompt import build_prompt
from .settings import KNOWN_POS, OPENROUTER_URL, REQUEST_TIMEOUT, logger


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

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": build_prompt(prompt_entries)}],
        "max_tokens": max(
            len(batch) * 400
            + sum(
                len(definition["text"])
                for entry in prompt_entries
                for definition in entry["definitions"]
            )
            // 2,
            1024,
        ),
        "temperature": 0.1,
    }
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {index: "missing_api_key" for index in range(len(batch))}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, config.max_retries + 1):
        try:
            response = session.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt == config.max_retries:
                return {index: f"request_error: {exc}" for index in range(len(batch))}
            logger.warning("Request failed on attempt %s/%s: %s", attempt, config.max_retries, exc)
            # Short cap for transient network blips; rate-limit sleeps use the full retry_delay.
            _NETWORK_ERROR_MAX_SLEEP = 5
            time.sleep(min(_NETWORK_ERROR_MAX_SLEEP, config.retry_delay))
            continue

        if response.status_code in {429, 500, 502, 503, 504} and attempt < config.max_retries:
            logger.warning(
                "Transient HTTP %s on attempt %s/%s",
                response.status_code,
                attempt,
                config.max_retries,
            )
            time.sleep(config.retry_delay)
            continue
        if response.status_code != 200:
            return {index: f"http_{response.status_code}" for index in range(len(batch))}

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return {index: f"invalid_response: {exc}" for index in range(len(batch))}

        article_ids = [
            int(entry["article_id"]) for entry in prompt_entries if entry["article_id"] is not None
        ]
        return _parse_json_response(content, article_ids)

    return {index: "unknown_error" for index in range(len(batch))}


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
