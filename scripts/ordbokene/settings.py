from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_ARTICLES_DIR = REPO_ROOT / "data" / "articles"
DEFAULT_LEMMA_DIR = REPO_ROOT / "data" / "export" / "lemma"
DEFAULT_AUDIO_DIR = REPO_ROOT / "data" / "export" / "audio"
DEFAULT_KELLY_CSV = REPO_ROOT / "data" / "vendor" / "kelly" / "kelly.csv"
DEFAULT_KELLY_REPORT = REPO_ROOT / "data" / "vendor" / "kelly" / "match-report.json"
DEFAULT_ERROR_LOG = REPO_ROOT / "error_openrouter.log"
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_REVIEW_MODEL = "openai/gpt-5.4"
DEFAULT_BATCH_SIZE = 10
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 60
REQUEST_TIMEOUT = 60
CLI_TIMEOUT = 600  # agentic CLI harnesses need a longer wall-clock budget than HTTP
DEFAULT_HARNESS = "openrouter"
HARNESS_CHOICES = ("openrouter", "codex", "claude", "opencode", "droid", "pi")

# Maximum number of example sentences shown per definition in both the prompt
# and the emitted lemma JSON. Single source of truth so the prompt cap and the
# output cap stay aligned.
MAX_EXAMPLES = 4

logger = logging.getLogger(__name__)
