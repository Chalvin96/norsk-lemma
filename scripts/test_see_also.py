"""Tests for see-also cross-reference extraction and build_lemma emission.

Covers the Repo A exporter changes from the see-also plan:
  - ``Se: X`` (capital + colon) alongside a real definition.
  - Multi-target ``Se: a, b``.
  - ``jamfør X`` → relation "compare".
  - Pure-redirect article → ``cross_reference`` set, ``definitions == []``.
  - Mixed article → ``cross_reference is None``, real definitions preserved.
  - Ref-less ``jamføre`` (the real verb) → not treated as see-also.
"""
import translate
from ordbokene.extract import extract_see_also

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _article_ref(article_id: int, lemma: str) -> dict:
    """Build a minimal article_ref item matching the Ordbokene raw shape."""
    return {
        "type_": "article_ref",
        "article_id": article_id,
        "lemmas": [{"lemma": lemma}],
    }


def _explanation(content: str, items: list[dict] | None = None) -> dict:
    """Build a minimal explanation element."""
    return {"type_": "explanation", "content": content, "items": items or []}


def _real_llm_result(source_id: int, translation: str) -> dict:
    """Build an LLM result dict with one definition translation."""
    return {
        "definitions": [{"source_id": source_id, "translation": translation}],
        "lemma_primary": "",
    }


# ---------------------------------------------------------------------------
# 1. Se: X (capital + colon) with a real definition
# ---------------------------------------------------------------------------

def test_se_colon_with_real_def_yields_definition_and_see_also() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "test", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("real sense"),
                        _explanation("Se: $", [_article_ref(99, "X")]),
                    ],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, _real_llm_result(2, "real translation"), 1)

    # Real definition preserved.
    assert len(lemma["definitions"]) == 1
    assert lemma["definitions"][0]["text"] == "real sense"
    assert lemma["definitions"][0]["translation"] == "real translation"

    # No blank-translation definition for the "Se: X" pointer.
    assert all(d["text"] != "Se: X" for d in lemma["definitions"])

    # see_also populated.
    assert lemma["see_also"] == [
        {"article_id": 99, "lemma": "X", "relation": "see"}
    ]


# ---------------------------------------------------------------------------
# 2. Se: a, b (two article_ref items)
# ---------------------------------------------------------------------------

def test_multi_target_see_also_preserves_order() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "test", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("real sense"),
                        _explanation(
                            "Se: $, $",
                            [_article_ref(10, "a"), _article_ref(20, "b")],
                        ),
                    ],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, _real_llm_result(2, "real translation"), 1)

    assert lemma["see_also"] == [
        {"article_id": 10, "lemma": "a", "relation": "see"},
        {"article_id": 20, "lemma": "b", "relation": "see"},
    ]
    assert len(lemma["definitions"]) == 1


# ---------------------------------------------------------------------------
# 3. jamfør X → relation "compare"
# ---------------------------------------------------------------------------

def test_jamfor_yields_compare_relation() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "test", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("real sense"),
                        _explanation("jamfør $", [_article_ref(50, "Y")]),
                    ],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, _real_llm_result(2, "real translation"), 1)

    assert lemma["see_also"] == [
        {"article_id": 50, "lemma": "Y", "relation": "compare"}
    ]
    assert len(lemma["definitions"]) == 1


# ---------------------------------------------------------------------------
# 4. Pure-redirect article → cross_reference set, definitions == []
# ---------------------------------------------------------------------------

