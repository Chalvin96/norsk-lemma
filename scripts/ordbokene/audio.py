from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SPACE_RE = re.compile(r"\s+")

# `path` is the host-free key; `url` prepends this host. Change here to move the CDN.
AUDIO_BASE_URL = "https://media.umebocchi.my.id"


@dataclass
class AudioJob:
    key: tuple[str, str | None, int | None]
    text: str
    provider: str
    voice: str
    language_code: str
    filename: str
    article_ids: list[int] = field(default_factory=list)
    source_lemma_ids: list[int] = field(default_factory=list)
    pronunciation_source: str | None = None
    tone_status: str | None = None
    tone: int | None = None
    content_sha256: str | None = None

    @property
    def relative_path(self) -> str:
        return f"audio/lemma/{self.provider}/{self.voice}/{self.filename}"

    @property
    def public_url(self) -> str:
        return f"{AUDIO_BASE_URL}/{self.relative_path}"


def normalize_audio_text(text: str) -> str:
    return _SPACE_RE.sub(" ", text.strip())


def audio_filename(
    provider: str,
    voice: str,
    language_code: str,
    text: str,
    tone_status: str | None,
    tone: int | None,
) -> str:
    normalized = normalize_audio_text(text)
    digest = hashlib.sha256(f"{provider}|{voice}|{language_code}|{normalized}|{tone_status}|{tone}".encode()).hexdigest()
    return f"{digest[:12]}.mp3"


