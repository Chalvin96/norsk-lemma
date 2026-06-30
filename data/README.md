# Data Directory

This directory holds source pronunciation data required by `scripts/enrich_pronunciation.py`.
**Nothing here is committed to git** — download and place files manually.

## Also required: Ordbøkene articles

The enrichment pipeline reads and writes `data/articles/` in-place (pronunciation is added
directly to each article JSON). If the directory is empty, run `uv run python scripts/translate.py`
first — it downloads and flattens the Ordbøkene archive automatically. See `scripts/README.md` for details.

## Required files

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
