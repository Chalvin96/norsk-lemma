# Data Sources and Attribution

## Ordbøkene (Bokmålsordboka)

The article JSON files in `data/articles/` are derived from the official Norwegian
dictionaries published by Universitetet i Bergen and Språkrådet.

- **Source:** https://ordbokene.no / https://ord.uib.no
- **API:** https://ord.uib.no/api/
- **License:** CC BY 4.0; preserve the attribution in `NOTICE`.

The article cache retains the upstream definitions, inflections, examples, and
cross-references. Translation, pronunciation, frequency, and audio fields are
project enrichments and are not part of the upstream dictionary data.

## NB Uttale — Norwegian Pronunciation Lexicon

The pronunciation data (IPA, SAMPA, pitch accent) added by the enrichment pipeline
comes from NB Uttale, produced by the Norwegian Language Bank
(Nasjonalbiblioteket / Språkbanken). NB Uttale extends the original NST
(Nordisk Språkteknologi) pronunciation lexicon with corrections and new entries.

### nb_uttale_leksika

- **File used:** `e_written_pronunciation_lexicon.csv` (East Norwegian, written form)
- **Entries:** ~708,000 wordforms with pre-computed IPA, SAMPA, and NoFAbet
- **Dialect:** East Norwegian (standard Bokmål pronunciation, Oslo region)
- **Source:** https://www.nb.no/sprakbanken/ressurskatalog/oai-nb-no-sbr-79/
- **License:** CC0 1.0 Universal (public domain)
- **Citation:** Pettersen, M. et al. "NB Uttale: A Norwegian Pronunciation Lexicon
  with Dialect Variation." LREC-COLING 2024.

Entries from this file appear as `"source": "nb_uttale"` for exact wordform matches.

### nb_uttale_tillegg (supplementary package)

- **Files used:** `newwords_2022.csv` (25,500 entries added 2022) and `conversion.py`
  (NoFAbet → IPA/SAMPA conversion rules, ported inline into the enrichment script)
- **Source:** https://www.nb.no/sprakbanken/ressurskatalog/oai-nb-no-sbr-79/
- **License:** CC0 1.0 Universal (public domain)

Entries from `newwords_2022.csv` appear as `"source": "nb_uttale_newwords"`.

## nb-g2p

`nb-g2p` is used in the enrichment pipeline when a wordform is not found directly in
NB Uttale or the NB Uttale supplementary newwords list.

1. **Fallback transcription** — predicts NoFAbet tokens for single-token inflected
   forms and compounds directly from spelling, then the pipeline converts those tokens
   to IPA and extracts tone from the NoFAbet stress digit.

Unlike the previous OBT-based flow, this fallback does not require a separate HTTP
service or Docker image.

The pipeline does not send multiword expressions or Ordbokene bracket alternatives to
`nb-g2p`; those forms remain unresolved unless a lexical source has an exact match.

- **Source:** https://github.com/NationalLibraryOfNorway/sprakbanken-nb-g2p
- **Authors:** National Library of Norway / Sprakbanken
- **License:** See upstream project metadata
- **Runtime note:** uses Phonetisaurus/OpenFST under the hood. The current NB model
  path is CPU-only; this repo batches fallback calls per article, then serializes each
  batch because the library is not thread-safe.

Entries resolved through this fallback appear as `"source": "nb_g2p"` and are marked
`prosody_trusted=false` and `needs_review=true`. If the model output lacks tone 1 or
tone 2, the entry uses `"tone": null` and `"tone_status": "unknown"`.

## Google Cloud Text-to-Speech

MP3 audio files in `data/export/audio/` are synthesized using Google Cloud
Text-to-Speech with the `nb-NO-Chirp3-HD-Aoede` (Chirp3-HD) voice.

- **Provider:** Google LLC
- **Voice:** `nb-NO-Chirp3-HD-Aoede`
- **Format:** MP3, 24 kHz
- **Usage terms:** Subject to [Google Cloud TTS Terms of Service](https://cloud.google.com/text-to-speech/terms)
- **Note:** Audio files are generated from Ordbokene lemma text and are
  distributed as a separate release artifact. The synthesis process is
  documented in `scripts/generate_audio.py`.

Audio metadata (provider, voice, file hash, tone) is embedded in each
`data/export/lemma/*.json` entry under `lemmas[*].audio`.

## English Translations

English headword glosses, definition glosses, and example translations are
project-generated enrichments. The pipeline defaults to
`google/gemini-2.5-flash-lite`, but values may come from later review and
correction passes, and the export does not record per-field model provenance.
Do not attribute every current translation to one model or run. The fields are
described in the [schema reference](schema.md).

## Norwegian Kelly list (frequency rank)

The `frequency_rank` field on each exported lemma is derived from the **Norwegian
Kelly list**, a corpus-frequency vocabulary list for language learners.

- **Provider:** Universitetet i Oslo, Tekstlaboratoriet (UiO Text Laboratory) —
  [tekstlab.uio.no/kelly](https://www.hf.uio.no/iln/english/about/organisation/text-laboratory/services/kelly.html)
- **License:** [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
  (**share-alike** — a different licence from the CC BY 4.0 Ordbøkene data above).
- **Citation:** Kilgarriff, A., Charalabopoulou, F., et al. 2014. "Corpus-based
  vocabulary lists for language learners for nine languages." *Language Resources
  and Evaluation* 48:121–163.
- **Vendored at:** `data/vendor/kelly/` (raw `.xls` + normalized `kelly.csv` +
  provenance README). Rank = Kelly row order; the list has no CEFR column.
- **Join:** the `frequency` stage matches Kelly `(lemma, POS)` onto exported
  lemmas. Homographs share a rank and are flagged `frequency_ambiguous`. See the
  [Frequency rank](schema.md#frequency-rank) section of the schema doc and
  `data/vendor/kelly/match-report.json` for coverage.

The frequency ranks are factual data, but the vendored Kelly source in
`data/vendor/kelly/` is redistributed under CC BY-SA 4.0.
