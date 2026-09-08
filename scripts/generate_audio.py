from __future__ import annotations

import argparse
import itertools
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from ordbokene.audio import (
    AudioJob,
    collect_audio_jobs,
    embed_audio_into_articles,
    file_sha256,
    is_existing_audio_valid,
    jobs_from_manifest,
    load_manifest,
    manifest_checksum_index,
    output_path_for_job,
    write_manifest,
)
from ordbokene.google_tts import list_google_voices, synthesize_google_mp3
from ordbokene.settings import DEFAULT_ARTICLES_DIR, DEFAULT_AUDIO_DIR, DEFAULT_LEMMA_DIR

DEFAULT_GOOGLE_VOICE = "nb-NO-Chirp3-HD-Aoede"
DEFAULT_PRICE_PER_MILLION_CHARS = 30.0
DEFAULT_WORKERS = 8
MANIFEST_WRITE_INTERVAL = 100


def _iter_jobs(
    lemma_dir: Path,
    *,
    provider: str,
    voice: str,
    language_code: str,
    audio_dir: Path,
    force: bool,
    manifest: dict[str, object] | None = None,
    checksum_index: dict[str, str] | None = None,
    skip_existing: bool = True,
):
    """Yield current audio jobs, optionally skipping valid existing files."""
    for job in collect_audio_jobs(
        lemma_dir,
        provider=provider,
        voice=voice,
        language_code=language_code,
    ):
        if (
            skip_existing
            and not force
            and is_existing_audio_valid(audio_dir, job, manifest, checksum_index)
        ):
            continue
        yield job


