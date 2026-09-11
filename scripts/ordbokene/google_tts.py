from __future__ import annotations

from pathlib import Path
from typing import Any

from .audio import AudioJob

K_GOOGLE_TTS_TIMEOUT = 30


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


def synthesize_google_mp3(job: AudioJob, output_path: Path) -> None:
    """Synthesize one file; retry ownership belongs to the caller."""
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
        response = client.synthesize_speech(
            **request,
            retry=None,
            timeout=K_GOOGLE_TTS_TIMEOUT,
        )
        tmp_path.write_bytes(response.audio_content)
        tmp_path.replace(output_path)
    finally:
        tmp_path.unlink(missing_ok=True)
