from __future__ import annotations

import copy
import time
from argparse import Namespace

import requests
from tqdm import tqdm

from .build import build_lemma
from .client import complete_existing_translation, request_translations
from .io import ExplodedEntry, collect_pending, explode, write_error, write_lemma
from .settings import logger
from .source import ensure_articles_dir


def process_batch(
    session: requests.Session,
    args: Namespace,
    batch: list[ExplodedEntry],
) -> int:
    batch_translations: dict[int, object] = {}
    needs_llm: dict[int, ExplodedEntry] = {}
    reuse_existing = getattr(args, "reuse_existing_translations", True)

    for index, (article_id, raw_dict) in enumerate(batch):
        existing = complete_existing_translation(raw_dict) if reuse_existing else None
        if existing is not None:
            batch_translations[index] = existing
        else:
            needs_llm[index] = (article_id, raw_dict)

    if needs_llm:
        llm_results = request_translations(session, args, list(needs_llm.values()))
        for local_idx, original_idx in enumerate(needs_llm):
            batch_translations[original_idx] = llm_results.get(local_idx, "missing_result")

    written = 0
    for index, (article_id, raw_dict) in enumerate(batch):
        result = batch_translations.get(index, "missing_result")
        lemmas = [lemma for lemma in raw_dict.get("lemmas", []) if isinstance(lemma, dict)]
        word = ", ".join(str(lemma.get("lemma", "")) for lemma in lemmas)

        if isinstance(result, str):
            write_error(args.error_log, args.lemma_dir / f"{article_id}.json", word, result)
            continue

        lemma_data = build_lemma(copy.deepcopy(raw_dict), result, article_id)
        if not args.dry_run:
            write_lemma(args.lemma_dir, article_id, lemma_data)
        written += 1

    return written


def run(args: Namespace) -> int:
    ensure_articles_dir(args.articles_dir)

    exploded = explode(args.articles_dir)
    pending = collect_pending(exploded, args.lemma_dir, args.force)
    logger.info(
        "Exploded %s articles, %s pending%s",
        len(exploded),
        len(pending),
        " with --force" if args.force else "",
    )

    if not pending:
        logger.info("Nothing to do — all articles already translated (use --force to reprocess)")
        return 0

    if args.dry_run:
        if args.limit:
            pending = pending[: args.limit]
        logger.info("Dry run — would process %s articles", len(pending))
        return 0

    if args.limit:
        pending = pending[: args.limit]

    written = 0
    started_at = time.time()
    session = requests.Session()
    batch_starts = range(0, len(pending), args.batch_size)

    progress = tqdm(batch_starts, unit="batch", desc="Translating")
    for start in progress:
        batch = pending[start : start + args.batch_size]
        written += process_batch(session, args, batch)
        elapsed = max(time.time() - started_at, 1e-6)
        progress.set_postfix(written=written, rate=f"{written / elapsed:.1f}/s")

    logger.info("Finished with %s lemma files written", written)
    return written
