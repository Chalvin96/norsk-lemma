# Article JSON Schema

One file per Bokmålsordboka article. Filename is `{article_id}.json`.

## Top-level fields

```jsonc
{
  "article_id": 51585,          // integer, unique
  "submitted": "2021-01-19",    // ISO datetime string
  "suggest": ["simultan"],      // search suggestions
  "lemmas": [ /* see below */ ],
  "body": { /* definitions, examples, original pronunciation hints */ },
  "to_index": ["simultan"],     // additional index forms
  "author": "...",
  "edit_state": "Eksisterende",
  "status": 8,
  "updated": "2022-02-23"
}
```

## Lemma

```jsonc
{
  "lemma": "simultan",
  "id": 59300,
  "hgno": 0,                     // homograph number (0 = primary)
  "inflection_class": "a1",
  "split_inf": false,
  "audio": {
    "lemma": [
      {
        "type": "tts",
        "provider": "google",
        "voice": "nb-NO-Chirp3-HD-Aoede",
        "language_code": "nb-NO",
        "format": "mp3",
        "file": "9e1c4b4f2a7d.mp3",
        "path": "audio/lemma/google/nb-NO-Chirp3-HD-Aoede/9e1c4b4f2a7d.mp3",
        "url": "https://media.umebocchi.my.id/audio/lemma/google/nb-NO-Chirp3-HD-Aoede/9e1c4b4f2a7d.mp3",
        "text": "simultan",
        "source": "generated",
        "content_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "pronunciation_source": "nb_uttale",
        "tone_status": "known",
        "tone": 1
      }
    ]
  },

  // Added by pronunciation pipeline — copied from the canonical base-form inflection:
  "pronunciation": [
    {
      "sampa":  "sI$mu0l$\"tA:n", // X-SAMPA from NB Uttale
      "ipa":    "ˈsɪmʉlˌtɑːn",   // IPA (ˈ = primary stress, ˌ = secondary stress)
      "tone":   1,                 // pitch accent: 1, 2, or null (see below)
      "tone_status": "known",       // known, none, or unknown
      "tone_label": "Accent 1",     // learner-facing display label; absent when tone is null
      "source": "nb_uttale"        // see source values below
    }
  ],

  "paradigm_info": [ /* see below */ ],

  // Added by translation pipeline:
  "en_translation": {
    "primary": "simultaneous",
    "secondary": ["concurrent"],
    "notes": "adjective, describing things happening at the same time"
  }
}
```

## Paradigm info

```jsonc
{
  "from": "2014-01-01",
  "to": null,                    // null = currently active paradigm
  "tags": ["ADJ"],               // POS tags: NOUN, VERB, ADJ, ADV, PRON, …
  "inflection_group": "ADJ_regular",
  "paradigm_id": 441,
  "standardisation": "STANDARD",
  "inflection": [ /* see below */ ]
}
```

## Inflection (wordform)

```jsonc
{
  "tags": ["Pos", "Masc/Fem", "Ind", "Sing"],
  "word_form": "simultan",       // null for some paradigm slots (no form exists)

  // Added by pronunciation pipeline (absent if word_form is null):
  "pronunciation": [
    {
      "sampa":  "sI$mu0l$\"tA:n",
      "ipa":    "ˈsɪmʉlˌtɑːn",
      "tone":   1,
      "tone_status": "known",
      "tone_label": "Accent 1",
      "source": "nb_uttale"
    }
  ]
}
```

Fallback model entries are marked as review candidates because generated
pronunciations can miss stress, vowels, or pitch accent:

```jsonc
{
  "pronunciation": [
    {
      "sampa":  null,
      "ipa":    "ˈhʉn",
      "tone":   1,
      "tone_status": "known",
      "tone_label": "Accent 1",
      "source": "nb_g2p",
      "prosody_trusted": false,
      "needs_review": true
    }
  ]
}
```

## Pronunciation field reference

| Field    | Type            | Notes |
|----------|-----------------|-------|
| `sampa`  | string or null  | X-SAMPA from NB Uttale. |
| `ipa`    | string          | IPA with `ˈ` primary stress and `ˌ` secondary stress. No tone diacritics — tone is the integer field below. |
| `tone`   | 1, 2, or null   | Norwegian pitch accent. |
| `tone_status` | string | `known` when `tone` is 1 or 2; `none` when trusted NB data has no tone 1/2 marker; `unknown` when model fallback has no tone 1/2 marker. |
| `tone_label` | string or absent | Learner-facing label: `Accent 1` or `Accent 2`. Absent when `tone` is null. |
| `source` | string          | See table below. |
| `prosody_trusted` | boolean or absent | `false` when pronunciation was copied from a related lexical form rather than matched exactly. |
| `needs_review` | boolean or absent | `true` when the entry should not be treated as learner-trustworthy without review. |

## Source values

| `source`             | Meaning |
|----------------------|---------|
| `nb_uttale`          | Direct match in NB Uttale leksika (`e_written_pronunciation_lexicon.csv`) |
| `nb_uttale_newwords` | Match in NB Uttale 2022 additions (`newwords_2022.csv`) |
| `nb_g2p`             | Fallback transcription from NB's `nb-g2p` model. Marked `prosody_trusted=false`, `needs_review=true`. |

## Audio field reference

Audio is added only to audio-enriched release JSON (`dist/lemma-with-audio/`), not
to the clean `lemma/` builder output.

