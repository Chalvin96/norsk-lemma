# Pronunciation Audio Strategy

This project should not claim tone-controllable Norwegian TTS. Current cloud TTS
systems can produce useful Norwegian audio, but they do not expose a reliable
control for Bokmal pitch accent in isolated dictionary words. Tone 1 / Tone 2
belongs in the pronunciation metadata and learner UI, not in a hidden audio hack.

## Decision

Use generated audio as reference audio, and use trusted pronunciation metadata as
the authority.

For each entry:

- Prefer trusted NB Uttale / Leksika pronunciation data.
- Use `nb-g2p` only as a fallback, marked as generated and reviewable.
- Store IPA, tone, tone status, and pronunciation source separately.
- Generate lemma audio from normal Norwegian text.
- Teach tone with labels, minimal pairs, and comparison exercises.
- Do not present generated audio as proof of correct Norwegian pitch accent.

This is the portfolio story:

> A Norwegian pronunciation pipeline that integrates trusted lexical
> pronunciation sources with G2P fallback, tracks provenance and confidence, and
> exposes coverage/evaluation tools for learner-facing dictionary entries.

## What To Show In The App

### Pronunciation panel

Show this on dictionary entries and lesson cards:

- IPA
- tone badge: `Accent 1`, `Accent 2`, no accent, or unknown
- source badge: `nb_uttale`, `nb_uttale_newwords`, or `nb_g2p`
- confidence label: verified vs generated
- audio button when an audio file exists

Fallback pronunciation should be useful, but visibly less authoritative than
trusted lexicon data.

### Coverage dashboard

Add a portfolio-facing dashboard or report with:

- count and percent from trusted NB Uttale / Leksika
- count and percent from `nb-g2p`
- unresolved/null count
- tone 1 / tone 2 / none / unknown distribution
- examples from each category

This demonstrates data engineering judgment better than a generic audio feature.

### G2P evaluation view

Where trusted data exists, compare:

- trusted IPA
- generated `nb-g2p` IPA
- mismatch category: stress, vowel, consonant, tone, or other

The point is not that G2P is perfect. The point is that the pipeline knows which
pronunciations are trusted, which are estimates, and where the estimates fail.

### Tone-aware lessons

Teach tone explicitly with trusted metadata:

- minimal-pair cards such as `bønder` vs `bønner`
- tone labels beside the word
- short learner explanation of Accent 1 vs Accent 2
- audio as supporting reference, not as the source of truth

Avoid hiding the tone distinction inside audio only. Many learners will not hear it
reliably at first, and synthetic audio may not preserve it.

## Lessons Learned From The Tone POC

We prototyped WORLD F0 post-processing to bend an existing TTS clip toward tone 1
or tone 2. The output changed numerically, but listening tests made the variants
sound nearly identical.

Keep this as a documented negative result:

- Norwegian pitch accent is not just a simple global F0 curve.
- Stressed-syllable alignment matters.
- Duration, intensity, vowel quality, dialect, and phrase context may contribute.
- A barely audible post-process is worse than no feature because it creates false
  confidence for learners.

The right production decision is to remove it from the learner experience unless a
future native-speaker evaluation proves the effect is useful.

## Audio Generation Plan

Start with lemma audio only.

1. Build a deduplicated lemma list from entries with displayable lemmas.
2. Generate one audio file per lemma signature.
3. Store audio under a stable path, for example:

   ```text
   audio/lemma/{provider}/{voice}/{lemma_id}.mp3
   ```

4. Add an audio field to the generated lemma JSON:

   ```jsonc
   {
     "audio": [
       {
         "type": "tts",
         "provider": "azure",
         "voice": "nb-NO-PernilleNeural",
         "file": "audio/lemma/azure/nb-NO-PernilleNeural/12345.mp3",
         "text": "bønner",
         "pronunciation_source": "nb_uttale",
         "tone_trusted": true
       }
     ]
   }
   ```

5. Include audio files in release archives only after a bakeoff picks a provider.

Full wordform coverage is much more expensive and less important for a first
lesson-app release. Lemma-first gives useful coverage with lower cost, fewer files,
and a cleaner review loop.

## Provider Bakeoff

Use `docs/audio-bakeoff-lemmas.tsv` as the test list. Generate the same 200 words
with each candidate provider and listen for:

- naturalness
- Norwegian Bokmal quality
- compounds
- short function-like words
- long derived words
- homographs
- tone minimal pairs