def run(
    *,
    lemma_dir: Path,
    audio_dir: Path,
    voice: str,
    language_code: str,
    dry_run: bool,
    limit: int | None,
    force: bool,
    confirm_cost: bool,
    price_per_million_chars: float,
    articles_dir: Path = DEFAULT_ARTICLES_DIR,
    list_voices: bool = False,
    enrich_only: bool = False,
    workers: int = DEFAULT_WORKERS,
    synthesize: Callable[[AudioJob, Path], None] = synthesize_google_mp3,
) -> int:
    if list_voices:
        for voice_info in list_google_voices(language_code):
            languages = ",".join(voice_info["language_codes"])
            print(f"{voice_info['name']}\t{languages}\t{voice_info['ssml_gender']}")
        return 0

    manifest_path = audio_dir / f"manifest-google-{voice}.json"
    existing_manifest = load_manifest(manifest_path)
    checksum_index = manifest_checksum_index(existing_manifest)
    if enrich_only:
        all_jobs = jobs_from_manifest(
            manifest_path, provider="google", voice=voice, language_code=language_code
        )
        if not dry_run:
            current_jobs = collect_audio_jobs(
                lemma_dir,
                provider="google",
                voice=voice,
                language_code=language_code,
            )
            all_jobs = _refresh_manifest_metadata(all_jobs, current_jobs)
            write_manifest(audio_dir, "google", voice, language_code, all_jobs)
        if dry_run:
            print(
                f"dry-run -- would embed audio from {len(all_jobs)} manifest item(s) "
                f"into {articles_dir}"
            )
            return 0
        # Audio metadata belongs in articles/ so export can reproduce it.
        written = embed_audio_into_articles(articles_dir, all_jobs)
        print(
            f"embedded audio into {written} article JSON files (run `export` to propagate to lemma/)"
        )
        return 0

    current_jobs = list(
        _iter_jobs(
            lemma_dir,
            provider="google",
            voice=voice,
            language_code=language_code,
            audio_dir=audio_dir,
            force=force,
            manifest=existing_manifest,
            checksum_index=checksum_index,
            skip_existing=False,
        )
    )
    preserved_jobs, pending_jobs = _prepare_manifest_jobs(
        jobs_from_manifest(
            manifest_path,
            provider="google",
            voice=voice,
            language_code=language_code,
        ),
        current_jobs,
        audio_dir,
        existing_manifest,
        force,
    )

    if dry_run:
        pending = chars = 0
        for job in pending_jobs:
            pending += 1
            chars += len(job.text)
            if pending <= 20:
                print(f"- {job.text} -> {job.filename}")
        print(f"pending api calls: {pending}")
        print(f"pending characters: {chars}")
        print(f"estimated cost: ${chars / 1_000_000 * price_per_million_chars:.4f}")
        return 0

    if limit is None and not confirm_cost:
        raise SystemExit(
            "Refusing full audio generation without --confirm-cost. Run --dry-run first."
        )

    completed: dict[str, AudioJob] = {}
    lock = threading.Lock()
    synthesized_count = 0

    def process(job: AudioJob) -> None:
        nonlocal synthesized_count
        output_path = output_path_for_job(audio_dir, job)
        for attempt in range(6):
            try:
                synthesize(job, output_path)
                break
            except Exception as exc:
                if "429" in str(exc) or "ResourceExhausted" in type(exc).__name__:
                    time.sleep(2**attempt)
                else:
                    raise
        job.content_sha256 = file_sha256(output_path)
        with lock:
            completed[job.filename] = job
            synthesized_count += 1
            if synthesized_count % MANIFEST_WRITE_INTERVAL == 0:
                write_manifest(
                    audio_dir,
                    "google",
                    voice,
                    language_code,
                    _merge_manifest_jobs(preserved_jobs, list(completed.values())),
                )

    jobs_iter = iter(pending_jobs)
    if limit is not None:
        jobs_iter = itertools.islice(jobs_iter, limit)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, job): job for job in jobs_iter}
        for future in as_completed(futures):
            future.result()

    write_manifest(
        audio_dir,
        "google",
        voice,
        language_code,
        _merge_manifest_jobs(preserved_jobs, list(completed.values())),
    )
    all_jobs = jobs_from_manifest(
        manifest_path, provider="google", voice=voice, language_code=language_code
    )
    embedded = embed_audio_into_articles(articles_dir, all_jobs)

    print(f"synthesized {synthesized_count} audio file(s)")
    print(
        f"embedded audio into {embedded} article JSON files (run `export` to propagate to lemma/)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate lemma audio with Google Cloud Text-to-Speech."
    )
    parser.add_argument("--lemma-dir", type=Path, default=DEFAULT_LEMMA_DIR)
    parser.add_argument("--articles-dir", type=Path, default=DEFAULT_ARTICLES_DIR)
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--voice", default=DEFAULT_GOOGLE_VOICE)
    parser.add_argument("--language-code", default="nb-NO")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    parser.add_argument(
        "--price-per-million-chars", type=float, default=DEFAULT_PRICE_PER_MILLION_CHARS
    )
    parser.add_argument("--list-voices", action="store_true")
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="Skip synthesis; embed audio metadata into articles/ from existing manifest (run export to propagate to lemma/).",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(
        lemma_dir=args.lemma_dir.resolve(),
        audio_dir=args.audio_dir.resolve(),
        voice=args.voice,
        language_code=args.language_code,
        dry_run=args.dry_run,
        limit=args.limit,
        force=args.force,
        confirm_cost=args.confirm_cost,
        price_per_million_chars=args.price_per_million_chars,
        articles_dir=args.articles_dir.resolve(),
        list_voices=args.list_voices,
        enrich_only=args.enrich_only,
        workers=args.workers,
    )


def _merge_manifest_jobs(existing: list[AudioJob], updates: list[AudioJob]) -> list[AudioJob]:
    jobs_by_filename = {job.filename: job for job in existing}
    for job in updates:
        current = jobs_by_filename.get(job.filename)
        if current is None:
            jobs_by_filename[job.filename] = job
            continue
        _merge_job_associations(current, job)
        current.content_sha256 = job.content_sha256
    return list(jobs_by_filename.values())


def _merge_job_associations(existing: AudioJob, current: AudioJob) -> None:
    for article_id in current.article_ids:
        if article_id not in existing.article_ids:
            existing.article_ids.append(article_id)
    for source_lemma_id in current.source_lemma_ids:
        if source_lemma_id not in existing.source_lemma_ids:
            existing.source_lemma_ids.append(source_lemma_id)
    if existing.pronunciation_source is None:
        existing.pronunciation_source = current.pronunciation_source


