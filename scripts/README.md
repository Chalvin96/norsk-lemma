# scripts/translate.py

Reads article JSON files from `articles/` and generates import-ready lemma payloads in `lemma/`. Translations are generated in batches via an LLM on [OpenRouter](https://openrouter.ai).

## Requirements

```bash
uv sync
```

You also need an OpenRouter API key:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

## Basic usage

```bash
uv run python scripts/translate.py
```

If `articles/` is missing or empty, the script downloads and unpacks the Bokmålsordboka source archive from [`https://ord.uib.no/bm/fil/article.tar.gz`](https://ord.uib.no/bm/fil/article.tar.gz) first. It is safe to interrupt and re-run because existing `lemma/*.json` files are skipped unless you pass `--force`.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--articles-dir` | `articles/` | Path to the article JSON directory |
| `--lemma-dir` | `lemma/` | Path to the generated lemma JSON directory |
| `--model` | `google/gemini-2.5-flash-lite` | OpenRouter model to use |
| `--batch-size` | `10` | Articles per API request |
| `--limit` | _(none)_ | Stop after N article files (useful for testing) |
| `--max-retries` | `3` | Retries per batch on failure or rate limit |
| `--retry-delay` | `60` | Seconds to wait before retrying after a 429 |
| `--force` | `false` | Rebuild lemma JSON files that already exist |
| `--dry-run` | `false` | Scan and report pending work without making API calls |
| `--fresh` | `false` | Ignore embedded translation fields and call the LLM for every pending article |
| `--error-log` | `error_openrouter.log` | File to append failed translations to |

## Examples

Test on a small slice before running the full dataset:

```bash
uv run python scripts/translate.py --limit 50 --dry-run
uv run python scripts/translate.py --limit 50
```

Force fresh LLM calls for every pending entry:

```bash
uv run python scripts/translate.py --force --fresh
```

Rebuild from embedded article translations without paying for already-translated
entries. This is the default behavior when source articles already contain
`primary_translation` or definition `translation` fields.

```bash
uv run python scripts/translate.py --force
```

Use a different model:

```bash
uv run python scripts/translate.py --model anthropic/claude-haiku-4-5
```

## Output format

Each generated lemma payload contains one or more lemma entries with a `primary_translation` string:

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
      "translation": "stretch the body"
    }
  ]
}
```

Failed translations are skipped in the file and logged to `error_openrouter.log` with a timestamp, filename, word, and error reason.

## Development

Linting and formatting use [Ruff](https://docs.astral.sh/ruff/) via pre-commit. From the repo root:

```bash
uv sync --dev
uv run pre-commit install
uv run pre-commit run --all-files
```

Configuration lives in `ruff.toml` and `.pre-commit-config.yaml`.