| Field | Type | Notes |
|-------|------|-------|
| `type` | string | `tts` for generated text-to-speech audio. |
| `provider` | string | `google` for Google Cloud Text-to-Speech. |
| `voice` | string | Provider voice name used to generate the file. |
| `language_code` | string | BCP-47 language code, normally `nb-NO`. |
| `format` | string | `mp3` for release audio. |
| `file` | string | Stable MP3 filename only. Resolve to a full path using the manifest or the convention below. |
| `path` | string | Host-free object key, `audio/lemma/{provider}/{voice}/{file}`. Portable; prepend any media host. |
| `url` | string | Ready-to-use absolute URL (`https://media.umebocchi.my.id/` + `path`). Convenience; if the host moves, the data is re-exported. |
| `text` | string | Text sent to TTS. |
| `source` | string | `generated`; not human-recorded audio. |
| `content_sha256` | string | SHA-256 checksum of the generated MP3. Verify integrity before serving. |
| `pronunciation_source` | string or absent | Pronunciation data source associated with the lemma. |
| `tone_status` | string or absent | Copied from trusted/fallback pronunciation metadata. |
| `tone` | 1, 2, or absent | Copied when known; audio is not guaranteed tone-controlled. |

## Importing audio

### Release archive layout

The audio release asset (`norsk-lemma-audio-google-{version}.tar.gz`) unpacks to:

```
README.md
data/audio/
  manifest-google-{voice}.json
  lemma/
    google/
      {voice}/
        {sha256_prefix}.mp3   # one file per unique (text, tone) pair
dist/
  lemma-with-audio/
    {article_id}.json         # same shape as lemma/ but with audio[] on each lemma
```

### Resolving audio to a URL

Each audio entry already carries the resolved location:

- `url` — the absolute, ready-to-fetch URL. Use this directly.
- `path` — the host-free key, in case you serve the MP3s from a different host:
  `media_base + "/" + path`.

For on-disk resolution, `path` is rooted differently depending on where you are:

```
repo working tree:   data/{path}  →  data/audio/lemma/google/{voice}/...mp3
release archives:     {path}      →  audio/lemma/google/{voice}/...mp3   (archive root)
```

Both release tarballs lay the audio at `audio/lemma/...` from their root, so
`path` resolves directly there.

The `file` field is a stable content-addressed name (SHA-256 of `provider|voice|language_code|text|tone_status|tone`). It does not change if the same word is re-synthesized with the same parameters, so `url`/`path` are equally stable.

### Manifest

`data/audio/manifest-google-{voice}.json` lists every synthesized file:

```jsonc
{
  "provider": "google",
  "voice": "nb-NO-Chirp3-HD-Aoede",
  "language_code": "nb-NO",
  "format": "mp3",
  "items": [
    {
      "text": "simultan",
      "file": "9e1c4b4f2a7d.mp3",
      "path": "audio/lemma/google/nb-NO-Chirp3-HD-Aoede/9e1c4b4f2a7d.mp3",
      "url": "https://media.umebocchi.my.id/audio/lemma/google/nb-NO-Chirp3-HD-Aoede/9e1c4b4f2a7d.mp3",
      "article_ids": [51585],
      "source_lemma_ids": [59300],
      "tone_status": "known",
      "tone": 1,
      "content_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ]
}
```

Use the manifest to:
- Build a lookup table from `file` → `path` without reading every lemma JSON
- Verify file integrity (`content_sha256`)
- Enumerate all audio files for bulk import or CDN upload

### Choosing which JSON to import

| Use case | Import from |
|----------|-------------|
| Vocabulary, pronunciation, inflection — no audio | `lemma/` |
| Dictionary with audio playback | `dist/lemma-with-audio/` |
| Serve audio from CDN / object storage | Copy `data/audio/lemma/` tree; use `path` from manifest as the object key |

### Tonal homographs

Words with two tones (e.g. `tanken` — the thought vs the tank) produce two separate
MP3 files with different filenames. Each lemma entry in the enriched JSON carries its
own `audio.lemma[]` pointing to the correct file for that sense. Match on `tone` when
displaying to learners.

## Pitch accent (tone)

Norwegian Bokmål has two lexical tones (tonelag) that distinguish minimal pairs:

- **Tone 1** — single pitch peak. Stored as `"tone": 1`.
- **Tone 2** — double pitch peak. Stored as `"tone": 2`.
- **No tone marker in trusted data** — stored as `"tone": null`,
  `"tone_status": "none"`.
- **Unknown model tone** — stored as `"tone": null`, `"tone_status": "unknown"`.
- **No third learner tone** — NoFAbet `3` means secondary stress, which stays in
  `ipa` as `ˌ` and is not represented in the `tone` field.

The IPA string does **not** include a tone diacritic — there is no universal standard
for Norwegian tone representation in IPA (sources use grave/circumflex, superscript
¹/², or tone letters). The integer field is unambiguous and lets the rendering layer
choose the convention:

- Recommended learner display: label or superscript — `Accent 1` / `Accent 2`, or
  `¹bønder` / `²bønner`
- Phonological notation: `bønder` [L̂HL] vs `bønner` [LHL]
- TTS engines assign tone from their own dictionaries. IPA phoneme injection via SSML
  does not control Norwegian tonelag reliably.

Example minimal pair: `bønder` "farmers" (tone 1) vs `bønner` "beans" (tone 2).
Both have `ipa: "ˈbœnər"` — only `tone` differs.

For learner-facing UI:

- Use `tone_label` directly when present
- Show `tone_status: none` as no accent label
- Show `tone_status: unknown` as unknown/unverified accent
- Do not surface secondary stress as tonal information

## Body pronunciation

`body.pronunciation[]` entries are the original Norwegian phonetic respelling from
Ordbøkene (~4,500 articles). Not IPA, not SAMPA. Preserved as-is; separate from the
`pronunciation` field added by this pipeline.
