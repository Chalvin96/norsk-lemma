"""Translate example sentences and embed English into article JSON.

This module is the dedicated example-translation pipeline.  It uses the SAME
``iter_definition_cores`` traversal as :mod:`extract` / :mod:`build` so that the
prompt order, embed order, and read-back order are all identical by construction
(see plan §0).

Workflow per article:
  1. ``iter_definition_cores`` → resolved Norwegian text + raw ``example`` element
     refs per surviving definition (capped ``[:MAX_EXAMPLES]``).
  2. Select examples whose raw element lacks ``en`` (or all with ``--force``).
  3. Build a prompt showing the definition gloss as read-only context plus the
     enumerated Norwegian sentences.
  4. Request a positional JSON response ``{article_id: {def_index: {example_index: en}}}``.
  5. Parse + validate (indices match; reject ``en == no``, ``en == gloss``, leaked
     markers).  Skip + log on mismatch; never pad.
  6. Embed ``en`` directly onto the raw ``example`` element refs.
  7. Persist via :func:`embed.write_article` (atomic temp + rename).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol

import requests

from .embed import write_article
from .extract import _example_text, iter_definition_cores
from .settings import (
    DEFAULT_MODEL,
    MAX_EXAMPLES,
    OPENROUTER_URL,
    REQUEST_TIMEOUT,
    logger,
)

# Markers that indicate the LLM leaked prompt structure into the translation.
_LEAKED_MARKERS = ("\t", "Example:", "example:", "EXAMPLE:")


class ExampleConfig(Protocol):
    model: str
    max_retries: int
    retry_delay: int
    reasoning_effort: str | None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def collect_pending_examples(
    articles_dir: Path,
    *,
    force: bool = False,
    limit: int | None = None,
) -> list[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]]:
    """Return articles with untranslated examples.

    Each tuple is ``(article_id, raw_dict, definitions)`` where *definitions* is
    ``[(def_index, gloss_text, [raw_example_el, ...]), ...]``.

    With ``force=False`` only definitions that have at least one example whose
    raw element lacks ``en`` are included.  With ``force=True`` all definitions
    with examples are included.
    """
    return list(iter_pending_examples(articles_dir, force=force, limit=limit))


def iter_pending_examples(
    articles_dir: Path,
    *,
    force: bool = False,
    limit: int | None = None,
) -> Iterator[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]]:
    """Stream pending articles one at a time (constant memory).

    Same selection as :func:`collect_pending_examples` but lazy — only the
    current article is held, so a full-corpus run does not load ~100k dicts.
    """
    from .io import iter_exploded

    yielded = 0
    for article_id, raw in iter_exploded(articles_dir):
        defs_with_work: list[tuple[int, str, list[dict[str, Any]]]] = []
        for def_index, core in enumerate(iter_definition_cores(raw)):
            capped = core["example_elements"][:MAX_EXAMPLES]
            if not capped:
                continue
            if force:
                selected = capped
            else:
                selected = [el for el in capped if not (el.get("en") or "").strip()]
            if selected:
                defs_with_work.append((def_index, core["text"], selected))
        if defs_with_work:
            yield article_id, raw, defs_with_work
            yielded += 1
            if limit is not None and yielded >= limit:
                return


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_example_prompt(
    batch: list[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]],
) -> str:
    """Build the LLM prompt for example translation.

    *batch* is a list of ``(article_id, raw_dict, [(def_index, gloss, [el, ...]), ...])``.
    """
    blocks: list[str] = []
    for article_id, _raw, definitions in batch:
        for def_index, gloss, elements in definitions:
            lines = [f"Article {article_id}, definition {def_index}"]
            lines.append(f"  gloss: {gloss}")
            for ex_index, el in enumerate(elements):
                norwegian = _example_text(el)
                lines.append(f"  {ex_index}: {norwegian}")
            blocks.append("\n".join(lines))

    rendered = "\n\n".join(blocks)
    json_example = '{"1": {"0": {"0": "English translation", "1": "another translation"}}}'
    return f"""You are an expert Norwegian-to-English translator.

Translate ONLY the numbered example sentences below. The "gloss" line is
read-only context for the sense; do NOT translate or copy the gloss.

Return ONLY a JSON object mapping article IDs to definition indices to example
indices, where each value is the natural English translation of that example
sentence:
{json_example}

Rules:
- Translate each sentence into natural, fluent English.
- Mirror the source casing and punctuation: if the Norwegian is a lowercase
  fragment with no ending period, keep the English a lowercase fragment with no
  ending period; if it is a full sentence with a capital and a period, keep that.
  Do NOT add capitalization or terminal punctuation the source does not have.
- Do NOT copy the gloss text as the translation.
- Do NOT include any markers, labels, or extra formatting.
- If you cannot translate a sentence, omit it.

