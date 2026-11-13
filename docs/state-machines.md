# State machines and invariants

This document lists the state machines in the pipeline, the invariants each one
relies on, and whether the code enforces them. Each entry is marked
**enforced** (code rejects the bad state), **unenforced** (the code assumes
it), or **violated** (the bad state is reachable, with evidence).

Evidence was gathered against `main` at `053c54684da`, the committed
`data/export/lemma/` (96,401 files), and the public upstream archive
`https://ord.uib.no/bm/fil/article.tar.gz` downloaded 2026-11-12 (98,182 files,
97,613 articles after sub-article explosion).

Issue IDs (`SM-n`) match the fix list at the end.

## 1. Article cache (`data/articles/{id}.json`)

The cache is the source of truth. Every stage except `export`, `frequency`
and `review` mutates it in place by rewriting whole files.

States per article file:

| State | Meaning |
|---|---|
| `absent` | No file. |
| `fetched` | Upstream JSON, no pipeline fields. |
| `translated:none` | `extract_existing_translations` returns `None`. |
| `translated:partial` | Some of primary, definition `translation`, example `en` present. |
| `translated:complete` | `complete_existing_translation` returns a result. |
| `blocked` | `source_packet` raises (alias it cannot resolve, no definitions, reordered examples). Translate can never complete it. |
| `pronounced` | Every non-null `word_form` has `pronunciation`. Orthogonal to translation state. |
| `audio` | Lemma carries `audio.lemma[]`. Orthogonal. |

Transitions:

| From | Command | To | Code |
|---|---|---|---|
| absent (dir empty) | `fetch` / any stage via `ensure_articles_dir` | fetched | `source.py:7` |
| any | `fetch --force` | absent, then fetched | `cli.py:134` |
| translated:none, partial | `translate` | translated:complete | `cli.py:389-414` |
| blocked | `translate` | blocked (error logged each run) | `client.py:476` |
| translated:none | `hydrate` | partial or complete | `cli.py:300-342` |
| translated:partial | `hydrate` (no `--force`) | blanks filled from release | `cli.py` |
| any | `translate-examples` | example `en` filled | `examples.py:384` |
| any | `apply-review` | example `en` replaced | `examples.py:519` |
| any | `pronounce` | pronounced | `enrich_pronunciation.py:410` |
| any | `audio` | audio | `audio.py:222` |

Invariants:

- **I1.1 One writer per article directory at a time.** Enforced (SM-2).
  Mutating commands hold an advisory process lock across their article writes.
- **I1.2 `fetched` means the whole archive landed.** Enforced (SM-4).
  An incomplete marker triggers a resume that preserves existing files.
- **I1.3 Article ids are unique after explosion.** Unenforced in `iter_exploded`,
  checked per batch (`client.py:466`). Holds today: 0 duplicates in upstream.
- **I1.4 Definition order is one traversal.** Enforced. `iter_definition_cores`
  is the single source of ordering; `embed_translations` checks
  `source_id` list and example cardinality before writing (`embed.py:20-30`).
- **I1.5 `hydrate` without `--force` fills what is missing.** Enforced (SM-5).
  Hydrate preserves nonblank values and writes only changed articles.
- **I1.6 `blocked` articles are a known, stable set.** Unenforced. 15,717
  upstream articles raise in `source_packet` (8,532 unscoped multi-sense alias
  targets, 5,850 multi-target aliases, 1,233 without definitions, 61 reordered
  examples, 66 other). They are retried and re-logged on every run. This is
  expected behavior, documented here so it is not mistaken for a bug.

## 2. Translation request (`client.request_translations`)

Per article in a batch:

| State | Meaning |
|---|---|
| `rejected:source` | `source_packet` raised; terminal for this run. |
| `cached` | Valid cache hit; terminal. |
| `pending` | Awaiting an LLM attempt. |
| `accepted` | Passed `validate_translation`; cached durably, then terminal. |
| `rejected:llm` | Failed validation on the last attempt. |
| `stopped` | Quota detected; batch loop exits. |

| From | Event | To |
|---|---|---|
| pending | valid result | accepted (cache write before return) |
| pending | invalid result, attempts left | pending (sleep `retry_delay`) |
| pending | invalid result, last attempt | rejected:llm |
| pending | `missing_api_key` | stopped |
| pending | quota error | stopped |

Invariants:

- **I2.1 Cache key covers every input that changes output.** Enforced:
  `PIPELINE_VERSION`, model, harness, effort, full prompt (`client.py:481`).
- **I2.2 Accepted results are durable before the caller writes articles.**
  Enforced (`client.py:536-538`).
- **I2.3 "Quota" means a provider billing or rate limit.** Enforced (SM-1).
  Transport errors carry a quota flag from HTTP status or CLI diagnostics, and callers use that flag rather than classifying validation errors.
- **I2.4 A missing API key stops the run.** Enforced (SM-1).
  Translate stops after the first affected batch and logs the missing key once.

## 3. Example translation (`examples.run`)

Batch states: `queued`, `in-flight`, `embedded`, `failed`, `stopped`.

| From | Event | To |
|---|---|---|
| queued | worker free and not stopped | in-flight |
| in-flight | dict result | embedded (articles with changed examples written) |
| in-flight | non-quota error string | failed (logged, examples stay pending) |
| in-flight | quota error string | stopped (no refill; in-flight drained) |

Invariants:

- **I3.1 Each article belongs to one batch.** Enforced by the single
  `iter_pending_examples` generator.
- **I3.2 Written means changed.** Enforced (SM-9). An article is written and counted only when embedding changes an example translation.
- **I3.3 Quota classification.** Enforced (SM-1). Example translation uses the transport error quota flag.

