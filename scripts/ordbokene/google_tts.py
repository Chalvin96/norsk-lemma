from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .audio import AudioJob


def list_google_voices(language_code: str = "nb-NO") -> list[dict[str, Any]]:
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    response = client.list_voices(language_code=language_code)
    return [
        {
            "name": voice.name,
            "language_codes": list(voice.language_codes),
            "ssml_gender": voice.ssml_gender.name,
            "natural_sample_rate_hertz": voice.natural_sample_rate_hertz,
        }
        for voice in response.voices
    ]


def synthesize_google_mp3(job: AudioJob, output_path: Path, *, max_retries: int = 3, retry_delay: float = 1.0) -> None:
    _check_google_auth_hint()

    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    request = {
        "input": texttospeech.SynthesisInput(text=job.text),
        "voice": texttospeech.VoiceSelectionParams(
            language_code=job.language_code,
            name=job.voice,
        ),
        "audio_config": texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
        ),
    }

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        for attempt in range(max_retries + 1):
            try:
                response = client.synthesize_speech(**request)
                tmp_path.write_bytes(response.audio_content)
                tmp_path.replace(output_path)
                return
            except Exception:
                if attempt >= max_retries:
                    raise
                time.sleep(retry_delay * (2**attempt))
    finally:
        tmp_path.unlink(missing_ok=True)


def _check_google_auth_hint() -> None:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return
    # ADC can still work without these env vars. This intentionally does not fail;
    # the Google client will raise the authoritative auth error if ADC is missing.
