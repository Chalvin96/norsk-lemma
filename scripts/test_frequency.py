"""Tests for Kelly-list frequency ranking (ordbokene.frequency)."""

import json
from pathlib import Path

from ordbokene import frequency

REPO_ROOT = Path(__file__).resolve().parents[1]
KELLY_CSV = REPO_ROOT / "data" / "vendor" / "kelly" / "kelly.csv"


# ---------------------------------------------------------------------------
# POS map + index loading
# ---------------------------------------------------------------------------


def test_pos_map_covers_all_kelly_tags() -> None:
    assert frequency.POS_MAP["n"] == "NOUN"
    assert frequency.POS_MAP["v"] == "VERB"
    assert frequency.POS_MAP["conj"] == "CONJ"
    assert set(frequency.POS_MAP) == {"n", "v", "adj", "adv", "prep", "det", "interj", "conj", "pron"}


def test_load_kelly_index_known_ranks() -> None:
    index = frequency.load_kelly_index(KELLY_CSV)
    # være is rank 1 (verb); og rank 2 (conj); i rank 3 (prep).
    assert index[("være", "VERB")] == 1
    assert index[("og", "CONJ")] == 2
    assert index[("i", "PREP")] == 3


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------


def _resolved(index_rows: dict[tuple[str, str], int], lemma_keys):
    return frequency.resolve(index_rows, lemma_keys)


def test_exact_match_sets_rank() -> None:
    index = {("hus", "NOUN"): 42}
    resolved = _resolved(index, [("hus", "NOUN")])
    rank, ambiguous = resolved.lookup("hus", "NOUN")
    assert rank == 42
    assert ambiguous is False


def test_case_insensitive_fallback() -> None:
    index = {("oslo", "NOUN"): 10}
    resolved = _resolved(index, [("Oslo", "NOUN")])
    rank, _ = resolved.lookup("Oslo", "NOUN")
    assert rank == 10


def test_unmatched_returns_none() -> None:
    index = {("hus", "NOUN"): 42}
    resolved = _resolved(index, [("hus", "NOUN")])
    rank, ambiguous = resolved.lookup("xyzzy", "NOUN")
    assert rank is None
    assert ambiguous is False


def test_pos_mismatch_does_not_match() -> None:
    index = {("lys", "NOUN"): 5}
    resolved = _resolved(index, [("lys", "ADJ")])
    rank, _ = resolved.lookup("lys", "ADJ")
    assert rank is None


def test_homographs_all_ranked_and_flagged() -> None:
    # Two lexicon lemmas share (ball, NOUN) -> both get rank 7, both ambiguous.
    index = {("ball", "NOUN"): 7}
    resolved = _resolved(index, [("ball", "NOUN"), ("ball", "NOUN")])
    rank, ambiguous = resolved.lookup("ball", "NOUN")
    assert rank == 7
    assert ambiguous is True


def test_single_match_not_ambiguous() -> None:
    index = {("ball", "NOUN"): 7}
    resolved = _resolved(index, [("ball", "NOUN")])
    _, ambiguous = resolved.lookup("ball", "NOUN")
    assert ambiguous is False


# ---------------------------------------------------------------------------
# annotate_lemma — schema shape
# ---------------------------------------------------------------------------


def test_annotate_sets_explicit_null_when_unmatched() -> None:
    resolved = _resolved({("hus", "NOUN"): 1}, [("hus", "NOUN")])
    lemma = {"lemma": "ukjent", "pos": "NOUN"}
    frequency.annotate_lemma(lemma, resolved)
    assert lemma["frequency_rank"] is None
    assert "frequency_ambiguous" not in lemma


def test_annotate_sets_rank_and_flag() -> None:
    resolved = _resolved({("ball", "NOUN"): 7}, [("ball", "NOUN"), ("ball", "NOUN")])
    lemma = {"lemma": "ball", "pos": "NOUN"}
    frequency.annotate_lemma(lemma, resolved)
    assert lemma["frequency_rank"] == 7
    assert lemma["frequency_ambiguous"] is True


def test_annotate_is_idempotent_and_clears_stale_flag() -> None:
    resolved = _resolved({("hus", "NOUN"): 3}, [("hus", "NOUN")])
    lemma = {"lemma": "hus", "pos": "NOUN", "frequency_ambiguous": True}
    frequency.annotate_lemma(lemma, resolved)
    frequency.annotate_lemma(lemma, resolved)
    assert lemma["frequency_rank"] == 3
    assert "frequency_ambiguous" not in lemma  # stale True cleared, single match


# ---------------------------------------------------------------------------
# run — end to end over a tiny lemma dir
# ---------------------------------------------------------------------------


