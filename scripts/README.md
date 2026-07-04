# Pipeline CLI

All stages run through the repo-root entry point:

```bash
uv run python pipeline.py <command> [options]
```

| Command | What it does |
|---------|--------------|
| `fetch` | Download and unpack the Bokmålsordboka article archive into `data/articles/`. |
| `hydrate` | Download a GitHub release and embed its translations into articles — no LLM cost. |
| `translate` | Translate pending articles via an LLM (default [OpenRouter](https://openrouter.ai); see [Harness backends](#harness-backends)); embed results into the articles. |
| `pronounce` | Enrich articles with IPA and pitch accent (NB Uttale → newwords → nb-g2p). |
| `export` | Build lemma JSON from enriched articles. |
| `review` | Review exported English translations via an LLM; write a QA report of only the issues. |
| `apply-review` | Apply a review report's `suggested_en` values back into the articles. |
| `audio` | Generate lemma MP3s with Google Cloud Text-to-Speech; write audio metadata into the exported lemma JSON. |
| `build` | Run fetch → translate → pronounce → export in sequence. |

Every command is idempotent: existing outputs are skipped unless `--force` is
passed, so runs are safe to interrupt and resume. `--dry-run` previews pending
work without side effects; `--limit N` caps work for testing.

## Requirements

```bash
uv sync
export OPENROUTER_API_KEY=sk-or-...   # translate only
```

`pronounce` additionally needs the NB Uttale lexica downloaded into `data/` —
see [`../data/README.md`](../data/README.md).

## Common options

| Flag | Default | Description |
|------|---------|-------------|
| `--articles-dir` | `data/articles/` | Article JSON directory |
| `--lemma-dir` | `data/export/lemma/` | Exported lemma JSON directory |
| `--limit` | _(none)_ | Stop after N files |
| `--force` | `false` | Rebuild outputs that already exist |
| `--dry-run` | `false` | Report pending work without doing it |

### `translate`

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `google/gemini-2.5-flash-lite` | OpenRouter model |
| `--harness` | `openrouter` | Transport backend: `openrouter`, `codex`, `claude`, `opencode`, `droid`, `pi` |
| `--reasoning-effort` | _(none)_ | Reasoning effort for CLI harnesses that support it; ignored by `openrouter` |
| `--batch-size` | `10` | Articles per API request |
| `--max-retries` | `3` | Retries per batch on failure or rate limit |
| `--retry-delay` | `60` | Seconds to wait after a 429 |
| `--error-log` | `error_openrouter.log` | File to append failed translations to |

#### Harness backends

`translate`, `review`, and `build` can route each LLM request through one of six
transports (same `--harness` / `--reasoning-effort` flags on each). `openrouter`
calls the OpenRouter HTTP API. The other five shell out
to a locally-installed agentic CLI, run non-interactively in a throwaway working
directory (so no repo `CLAUDE.md`/`AGENTS.md` leaks into the prompt) with each
tool's available isolation applied — `codex` read-only sandbox, `pi`
`--no-tools`, `claude` `--disallowedTools`, `opencode` `--pure`. Isolation is
best-effort and varies per tool; treat CLI harnesses as trusted-input only and
smoke-test each before a large run:

| Harness | Requires | Model id format |
|---------|----------|-----------------|
| `openrouter` | `OPENROUTER_API_KEY` | OpenRouter id, e.g. `google/gemini-2.5-flash-lite` |
| `codex` | `codex` CLI logged in | model id, e.g. `gpt-5.5` |
| `claude` | `claude` CLI logged in | model id/alias, e.g. `claude-sonnet-5` |
| `opencode` | `opencode` CLI configured | `provider/model` |
| `droid` | `droid` CLI logged in | model id, e.g. `claude-opus-4-8` |
| `pi` | `pi` CLI configured | `provider/model` |

```bash
uv run python pipeline.py translate --harness codex --model gpt-5.5 --reasoning-effort high --limit 20
```

CLI harnesses carry far higher per-call latency than the HTTP path — use them
for small or `--limit`ed runs, not the full corpus. Billing/usage errors from
any harness stop the run instead of burning retries.

### `hydrate`

| Flag | Default | Description |
|------|---------|-------------|
| `--tag` | latest release | Release tag to download (e.g. `v1.3.0`) |

### `pronounce`

| Flag | Default | Description |
|------|---------|-------------|
| `--leksika` / `--newwords` | auto-detected under `data/` | NB Uttale source files |
| `--workers` | CPU count | Parallel workers |

### `audio`

| Flag | Default | Description |
|------|---------|-------------|
| `--voice` | — | TTS voice, e.g. `nb-NO-Chirp3-HD-Aoede` |
| `--language-code` | `nb-NO` | TTS language |
| `--audio-dir` | `data/audio/` | MP3 + manifest output |
| `--enrich-only` | `false` | Skip synthesis; write metadata from existing manifest |
| `--confirm-cost` | `false` | Required for full-corpus synthesis |
| `--list-voices` | `false` | List available voices and exit |

## Examples

Test on a small slice before running the full dataset:

```bash
uv run python pipeline.py translate --limit 50 --dry-run
uv run python pipeline.py translate --limit 50
```

Rebuild lemma JSON from already-embedded article translations (no LLM cost):

```bash
uv run python pipeline.py export --force
```

Use a different gloss model:

```bash
uv run python pipeline.py translate --model anthropic/claude-haiku-4-5
```

### Review

Review exported English translations with a stronger model:

```bash
uv run python pipeline.py review --limit 100 --dry-run
uv run python pipeline.py review --review-model openai/gpt-5.4 --limit 100 --output translation-review.json
```

The review step checks `primary_translation`, definition glosses, and example
translations together. It reports only issues, so it works well after cheaper
bulk generation.

### Audio generation

Requires a Google Cloud service account with the `Cloud Text-to-Speech User`
role, plus the `audio` dependency group:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

uv run --group audio python pipeline.py audio --list-voices --language-code nb-NO
uv run --group audio python pipeline.py audio --voice nb-NO-Chirp3-HD-Aoede --dry-run
uv run --group audio python pipeline.py audio --voice nb-NO-Chirp3-HD-Aoede --limit 20
```

Full-corpus generation requires an explicit cost confirmation:

```bash
uv run --group audio python pipeline.py audio \
  --voice nb-NO-Chirp3-HD-Aoede \
  --confirm-cost
```

MP3s land under `data/export/audio/lemma/google/{voice}/`, a SHA-256 manifest
under `data/export/audio/`, and audio metadata is written into the lemma JSON
files under `data/export/lemma/`.

## Standalone scripts

| Script | Purpose |
|--------|---------|
| `enrich_pronunciation.py` | Pronunciation enrichment (wrapped by `pipeline.py pronounce`) |
| `generate_audio.py` | TTS synthesis (wrapped by `pipeline.py audio`) |
| `upload_audio.py` | Push MP3s to S3-compatible object storage (see root README) |
| `release.sh` | Tag + build + publish GitHub release tarballs |
| `ordbokene/` | Pipeline modules (client, extract, build, prompt, audio, io, settings) |

## Output format

Each exported lemma payload contains one or more lemma entries with a
`primary_translation` string:

```json
{
  "source_article_id": 123,
  "lemmas": [
    {
      "lemma": "strekke seg",
      "primary_translation": "stretch"
    }
  ],
  "definitions": [
    {
      "text": "rette ut kroppen",
      "translation": "stretch the body",
      "examples": [
        {
          "no": "hun strakte seg etter boken",
          "en": "she stretched to reach the book"
        }
      ]
    }
  ]
}
```

Each definition includes up to two example sentences as `{ "no", "en" }` pairs
(original Norwegian plus English translation), translated in the same LLM call as
the sense. Reuse-path entries emit `en: ""` until re-run through the model.

Failed translations are skipped in the file and logged to `error_openrouter.log` with a timestamp, filename, word, and error reason.

Full field reference: [`../docs/schema.md`](../docs/schema.md).

## Development

Linting and formatting use [Ruff](https://docs.astral.sh/ruff/) via pre-commit. From the repo root:

```bash
uv sync --dev
uv run pre-commit install
uv run pre-commit run --all-files
```

Configuration lives in `ruff.toml` and `.pre-commit-config.yaml`.
