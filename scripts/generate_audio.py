from __future__ import annotations

import argparse
import itertools
import json
import time
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ordbokene.audio import (
    AudioJob,
    _best_pronunciation_for_lemma,
    _is_expression,
    _is_unaudioable,
    audio_filename,
    file_sha256,
    jobs_from_manifest,
    normalize_audio_text,
    output_path_for_job,
    write_audio_into_lemmas,
    write_manifest,
)
from ordbokene.google_tts import list_google_voices, synthesize_google_mp3
from ordbokene.settings import DEFAULT_LEMMA_DIR, REPO_ROOT

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
    seen: set[str],
):
    """Yield AudioJobs one file at a time, skipping already-synthesized entries."""
    for path in sorted(lemma_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        article_id = int(data.get("source_article_id") or path.stem)

        for lemma in data.get("lemmas", []):
            if not isinstance(lemma, dict):
                continue
            text = normalize_audio_text(str(lemma.get("lemma") or ""))
            if not text or _is_expression(lemma, text) or _is_unaudioable(text):
                continue

            pron = _best_pronunciation_for_lemma(lemma, text)
            tone_status = pron.get("tone_status") if pron else None
            tone = pron.get("tone") if pron else None
            filename = audio_filename(provider, voice, language_code, text, tone_status, tone)

            if filename in seen:
                continue
            seen.add(filename)

            if not force and (audio_dir / "lemma" / provider / voice / filename).exists():
                continue

            source_lemma_id = lemma.get("source_lemma_id")
            yield AudioJob(
                key=(text, tone_status, tone),
                text=text,
                provider=provider,
                voice=voice,
                language_code=language_code,
                filename=filename,
                article_ids=[article_id],
                source_lemma_ids=[source_lemma_id] if isinstance(source_lemma_id, int) else [],
                pronunciation_source=pron.get("source") if pron else None,
                tone_status=tone_status,
                tone=tone,
            )


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

    if enrich_only:
        all_jobs = jobs_from_manifest(manifest_path, provider="google", voice=voice, language_code=language_code)
        written = write_audio_into_lemmas(lemma_dir, all_jobs)
        print(f"wrote audio into {written} lemma JSON files")
        return 0

    seen: set[str] = set()

    if dry_run:
        pending = chars = 0
        for job in _iter_jobs(lemma_dir, provider="google", voice=voice,
                              language_code=language_code, audio_dir=audio_dir,
                              force=force, seen=seen):
            pending += 1
            chars += len(job.text)
            if pending <= 20:
                print(f"- {job.text} -> {job.filename}")
        print(f"pending api calls: {pending}")
        print(f"pending characters: {chars}")
        print(f"estimated cost: ${chars / 1_000_000 * price_per_million_chars:.4f}")
        return 0

    if limit is None and not confirm_cost:
        raise SystemExit("Refusing full audio generation without --confirm-cost. Run --dry-run first.")

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
                    time.sleep(2 ** attempt)
                else:
                    raise
        job.content_sha256 = file_sha256(output_path)
        with lock:
            completed[job.filename] = job
            synthesized_count += 1
            if synthesized_count % MANIFEST_WRITE_INTERVAL == 0:
                write_manifest(audio_dir, "google", voice, language_code, list(completed.values()))

    jobs_iter = _iter_jobs(lemma_dir, provider="google", voice=voice,
                           language_code=language_code, audio_dir=audio_dir,
                           force=force, seen=seen)
    if limit is not None:
        jobs_iter = itertools.islice(jobs_iter, limit)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, job): job for job in jobs_iter}
        for future in as_completed(futures):
            future.result()

    write_manifest(audio_dir, "google", voice, language_code, list(completed.values()))
    all_jobs = jobs_from_manifest(manifest_path, provider="google", voice=voice, language_code=language_code)
    write_audio_into_lemmas(lemma_dir, all_jobs)

    print(f"synthesized {synthesized_count} audio file(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate lemma audio with Google Cloud Text-to-Speech.")
    parser.add_argument("--lemma-dir", type=Path, default=DEFAULT_LEMMA_DIR)
    parser.add_argument("--audio-dir", type=Path, default=REPO_ROOT / "data" / "audio")
    parser.add_argument("--voice", default=DEFAULT_GOOGLE_VOICE)
    parser.add_argument("--language-code", default="nb-NO")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    parser.add_argument("--price-per-million-chars", type=float, default=DEFAULT_PRICE_PER_MILLION_CHARS)
    parser.add_argument("--list-voices", action="store_true")
    parser.add_argument("--enrich-only", action="store_true",
                        help="Skip synthesis; write audio metadata into lemma/ JSON files from existing manifest.")
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
        list_voices=args.list_voices,
        enrich_only=args.enrich_only,
        workers=args.workers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
