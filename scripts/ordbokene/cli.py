"""Unified CLI for the Bokmål lexicon data pipeline.

Stages in order:
  fetch      Download raw article JSONs from Ordbøkene into data/articles/.
  hydrate    Embed translations from an existing lemma/ release into articles
             (alternative to running translate; no LLM needed).
  translate  Call LLM for untranslated articles; embed results back into articles.
  pronounce  Enrich articles with IPA pronunciation in-place.
  export     Build tracked lemma/ from enriched articles (no LLM needed).
  audio      Generate lemma audio from exported lemma JSON.
  build      Run fetch → translate → pronounce → export in sequence.

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
import shutil
import sys
from pathlib import Path

import requests
from tqdm import tqdm

from .build import build_lemma
from .client import request_translations
from .embed import embed_translations, write_article
from .extract import extract_definitions, extract_existing_translations
from .io import ExplodedEntry, explode, write_error, write_lemma
from .settings import (
    DEFAULT_ARTICLES_DIR,
    DEFAULT_BATCH_SIZE,
    DEFAULT_ERROR_LOG,
    DEFAULT_LEMMA_DIR,
    DEFAULT_MODEL,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY,
    logger,
)
from .source import ensure_articles_dir

# ---------------------------------------------------------------------------
# Shared argument helpers
# ---------------------------------------------------------------------------

def _add_dir_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--articles-dir", type=Path, default=DEFAULT_ARTICLES_DIR)
    p.add_argument("--lemma-dir", type=Path, default=DEFAULT_LEMMA_DIR)


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--max-retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY)
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

    tar_asset = next((a for a in release.get("assets", []) if a["name"].endswith(".tar.gz")), None)
    if tar_asset is None:
        sys.exit(f"No .tar.gz asset in release {resolved_tag}")

    size_mb = tar_asset["size"] / 1024 / 1024
    logger.info("Downloading %s (%.1f MB)", tar_asset["name"], size_mb)

    archive_path: Path | None = None
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
            json_files = list(Path(tmp_dir).rglob("*.json"))
            if not json_files:
                sys.exit(f"No JSON files found in release archive {resolved_tag}")
            lemma_dir.mkdir(parents=True, exist_ok=True)
            for f in lemma_dir.glob("*.json"):
                f.unlink()
            for f in json_files:
                shutil.move(str(f), lemma_dir / f.name)
    finally:
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
            (lm["primary_translation"] for lm in lemma_data.get("lemmas", [])
             if isinstance(lm, dict) and lm.get("primary_translation")),
            "",
        )
        article_defs = extract_definitions(raw)
        source_id_by_text = {d["text"]: d["source_id"] for d in article_defs}
        synthetic_defs = [
            {"source_id": source_id_by_text[ld["text"]], "translation": ld.get("translation", "")}
            for ld in lemma_data.get("definitions", [])
            if ld.get("text") in source_id_by_text and ld.get("translation")
        ]

        article = copy.deepcopy(raw)
        embed_translations(article, {"lemma_primary": primary, "definitions": synthetic_defs})
        write_article(articles_dir, article_id, article)
        written += 1

    logger.info("hydrate done — %d articles updated", written)


# ---------------------------------------------------------------------------
# translate
# ---------------------------------------------------------------------------

def _collect_untranslated(
    exploded: list[ExplodedEntry], force: bool
) -> list[ExplodedEntry]:
    if force:
        return exploded
    return [
        (aid, raw)
        for aid, raw in exploded
        if extract_existing_translations(raw) is None
    ]


def cmd_translate(args: argparse.Namespace) -> None:
    articles_dir = args.articles_dir.resolve()
    ensure_articles_dir(articles_dir)

    exploded = explode(articles_dir)
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
    for start in progress:
        batch = pending[start : start + args.batch_size]
        results = request_translations(session, args, batch)
        for local_idx, (article_id, raw) in enumerate(batch):
            result = results.get(local_idx)
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

    logger.info("translate done — %d articles updated", written)


# ---------------------------------------------------------------------------
# pronounce
# ---------------------------------------------------------------------------

def cmd_pronounce(args: argparse.Namespace) -> None:
    if getattr(args, "dry_run", False):
        logger.info("dry-run — skipping pronounce")
        return
    try:
        from enrich_pronunciation import main as _pronounce_main  # type: ignore[import]
    except ImportError:
        enrich_path = Path(__file__).parents[1] / "enrich_pronunciation.py"
        sys.path.insert(0, str(enrich_path.parent))
        from enrich_pronunciation import main as _pronounce_main  # type: ignore[import]

    new_argv = [
        "enrich_pronunciation",
        "--input", str(args.articles_dir.resolve()),
        "--leksika", str(args.leksika),
        "--newwords", str(args.newwords),
        "--workers", str(args.workers),
    ]
    if args.force:
        new_argv.append("--force")
    if args.limit:
        new_argv += ["--limit", str(args.limit)]

    saved_argv = sys.argv[:]
    try:
        sys.argv = new_argv
        _pronounce_main()
    finally:
        sys.argv = saved_argv


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> None:
    articles_dir = args.articles_dir.resolve()
    lemma_dir = args.lemma_dir.resolve()

    exploded = explode(articles_dir)
    if args.force:
        pending = exploded
    else:
        pending = [(aid, raw) for aid, raw in exploded if not (lemma_dir / f"{aid}.json").exists()]

    logger.info("export — %d/%d articles pending", len(pending), len(exploded))

    if args.dry_run:
        logger.info("dry-run — would export %d lemma files", len(pending))
        return

    if args.limit:
        pending = pending[: args.limit]

    written = 0
    for article_id, raw in tqdm(pending, unit="article", desc="Exporting"):
        existing = extract_existing_translations(raw)
        if existing is None:
            logger.debug("article %d has no embedded translations — skipping", article_id)
            continue
        lemma_data = build_lemma(copy.deepcopy(raw), existing, article_id)
        write_lemma(lemma_dir, article_id, lemma_data)
        written += 1

    logger.info("export done — %d lemma files written to %s", written, lemma_dir)


# ---------------------------------------------------------------------------
# build  (fetch → translate → pronounce → export)
# ---------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> None:
    cmd_fetch(args)
    cmd_translate(args)
    cmd_pronounce(args)
    cmd_export(args)


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

    # pronounce
    from .settings import REPO_ROOT
    p_pr = sub.add_parser("pronounce", help="Enrich articles with IPA pronunciation.")
    _add_dir_args(p_pr)
    _add_run_args(p_pr)
    p_pr.add_argument(
        "--leksika",
        default=str(REPO_ROOT / "data/nb_uttale_leksika/nb_uttale_leksika/e_written_pronunciation_lexicon.csv"),
    )
    p_pr.add_argument(
        "--newwords",
        default=str(REPO_ROOT / "data/nb_uttale_tillegg/nb_uttale_tillegg/newwords_2022.csv"),
    )
    p_pr.add_argument("--workers", type=int, default=8)

    # export
    p_ex = sub.add_parser("export", help="Build lemma/ from enriched articles.")
    _add_dir_args(p_ex)
    _add_run_args(p_ex)

    # audio
    p_audio = sub.add_parser("audio", help="Generate lemma audio with Google Cloud Text-to-Speech.")
    _add_dir_args(p_audio)
    _add_run_args(p_audio)
    p_audio.add_argument("--audio-dir", type=Path, default=REPO_ROOT / "data" / "audio")
    p_audio.add_argument("--voice", default="nb-NO-Chirp3-HD-Aoede")
    p_audio.add_argument("--language-code", default="nb-NO")
    p_audio.add_argument("--enrich-only", action="store_true")
    p_audio.add_argument("--confirm-cost", action="store_true")
    p_audio.add_argument("--price-per-million-chars", type=float, default=30.0)
    p_audio.add_argument("--list-voices", action="store_true")
    p_audio.add_argument("--workers", type=int, default=8)

    # build
    p_build = sub.add_parser("build", help="Run fetch → translate → pronounce → export.")
    _add_dir_args(p_build)
    _add_llm_args(p_build)
    _add_run_args(p_build)
    p_build.add_argument(
        "--leksika",
        default=str(REPO_ROOT / "data/nb_uttale_leksika/nb_uttale_leksika/e_written_pronunciation_lexicon.csv"),
    )
    p_build.add_argument(
        "--newwords",
        default=str(REPO_ROOT / "data/nb_uttale_tillegg/nb_uttale_tillegg/newwords_2022.csv"),
    )
    p_build.add_argument("--workers", type=int, default=8)

    return root


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()
    args.articles_dir = args.articles_dir.resolve()
    if hasattr(args, "lemma_dir"):
        args.lemma_dir = args.lemma_dir.resolve()

    dispatch = {
        "fetch": cmd_fetch,
        "hydrate": cmd_hydrate,
        "translate": cmd_translate,
        "pronounce": cmd_pronounce,
        "export": cmd_export,
        "audio": cmd_audio,
        "build": cmd_build,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
