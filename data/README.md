# Data Directory

This directory contains both tracked product data and ignored pipeline inputs:

- `export/lemma/` is the tracked application dataset.
- `vendor/kelly/` contains the tracked frequency source and its provenance.
- `articles/` is the ignored, mutable article cache used to rebuild the export.
- The two NB Uttale directories below are ignored local pronunciation inputs.

Populate an empty article cache with:

```bash
uv run python pipeline.py fetch
```

Translation, pronunciation, and audio metadata are written into the article cache
before `export` builds the tracked lemma files. See the
[project README](../README.md#how-the-pipeline-works) for the complete workflow.

## Required pronunciation files

### 1. NB Uttale — tillegg (supplementary package)

Contains 25,500 additional words not in the main leksika (neologisms, 2022 additions)
plus `conversion.py` — the NoFAbet→IPA conversion rules ported inline into the enrichment
script. Secondary source; leksika is primary.

**Download:**
```
https://www.nb.no/sbfil/uttaleleksikon/nb_uttale_tillegg.zip
```

**Extract into `data/`** — the zip contains a `nb_uttale_tillegg/` subdirectory:
```bash
cd data && unzip nb_uttale_tillegg.zip
```

Expected layout after extraction:
```
data/
└── nb_uttale_tillegg/
    └── nb_uttale_tillegg/
        ├── nor030224NST_utf8.pron    ← 784k entries, UTF-8, 51-field semicolon CSV
        ├── newwords_2022.csv         ← 25.5k extra entries in NoFAbet format
        ├── conversion.py             ← NoFAbet conversion rules
        ├── rules_v1.py
        └── exemptions_v1.py
```

### 2. NB Uttale — main lexica

Provides East/West/Trønder/North/Southwest dialect transcriptions.
`e_written_pronunciation_lexicon.csv` (East Norwegian) is the primary source.

**Download:**
```
https://www.nb.no/sbfil/uttaleleksikon/nb_uttale_leksika.zip
```

**Extract into `data/`:**
```bash
cd data && unzip nb_uttale_leksika.zip
```

Expected layout after extraction:
```
data/
└── nb_uttale_leksika/
    └── nb_uttale_leksika/
        ├── e_written_pronunciation_lexicon.csv   ← East Norwegian written (primary)
        ├── e_spoken_pronunciation_lexicon.csv
        ├── w_written_pronunciation_lexicon.csv   ← West Norwegian
        ├── sw_written_pronunciation_lexicon.csv  ← Southwest Norwegian
        ├── n_written_pronunciation_lexicon.csv   ← North Norwegian
        └── t_written_pronunciation_lexicon.csv   ← Trønder
```

## License

Both NB Uttale packages are published by the Norwegian Language Bank
(Nasjonalbiblioteket / Språkbanken) under **CC0 1.0 Universal** (public domain).
No restrictions on use.

Source: https://www.nb.no/sprakbanken/ressurskatalog/oai-nb-no-sbr-79/