def _refresh_job_metadata(existing: AudioJob, current: AudioJob) -> None:
    """Refresh pronunciation metadata while retaining the existing file identity."""
    _merge_job_associations(existing, current)
    existing.key = current.key
    existing.pronunciation_source = current.pronunciation_source
    existing.tone_status = current.tone_status
    existing.tone = current.tone


def _refresh_manifest_metadata(
    existing_jobs: list[AudioJob], current_jobs: list[AudioJob]
) -> list[AudioJob]:
    """Backfill metadata from current pronunciations without changing filenames."""
    by_filename = {job.filename: job for job in current_jobs}
    by_legacy_key: dict[tuple[str, int | None], list[AudioJob]] = {}
    by_source_lemma_id: dict[int, list[AudioJob]] = {}
    for job in current_jobs:
        by_legacy_key.setdefault((job.text, job.tone), []).append(job)
        for source_lemma_id in job.source_lemma_ids:
            by_source_lemma_id.setdefault(source_lemma_id, []).append(job)

    refreshed: list[AudioJob] = []
    used_current: set[str] = set()
    for existing in existing_jobs:
        current = by_filename.get(existing.filename)
        if current is None and existing.tone_status is None:
            candidates = [
                job
                for job in by_legacy_key.get((existing.text, existing.tone), [])
                if job.filename not in used_current
            ]
            if len(candidates) != 1:
                candidates = [
                    job
                    for source_lemma_id in existing.source_lemma_ids
                    for job in by_source_lemma_id.get(source_lemma_id, [])
                    if job.filename not in used_current
                ]
                candidates = list({job.filename: job for job in candidates}.values())
            if len(candidates) == 1:
                current = candidates[0]
        if current is not None:
            _refresh_job_metadata(existing, current)
            used_current.add(current.filename)
        refreshed.append(existing)
    return refreshed


def _prepare_manifest_jobs(
    existing_jobs: list[AudioJob],
    current_jobs: list[AudioJob],
    audio_dir: Path,
    manifest: dict[str, object],
    force: bool,
) -> tuple[list[AudioJob], list[AudioJob]]:
    checksum_index = manifest_checksum_index(manifest)
    preserved_by_filename: dict[str, AudioJob] = {
        job.filename: job
        for job in existing_jobs
        if is_existing_audio_valid(audio_dir, job, manifest, checksum_index)
    }
    legacy_by_key: dict[tuple[str, int | None], list[AudioJob]] = {}
    legacy_by_source_lemma_id: dict[int, list[AudioJob]] = {}
    for job in preserved_by_filename.values():
        if job.tone_status is None:
            legacy_by_key.setdefault((job.text, job.tone), []).append(job)
            for source_lemma_id in job.source_lemma_ids:
                legacy_by_source_lemma_id.setdefault(source_lemma_id, []).append(job)
    used_legacy: set[str] = set()
    pending: list[AudioJob] = []

    for current in current_jobs:
        preserved = preserved_by_filename.get(current.filename)
        if preserved is None:
            candidates = [
                job
                for job in legacy_by_key.get((current.text, current.tone), [])
                if job.filename not in used_legacy
            ]
            if len(candidates) != 1:
                candidates = [
                    job
                    for source_lemma_id in current.source_lemma_ids
                    for job in legacy_by_source_lemma_id.get(source_lemma_id, [])
                    if job.filename not in used_legacy
                ]
                candidates = list({job.filename: job for job in candidates}.values())
            if len(candidates) == 1:
                preserved = candidates[0]
                used_legacy.add(preserved.filename)
        if preserved is not None:
            _refresh_job_metadata(preserved, current)
            if force:
                pending.append(replace(current, filename=preserved.filename))
            continue
        if not force and is_existing_audio_valid(audio_dir, current, manifest, checksum_index):
            current.content_sha256 = file_sha256(output_path_for_job(audio_dir, current))
            preserved_by_filename[current.filename] = current
            continue
        pending.append(current)

    return list(preserved_by_filename.values()), pending


if __name__ == "__main__":
    raise SystemExit(main())
