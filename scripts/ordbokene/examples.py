"""Translate example sentences and embed English into article JSON.

This module is the dedicated example-translation pipeline.  It uses the SAME
``iter_definition_cores`` traversal as :mod:`extract` / :mod:`build` so that the
prompt order, embed order, and read-back order are all identical by construction
(see plan §0).

Workflow per article:
  1. ``iter_definition_cores`` → resolved Norwegian text + raw ``example`` element
     refs per surviving definition (all source examples).
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
import re
import subprocess as _subprocess
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Protocol

import requests

from .embed import write_article
from .extract import _example_text, iter_definition_cores
from .llm import PROVIDERS, LlmConfig, LlmError, is_quota_error, parse_fenced_json
from .settings import (
    DEFAULT_MODEL,
    logger,
)

# Kept as a module attribute for callers/tests that patch the legacy seam;
# transport execution itself lives in :mod:`ordbokene.llm`.
subprocess = _subprocess

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
            source_examples = core["example_elements"]
            if not source_examples:
                continue
            if force:
                selected = list(enumerate(source_examples))
            else:
                selected = [
                    (index, el)
                    for index, el in enumerate(source_examples)
                    if not (el.get("en") or "").strip()
                ]
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
            for ex_index, el in _indexed_examples(elements):
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
    parsed = parse_fenced_json(content)
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
            requested_keys = {str(i) for i, _ in _indexed_examples(elements)}
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
            for ex_index, el in _indexed_examples(elements):
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
        source_examples = core["example_elements"]
        for ex_index, el in _indexed_examples(source_examples):
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
    """Call OpenRouter and return parsed example translations per article."""
    return _request_example_translations_with_harness(session, config, batch, "openrouter")


def request_example_translations_codex(
    _session: requests.Session,
    config: ExampleConfig,
    batch: list[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]],
) -> dict[int, dict[int, dict[int, str]]] | str:
    """Call the local Codex harness and return parsed translations per article."""
    return _request_example_translations_with_harness(None, config, batch, "codex")


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
            if is_quota_error(result):
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
    example_index)`` via the §0 core traversal (all source examples).

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
        by_article.setdefault(article_id, []).append((def_index, example_index, suggested.strip()))

    if dry_run:
        logger.info(
            "dry-run — would apply %d suggestions across %d articles",
            sum(len(v) for v in by_article.values()),
            len(by_article),
        )
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
                    article_id,
                    def_index,
                    len(cores),
                )
                continue
            source_examples = cores[def_index]["example_elements"]
            if example_index >= len(source_examples):
                logger.warning(
                    "article %d def %d: example_index %d out of range (%d examples) — skipping",
                    article_id,
                    def_index,
                    example_index,
                    len(source_examples),
                )
                continue
            source_examples[example_index]["en"] = suggested
            changed = True

        if changed:
            write_article(articles_dir, article_id, article)
            written += 1

    logger.info("apply-review done — %d articles updated", written)
    return written


def _indexed_examples(elements: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    """Return source indices with raw example refs, accepting legacy batches."""
    indexed: list[tuple[int, dict[str, Any]]] = []
    for position, item in enumerate(elements):
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], int)
            and isinstance(item[1], dict)
        ):
            indexed.append((item[0], item[1]))
        elif isinstance(item, dict):
            indexed.append((position, item))
    return indexed


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


def _request_example_translations_with_harness(
    session: requests.Session | None,
    config: ExampleConfig,
    batch: list[tuple[int, dict[str, Any], list[tuple[int, str, list[dict[str, Any]]]]]],
    harness: str,
) -> dict[int, dict[int, dict[int, str]]] | str:
    """Call one configured LLM harness and parse example translations."""
    if not batch:
        return {}

    llm_config = LlmConfig(
        model=config.model,
        harness=harness,
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
        reasoning_effort=getattr(config, "reasoning_effort", None),
    )
    max_tokens = max(
        1024,
        sum(len(elements) * 100 for _, _, definitions in batch for _, _, elements in definitions),
    )
    try:
        content = PROVIDERS[harness](
            session if harness == "openrouter" else None,
            llm_config,
            build_example_prompt(batch),
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except LlmError as exc:
        return str(exc)
    return parse_example_response(content, batch)


def _meets_threshold(severity: str | None, threshold: str) -> bool:
    return _SEVERITY_RANK.get(severity or "low", 0) >= _SEVERITY_RANK.get(threshold, 1)
