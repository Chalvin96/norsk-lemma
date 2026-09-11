"""Unified CLI for the Bokmål lexicon data pipeline.

Stages in order:
  fetch               Download article JSONs from Ordbøkene into data/articles/.
  hydrate             Embed translations from an existing lemma/ release into articles
                      (alternative to running translate; no LLM needed).
  translate           Call LLM for untranslated articles; embed results back into articles.
  translate-examples  Call LLM for untranslated example sentences; embed en into articles.
  pronounce           Enrich articles with IPA pronunciation in-place.
  export              Build tracked lemma/ from enriched articles (no LLM needed).
  frequency           Annotate lemma/ with Kelly frequency_rank (post-export).
  audio               Generate lemma audio from exported lemma JSON.
  review              Review exported English translations via LLM.
  apply-review        Apply review suggested_en values back into articles.
  build               Run fetch → translate → pronounce → export in sequence.

Typical workflow when a release already exists:
  python pipeline.py fetch
  python pipeline.py hydrate
  python pipeline.py pronounce
  python pipeline.py export
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

import requests
from tqdm import tqdm

from . import frequency
from .arguments import add_audio_args, add_pronunciation_args
from .build import build_lemma
from .client import complete_existing_translation, request_translations
from .embed import embed_translations, write_article
from .extract import (
    _example_text,
    extract_definitions,
    extract_existing_translations,
    iter_definition_cores,
)
from .io import ExplodedEntry, explode, write_error, write_lemma
from .llm import is_quota_error
from .review import build_review_prompt, collect_translation_reviews, request_translation_review
from .settings import (
    DEFAULT_ARTICLES_DIR,
    DEFAULT_BATCH_SIZE,
    DEFAULT_CACHE_DIR,
    DEFAULT_ERROR_LOG,
    DEFAULT_HARNESS,
    DEFAULT_KELLY_CSV,
    DEFAULT_KELLY_REPORT,
    DEFAULT_LEMMA_DIR,
    DEFAULT_MODEL,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_REVIEW_MODEL,
    HARNESS_CHOICES,
    logger,
)
from .source import ensure_articles_dir

# ---------------------------------------------------------------------------
# Shared argument helpers
# ---------------------------------------------------------------------------


def _add_dir_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--articles-dir", type=Path, default=DEFAULT_ARTICLES_DIR)
    p.add_argument("--lemma-dir", type=Path, default=DEFAULT_LEMMA_DIR)


def _add_llm_args(
    p: argparse.ArgumentParser,
    *,
    model_option: str = "--model",
    model_default: str = DEFAULT_MODEL,
    include_batch: bool = True,
    include_error_log: bool = True,
) -> None:
    p.add_argument(model_option, default=model_default)
    p.add_argument(
        "--harness",
        default=DEFAULT_HARNESS,
        choices=HARNESS_CHOICES,
        help="Transport backend: openrouter (HTTP) or a local agentic CLI.",
    )
    p.add_argument(
        "--reasoning-effort",
        default=None,
        metavar="EFFORT",
        help="Reasoning effort passed to CLI harnesses that support it (ignored by openrouter).",
    )
    if include_batch:
        p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Durable definition translation cache; use a new directory to bypass it.",
    )
    p.add_argument("--max-retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY)
    if include_error_log:
        p.add_argument("--error-log", type=Path, default=DEFAULT_ERROR_LOG)


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def cmd_fetch(args: argparse.Namespace) -> None:
    articles_dir = args.articles_dir.resolve()
    if getattr(args, "dry_run", False):
        existing = sum(1 for _ in articles_dir.glob("*.json")) if articles_dir.exists() else 0
        action = "clear and re-download" if args.force else "download if empty"
        logger.info("dry-run — would %s articles (%d currently present)", action, existing)
        return
    if args.force and articles_dir.exists():
        logger.info("--force: clearing %s", articles_dir)
        for f in articles_dir.glob("*.json"):
            f.unlink()
    ensure_articles_dir(articles_dir)
    count = sum(1 for _ in articles_dir.glob("*.json"))
    logger.info("fetch done — %d articles in %s", count, articles_dir)


# ---------------------------------------------------------------------------
# hydrate  (download release + embed translations into articles, no LLM)
# ---------------------------------------------------------------------------

_GITHUB_REPO = "Chalvin96/norsk-lemma"
_GITHUB_API = "https://api.github.com/repos"


def _download_lemma_release(tag: str | None, lemma_dir: Path) -> str:
    """Download and extract a GitHub release to lemma_dir. Returns the resolved tag."""
    import tarfile
    import tempfile

    if tag:
        url = f"{_GITHUB_API}/{_GITHUB_REPO}/releases/tags/{tag}"
    else:
        url = f"{_GITHUB_API}/{_GITHUB_REPO}/releases/latest"

    resp = requests.get(url, timeout=30)
    if not resp.ok:
        sys.exit(f"GitHub API error {resp.status_code}: {resp.text[:200]}")
    release = resp.json()
    resolved_tag = release["tag_name"]

    asset_name = f"norsk-lemma-{resolved_tag}.tar.gz"
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == asset_name]
    if len(assets) != 1:
        available = ", ".join(str(asset.get("name", "")) for asset in release.get("assets", []))
        sys.exit(
            f"Could not identify the lemma archive for release {resolved_tag}; "
            f"expected {asset_name!r}. Available assets: {available}"
        )
    tar_asset = assets[0]

    size_mb = tar_asset["size"] / 1024 / 1024
    logger.info("Downloading %s (%.1f MB)", tar_asset["name"], size_mb)

    archive_path: Path | None = None
    staging_dir: Path | None = None
    backup_dir: Path | None = None
    try:
        with requests.get(tar_asset["browser_download_url"], stream=True, timeout=300) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                archive_path = Path(tmp.name)
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        tmp.write(chunk)

        # Extract to a temp dir first so lemma_dir is not wiped on failure.
        with tempfile.TemporaryDirectory() as tmp_dir:
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(tmp_dir, filter="data")
            extracted_root = Path(tmp_dir)
            lemma_roots = [path for path in extracted_root.rglob("lemma") if path.is_dir()]
            json_files = [
                path
                for lemma_root in lemma_roots
                for path in lemma_root.glob("*.json")
                if path.stem.isdigit()
            ]
            if not json_files:
                sys.exit(f"No numeric lemma JSON files found in release archive {resolved_tag}")
            for path in json_files:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    sys.exit(
                        f"Invalid lemma JSON in release archive {resolved_tag}: {path.name}: {exc}"
                    )
                if not isinstance(payload, dict) or not isinstance(payload.get("lemmas"), list):
                    sys.exit(
                        f"Unexpected non-lemma JSON in release archive {resolved_tag}: {path.name}"
                    )
            lemma_dir.parent.mkdir(parents=True, exist_ok=True)
            staging_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{lemma_dir.name}.staging-",
                    dir=lemma_dir.parent,
                )
            )
            for f in json_files:
                shutil.move(str(f), staging_dir / f.name)

            try:
                if lemma_dir.exists():
                    backup_dir = lemma_dir.with_name(f".{lemma_dir.name}.backup-{uuid.uuid4().hex}")
                    os.replace(lemma_dir, backup_dir)
                os.replace(staging_dir, lemma_dir)
                staging_dir = None
            except BaseException:
                if backup_dir is not None and backup_dir.exists():
                    if lemma_dir.exists():
                        shutil.rmtree(lemma_dir)
                    os.replace(backup_dir, lemma_dir)
                    backup_dir = None
                raise
            if backup_dir is not None:
                try:
                    shutil.rmtree(backup_dir)
                except OSError as exc:
                    logger.warning(
                        "Could not remove old lemma export backup %s: %s",
                        backup_dir,
                        exc,
                    )
                backup_dir = None
    finally:
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)

    count = sum(1 for _ in lemma_dir.glob("*.json"))
    logger.info("release %s extracted — %d lemma files", resolved_tag, count)
    return resolved_tag


def cmd_hydrate(args: argparse.Namespace) -> None:
    """Download a release by tag (default: latest), then embed translations into articles."""
    articles_dir = args.articles_dir.resolve()
    lemma_dir = args.lemma_dir.resolve()

    if not args.dry_run:
        _download_lemma_release(getattr(args, "tag", None), lemma_dir)

    if not lemma_dir.exists() or not any(lemma_dir.glob("*.json")):
        sys.exit(f"lemma dir empty or missing: {lemma_dir}")

    if args.dry_run and (not articles_dir.exists() or not any(articles_dir.glob("*.json"))):
        logger.info("dry-run — article directory is empty; skipping source download")
        return
    ensure_articles_dir(articles_dir)
    exploded = explode(articles_dir)
    article_map = {aid: raw for aid, raw in exploded}

    pending = []
    for lemma_path in sorted(lemma_dir.glob("*.json")):
        if not lemma_path.stem.isdigit():
            continue
        article_id = int(lemma_path.stem)
        raw = article_map.get(article_id)
        if raw is None:
            continue
        if not args.force and extract_existing_translations(raw) is not None:
            continue
        pending.append((article_id, raw, lemma_path))

    logger.info("hydrate — %d articles to update", len(pending))
    if args.dry_run:
        logger.info("dry-run — would hydrate %d articles", len(pending))
        return

    if args.limit:
        pending = pending[: args.limit]

    written = 0
    for article_id, raw, lemma_path in tqdm(pending, unit="article", desc="Hydrating"):
        lemma_data = json.loads(lemma_path.read_text(encoding="utf-8"))

        primary = next(
            (
                lm["primary_translation"]
                for lm in lemma_data.get("lemmas", [])
                if isinstance(lm, dict) and lm.get("primary_translation")
            ),
            "",
        )
        article_defs = extract_definitions(raw)
        exported_by_text: dict[str, list[dict]] = {}
        for definition in lemma_data.get("definitions", []):
            exported_by_text.setdefault(definition.get("text", ""), []).append(definition)
        synthetic_defs = []
        for definition, core in zip(article_defs, iter_definition_cores(raw), strict=True):
            candidates = exported_by_text.get(definition["text"], [])
            exported = candidates.pop(0) if candidates else {}
            # Releases may predate added senses/examples. Match Norwegian text,
            # never zip another record's sentences by position or erase new ones.
            english_by_no: dict[str, list[str]] = {}
            for example in exported.get("examples", []):
                if isinstance(example, dict):
                    english_by_no.setdefault(example.get("no", ""), []).append(
                        example.get("en", "")
                    )
            example_translations = []
            for element in core["example_elements"]:
                matches = english_by_no.get(_example_text(element), [])
                example_translations.append(matches.pop(0) if matches else element.get("en", ""))
            synthetic_defs.append(
                {
                    "source_id": definition["source_id"],
                    "translation": exported.get("translation", ""),
                    "examples": example_translations,
                }
            )

        article = copy.deepcopy(raw)
        embed_translations(article, {"lemma_primary": primary, "definitions": synthetic_defs})
        write_article(articles_dir, article_id, article)
        written += 1

    logger.info("hydrate done — %d articles updated", written)


# ---------------------------------------------------------------------------
# translate
# ---------------------------------------------------------------------------


def cmd_translate(args: argparse.Namespace) -> None:
    articles_dir = args.articles_dir.resolve()
    if args.dry_run and (not articles_dir.exists() or not any(articles_dir.glob("*.json"))):
        logger.info("dry-run — article directory is empty; skipping source download")
        return
    ensure_articles_dir(articles_dir)

    exploded = explode(articles_dir)
    if ids_file := getattr(args, "article_ids_file", None):
        ids = json.loads(ids_file.read_text(encoding="utf-8"))
        if not isinstance(ids, list) or any(type(aid) is not int or aid <= 0 for aid in ids):
            raise ValueError("--article-ids-file must contain a JSON array of positive integer IDs")
        ids = set(ids)
        missing = ids - {aid for aid, _ in exploded}
        if missing:
            raise ValueError(f"selected article IDs not found: {sorted(missing)}")
        exploded = [(aid, raw) for aid, raw in exploded if aid in ids]

    pending = _collect_untranslated(exploded, args.force)
    logger.info("%d/%d articles need translation", len(pending), len(exploded))

    if not pending or args.dry_run:
        if args.dry_run:
            logger.info("dry-run — would translate %d articles", len(pending))
        return

    if args.limit:
        pending = pending[: args.limit]

    session = requests.Session()
    written = 0

    batch_starts = range(0, len(pending), args.batch_size)
    progress = tqdm(batch_starts, unit="batch", desc="Translating")
    stopped = False
    for start in progress:
        batch = pending[start : start + args.batch_size]
        results = request_translations(session, args, batch)
        for local_idx, (article_id, raw) in enumerate(batch):
            result = results.get(local_idx)
            if isinstance(result, str) and is_quota_error(result):
                stopped = True
                # Continue through this batch so successful responses already
                # returned alongside the quota error are still persisted.
                write_error(
                    args.error_log,
                    articles_dir / f"{article_id}.json",
                    str(article_id),
                    result,
                )
                continue
            if not isinstance(result, dict):
                write_error(
                    args.error_log,
                    articles_dir / f"{article_id}.json",
                    str(article_id),
                    str(result),
                )
                continue
            article = copy.deepcopy(raw)
            embed_translations(article, result)
            write_article(articles_dir, article_id, article)
            written += 1
        progress.set_postfix(written=written)
        if stopped:
            logger.error(
                "BILLING/QUOTA LIMIT hit — stopping after current batch; "
                "re-run later to resume remaining articles."
            )
            break

    logger.info(
        "translate %s — %d articles updated", "stopped at limit" if stopped else "done", written
    )


# ---------------------------------------------------------------------------
# pronounce
# ---------------------------------------------------------------------------


def cmd_pronounce(args: argparse.Namespace) -> None:
    if getattr(args, "dry_run", False):
        logger.info("dry-run — skipping pronounce")
        return
    try:
        from enrich_pronunciation import run as _pronounce_run  # type: ignore[import]
    except ImportError:
        enrich_path = Path(__file__).parents[1] / "enrich_pronunciation.py"
        sys.path.insert(0, str(enrich_path.parent))
        from enrich_pronunciation import run as _pronounce_run  # type: ignore[import]

    _pronounce_run(
        args.articles_dir.resolve(),
        Path(args.leksika),
        Path(args.newwords),
        workers=args.workers,
        force=args.force,
        limit=args.limit,
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> None:
    articles_dir = args.articles_dir.resolve()
    lemma_dir = args.lemma_dir.resolve()

    exploded = explode(articles_dir)
    pending = exploded

    logger.info("export — %d/%d articles pending", len(pending), len(exploded))

    if args.dry_run:
        logger.info("dry-run — would export %d lemma files", len(pending))
        return

    if args.limit:
        pending = pending[: args.limit]

    written = 0
    for article_id, raw in tqdm(pending, unit="article", desc="Exporting"):
        output_path = lemma_dir / f"{article_id}.json"
        if not raw.get("lemmas"):
            if output_path.exists():
                output_path.unlink()
                logger.info("export — removed stale empty-lemma file %s", output_path)
            continue
        existing = extract_existing_translations(raw)
        if existing is None:
            logger.debug("article %d has no embedded translations — skipping", article_id)
            continue
        lemma_data = build_lemma(copy.deepcopy(raw), existing, article_id)
        if not lemma_data.get("lemmas"):
            if output_path.exists():
                output_path.unlink()
                logger.info("export — removed stale empty-lemma file %s", output_path)
            continue
        old_data: dict[str, object] = {}
        if output_path.exists():
            try:
                loaded = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                old_data = loaded
        _preserve_frequency_metadata(lemma_data, old_data)
        if not args.force and old_data == lemma_data:
            continue
        write_lemma(lemma_dir, article_id, lemma_data)
        written += 1

    logger.info("export done — %d lemma files written to %s", written, lemma_dir)


# ---------------------------------------------------------------------------
# build  (fetch → translate → pronounce → export → frequency)
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> None:
    cmd_fetch(args)
    cmd_translate(args)
    cmd_pronounce(args)
    cmd_export(args)
    dry_run = getattr(args, "dry_run", False)
    frequency.run(
        args.lemma_dir.resolve(),
        DEFAULT_KELLY_CSV,
        None if dry_run else DEFAULT_KELLY_REPORT,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# frequency
# ---------------------------------------------------------------------------


def cmd_frequency(args: argparse.Namespace) -> None:
    lemma_dir = args.lemma_dir.resolve()
    csv_path = args.kelly_csv.resolve()
    report_path = None if args.dry_run else args.report.resolve()
    report = frequency.run(lemma_dir, csv_path, report_path, dry_run=args.dry_run)
    logger.info(
        "frequency done — %d/%d Kelly keys matched, %d unmatched, %d ambiguous, %d lemmas ranked",
        report["matched_keys"],
        report["kelly_entries"],
        report["unmatched_keys"],
        report["ambiguous_keys"],
        report["lemmas_ranked"],
    )


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


def cmd_audio(args: argparse.Namespace) -> None:
    try:
        import generate_audio  # type: ignore[import]
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parents[1]))
        import generate_audio  # type: ignore[import]

    generate_audio.run(
        lemma_dir=args.lemma_dir.resolve(),
        articles_dir=args.articles_dir.resolve(),
        audio_dir=args.audio_dir.resolve(),
        voice=args.voice,
        language_code=args.language_code,
        dry_run=args.dry_run,
        limit=args.limit,
        force=args.force,
        enrich_only=args.enrich_only,
        confirm_cost=args.confirm_cost,
        price_per_million_chars=args.price_per_million_chars,
        list_voices=args.list_voices,
        workers=args.workers,
    )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def cmd_review(args: argparse.Namespace) -> None:
    lemma_dir = args.lemma_dir.resolve()
    items = collect_translation_reviews(lemma_dir, limit=args.limit)
    logger.info("review — %d lemma entries selected", len(items))

    if args.dry_run:
        print(build_review_prompt(items))
        return

    result = request_translation_review(requests.Session(), args, items)
    if not isinstance(result, dict):
        sys.exit(f"review failed: {result}")

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        logger.info("review written to %s", args.output)
    else:
        print(text)


# ---------------------------------------------------------------------------
# translate-examples
# ---------------------------------------------------------------------------


def cmd_translate_examples(args: argparse.Namespace) -> None:
    from .examples import run as run_examples

    articles_dir = args.articles_dir.resolve()
    count = run_examples(
        articles_dir,
        model=args.model,
        batch_size=args.batch_size,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        provider=args.provider,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
        workers=args.workers,
        reasoning_effort=args.reasoning_effort,
    )
    logger.info("translate-examples — %d articles updated", count)


# ---------------------------------------------------------------------------
# apply-review
# ---------------------------------------------------------------------------


def cmd_apply_review(args: argparse.Namespace) -> None:
    from .examples import apply_review

    articles_dir = args.articles_dir.resolve()
    count = apply_review(
        articles_dir,
        args.review,
        severity_threshold=args.severity_threshold,
        dry_run=args.dry_run,
    )
    logger.info("apply-review — %d articles updated", count)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="pipeline",
        description="Bokmål lexicon data pipeline.",
    )
    sub = root.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = sub.add_parser("fetch", help="Download raw articles from Ordbøkene.")
    _add_dir_args(p_fetch)
    p_fetch.add_argument("--force", action="store_true", help="Re-download even if articles exist.")
    p_fetch.add_argument("--dry-run", action="store_true")

    # hydrate
    p_hy = sub.add_parser(
        "hydrate",
        help="Download a release and embed its translations into articles (no LLM).",
    )
    _add_dir_args(p_hy)
    _add_run_args(p_hy)
    p_hy.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help="Release tag to download (e.g. v1.3.0). Defaults to the latest release.",
    )

    # translate
    p_tr = sub.add_parser("translate", help="Translate articles via LLM; embed into articles.")
    _add_dir_args(p_tr)
    _add_llm_args(p_tr)
    _add_run_args(p_tr)
    p_tr.add_argument(
        "--article-ids-file",
        type=Path,
        help="Translate only IDs in this JSON array; source directory still supplies references.",
    )

    # pronounce
    p_pr = sub.add_parser("pronounce", help="Enrich articles with IPA pronunciation.")
    _add_dir_args(p_pr)
    _add_run_args(p_pr)
    add_pronunciation_args(p_pr)

    # export
    p_ex = sub.add_parser("export", help="Build lemma/ from enriched articles.")
    _add_dir_args(p_ex)
    _add_run_args(p_ex)

    # frequency
    p_freq = sub.add_parser(
        "frequency",
        help="Annotate lemma/ with Kelly frequency_rank (post-export, in place).",
    )
    _add_dir_args(p_freq)
    p_freq.add_argument("--kelly-csv", type=Path, default=DEFAULT_KELLY_CSV)
    p_freq.add_argument("--report", type=Path, default=DEFAULT_KELLY_REPORT)
    p_freq.add_argument("--dry-run", action="store_true")

    # audio
    p_audio = sub.add_parser("audio", help="Generate lemma audio with Google Cloud Text-to-Speech.")
    _add_dir_args(p_audio)
    _add_run_args(p_audio)
    add_audio_args(p_audio)

    # review
    p_review = sub.add_parser("review", help="Review exported English translations via LLM.")
    _add_dir_args(p_review)
    _add_llm_args(
        p_review,
        model_option="--review-model",
        model_default=DEFAULT_REVIEW_MODEL,
        include_batch=False,
        include_error_log=False,
    )
    p_review.add_argument("--limit", type=int, default=100)
    p_review.add_argument("--dry-run", action="store_true")
    p_review.add_argument("--output", type=Path)

    # translate-examples
    p_te = sub.add_parser(
        "translate-examples",
        help="Translate example sentences via LLM; embed en into articles.",
    )
    _add_dir_args(p_te)
    p_te.add_argument("--model", default=DEFAULT_MODEL)
    p_te.add_argument(
        "--provider",
        choices=["openrouter", "codex"],
        default="openrouter",
        help="LLM provider for example translation. 'codex' uses the local Codex CLI.",
    )
    p_te.add_argument("--batch-size", type=int, default=25)
    p_te.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent in-flight LLM requests (bounded). Stops refilling on a "
        "billing/usage limit; run is resumable.",
    )
    p_te.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high"],
        default=None,
        help="Codex reasoning effort. Omit to use the Codex config default.",
    )
    p_te.add_argument("--max-retries", type=int, default=DEFAULT_RETRIES)
    p_te.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY)
    p_te.add_argument("--error-log", type=Path, default=DEFAULT_ERROR_LOG)
    _add_run_args(p_te)

    # apply-review
    p_ar = sub.add_parser(
        "apply-review",
        help="Apply review suggested_en values back into articles.",
    )
    _add_dir_args(p_ar)
    p_ar.add_argument("--review", type=Path, required=True, help="Review issues JSON file.")
    p_ar.add_argument(
        "--severity-threshold",
        default="medium",
        choices=["low", "medium", "high"],
        help="Apply issues at or above this severity.",
    )
    p_ar.add_argument("--dry-run", action="store_true")

    # build
    p_build = sub.add_parser("build", help="Run fetch → translate → pronounce → export.")
    _add_dir_args(p_build)
    _add_llm_args(p_build)
    _add_run_args(p_build)
    add_pronunciation_args(p_build)

    return root


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()
    for field in ("batch_size", "max_retries"):
        if hasattr(args, field) and getattr(args, field) <= 0:
            parser.error(f"--{field.replace('_', '-')} must be greater than zero")
    if getattr(args, "retry_delay", 0) < 0:
        parser.error("--retry-delay cannot be negative")
    args.articles_dir = args.articles_dir.resolve()
    if hasattr(args, "lemma_dir"):
        args.lemma_dir = args.lemma_dir.resolve()

    dispatch = {
        "fetch": cmd_fetch,
        "hydrate": cmd_hydrate,
        "translate": cmd_translate,
        "translate-examples": cmd_translate_examples,
        "pronounce": cmd_pronounce,
        "export": cmd_export,
        "frequency": cmd_frequency,
        "audio": cmd_audio,
        "review": cmd_review,
        "apply-review": cmd_apply_review,
        "build": cmd_build,
    }
    dispatch[args.command](args)


def _preserve_frequency_metadata(
    lemma_data: dict[str, object], existing: dict[str, object]
) -> None:
    old_lemmas = [lemma for lemma in existing.get("lemmas", []) if isinstance(lemma, dict)]
    by_key = {
        (
            lemma.get("source_lemma_id"),
            lemma.get("lemma"),
            lemma.get("hgno"),
            lemma.get("pos"),
        ): lemma
        for lemma in old_lemmas
    }
    for lemma in lemma_data.get("lemmas", []):
        if not isinstance(lemma, dict):
            continue
        old = by_key.get(
            (
                lemma.get("source_lemma_id"),
                lemma.get("lemma"),
                lemma.get("hgno"),
                lemma.get("pos"),
            )
        )
        if old is None:
            continue
        for field in ("frequency_rank", "frequency_ambiguous"):
            if field in old:
                lemma[field] = old[field]


def _collect_untranslated(exploded: list[ExplodedEntry], force: bool) -> list[ExplodedEntry]:
    if force:
        return exploded
    return [(aid, raw) for aid, raw in exploded if complete_existing_translation(raw) is None]


if __name__ == "__main__":
    main()