def _write_lemma_file(lemma_dir: Path, article_id: int, lemmas: list[dict]) -> None:
    lemma_dir.mkdir(parents=True, exist_ok=True)
    (lemma_dir / f"{article_id}.json").write_text(
        json.dumps({"source_article_id": article_id, "lemmas": lemmas}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_run_annotates_files_and_writes_report(tmp_path: Path) -> None:
    csv_path = tmp_path / "kelly.csv"
    csv_path.write_text(
        "rank,lemma,pos_kelly,pos_ud,english\n"
        "1,være,v,VERB,be\n"
        "2,hus,n,NOUN,house\n",
        encoding="utf-8",
    )
    lemma_dir = tmp_path / "lemma"
    _write_lemma_file(lemma_dir, 1, [{"lemma": "være", "pos": "VERB"}])
    _write_lemma_file(lemma_dir, 2, [{"lemma": "hus", "pos": "NOUN"}, {"lemma": "ukjent", "pos": "NOUN"}])
    report_path = tmp_path / "match-report.json"

    report = frequency.run(lemma_dir, csv_path, report_path)

    a1 = json.loads((lemma_dir / "1.json").read_text(encoding="utf-8"))
    a2 = json.loads((lemma_dir / "2.json").read_text(encoding="utf-8"))
    assert a1["lemmas"][0]["frequency_rank"] == 1
    assert a2["lemmas"][0]["frequency_rank"] == 2
    assert a2["lemmas"][1]["frequency_rank"] is None
    assert report["kelly_entries"] == 2
    assert report["matched_keys"] == 2
    assert report["lemmas_ranked"] == 2
    assert json.loads(report_path.read_text(encoding="utf-8"))["matched_keys"] == 2


def test_run_dry_run_does_not_write(tmp_path: Path) -> None:
    csv_path = tmp_path / "kelly.csv"
    csv_path.write_text("rank,lemma,pos_kelly,pos_ud,english\n1,hus,n,NOUN,house\n", encoding="utf-8")
    lemma_dir = tmp_path / "lemma"
    _write_lemma_file(lemma_dir, 1, [{"lemma": "hus", "pos": "NOUN"}])

    frequency.run(lemma_dir, csv_path, dry_run=True)

    data = json.loads((lemma_dir / "1.json").read_text(encoding="utf-8"))
    assert "frequency_rank" not in data["lemmas"][0]


# ---------------------------------------------------------------------------
# Coverage guard against the real vendored data + committed export.
# ---------------------------------------------------------------------------


def test_vendored_csv_has_expected_shape() -> None:
    index = frequency.load_kelly_index(KELLY_CSV)
    # 6000 Kelly rows minus one null-lemma row; every entry maps to a UD POS.
    assert 5900 <= len(index) <= 6000


def test_null_lemma_value_does_not_crash() -> None:
    # Explicit JSON null (not a missing key) must not crash the run.
    resolved = _resolved({("hus", "NOUN"): 1}, [(None, "NOUN"), ("hus", None)])
    lemma = {"lemma": None, "pos": "NOUN"}
    frequency.annotate_lemma(lemma, resolved)
    assert lemma["frequency_rank"] is None
    assert "frequency_ambiguous" not in lemma


def test_case_insensitive_collapse_counts_as_ambiguous() -> None:
    # "Gud" and "gud" both resolve to Kelly ("gud", NOUN); both flagged ambiguous.
    index = {("gud", "NOUN"): 500}
    resolved = _resolved(index, [("Gud", "NOUN"), ("gud", "NOUN")])
    for lemma_text in ("Gud", "gud"):
        rank, ambiguous = resolved.lookup(lemma_text, "NOUN")
        assert rank == 500
        assert ambiguous is True


def test_report_includes_n_lemmas_for_ambiguous() -> None:
    index = {("ball", "NOUN"): 7}
    resolved = _resolved(index, [("ball", "NOUN"), ("ball", "NOUN"), ("ball", "NOUN")])
    report = frequency.build_report(index, resolved, lemmas_ranked=3)
    assert report["ambiguous_samples"][0] == {
        "lemma": "ball",
        "pos": "NOUN",
        "rank": 7,
        "n_lemmas": 3,
    }


def test_convert_raises_on_unmapped_pos(tmp_path: Path) -> None:
    import pytest

    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        pytest.skip("pandas not available")
    xls = tmp_path / "bad.xls"
    pd.DataFrame(
        {"Norwegian": ["foo"], "POS": ["bogus"], "English": ["foo"]}
    ).to_excel(xls, sheet_name="Ark1", index=False)
    with pytest.raises(ValueError, match="unmapped Kelly POS"):
        frequency.convert_xls_to_csv(xls, tmp_path / "out.csv")


def test_cli_has_frequency_subcommand() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(["frequency", "--dry-run"])
    assert args.command == "frequency"
    assert args.dry_run is True
    assert args.kelly_csv.name == "kelly.csv"
