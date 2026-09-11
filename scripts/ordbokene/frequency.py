"""Norwegian Kelly-list frequency ranking.

Joins the vendored Kelly list (``data/vendor/kelly/kelly.csv``) onto exported
lemmas by ``(lemma, pos)`` and writes a ``frequency_rank`` field.

The Kelly list ranks corpus counts of *surface forms*; it never distinguished
homographs. So when one Kelly ``(lemma, pos)`` matches more than one lexicon
lemma, every match receives the rank and is flagged ``frequency_ambiguous``
rather than picking a single winner by ``hgno`` (which is editorial ordering,
not frequency).

Source: Norwegian Kelly list, UiO Text Laboratory (tekstlab.uio.no/kelly),
CC BY-SA 4.0. See ``data/vendor/kelly/README.md``.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .io import write_text_atomically

logger = logging.getLogger(__name__)

# Kelly POS tag -> the lexicon's own POS tag (as emitted by build_lemma). These
# are UD-derived but not all standard UD (e.g. PREP/CONJ/INTERJ, not ADP/CCONJ/INTJ);
# they match the `pos` values in data/export/lemma/.
POS_MAP: dict[str, str] = {
    "n": "NOUN",
    "v": "VERB",
    "adj": "ADJ",
    "adv": "ADV",
    "prep": "PREP",
    "det": "DET",
    "interj": "INTERJ",
    "conj": "CONJ",
    "pron": "PRON",
}

# Join key: (lemma_text, ud_pos). Value on the resolved index is (rank, ambiguous).
KellyKey = tuple[str, str]


def load_kelly_index(csv_path: Path) -> dict[KellyKey, int]:
    """Read the vendored ``kelly.csv`` into ``(lemma, pos_ud) -> rank``.

    Lowest rank wins if a key ever repeats (it should not — Kelly has no
    duplicate lemmas — but the guard keeps the join deterministic).
    """
    index: dict[KellyKey, int] = {}
    skipped = 0
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            pos = (row.get("pos_ud") or "").strip()
            lemma = (row.get("lemma") or "").strip()
            if not pos or not lemma:
                skipped += 1
                continue
            rank = int(row["rank"])
            key = (lemma, pos)
            if key not in index or rank < index[key]:
                index[key] = rank
    if skipped:
        # Blank pos_ud means an unmapped Kelly POS (see POS_MAP) — visible, not silent,
        # so a shifted column or a new UiO tag can't drop ranks unnoticed.
        logger.warning("frequency — skipped %d Kelly rows with blank lemma/pos_ud", skipped)
    return index


def _ci_index(index: dict[KellyKey, int]) -> dict[KellyKey, tuple[int, KellyKey]]:
    """Case-insensitive fallback: ``(lower(lemma), pos) -> (rank, canonical_key)``."""
    ci: dict[KellyKey, tuple[int, KellyKey]] = {}
    for (lemma, pos), rank in index.items():
        ci_key = (lemma.lower(), pos)
        if ci_key not in ci or rank < ci[ci_key][0]:
            ci[ci_key] = (rank, (lemma, pos))
    return ci


def _match(
    lemma: str,
    pos: str,
    index: dict[KellyKey, int],
    ci: dict[KellyKey, tuple[int, KellyKey]],
) -> tuple[int, KellyKey] | None:
    """Return ``(rank, canonical_kelly_key)`` for a lexicon lemma, or ``None``.

    Exact ``(lemma, pos)`` first, then a case-insensitive fallback. The
    canonical key is Kelly's own spelling, so case variants of the same word
    collapse onto one key for ambiguity counting.

    Falsy lemma/pos (missing or explicit JSON null in a lemma object) never
    match — guarded so a single malformed record can't crash a full run.
    """
    if not lemma or not pos:
        return None
    exact = index.get((lemma, pos))
    if exact is not None:
        return exact, (lemma, pos)
    return ci.get((lemma.lower(), pos))


@dataclass
class Resolved:
    """The join result the annotators consume: rank + ambiguity per Kelly key."""

    rank_by_key: dict[KellyKey, int] = field(default_factory=dict)
    ambiguous_keys: set[KellyKey] = field(default_factory=set)
    counts: dict[KellyKey, int] = field(default_factory=dict)
    _index: dict[KellyKey, int] = field(default_factory=dict)
    _ci: dict[KellyKey, tuple[int, KellyKey]] = field(default_factory=dict)

    def lookup(self, lemma: str, pos: str) -> tuple[int | None, bool]:
        """``(rank_or_none, is_ambiguous)`` for one lexicon lemma."""
        matched = _match(lemma, pos, self._index, self._ci)
        if matched is None:
            return None, False
        rank, key = matched
        return rank, key in self.ambiguous_keys


def resolve(
    index: dict[KellyKey, int],
    lemma_keys: Iterable[tuple[str, str]],
) -> Resolved:
    """Build the resolved join from the Kelly index and every lexicon ``(lemma, pos)``.

    ``lemma_keys`` is one entry per lexicon lemma object (duplicates expected —
    that is exactly what makes a Kelly key ambiguous).
    """
    ci = _ci_index(index)
    counts: Counter[KellyKey] = Counter()
    for lemma, pos in lemma_keys:
        matched = _match(lemma, pos, index, ci)
        if matched is not None:
            counts[matched[1]] += 1
    ambiguous = {key for key, n in counts.items() if n > 1}
    rank_by_key = {key: index[key] for key in counts}
    return Resolved(
        rank_by_key=rank_by_key,
        ambiguous_keys=ambiguous,
        counts=dict(counts),
        _index=index,
        _ci=ci,
    )


def annotate_lemma(lemma: dict[str, Any], resolved: Resolved) -> None:
    """Set ``frequency_rank`` (int or explicit null) and ``frequency_ambiguous``.

    ``frequency_ambiguous`` is written only when true, to keep files lean;
    absent means false.
    """
    rank, ambiguous = resolved.lookup(lemma.get("lemma", ""), lemma.get("pos", ""))
    lemma["frequency_rank"] = rank
    if ambiguous:
        lemma["frequency_ambiguous"] = True
    else:
        lemma.pop("frequency_ambiguous", None)


# ---------------------------------------------------------------------------
# Stage: annotate the committed lemma export in place.
# ---------------------------------------------------------------------------


def _lemma_keys(files: list[Path]) -> Iterable[tuple[str, str]]:
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for lemma in data.get("lemmas", []):
            if isinstance(lemma, dict):
                yield lemma.get("lemma", ""), lemma.get("pos", "")


def build_report(
    index: dict[KellyKey, int], resolved: Resolved, lemmas_ranked: int
) -> dict[str, Any]:
    matched_keys = set(resolved.rank_by_key)
    unmatched = [
        {"rank": rank, "lemma": lemma, "pos": pos}
        for (lemma, pos), rank in sorted(index.items(), key=lambda kv: kv[1])
        if (lemma, pos) not in matched_keys
    ]
    ambiguous = [
        {
            "lemma": lemma,
            "pos": pos,
            "rank": resolved.rank_by_key[(lemma, pos)],
            "n_lemmas": resolved.counts[(lemma, pos)],
        }
        for (lemma, pos) in sorted(resolved.ambiguous_keys, key=lambda k: resolved.rank_by_key[k])
    ]
    return {
        "source": "Norwegian Kelly list, UiO Text Laboratory, CC BY-SA 4.0",
        "kelly_entries": len(index),
        "matched_keys": len(matched_keys),
        "unmatched_keys": len(unmatched),
        "ambiguous_keys": len(resolved.ambiguous_keys),
        "lemmas_ranked": lemmas_ranked,
        "unmatched_samples": unmatched[:50],
        "ambiguous_samples": ambiguous[:50],
    }


def run(
    lemma_dir: Path,
    csv_path: Path,
    report_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Annotate every lemma file under ``lemma_dir`` with ``frequency_rank``.

    Idempotent and deterministic: re-running reproduces the same fields. Returns
    the match report.
    """
    index = load_kelly_index(csv_path)
    files = sorted(lemma_dir.glob("*.json"))
    logger.info("frequency — %d Kelly entries, %d lemma files", len(index), len(files))

    resolved = resolve(index, _lemma_keys(files))

    lemmas_ranked = 0
    written = 0
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        lemmas = [lm for lm in data.get("lemmas", []) if isinstance(lm, dict)]
        for lemma in lemmas:
            annotate_lemma(lemma, resolved)
            if lemma.get("frequency_rank") is not None:
                lemmas_ranked += 1
        if not dry_run:
            write_text_atomically(
                path,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )
            written += 1

    report = build_report(index, resolved, lemmas_ranked)
    logger.info(
        "frequency — %d matched keys, %d unmatched, %d ambiguous, %d lemmas ranked",
        report["matched_keys"],
        report["unmatched_keys"],
        report["ambiguous_keys"],
        report["lemmas_ranked"],
    )
    if report_path is not None and not dry_run:
        write_text_atomically(
            report_path,
            json.dumps(report, ensure_ascii=False, indent=2),
        )
    return report


