#!/usr/bin/env python3
"""Enrich articles with IPA pronunciation and pitch accent."""

import argparse
import csv
import dataclasses
import json
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import nb_g2p

# ---------------------------------------------------------------------------
# NoFAbet → IPA
# ---------------------------------------------------------------------------
# Based on the X-SAMPA–IPA–NoFAbet equivalence table published by
# Nasjonalbiblioteket (National Library of Norway):
# https://github.com/NationalLibraryOfNorway/sprakbanken-nb-g2p#transcription-standard
# NoFAbet was designed by Nate Young for NB's NoFA forced aligner.
# Stress/tone numbers: 0 = unstressed, 1 = tone 1, 2 = tone 2, 3 = secondary stress.

NOFABET_TO_IPA_MAP = {
    "S": "s", "H": "h", "J": "j",
    "V": "ʋ", "W": "w",           # V = ʋ (labio-dental approximant)
    "NG": "ŋ",
    "K": "k", "P": "p", "T": "t",
    "B": "b", "D": "d", "G": "g",
    "RD": "ɖ", "RT": "ʈ",
    "F": "f", "SJ": "ʃ", "KJ": "ç", "RS": "ʂ",
    "L": "l", "R": "r", "RL": "ɭ",
    "M": "m", "N": "n", "RN": "ɳ",
    "AA": "ɑː", "AE": "æː", "EE": "eː", "II": "iː",   # II = iː (not ɪː)
    "OO": "uː", "OA": "oː", "OE": "øː", "UU": "ʉː", "YY": "yː",
    "AEH": "æ", "AH": "ɑ", "AX": "ə", "EH": "ɛ",
    "IH": "ɪ", "OAH": "ɔ", "OEH": "œ", "OH": "u", "UH": "ʉ", "YH": "ʏ",  # OH = u (not ʊ)
    "AEJ": "æ͡ɪ", "AEW": "æ͡ʉ", "AJ": "ɑ͡ɪ",
    "OEJ": "œ͡ʏ", "OAJ": "ɔ͡ʏ", "OJ": "ɔ͡ʏ", "OU": "o͡ʊ",
    "LX": "l̩", "MX": "m̩", "NX": "n̩", "RLX": "ɭ̩", "RNX": "ɳ̩", "RX": "r̩", "SX": "s̩",
    "0": "", "1": "ˈ", "2": "ˈ", "3": "ˌ",
    "_": " ", "$": ".",
}

_NUC_RE = re.compile(r"^([A-Z]+)([0-3])$")
_TONE_RE = re.compile(r"[A-Z]+([12])")


def tone_label(tone: int | None) -> str | None:
    if tone == 1:
        return "Accent 1"
    if tone == 2:
        return "Accent 2"
    return None


def tone_status(source: str | None, tone: int | None) -> str:
    if tone in (1, 2):
        return "known"
    if source in {"nb_uttale", "nb_uttale_newwords"}:
        return "none"
    return "unknown"


def nofabet_to_ipa(transcription: str) -> str:
    tokens = transcription.split()
    parts = []
    for tok in tokens:
        m = _NUC_RE.match(tok)
        if m:
            stress = NOFABET_TO_IPA_MAP.get(m.group(2), "")
            vowel = NOFABET_TO_IPA_MAP.get(m.group(1), tok)
            parts.append(stress + vowel)
        else:
            parts.append(NOFABET_TO_IPA_MAP.get(tok, tok))
    return "".join(parts)


def extract_tone(nofabet: str) -> int | None:
    m = _TONE_RE.search(nofabet)
    return int(m.group(1)) if m else None


def normalize_leksika_ipa(raw: str) -> str:
    # ' = tone-1 stress, " = tone-2 stress in NB Uttale house convention — both → ˈ
    # tone is stored separately in the `tone` integer field
    return raw.replace("'", "ˈ").replace('"', "ˈ").replace(".", "")


# ---------------------------------------------------------------------------
# POS mapping
# ---------------------------------------------------------------------------

