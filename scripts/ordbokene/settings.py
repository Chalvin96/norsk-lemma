from __future__ import annotations

import json
import logging
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_ARTICLES_DIR = REPO_ROOT / "articles"
DEFAULT_LEMMA_DIR = REPO_ROOT / "lemma"
DEFAULT_ERROR_LOG = REPO_ROOT / "error_openrouter.log"
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_BATCH_SIZE = 10
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 60
REQUEST_TIMEOUT = 60

logger = logging.getLogger(__name__)

ABBREVIATIONS_PATH = REPO_ROOT / "scripts" / "ordbokene_concepts_bm.json"

# Full Ordbokene POS tag set. PREP/PRON/CONJ/INTERJ/DET/NUM were added to match
# the 10-value POS_MAP in the backend importer (ordbokene_importer.py). Articles
# with these tags now get a specific pos value instead of falling back to "unknown".
KNOWN_POS = frozenset(
    {"NOUN", "VERB", "ADJ", "ADV", "PREP", "PRON", "CONJ", "INTERJ", "DET", "NUM"}
)

CONTEXT_LABEL_ITEM_TYPES = frozenset(
    {"domain", "grammar", "rhetoric", "relation", "article_ref", "temporal"}
)

PLACEHOLDER_ONLY_RE = re.compile(r"^[\$: ]+$")
REDIRECT_RE = re.compile(r"^(se|sjå)(?:\s+også)?\s+", re.IGNORECASE)


def load_abbreviations() -> dict[str, str]:
    if ABBREVIATIONS_PATH.exists():
        return json.loads(ABBREVIATIONS_PATH.read_text(encoding="utf-8"))
    return {}


ABBREVIATIONS: dict[str, str] = load_abbreviations()