def test_pure_redirect_has_cross_reference_and_no_definitions() -> None:
    raw = {
        "article_id": 99999,
        "lemmas": [{"id": 1, "lemma": "praktildkvede", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("se $", [_article_ref(25773, "ildkvede")]),
                    ],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, "error", 99999)

    assert lemma["cross_reference"] == {
        "article_id": 25773,
        "lemma": "ildkvede",
    }
    assert lemma["definitions"] == []


def test_pure_redirect_colon_form_has_cross_reference() -> None:
    """Regression: a pure redirect whose only pointer is the colon/capital
    ``Se: X`` form must still produce a cross_reference (not empty defs + null
    xref, which would fail import). The guard and the payload must use the same
    broad SEE_ALSO_RE detection."""
    raw = {
        "article_id": 110929,
        "lemmas": [{"id": 1, "lemma": "lette byrden", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [_explanation("Se: $", [_article_ref(8386, "byrde")])],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, "error", 110929)

    assert lemma["cross_reference"] == {"article_id": 8386, "lemma": "byrde"}
    assert lemma["definitions"] == []


def test_pure_redirect_jamfor_form_has_cross_reference() -> None:
    """Regression: a pure ``jamfør X`` redirect must also produce a
    cross_reference via the broad detection."""
    raw = {
        "article_id": 43592,
        "lemmas": [{"id": 1, "lemma": "jamførbar", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [_explanation("jamfør $", [_article_ref(43590, "jamføre")])],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, "error", 43592)

    assert lemma["cross_reference"] == {"article_id": 43590, "lemma": "jamføre"}
    assert lemma["definitions"] == []


# ---------------------------------------------------------------------------
# 5. Mixed article → cross_reference is None, real defs preserved
# ---------------------------------------------------------------------------

def test_mixed_article_has_no_cross_reference_and_preserves_definitions() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "test", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("real sense one"),
                        _explanation("Se: $", [_article_ref(99, "X")]),
                    ],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, _real_llm_result(2, "real translation"), 1)

    assert lemma["cross_reference"] is None
    assert len(lemma["definitions"]) == 1
    assert lemma["definitions"][0]["text"] == "real sense one"
    assert lemma["see_also"] == [
        {"article_id": 99, "lemma": "X", "relation": "see"}
    ]


def test_sub_definition_see_also_is_collected_without_definition() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "test", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("real sense"),
                        {
                            "type_": "definition",
                            "sub_definition": True,
                            "elements": [
                                _explanation("Se: $", [_article_ref(77, "nested")]),
                            ],
                        },
                    ],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, _real_llm_result(2, "real translation"), 1)

    assert lemma["definitions"] == [
        {
            "text": "real sense",
            "translation": "real translation",
            "examples": [],
        }
    ]
    assert lemma["see_also"] == [
        {"article_id": 77, "lemma": "nested", "relation": "see"}
    ]


# ---------------------------------------------------------------------------
# 6. jamføre (the real verb, no article_ref) → not see-also
# ---------------------------------------------------------------------------

def test_refless_jamfore_is_not_see_also() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "jamføre", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("jamføre to ting"),
                    ],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, _real_llm_result(2, "to compare"), 1)

    # No see_also entry.
    assert lemma["see_also"] == []

    # The "jamføre" explanation is a real definition, not skipped.
    assert len(lemma["definitions"]) == 1
    assert lemma["definitions"][0]["text"] == "jamføre to ting"
    assert lemma["definitions"][0]["translation"] == "to compare"


# ---------------------------------------------------------------------------
# Bonus: see_also key always emitted (even when empty) for stable schema
# ---------------------------------------------------------------------------

def test_see_also_key_always_present() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "test", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [_explanation("just a sense")],
                }
            ]
        },
    }
    lemma = translate.build_lemma(raw, _real_llm_result(2, "translation"), 1)
    assert "see_also" in lemma
    assert lemma["see_also"] == []


# ---------------------------------------------------------------------------
# Bonus: dedup by article_id preserves first-seen order
# ---------------------------------------------------------------------------

def test_see_also_dedup_by_article_id() -> None:
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "test", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        _explanation("real sense"),
                        _explanation("Se: $", [_article_ref(10, "first")]),
                        _explanation("Se også: $", [_article_ref(10, "duplicate")]),
                        _explanation("Se: $", [_article_ref(20, "second")]),
                    ],
                }
            ]
        },
    }
    entries = extract_see_also(raw)
    assert entries == [
        {"article_id": 10, "lemma": "first", "relation": "see"},
        {"article_id": 20, "lemma": "second", "relation": "see"},
    ]