{rendered}
"""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Substrings (case-insensitive) in an LLM/CLI error that mean we've hit an
# account billing / usage / rate limit and should STOP rather than burn retries.
# Stopping is safe: the run is resumable (already-translated examples are skipped).
_QUOTA_ERROR_MARKERS = (
    "usage limit",
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "insufficient",
    "billing",
    "payment required",
    "402",
    "credit",
    "limit reached",
    "limit exceeded",
    "exceeded your",
)


def _is_quota_error(message: str) -> bool:
    """Return True if *message* signals a billing/usage/rate limit (hard stop)."""
    low = (message or "").lower()
    return any(marker in low for marker in _QUOTA_ERROR_MARKERS)


def _is_valid_translation(en: str, no: str, gloss: str) -> bool:
    """Return True if *en* is an acceptable translation of *no*."""
    en = (en or "").strip()
    if not en:
        return False
    if en == no.strip():
        return False
    if en == gloss.strip():
        return False
    if any(marker in en for marker in _LEAKED_MARKERS):
        return False
    return True


# ---------------------------------------------------------------------------
# Parse + validate LLM response
# ---------------------------------------------------------------------------

def parse_example_response(
    content: str,
    batch: list[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]],
) -> dict[int, dict[int, dict[int, str]]]:
    """Parse and validate the LLM positional response.

    Returns ``{article_id: {def_index: {example_index: en}}}``.

    On count/index mismatch for a definition, that definition's examples are
    skipped (never padded).  Invalid translations are silently dropped.
    """
    content = content.strip()
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1 :]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match is None:
            return {}
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return {}

    if not isinstance(parsed, dict):
        return {}

    result: dict[int, dict[int, dict[int, str]]] = {}

    for article_id, _raw, definitions in batch:
        article_key = str(article_id)
        raw_article = parsed.get(article_key)
        if not isinstance(raw_article, dict):
            result.setdefault(article_id, {})
            continue

        article_result: dict[int, dict[int, str]] = {}
        for def_index, gloss, elements in definitions:
            raw_def = raw_article.get(str(def_index))
            if not isinstance(raw_def, dict):
                continue

            # Require exact index match: the set of returned keys must equal
            # the set of requested indices.
            requested_keys = {str(i) for i in range(len(elements))}
            returned_keys = set(raw_def.keys())
            if returned_keys != requested_keys:
                logger.warning(
                    "article %d def %d: index mismatch (requested %s, got %s) — skipping",
                    article_id,
                    def_index,
                    sorted(requested_keys),
                    sorted(returned_keys),
                )
                continue

            def_result: dict[int, str] = {}
            for ex_index, el in enumerate(elements):
                en = raw_def.get(str(ex_index))
                if not isinstance(en, str):
                    continue
                no_text = _example_text(el)
                if _is_valid_translation(en, no_text, gloss):
                    def_result[ex_index] = en.strip()
                else:
                    logger.warning(
                        "article %d def %d ex %d: rejected translation %r",
                        article_id,
                        def_index,
                        ex_index,
                        en[:80],
                    )
            if def_result:
                article_result[def_index] = def_result

        result[article_id] = article_result

    return result


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------

def embed_example_translations(
    article: dict[str, Any],
    translations: dict[int, dict[int, dict[int, str]]],
) -> None:
    """Embed ``en`` values onto the raw example element refs via the §0 core.

    Mutates *article* in place.  *translations* is the full batch result
    ``{article_id: {def_index: {example_index: en}}}``.

    Only valid translations are embedded; invalid ones (``en == no``,
    ``en == gloss``, leaked markers) are silently dropped.
    """
    article_id = article.get("article_id")
    if article_id is None:
        return
    article_translations = translations.get(article_id, {})
    if not article_translations:
        return

    cores = iter_definition_cores(article)
    for def_index, core in enumerate(cores):
        def_translations = article_translations.get(def_index, {})
        if not def_translations:
            continue
        gloss = core["text"]
        capped = core["example_elements"][:MAX_EXAMPLES]
        for ex_index, el in enumerate(capped):
            en = def_translations.get(ex_index)
            if en is None:
                continue
            no_text = _example_text(el)
            if _is_valid_translation(en, no_text, gloss):
                el["en"] = en.strip()


# ---------------------------------------------------------------------------
# LLM request (mirrors client.py / review.py plumbing)
# ---------------------------------------------------------------------------

def request_example_translations(
    session: requests.Session,
    config: ExampleConfig,
    batch: list[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]],
) -> dict[int, dict[int, dict[int, str]]] | str:
    """Call the LLM and return parsed example translations per article."""
    if not batch:
        return {}

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return "missing_api_key"

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": build_example_prompt(batch)}],
        "temperature": 0.0,
        "max_tokens": max(
            1024,
            sum(len(elements) * 100 for _, _, defs in batch for _, _, elements in defs),
        ),
    }
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
                return f"request_error: {exc}"
            time.sleep(min(5, config.retry_delay))
            continue

        if response.status_code in {429, 500, 502, 503, 504} and attempt < config.max_retries:
            logger.warning(
                "HTTP %s on attempt %s/%s",
                response.status_code,
                attempt,
                config.max_retries,
            )
            time.sleep(config.retry_delay)
            continue
        if response.status_code != 200:
            return f"http_{response.status_code}"

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return f"invalid_response: {exc}"

        return parse_example_response(content, batch)

    return "unknown_error"


def request_example_translations_codex(
    _session: requests.Session,
    config: ExampleConfig,
    batch: list[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]],
) -> dict[int, dict[int, dict[int, str]]] | str:
    """Call Codex CLI and return parsed example translations per article.

    Temporary escape hatch for running the same JSON-only prompt through a local
    Codex subscription instead of OpenRouter billing.
    """
    if not batch:
        return {}

    prompt = build_example_prompt(batch)
    effort = getattr(config, "reasoning_effort", None)
    effort_args = ["-c", f"model_reasoning_effort={effort}"] if effort else []

    for attempt in range(1, config.max_retries + 1):
        output_path: Path | None = None
        try:
            with NamedTemporaryFile("w", suffix=".txt", delete=False) as output:
                output_path = Path(output.name)

            completed = subprocess.run(
                [
                    "codex",
                    "exec",
                    "--model",
                    config.model,
                    *effort_args,
                    "--sandbox",
                    "read-only",
                    "--ephemeral",
                    "--output-last-message",
                    str(output_path),
                    "-",
                ],
                input=prompt,
                text=True,
                capture_output=True,
                timeout=REQUEST_TIMEOUT * 5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if attempt == config.max_retries:
                return f"codex_error: {exc}"
            time.sleep(min(5, config.retry_delay))
            continue

        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "").strip()
            # Billing/usage/rate limits won't clear by retrying — return at once so
            # run() can stop cleanly instead of sleeping through the retry loop.
            if _is_quota_error(message):
                return f"codex_quota: {message[:500]}"
            if attempt == config.max_retries:
                return f"codex_exit_{completed.returncode}: {message[:500]}"
            logger.warning(
                "Codex CLI exited %s on attempt %s/%s: %s",
                completed.returncode,
                attempt,
                config.max_retries,
                message[:200],
            )
            time.sleep(config.retry_delay)
            continue

        try:
            content = output_path.read_text(encoding="utf-8") if output_path else ""
        except OSError as exc:
            if attempt == config.max_retries:
                return f"codex_output_error: {exc}"
            time.sleep(min(5, config.retry_delay))
            continue
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

        if not content.strip():
            if attempt == config.max_retries:
                return "codex_empty_response"
            time.sleep(min(5, config.retry_delay))
            continue

        return parse_example_response(content, batch)

    return "unknown_error"


# ---------------------------------------------------------------------------
# Run (entry point for CLI)
# ---------------------------------------------------------------------------

def run(
    articles_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    batch_size: int = 25,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    provider: str = "openrouter",
    max_retries: int = 3,
    retry_delay: int = 60,
    workers: int = 1,
    reasoning_effort: str | None = None,
    session: requests.Session | None = None,
    request_fn: Callable[..., Any] | None = None,
) -> int:
    """Translate untranslated example sentences and persist to articles.

    Returns the number of articles written.  When *request_fn* is supplied it
    replaces the real LLM call (used by tests).  Sequential and resumable: an
    example with non-empty ``en`` is skipped unless *force*.
    """
    from argparse import Namespace

    articles_dir = articles_dir.resolve()

    if dry_run:
        count = sum(1 for _ in iter_pending_examples(articles_dir, force=force, limit=limit))
        logger.info("dry-run — would translate examples in %d articles", count)
        return 0

    if session is None:
        session = requests.Session()

    if request_fn is None:
        if provider == "codex":
            request_fn = request_example_translations_codex
        else:
            request_fn = request_example_translations

    config = Namespace(
        model=model,
        max_retries=max_retries,
        retry_delay=retry_delay,
        reasoning_effort=reasoning_effort,
    )

    def _iter_batches() -> Iterator[list]:
        batch: list = []
        for item in iter_pending_examples(articles_dir, force=force, limit=limit):
            batch.append(item)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    batches = _iter_batches()

    def do_batch(batch: list) -> tuple[list, dict | str]:
        return batch, request_fn(session, config, batch)

    written = 0
    stopped = False

    def handle(batch: list, result: dict | str) -> bool:
        """Embed a completed batch. Return True if a quota/billing stop was hit."""
        nonlocal written
        if isinstance(result, str):
            if _is_quota_error(result):
                logger.error(
                    "BILLING/QUOTA LIMIT hit (%s) — stopping. Re-run the same command "
                    "later to resume from where it left off (already-translated "
                    "examples are skipped).",
                    result,
                )
                return True
            logger.error("batch error (articles %s): %s", [a for a, _, _ in batch], result)
            return False
        for article_id, raw, _defs in batch:
            if article_id not in result:
                continue
            embed_example_translations(raw, result)
            write_article(articles_dir, article_id, raw)
            written += 1
        return False

    if workers <= 1:
        for batch in batches:
            if handle(*do_batch(batch)):
                stopped = True
                break
    else:
        # Bounded concurrency: keep at most `workers` requests in flight, refill as
        # each completes, and stop refilling once a quota/billing limit is detected
        # (in-flight requests are drained, not cancelled).
        pending_iter = iter(batches)
        inflight: set = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in range(workers):
                nxt = next(pending_iter, None)
                if nxt is None:
                    break
                inflight.add(pool.submit(do_batch, nxt))
            while inflight:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    if handle(*fut.result()):
                        stopped = True
                if not stopped:
                    while len(inflight) < workers:
                        nxt = next(pending_iter, None)
                        if nxt is None:
                            break
                        inflight.add(pool.submit(do_batch, nxt))

    logger.info(
        "translate-examples %s — %d articles updated",
        "STOPPED at limit" if stopped else "done",
        written,
    )
    return written


# ---------------------------------------------------------------------------
# apply-review
# ---------------------------------------------------------------------------

# Strict allowlist: only definitions[N].examples[M].en is accepted.
_FIELD_PATH_RE = re.compile(r"^definitions\[(\d+)\]\.examples\[(\d+)\]\.en$")

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _meets_threshold(severity: str | None, threshold: str) -> bool:
    return _SEVERITY_RANK.get(severity or "low", 0) >= _SEVERITY_RANK.get(threshold, 1)


def apply_review(
    articles_dir: Path,
    review_path: Path,
    *,
    severity_threshold: str = "medium",
    dry_run: bool = False,
) -> int:
    """Apply review ``suggested_en`` values back into articles.

    Reads a review issues JSON, filters by severity, and writes ``suggested_en``
    onto the raw example element matched by ``(article_id, def_index,
    example_index)`` via the §0 core traversal (capped).

    Field paths are parsed against a strict allowlist (only
    ``definitions[N].examples[M].en``). Indices are range-checked. Idempotent.
    Returns the number of articles written.
    """
    articles_dir = articles_dir.resolve()
    review_data = json.loads(review_path.read_text(encoding="utf-8"))
    issues = review_data.get("issues", []) if isinstance(review_data, dict) else []

    # Group issues by article_id to minimise file I/O.
    by_article: dict[int, list[dict[str, Any]]] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = issue.get("severity")
        if not _meets_threshold(severity, severity_threshold):
            continue
        match = _FIELD_PATH_RE.match(issue.get("field", ""))
        if match is None:
            continue
        suggested = issue.get("suggested_en")
        if not isinstance(suggested, str) or not suggested.strip():
            continue
        article_id = issue.get("article_id")
        if not isinstance(article_id, int):
            continue
        def_index = int(match.group(1))
        example_index = int(match.group(2))
        by_article.setdefault(article_id, []).append(
            (def_index, example_index, suggested.strip())
        )

    if dry_run:
        logger.info("dry-run — would apply %d suggestions across %d articles",
                     sum(len(v) for v in by_article.values()), len(by_article))
        return 0

    written = 0
    for article_id, suggestions in by_article.items():
        article_path = articles_dir / f"{article_id}.json"
        if not article_path.exists():
            logger.warning("article %d not found — skipping", article_id)
            continue

        article = json.loads(article_path.read_text(encoding="utf-8"))
        cores = iter_definition_cores(article)
        changed = False

        for def_index, example_index, suggested in suggestions:
            if def_index >= len(cores):
                logger.warning(
                    "article %d: def_index %d out of range (%d defs) — skipping",
                    article_id, def_index, len(cores),
                )
                continue
            capped = cores[def_index]["example_elements"][:MAX_EXAMPLES]
            if example_index >= len(capped):
                logger.warning(
                    "article %d def %d: example_index %d out of range (%d examples) — skipping",
                    article_id, def_index, example_index, len(capped),
                )
                continue
            capped[example_index]["en"] = suggested
            changed = True

        if changed:
            write_article(articles_dir, article_id, article)
            written += 1

    logger.info("apply-review done — %d articles updated", written)
    return written
