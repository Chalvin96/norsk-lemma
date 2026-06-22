# Norsk Lemma

An LLM-assisted Norwegian lexicon pipeline built from [Ordbokene](https://ordbokene.no) open data. This repo turns the Bokmålsordboka article dump into normalized lemma JSON with English glosses, ready for import into [Flyt](https://github.com/Chalvin/flyt).

This project demonstrates a practical data-processing workflow: source ingestion,
normalization, prompt construction, LLM response parsing, resumable batch jobs,
release packaging, and source attribution handled end to end.

> Scope: Bokmål only for now. Nynorsk is not included.

## Project Highlights

- End-to-end ingestion pipeline from public dictionary export to import-ready lemma JSON
- LLM enrichment via OpenRouter with retry handling, batching, and resumable processing
- Structured extraction of lemmas, senses, examples, redirects, and cross-references
- Release automation that publishes versioned lemma archives for downstream consumers
- Clear separation between permissively licensed code and CC BY 4.0 source-derived data

## Repository Layout

| Path | Purpose |
|---|---|
| `scripts/translate.py` | CLI entry point for building lemma output |
| `scripts/ordbokene/` | Modular pipeline components: source fetch, extraction, prompt building, client, I/O, settings |
| `articles/` | Local working directory for Ordbokene source files; ignored by git except for `.gitkeep` |
| `lemma/` | Generated lemma JSON files tracked for reproducible release archives |
| `.github/workflows/release-archive.yml` | GitHub Actions release packaging |

## How It Works

1. Source article JSON is pulled from the Ordbokene open data archive.
2. The pipeline explodes and normalizes article structures into lemma-oriented entries.
3. Missing English translations are generated in batches through OpenRouter.
4. Enriched lemma JSON is written to `lemma/`.
5. Tagged releases publish a tarball for downstream import.

## Quick Start

Install dependencies with `uv`:

```bash
uv sync
```

Set your OpenRouter API key:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

Run the pipeline:

```bash
uv run python scripts/translate.py
```

If `articles/` is empty, the script automatically downloads the Bokmålsordboka source archive from Ordbokene and flattens it into the local working directory before processing. That directory is intentionally not stored in git.

Preview pending work without making API calls:

```bash
uv run python scripts/translate.py --limit 50 --dry-run
```

More usage details live in [`scripts/README.md`](scripts/README.md).

Run the quality checks used for development:

```bash
uv sync --dev
uv run pytest scripts/test_translate.py
uv run ruff check
```

## Output

Each generated lemma payload contains one or more lemma entries with a `primary_translation` string and per-definition English glosses, for example:

```json
{
  "source_article_id": 123,
  "lemmas": [
    {
      "lemma": "utepils",
      "primary_translation": "outdoor beer"
    }
  ],
  "definitions": [
    {
      "text": "ol som blir drukket ute i fint ver",
      "translation": "beer enjoyed outside in good weather"
    }
  ]
}
```

## Release Downloads

Tagged releases publish versioned `lemma/` archives such as:

```text
norsk-lemma-v1.0.0.tar.gz
```

Example release URL:

```text
https://github.com/Chalvin96/norsk-lemma/releases/download/v1.0.0/norsk-lemma-v1.0.0.tar.gz
```

## Attribution

Data derived from Bokmålsordboka / Nynorskordboka:

```text
Bokmålsordboka/Nynorskordboka, Universitetet i Bergen og Språkrådet, ordbøkene.no, CC BY 4.0.
```

- License: <https://creativecommons.org/licenses/by/4.0/>
- Open data: <https://ordbokene.no/nob/about/open-data>
- Citation guide: <https://ordbokene.no/nob/help/cite>

## License

The code in this repository is licensed under Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

The source dictionary data in `articles/` and the derived data in `lemma/` remain subject to the Ordbokene attribution and CC BY 4.0 terms above.
