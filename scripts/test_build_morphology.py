from ordbokene.build import _source_morphology_tags


def test_source_morphology_tags_given_ud_pos_tags_expect_dropped() -> None:
    cleaned = _source_morphology_tags(["ADP", "CCONJ", "SCONJ", "INTJ", "PROPN", "SYM", "Sing", "Ind"])
    for leaked in ("ADP", "CCONJ", "SCONJ", "INTJ", "PROPN", "SYM"):
        assert leaked not in cleaned
    assert "Sing" in cleaned
    assert "Ind" in cleaned


def test_source_morphology_tags_given_sym_without_alias_expect_dropped() -> None:
    assert _source_morphology_tags(["SYM"]) == []
    assert _source_morphology_tags(["X", "AUX", "PART", "PUNCT"]) == []


def test_source_morphology_tags_given_canonical_pos_expect_dropped() -> None:
    assert _source_morphology_tags(["NOUN", "PREP", "Masc", "Sing"]) == ["Masc", "Sing"]


def test_source_morphology_tags_given_gender_variants_expect_normalized() -> None:
    assert _source_morphology_tags(["nøyt", "ADP"]) == ["Neuter"]
    assert _source_morphology_tags(["mask", "Fem"]) == ["Masc", "Fem"]


def test_source_morphology_tags_given_verb_echo_expect_left_intact() -> None:
    assert _source_morphology_tags(["verb", "Inf"]) == ["verb", "Inf"]
    assert _source_morphology_tags(["Adj", "Pos"]) == ["Adj", "Pos"]