POS_MAP = {
    "NOUN": "NN", "ADJ": "JJ", "VERB": "VB",
    "ADV": "AB", "PRON": "PN", "ADP": "PP",
    "NUM": "RG", "INTJ": "IN",
    "CCONJ": "KN", "SCONJ": "KN",
}

# ---------------------------------------------------------------------------
# PronEntry
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class PronEntry:
    sampa: str | None
    ipa: str
    tone: int | None
    source: str
    prosody_trusted: bool | None = None
    needs_review: bool | None = None

    def to_dict(self) -> dict:
        d = {"sampa": self.sampa, "ipa": self.ipa, "tone": self.tone, "source": self.source}
        d["tone_status"] = tone_status(self.source, self.tone)
        label = tone_label(self.tone)
        if label is not None:
            d["tone_label"] = label
        if self.prosody_trusted is not None:
            d["prosody_trusted"] = self.prosody_trusted
        if self.needs_review is not None:
            d["needs_review"] = self.needs_review
        return d


# ---------------------------------------------------------------------------
# Global indexes (populated at startup, read-only during processing)
# ---------------------------------------------------------------------------

primary_index: dict[tuple[str, str], list[PronEntry]] = {}
fallback_index: dict[str, list[PronEntry]] = {}
newwords_index: dict[tuple[str, str], list[PronEntry]] = {}
newwords_fallback_index: dict[str, list[PronEntry]] = {}
# Phonetisaurus (used by nb-g2p) is not thread-safe; single lock for all G2P calls.
_g2p_lock = threading.Lock()


def load_leksika(path: Path) -> None:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if len(row) < 8:
                continue
            wordform, pos = row[0], row[1]
            nofabet, ipa_raw, sampa = row[5], row[6], row[7]
            if not wordform or not ipa_raw:
                continue
            tone = extract_tone(nofabet)
            ipa = normalize_leksika_ipa(ipa_raw)
            entry = PronEntry(sampa=sampa or None, ipa=ipa, tone=tone, source="nb_uttale")
            key = (wordform.lower(), pos)
            primary_index.setdefault(key, []).append(entry)
            fallback_index.setdefault(wordform.lower(), []).append(entry)