## 4. Lemma export file (`data/export/lemma/{id}.json`)

| State | Meaning |
|---|---|
| `absent` | No file. |
| `current` | Equals `build_lemma(article)` plus preserved frequency fields. |
| `stale` | Exists but differs from what current code would build, or the article no longer produces output. |

| From | Condition in `cmd_export` | To |
|---|---|---|
| any | article has no lemmas | absent (`cli.py:478`) |
| any | article has embedded translations | current |
| any | article has no embedded translations | absent |
| any | article missing from cache | unchanged until `prune` |

Invariants:

- **I4.1 The export is a function of the article cache and code.** Enforced (SM-3).
  Export rebuilds eligible articles and standalone prune removes files that cannot be rebuilt.
- **I4.2 Every lemma has `frequency_rank`.** Enforced (SM-3).
  Export initializes the field to null and preserves existing frequency values.
- **I4.3 Definitions carry glosses.** 9,197 files have some blank definition
  `translation`, 2 have all blank. Partial translation is allowed by schema
  v3 for examples only; the doc does not say blank glosses are allowed.
  Tracked under SM-3 as a data check, not a code fix.

## 5. Lemma directory swap (`_download_lemma_release`)

States: `live`, `staged` (sibling temp dir), `backed-up` (old dir renamed),
`swapped`, `restored`.

| From | Event | To |
|---|---|---|
| live | archive validated, files moved to staging | live + staged |
| live + staged | `os.replace(lemma, backup)` | backed-up + staged |
| backed-up + staged | `os.replace(staging, lemma)` | swapped |
| backed-up | failure before swap | restored |
| swapped | backup removed | live |

- **I5.1 `lemma_dir` is never absent or half-written.** Enforced, with a gap
  of one rename between the two `os.replace` calls. Acceptable.
- **I5.2 Release archive matches requested tag.** Enforced by asset name.

## 6. Audio job

| State | Meaning |
|---|---|
| `pending` | In current lemma export, no valid local MP3. |
| `staged` | Synthesis wrote `.pending` file. |
| `local` | MP3 at `output_path_for_job`, checksum recorded. |
| `manifested` | Item in `manifest-google-<voice>.json`. |
| `embedded` | Article lemma carries `audio`. |
| `uploaded` | Object at bucket key `path`. |

Invariants:

- **I6.1 A bucket key matches the bytes the manifest describes.** Enforced (SM-6).
  Upload compares checksum metadata when available and replaces mismatched objects, warning that cached clips require a CDN purge.
- **I6.2 One audio clip per source lemma id.** Enforced (SM-6).
  Embedding selects the current pronunciation job when available and resolves other conflicts deterministically.
- **I6.3 Manifest is written before embedding.** Enforced.
- **I6.4 Partial synthesis is resumable.** Enforced: manifest flushed every
  100 jobs, files checksummed on resume.

## 7. Review cycle

`review` writes issues keyed by batch `id` plus `article_id` and positional
`definitions[N].examples[M].en`. `apply-review` writes `suggested_en` at that
position.

- **I7.1 An issue's `article_id` comes from the batch mapping.** Enforced (SM-7).
  Review drops invalid or conflicting IDs and assigns the mapped article ID.
- **I7.2 The target example is the one reviewed.** Enforced (SM-7).
  Review records Norwegian example text and apply checks it, warning for legacy issues without text.

## 8. Release (`scripts/release.sh`)

States: `clean`, `archives-built`, `published`.

- **I8.1 Archive contents equal the tagged tree.** Enforced (SM-8).
  Release rejects modified and untracked shipped paths unless `--allow-dirty` is set; audio is intentionally local.
- **I8.2 Tag is new.** Enforced locally and on `origin`.

## 9. Pronunciation enrichment

- **I9.1 G2P results align with inputs.** Enforced (SM-10).
  The pipeline leaves the batch unresolved if result count or word identities differ; an offline probe confirmed the model drops empty and `…` inputs.

## Fix list

Priority by impact. Each fix lands with a regression test that fails first.

| ID | Impact | Fix |
|---|---|---|
| SM-1 | High. `translate` cannot pass article 1153. | Classify quota where transport errors are raised; carry a typed flag; never text-match validation errors. Stop on `missing_api_key`. |
| SM-2 | High. Silent lost updates across stages. | Advisory lock on the articles directory for every mutating command; fail fast with the holder's command. |
| SM-3 | Medium. Export not reproducible. | Standalone `prune` removes files export cannot rebuild; export reports files not rebuilt. Regenerating the committed export is a separate data task. |
| SM-4 | Medium. Partial cache accepted. | Incomplete marker surrounds download and flatten; resume without clobbering. |
| SM-5 | Medium. Hydrate misses new senses. | Non-force hydrate fills blanks only and skips no-op writes. |
| SM-6 | Medium. Stale public audio. | Upload compares stored checksum metadata; embed resolves one clip per lemma id deterministically. |
| SM-7 | Low-medium. Misapplied review. | Batch mapping wins; issues carry `no` text; apply checks it. |
| SM-8 | Low. Archive differs from tag. | Dirty check includes untracked files under shipped paths. |
| SM-9 | Low. No-op writes. | Write only when an example changed. |
| SM-10 | Medium. Misattributed pronunciations. | Reject batches with misaligned G2P results. |

## Status

| ID | Status |
|---|---|
| SM-1 | fixed |
| SM-2 | fixed |
| SM-3 | fixed |
| SM-4 | fixed |
| SM-5 | fixed |
| SM-6 | fixed |
| SM-7 | fixed |
| SM-8 | fixed |
| SM-9 | fixed |
| SM-10 | fixed |
