from __future__ import annotations

import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

ABBREVIATIONS_PATH = _REPO_ROOT / "scripts" / "ordbokene_concepts_bm.json"

KNOWN_POS = frozenset(
    {"NOUN", "VERB", "ADJ", "ADV", "PREP", "PRON", "CONJ", "INTERJ", "DET", "NUM"}
)

UD_TAG_ALIASES: dict[str, str] = {
    "CCONJ": "CONJ",
    "SCONJ": "CONJ",
    "ADP": "PREP",
    "INTJ": "INTERJ",
    "PROPN": "NOUN",
}

UD_UPOS: frozenset[str] = frozenset(
    {
        "ADJ", "ADP", "ADV", "AUX", "CCONJ", "DET", "INTJ", "NOUN",
        "NUM", "PART", "PRON", "PROPN", "PUNCT", "SCONJ", "SYM", "VERB", "X",
    }
)

CONTEXT_LABEL_ITEM_TYPES = frozenset(
    {"domain", "grammar", "rhetoric", "relation", "article_ref", "temporal"}
)

PLACEHOLDER_ONLY_RE = re.compile(r"^[\$: ]+$")
REDIRECT_RE = re.compile(r"^(se|sjå)(?:\s+også)?\s+", re.IGNORECASE)
SEE_ALSO_RE = re.compile(r"^\s*(se|sjå|jamfør)\b", re.IGNORECASE)


def load_abbreviations() -> dict[str, str]:
    if ABBREVIATIONS_PATH.exists():
        return json.loads(ABBREVIATIONS_PATH.read_text(encoding="utf-8"))
    return {}


ABBREVIATIONS: dict[str, str] = load_abbreviations()