Score tone separately. A provider can still be useful even if isolated-word tone is
not reliable, as long as the app labels audio honestly.

## Azure Speech Setup

Official docs: <https://learn.microsoft.com/azure/ai-services/speech-service/get-started-text-to-speech>

Create:

1. An Azure account.
2. An Azure AI Speech resource.
3. A region for that resource.
4. A Speech key from the resource's "Keys and Endpoint" page.

Local environment:

```bash
export SPEECH_KEY="..."
export SPEECH_REGION="westeurope"
```

Install the SDK only in the script environment that needs it:

```bash
uv add azure-cognitiveservices-speech
```

Minimal Python smoke test:

```python
import os
import azure.cognitiveservices.speech as speechsdk

speech_config = speechsdk.SpeechConfig(
    subscription=os.environ["SPEECH_KEY"],
    region=os.environ["SPEECH_REGION"],
)
speech_config.speech_synthesis_voice_name = "nb-NO-PernilleNeural"
speech_config.set_speech_synthesis_output_format(
    speechsdk.SpeechSynthesisOutputFormat.Audio24Khz48KBitRateMonoMp3
)

audio_config = speechsdk.audio.AudioOutputConfig(filename="/tmp/norsk-azure.mp3")
synthesizer = speechsdk.SpeechSynthesizer(
    speech_config=speech_config,
    audio_config=audio_config,
)
result = synthesizer.speak_text_async("bønner").get()

if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
    raise RuntimeError(result.reason)
```

For this repo, Azure needs:

- `SPEECH_KEY`
- `SPEECH_REGION`
- chosen Norwegian voice name
- output format decision: MP3 for release, WAV only for local analysis

## Google Cloud Text-to-Speech Setup

Official docs: <https://docs.cloud.google.com/text-to-speech/docs/create-audio-text-client-libraries>

Create:

1. A Google Cloud project.
2. Billing on that project.
3. Enable the Cloud Text-to-Speech API.
4. Authenticate locally with Application Default Credentials, or create a service
   account for automation.

Local environment with user credentials:

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Local environment with a service account:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
export GOOGLE_CLOUD_PROJECT="your-project-id"
```

Install the SDK only in the script environment that needs it:

```bash
uv add google-cloud-texttospeech
```

List the live Norwegian voices before choosing one:

```python
from google.cloud import texttospeech

client = texttospeech.TextToSpeechClient()
for voice in client.list_voices(language_code="nb-NO").voices:
    print(voice.name, ",".join(voice.language_codes), voice.ssml_gender.name)
```

As of the current Google supported-voices documentation, Norwegian Bokmal includes
Chirp 3 HD voices such as `nb-NO-Chirp3-HD-Aoede`, Standard voices such as
`nb-NO-Standard-F/G`, and WaveNet voices such as `nb-NO-Wavenet-F/G`. The dashboard
may show only a subset, so the implementation should use `voices:list` as the
source of truth.

Minimal Python smoke test:

```python
from google.cloud import texttospeech

client = texttospeech.TextToSpeechClient()

response = client.synthesize_speech(
    input=texttospeech.SynthesisInput(text="bønner"),
    voice=texttospeech.VoiceSelectionParams(
        language_code="nb-NO",
        name="nb-NO-Chirp3-HD-Aoede",
    ),
    audio_config=texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
    ),
)

with open("/tmp/norsk-google.mp3", "wb") as fh:
    fh.write(response.audio_content)
```

For this repo, Google Cloud needs:

- authenticated ADC or `GOOGLE_APPLICATION_CREDENTIALS`
- `GOOGLE_CLOUD_PROJECT`
- chosen Norwegian voice name
- output format decision: MP3 for release, WAV only for local analysis

## What I Need Before Implementing Audio Generation

To add the real generation pipeline, I need:

- Which provider to wire first: Azure or Google Cloud.
- Credentials set locally, but not committed:
  - Azure: `SPEECH_KEY`, `SPEECH_REGION`
  - Google: ADC login or `GOOGLE_APPLICATION_CREDENTIALS`, plus project ID
- Preferred voice after the 200-word bakeoff.
- Target scope:
  - lemma-only first, recommended
  - full wordform coverage later
- Target output format:
  - MP3 for app/release, recommended
  - WAV only for analysis
- Whether generated audio should be committed, stored as release artifacts, or kept
  outside git and packaged only during release.
