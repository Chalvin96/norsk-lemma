"""Translate Ordbokene articles into lemma/{id}.json files."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ordbokene.build import build_lemma
from ordbokene.client import (
    _parse_json_response,
)
from ordbokene.client import (
    request_translations as translate_batch,
)
from ordbokene.extract import ABBREVIATIONS, _render_item, extract_definitions, extract_senses
from ordbokene.io import (
    _wrap_sub_as_article,
    collect_pending,
    explode,
    write_error,
    write_lemma,
)
from ordbokene.pipeline import process_batch as _pipeline_process_batch
from ordbokene.pipeline import run
from ordbokene.prompt import build_prompt
from ordbokene.settings import (
    DEFAULT_ARTICLES_DIR,
    DEFAULT_BATCH_SIZE,
    DEFAULT_ERROR_LOG,
    DEFAULT_LEMMA_DIR,
    DEFAULT_MODEL,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY,
)

__all__ = [
    "ABBREVIATIONS",
    "_parse_json_response",
    "_render_item",
    "_wrap_sub_as_article",
    "build_lemma",
    "build_prompt",
    "collect_pending",
    "explode",
    "extract_definitions",
    "extract_senses",
    "main",
    "parse_args",
    "process_batch",
    "run",
    "translate_batch",
    "write_error",
    "write_lemma",
]


def process_batch(session: object, args: argparse.Namespace, batch: list[tuple[int, dict]]) -> int:
    return _pipeline_process_batch(session, args, batch)  # type: ignore[arg-type]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate Ordbokene articles and output lemma JSON files."
    )
    parser.add_argument("--articles-dir", type=Path, default=DEFAULT_ARTICLES_DIR)
    parser.add_argument("--lemma-dir", type=Path, default=DEFAULT_LEMMA_DIR)
    parser.add_argument("--error-log", type=Path, default=DEFAULT_ERROR_LOG)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--fresh",
        action="store_false",
        dest="reuse_existing_translations",
        help="Call the LLM instead of reusing embedded translation fields.",
    )
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be greater than 0.")
    if args.max_retries <= 0:
        raise SystemExit("--max-retries must be greater than 0.")
    if args.retry_delay < 0:
        raise SystemExit("--retry-delay cannot be negative.")

    args.articles_dir = args.articles_dir.resolve()
    args.lemma_dir = args.lemma_dir.resolve()
    args.error_log = args.error_log.resolve()
    return args


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run(parse_args())


if __name__ == "__main__":
    main()