# ---------------------------------------------------------------------------
# Dev-only: regenerate kelly.csv from the vendored .xls (requires pandas).
# ---------------------------------------------------------------------------


def convert_xls_to_csv(xls_path: Path, csv_path: Path) -> int:
    """Regenerate ``kelly.csv`` from the UiO ``.xls``. One-time / auditability.

    rank = row order (1-based, gaps preserved where a row has no lemma).
    """
    import pandas as pd  # dev-only dependency

    frame = pd.ExcelFile(xls_path).parse("Ark1")
    frame.columns = ["no", "pos", "en"]
    rows: list[tuple[int, str, str, str, str]] = []
    for rank, (_, record) in enumerate(frame.iterrows(), start=1):
        word = record["no"]
        if not isinstance(word, str) or not word.strip():
            continue
        pos_kelly = str(record["pos"]).strip()
        if pos_kelly not in POS_MAP:
            # Fail loud rather than silently drop the rank — a new/typo POS or a
            # shifted column should stop the conversion, not vanish.
            raise ValueError(
                f"unmapped Kelly POS {pos_kelly!r} at row {rank} ({word!r}); update POS_MAP"
            )
        pos_ud = POS_MAP[pos_kelly]
        english = record["en"].strip() if isinstance(record["en"], str) else ""
        rows.append((rank, word.strip(), pos_kelly, pos_ud, english))
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "lemma", "pos_kelly", "pos_ud", "english"])
        writer.writerows(rows)
    return len(rows)
