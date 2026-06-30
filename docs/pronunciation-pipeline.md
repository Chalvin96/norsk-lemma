# Pronunciation Enrichment Pipeline

Adds IPA pronunciation and pitch accent to every inflected wordform in the lexicon.
Output is written back to `data/articles/` in-place.

## Why we do it this way

### Why NB Uttale (not generated IPA)

Norwegian has ~785,000 wordforms, many with irregular pronunciation, loanwords, and
pitch accent distinctions that no rule-based G2P system handles correctly. NB Uttale
is a hand-curated, corpus-validated lexicon produced by the Norwegian Language Bank —
the authoritative source. We use it as the primary lookup rather than generating
pronunciation from rules, because correctness matters more than coverage here.

When lexical resources miss a wordform, the pipeline falls back to `nb-g2p`, NB's own
G2P model trained on NB Uttale. Those predictions are still marked for review, but the
fallback is materially better aligned with the lexicon than the old hand-written suffix
rules and does not require a separate morphology service.

### Why pitch accent is a separate integer field

Norwegian minimal pairs (`bønder`/`bønner`, `lyset`/`lyset`) are distinguished only by
pitch accent. Tone 1 vs Tone 2 is stored as `"tone": 1` or `"tone": 2` rather than
embedded as a diacritic in the IPA string because:

1. No universal standard exists for Norwegian tone diacritics in IPA — sources use
   grave/circumflex, superscript ¹/², or tone letters. Keeping an integer lets the
   rendering layer choose (`¹bønder` for learners, tone letters for phonologists).
2. TTS engines assign tonelag from their own dictionaries — injecting IPA tone
   diacritics via SSML phoneme input does not make a reliable dictionary entry.

### Why we enrich at wordform level (not just lemma)

Norwegian inflection changes pronunciation. `hund` (tone 1) but `hunden` (tone 2).
`bonde` and `bønder` have different vowels entirely. Storing pronunciation only on the
lemma would give learners wrong audio for inflected forms they actually encounter in
sentences. Every inflection entry gets its own lookup.

### Why nb-g2p fallback (not OBT + suffix rules)

A suffix list (`-en`, `-et`, `-er`, `-ene` …) is fragile — `-en` is sometimes part of
the root, Norwegian has irregular inflection, and the list requires ongoing maintenance.

The previous pipeline used OBT to confirm lemmas before applying a small suffix table.
That was safer than blind suffix stripping, but it still missed many productive forms
and required Docker infrastructure just to unlock a narrow derivation path.

`nb-g2p` replaces both pieces. It is NB's own model, trained on NB Uttale, and can
transcribe inflected forms and compounds directly. Because the output is model-based
rather than lexicon-backed, those entries are still flagged with
`prosody_trusted=false` and `needs_review=true`.

---

## Pipeline steps

```
data/articles/{id}.json
    │
    ▼
1. BUILD LOOKUP INDEXES
   ├─ NB Uttale leksika  (data/nb_uttale_leksika/nb_uttale_leksika/e_written_pronunciation_lexicon.csv)
   │    708k entries · IPA pre-computed · tone from NoFAbet digit
   │    → primary_index[(wordform.lower(), nst_pos)]  and  fallback_index[wordform.lower()]
   │
   └─ NB Uttale newwords  (data/nb_uttale_tillegg/nb_uttale_tillegg/newwords_2022.csv)
        25.5k entries · NoFAbet → IPA converted on load (inline NOFABET_TO_IPA_MAP)
        → newwords_index[(token.lower(), nst_pos)]
    │
    ▼
2. FOR EACH ARTICLE → LEMMA → INFLECTION WORDFORM:

   a. DIRECT LOOKUP
      (wordform.lower(), nst_pos) in primary_index  → source="nb_uttale"
      wordform.lower() in fallback_index (POS miss)  → source="nb_uttale"

   b. NEWWORDS LOOKUP
      (wordform.lower(), nst_pos) in newwords_index
      → source="nb_uttale_newwords"

   c. NB G2P FALLBACK
      Only used for single-token wordforms. Multiword expressions and
      Ordbokene bracket alternatives stay unresolved rather than emitting
      misleading model IPA.
      nb_g2p.transcribe_words([...wordforms])  → NoFAbet tokens
      NoFAbet → IPA via inline conversion map
      tone extracted from the first tone-bearing vowel token
      → source="nb_g2p", prosody_trusted=false, needs_review=true

      tone_status distinguishes missing tone values:
      · known   → tone is 1 or 2
      · none    → trusted NB data has no tone 1/2 marker
      · unknown → model fallback did not provide tone 1/2

   null  →  wordform left unenriched
    │
    ▼
3. LEMMA-LEVEL SHORTCUT
   Copy pronunciation from the canonical inflection up to lemmas[].pronunciation.
   · Noun  → Sing + Ind inflection
   · Verb  → exact tag set {"Inf"}
   · Adj   → Pos + Sing + Ind + Masc/Fem inflection
   · Other → first inflection with pronunciation
    │
    ▼
4. ATOMIC WRITE  data/articles/{id}.json  (in-place)
   Write to .tmp then rename — safe for concurrent runs and Ctrl+C.

5. COVERAGE REPORT
   Per-source counts + percentages printed at end of run.
```

## IPA format

IPA comes directly from NB Uttale's pre-computed `ipa_transcription` field.
`'` and `"` in that field are NB's house convention for tone-1 and tone-2 stress —
both are normalised to the standard IPA primary stress mark `ˈ`.

Tone is stored as a separate integer (1 or 2) extracted from the NoFAbet field digit
(`OEH1` = tone 1, `OEH2` = tone 2). It is NOT embedded as a diacritic in the IPA
string. See schema.md for display conventions.

## POS mapping

NB Uttale uses NST POS codes; Ordbøkene uses its own tag set:

| NST code | Ordbøkene tag |
|----------|---------------|
| NN       | NOUN          |
| JJ       | ADJ           |
| VB       | VERB          |
| AB       | ADV           |
| PN       | PRON          |
| PP       | ADP           |
| RG       | NUM           |
| IN       | INTJ          |
| KN       | CCONJ / SCONJ |

## Running the pipeline

**Prerequisites:**
- Source data in `data/` — see `data/README.md` for download instructions
- Project dependencies installed with `uv sync`

**Run enrichment:**

```bash
uv run python scripts/enrich_pronunciation.py \
  --input    data/articles/ \
  --workers  8
```

Use `--force` to re-enrich already-processed articles. Use `--limit N` for a test run.

## Expected coverage

| Stage              | Approx. wordforms |
|--------------------|-------------------|
| Direct leksika     | measured at run time |
| NB Uttale newwords | measured at run time |
| nb-g2p fallback    | measured at run time |
| null / unresolved  | measured at run time |

Exact figures are printed in the coverage report at the end of each run.