def collect_audio_jobs(
    lemma_dir: Path,
    *,
    provider: str,
    voice: str,
    language_code: str,
    include_expressions: bool = False,
) -> list[AudioJob]:
    jobs_by_key: dict[tuple[str, str | None, int | None], AudioJob] = {}

    for path in sorted(lemma_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        article_id = int(data.get("source_article_id") or path.stem)
        for lemma in data.get("lemmas", []):
            if not isinstance(lemma, dict):
                continue
            text = normalize_audio_text(str(lemma.get("lemma") or ""))
            if not text or (not include_expressions and _is_expression(lemma, text)) or _is_unaudioable(text):
                continue

            pron = _best_pronunciation_for_lemma(lemma, text)
            tone_status = pron.get("tone_status") if pron else None
            tone = pron.get("tone") if pron else None
            key = (text, tone_status, tone)
            job = jobs_by_key.get(key)
            if job is None:
                job = AudioJob(
                    key=key,
                    text=text,
                    provider=provider,
                    voice=voice,
                    language_code=language_code,
                    filename=audio_filename(provider, voice, language_code, text, tone_status, tone),
                    pronunciation_source=pron.get("source") if pron else None,
                    tone_status=tone_status,
                    tone=tone,
                )
                jobs_by_key[key] = job

            if article_id not in job.article_ids:
                job.article_ids.append(article_id)
            source_lemma_id = lemma.get("source_lemma_id")
            if isinstance(source_lemma_id, int) and source_lemma_id not in job.source_lemma_ids:
                job.source_lemma_ids.append(source_lemma_id)

    return [
        jobs_by_key[key]
        for key in sorted(
            jobs_by_key,
            key=lambda item: (item[0], item[1] or "", -1 if item[2] is None else item[2]),
        )
    ]




def output_path_for_job(audio_dir: Path, job: AudioJob) -> Path:
    return audio_dir / "lemma" / job.provider / job.voice / job.filename


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def jobs_from_manifest(path: Path, *, provider: str, voice: str, language_code: str) -> list[AudioJob]:
    manifest = load_manifest(path)
    jobs = []
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        text = item.get("text") or ""
        tone_status = item.get("tone_status")
        tone = item.get("tone")
        filename = item.get("file") or ""
        if not text or not filename:
            continue
        jobs.append(AudioJob(
            key=(text, tone_status, tone),
            text=text,
            provider=provider,
            voice=voice,
            language_code=language_code,
            filename=filename,
            article_ids=item.get("article_ids") or [],
            source_lemma_ids=item.get("source_lemma_ids") or [],
            tone_status=tone_status,
            tone=tone,
            content_sha256=item.get("content_sha256"),
        ))
    return jobs


def write_manifest(audio_dir: Path, provider: str, voice: str, language_code: str, jobs: list[AudioJob]) -> Path:
    manifest = {
        "provider": provider,
        "voice": voice,
        "language_code": language_code,
        "format": "mp3",
        "items": [
            {
                "text": job.text,
                "file": job.filename,
                "path": job.relative_path,
                "url": job.public_url,
                "article_ids": job.article_ids,
                "source_lemma_ids": job.source_lemma_ids,
                "tone_status": job.tone_status,
                "tone": job.tone,
                "content_sha256": job.content_sha256,
            }
            for job in jobs
        ],
    }
    path = audio_dir / f"manifest-{provider}-{voice}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def existing_audio_is_valid(audio_dir: Path, job: AudioJob, manifest: dict[str, Any] | None = None) -> bool:
    path = output_path_for_job(audio_dir, job)
    if not path.exists() or path.stat().st_size == 0:
        return False
    expected = _manifest_checksum(manifest or {}, job.filename)
    return expected is None or file_sha256(path) == expected


def write_audio_into_lemmas(lemma_dir: Path, jobs: list[AudioJob]) -> int:
    """Write audio metadata directly into lemma JSON files in-place."""
    jobs_by_key = {job.key: job for job in jobs}
    written = 0

    for path in sorted(lemma_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for lemma in data.get("lemmas", []):
            if not isinstance(lemma, dict):
                continue
            text = normalize_audio_text(str(lemma.get("lemma") or ""))
            pron = _best_pronunciation_for_lemma(lemma, text)
            job = jobs_by_key.get((text, pron.get("tone_status") if pron else None, pron.get("tone") if pron else None))
            if job is None:
                continue
            item = audio_metadata(job)
            if lemma.get("audio") != {"lemma": [item]}:
                lemma["audio"] = {"lemma": [item]}
                changed = True
        if changed:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written += 1

    return written


def write_audio_enriched_lemmas(lemma_dir: Path, output_dir: Path, jobs: list[AudioJob]) -> int:
    """Write enriched copies of lemma JSONs into output_dir (source not modified)."""
    jobs_by_key = {job.key: job for job in jobs}
    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for path in sorted(lemma_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for lemma in data.get("lemmas", []):
            if not isinstance(lemma, dict):
                continue
            text = normalize_audio_text(str(lemma.get("lemma") or ""))
            pron = _best_pronunciation_for_lemma(lemma, text)
            job = jobs_by_key.get((text, pron.get("tone_status") if pron else None, pron.get("tone") if pron else None))
            if job is None:
                continue
            item = audio_metadata(job)
            if lemma.get("audio") != {"lemma": [item]}:
                lemma["audio"] = {"lemma": [item]}
                changed = True
        if changed:
            (output_dir / path.name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written += 1

    return written


def audio_metadata(job: AudioJob) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "tts",
        "provider": job.provider,
        "voice": job.voice,
        "language_code": job.language_code,
        "format": "mp3",
        "file": job.filename,
        "path": job.relative_path,
        "url": job.public_url,
        "text": job.text,
        "source": "generated",
    }
    if job.content_sha256:
        item["content_sha256"] = job.content_sha256
    if job.pronunciation_source is not None:
        item["pronunciation_source"] = job.pronunciation_source
    if job.tone_status is not None:
        item["tone_status"] = job.tone_status
    if job.tone is not None:
        item["tone"] = job.tone
    return item


def _manifest_checksum(manifest: dict[str, Any], filename: str) -> str | None:
    for item in manifest.get("items", []):
        if isinstance(item, dict) and item.get("file") == filename and isinstance(item.get("content_sha256"), str):
            return item["content_sha256"]
    return None


def _is_expression(lemma: dict[str, Any], text: str) -> bool:
    return bool(lemma.get("is_sub_article")) or " " in text


def _is_unaudioable(text: str) -> bool:
    # affixes, single chars, slash-units, digit-bearing strings
    return (
        text.startswith("-")
        or text.endswith("-")
        or len(text) == 1
        or "/" in text
        or any(c.isdigit() for c in text)
    )


def _best_pronunciation_for_lemma(lemma: dict[str, Any], text: str) -> dict[str, Any]:
    for form in lemma.get("word_forms", []):
        if not isinstance(form, dict) or form.get("word_form") != text:
            continue
        pron = form.get("pronunciation") or []
        if pron and isinstance(pron[0], dict):
            return pron[0]
    return {}
