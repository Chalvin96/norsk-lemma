from __future__ import annotations

import argparse
import itertools
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from ordbokene.arguments import K_DEFAULT_WORKERS, add_audio_args
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
from ordbokene.settings import DEFAULT_ARTICLES_DIR, DEFAULT_LEMMA_DIR

MANIFEST_WRITE_INTERVAL = 100


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
    workers: int = K_DEFAULT_WORKERS,
    synthesize: Callable[[AudioJob, Path], None] = synthesize_google_mp3,
) -> int:
    if list_voices:
        for voice_info in list_google_voices(language_code):
            languages = ",".join(voice_info["language_codes"])
            print(f"{voice_info['name']}\t{languages}\t{voice_info['ssml_gender']}")
        return 0

    manifest_path = audio_dir / f"manifest-google-{voice}.json"
    existing_manifest = load_manifest(manifest_path)
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

    current_jobs = collect_audio_jobs(
        lemma_dir,
        provider="google",
        voice=voice,
        language_code=language_code,
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

    def synthesize_audio_job(job: AudioJob) -> None:
        nonlocal synthesized_count
        output_path = output_path_for_job(audio_dir, job)
        staged_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.pending")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for attempt in range(6):
                staged_path.unlink(missing_ok=True)
                try:
                    synthesize(job, staged_path)
                    if not staged_path.exists() or staged_path.stat().st_size == 0:
                        raise RuntimeError(
                            f"synthesis returned without creating nonempty output for {output_path}"
                        )
                    staged_path.replace(output_path)
                    break
                except Exception as exc:
                    if not _is_retryable_synthesis_error(exc) or attempt == 5:
                        raise
                    time.sleep(2**attempt)
        finally:
            staged_path.unlink(missing_ok=True)
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
        futures = {executor.submit(synthesize_audio_job, job): job for job in jobs_iter}
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
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    add_audio_args(parser)
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


def _is_retryable_synthesis_error(exc: Exception) -> bool:
    retryable_names = {
        "ConnectionError",
        "DeadlineExceeded",
        "InternalServerError",
        "RetryError",
        "ResourceExhausted",
        "ServiceUnavailable",
        "TimeoutError",
        "TooManyRequests",
    }
    if any(cls.__name__ in retryable_names for cls in type(exc).__mro__):
        return True

    code = getattr(exc, "code", None)
    code = code() if callable(code) else code
    value = getattr(code, "value", code)
    if isinstance(value, tuple):
        value = value[0]
    return value in {429, 500, 502, 503, 504}


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