def _load_nb_convert(newwords_path: Path):
    """Dynamically import NB's authoritative convert_nofabet from the tillegg dir."""
    import importlib.util
    conv_path = newwords_path.parent / "conversion.py"
    if not conv_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("nb_uttale_conversion", conv_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return getattr(mod, "convert_nofabet", None)


def load_newwords(path: Path) -> None:
    nb_convert = _load_nb_convert(path)
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if len(row) < 3:
                continue
            token, transcription, pos = row[0], row[1], row[2]
            if not token or not transcription:
                continue
            try:
                tone = extract_tone(transcription)
                if nb_convert is not None:
                    ipa = normalize_leksika_ipa(nb_convert(transcription, to="ipa"))
                else:
                    ipa = nofabet_to_ipa(transcription)
            except Exception:
                continue
            entry = PronEntry(sampa=None, ipa=ipa, tone=tone, source="nb_uttale_newwords")
            key = (token.lower(), pos)
            newwords_index.setdefault(key, []).append(entry)
            newwords_fallback_index.setdefault(token.lower(), []).append(entry)


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

def _lookup(wf_lower: str, nst_pos: str | None) -> PronEntry | None:
    key = (wf_lower, nst_pos)
    if key in primary_index:
        return primary_index[key][0]
    if wf_lower in fallback_index:
        return fallback_index[wf_lower][0]
    return None


def resolve_lexical(wordform: str, nst_pos: str | None) -> PronEntry | None:
    wf_lower = wordform.lower()

    # a. NB Uttale leksika — manually curated, most accurate.
    entry = _lookup(wf_lower, nst_pos)
    if entry:
        return entry

    # b. NB Uttale newwords — lexical data, prefer over model inference.
    key = (wf_lower, nst_pos)
    if key in newwords_index:
        return newwords_index[key][0]
    if wf_lower in newwords_fallback_index:
        return newwords_fallback_index[wf_lower][0]

    return None


def resolve(wordform: str, nst_pos: str | None) -> PronEntry | None:
    entry = resolve_lexical(wordform, nst_pos)
    if entry:
        return entry

    if not can_g2p_wordform(wordform):
        return None

    return g2p_entries([wordform]).get(wordform)


def g2p_entries(wordforms: list[str]) -> dict[str, PronEntry]:
    if not wordforms:
        return {}

    # c. nb-g2p — NB's own G2P model trained on NB Uttale. Handles Norwegian
    #    morphology (inflection, compounds, tone) without requiring a separate
    #    morphological service.
    #    Source: https://github.com/NationalLibraryOfNorway/sprakbanken-nb-g2p
    unique_wordforms = list(dict.fromkeys(wordforms))
    entries: dict[str, PronEntry] = {}
    try:
        with _g2p_lock:
            results = list(nb_g2p.transcribe_words(unique_wordforms))
        for input_word, (_result_word, nofabet_tokens) in zip(unique_wordforms, results, strict=False):
            nofabet = " ".join(nofabet_tokens)
            tone = extract_tone(nofabet)
            ipa = nofabet_to_ipa(nofabet)
            if ipa:
                entries[input_word] = PronEntry(
                    sampa=None,
                    ipa=ipa,
                    tone=tone,
                    source="nb_g2p",
                    prosody_trusted=False,
                    needs_review=True,
                )
    except Exception:
        return {}

    return entries


def can_g2p_wordform(wordform: str) -> bool:
    return not re.search(r"\s|[\[\]|]", wordform)


# ---------------------------------------------------------------------------
# Lemma helpers
# ---------------------------------------------------------------------------

def get_lemma_pos(lemma: dict) -> str | None:
    for pi in lemma.get("paradigm_info", []):
        for tag in pi.get("tags", []):
            if tag in POS_MAP:
                return tag
    return None


def get_base_form_pronunciation(lemma: dict) -> list:
    pos_tag = get_lemma_pos(lemma)
    for pi in lemma.get("paradigm_info", []):
        for inf in pi.get("inflection", []):
            tags = set(inf.get("tags", []))
            pron = inf.get("pronunciation", [])
            if not pron:
                continue
            if pos_tag == "NOUN" and "Sing" in tags and "Ind" in tags:
                return pron
            if pos_tag == "VERB" and tags == {"Inf"}:
                return pron
            if pos_tag == "ADJ" and {"Pos", "Sing", "Ind", "Masc/Fem"} <= tags:
                return pron
    # fallback: first inflection with pronunciation
    for pi in lemma.get("paradigm_info", []):
        for inf in pi.get("inflection", []):
            pron = inf.get("pronunciation", [])
            if pron:
                return pron
    return []


def all_non_null_enriched(data: dict) -> bool:
    for lemma in data.get("lemmas", []):
        for pi in lemma.get("paradigm_info", []):
            for inf in pi.get("inflection", []):
                if inf.get("word_form") is not None and not inf.get("pronunciation"):
                    return False
    return True


def add_missing_tone_metadata(data: dict) -> bool:
    changed = False
    for lemma in data.get("lemmas", []):
        for pron in lemma.get("pronunciation") or []:
            changed = _add_tone_metadata(pron) or changed
        for pi in lemma.get("paradigm_info", []):
            for inf in pi.get("inflection", []):
                for pron in inf.get("pronunciation") or []:
                    changed = _add_tone_metadata(pron) or changed
    return changed


def _add_tone_metadata(pron: dict) -> bool:
    if not isinstance(pron, dict):
        return False
    changed = False
    if not pron.get("tone_status"):
        pron["tone_status"] = tone_status(pron.get("source"), pron.get("tone"))
        changed = True
    if not pron.get("tone_label"):
        label = tone_label(pron.get("tone"))
        if label is not None:
            pron["tone_label"] = label
            changed = True
    return changed


# ---------------------------------------------------------------------------
# Per-article processing
# ---------------------------------------------------------------------------

def enrich_article(article_path: Path, force: bool) -> Counter:
    try:
        data = json.loads(article_path.read_text(encoding="utf-8"))
    except Exception:
        return Counter()
    schema_changed = add_missing_tone_metadata(data)
    if not force and all_non_null_enriched(data):
        if schema_changed:
            write_article_data(article_path, data)
        return Counter()
    counts: Counter = Counter()
    pending_g2p: list[tuple[dict, str]] = []

    for lemma in data.get("lemmas", []):
        pos_tag = get_lemma_pos(lemma)
        nst_pos = POS_MAP.get(pos_tag) if pos_tag else None

        for pi in lemma.get("paradigm_info", []):
            for inf in pi.get("inflection", []):
                wf = inf.get("word_form")
                if wf is None:
                    continue
                if not force and inf.get("pronunciation"):
                    continue
                entry = resolve_lexical(wf, nst_pos)
                if entry is None and can_g2p_wordform(wf):
                    pending_g2p.append((inf, wf))
                    continue
                inf["pronunciation"] = [entry.to_dict()] if entry else []
                counts[entry.source if entry else "null"] += 1

    g2p_by_wordform = g2p_entries([wf for _, wf in pending_g2p])
    for inf, wf in pending_g2p:
        entry = g2p_by_wordform.get(wf)
        inf["pronunciation"] = [entry.to_dict()] if entry else []
        counts[entry.source if entry else "null"] += 1

    for lemma in data.get("lemmas", []):
        lemma["pronunciation"] = get_base_form_pronunciation(lemma)

    write_article_data(article_path, data)
    return counts


def write_article_data(article_path: Path, data: dict) -> None:
    tmp = article_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(article_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich lexicon articles with pronunciation.")
    parser.add_argument("--input", default="data/articles")
    parser.add_argument("--leksika", default="data/nb_uttale_leksika/nb_uttale_leksika/e_written_pronunciation_lexicon.csv")
    parser.add_argument("--newwords", default="data/nb_uttale_tillegg/nb_uttale_tillegg/newwords_2022.csv")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    input_dir = Path(args.input)

    leksika_path = Path(args.leksika)
    newwords_path = Path(args.newwords)

    if not leksika_path.exists():
        sys.exit(f"leksika not found: {leksika_path}\nSee data/README.md for download instructions.")
    if not newwords_path.exists():
        sys.exit(f"newwords not found: {newwords_path}\nSee data/README.md for download instructions.")

    print("Loading leksika … ", end="", flush=True)
    load_leksika(leksika_path)
    print(f"{len(primary_index):,} entries")

    print("Loading newwords … ", end="", flush=True)
    load_newwords(newwords_path)
    print(f"{len(newwords_index):,} entries")

    articles = sorted(input_dir.glob("*.json"))
    if args.limit:
        articles = articles[: args.limit]

    total_counts: Counter = Counter()
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(enrich_article, p, args.force): p
            for p in articles
        }
        for fut in as_completed(futures):
            try:
                total_counts += fut.result()
            except Exception as exc:
                print(f"\nERROR {futures[fut].name}: {exc}", file=sys.stderr)
            done += 1
            if done % 1000 == 0 or done == len(articles):
                print(f"\r  {done}/{len(articles)} articles", end="", flush=True)

    print(f"\r  {done}/{len(articles)} articles — done")

    total = sum(total_counts.values())
    print("\nCoverage report:")
    for source in ["nb_uttale", "nb_uttale_newwords", "nb_g2p", "null"]:
        n = total_counts.get(source, 0)
        pct = 100 * n / total if total else 0
        print(f"  {source:<24} {n:>8,}  ({pct:.1f}%)")
    print(f"  {'total':<24} {total:>8,}")


if __name__ == "__main__":
    main()
