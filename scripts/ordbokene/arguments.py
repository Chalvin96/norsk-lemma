from __future__ import annotations

import argparse
from pathlib import Path

from .settings import DEFAULT_AUDIO_DIR, REPO_ROOT

K_DEFAULT_GOOGLE_VOICE = "nb-NO-Chirp3-HD-Aoede"
K_DEFAULT_LANGUAGE_CODE = "nb-NO"
K_DEFAULT_PRICE_PER_MILLION_CHARS = 30.0
K_DEFAULT_WORKERS = 8
K_LEKSIKA_PATH = (
    REPO_ROOT / "data/nb_uttale_leksika/nb_uttale_leksika/e_written_pronunciation_lexicon.csv"
)
K_NEWWORDS_PATH = REPO_ROOT / "data/nb_uttale_tillegg/nb_uttale_tillegg/newwords_2022.csv"


def add_audio_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--voice", default=K_DEFAULT_GOOGLE_VOICE)
    parser.add_argument("--language-code", default=K_DEFAULT_LANGUAGE_CODE)
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="Skip synthesis; embed audio metadata into articles/ from an existing manifest.",
    )
    parser.add_argument("--confirm-cost", action="store_true")
    parser.add_argument(
        "--price-per-million-chars", type=float, default=K_DEFAULT_PRICE_PER_MILLION_CHARS
    )
    parser.add_argument("--list-voices", action="store_true")
    parser.add_argument("--workers", type=int, default=K_DEFAULT_WORKERS)


def add_pronunciation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--leksika", default=str(K_LEKSIKA_PATH))
    parser.add_argument("--newwords", default=str(K_NEWWORDS_PATH))
    parser.add_argument("--workers", type=int, default=K_DEFAULT_WORKERS)
