from __future__ import annotations

from ordbokene.build import build_lemma
from ordbokene.client import complete_existing_translation, validate_translation


def build_expression_article(*, word_class: str | None = None) -> dict:
    article = {
        "article_id": 1,
        "lemmas": [
            {
                "id": 2,
                "lemma": "slå seg til ro",
                "paradigm_info": [{"tags": ["NOUN"], "inflection": []}],
            }
        ],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 3,
                    "elements": [{"type_": "explanation", "content": "roe seg"}],
                }
            ]
        },
    }
    if word_class is not None:
        article["word_class"] = word_class
    return article


def test_build_lemma_given_expression_word_class_expect_expr_pos_and_self_marker() -> None:
    result = build_lemma(
        build_expression_article(word_class="EXPR"),
        {
            "definitions": [{"source_id": 3, "translation": "settle down"}],
            "lemma_primary": "settle down",
        },
        1,
    )

    lemma = result["lemmas"][0]
    assert lemma["pos"] == "EXPR"
    assert lemma["primary_translation"] == "settle down"
    self_form = next(form for form in lemma["word_forms"] if form["word_form"] == "slå seg til ro")
    assert "EXPR" in self_form["tags_json"]


def test_build_lemma_given_expression_tag_expect_expr_pos_per_lemma() -> None:
    article = build_expression_article()
    article["lemmas"][0]["paradigm_info"][0]["tags"] = ["EXPR"]

    result = build_lemma(
        article,
        {
            "definitions": [{"source_id": 3, "translation": "settle down"}],
            "lemma_primary": "settle down",
        },
        1,
    )

    assert result["lemmas"][0]["pos"] == "EXPR"
    assert "EXPR" in result["lemmas"][0]["word_forms"][0]["tags_json"]


def test_build_lemma_given_mixed_lemma_tags_expect_only_tagged_lemma_is_expression() -> None:
    article = build_expression_article(word_class="EXPR")
    article["lemmas"][0]["paradigm_info"][0]["tags"] = ["EXPR"]
    article["lemmas"].append(
        {
            "id": 4,
            "lemma": "ro",
            "paradigm_info": [{"tags": ["NOUN"], "inflection": []}],
        }
    )

    result = build_lemma(
        article,
        {
            "definitions": [{"source_id": 3, "translation": "settle down"}],
            "lemma_primary": "settle down",
        },
        1,
    )

    assert [lemma["pos"] for lemma in result["lemmas"]] == ["EXPR", "NOUN"]
    assert result["lemmas"][1]["word_forms"][0]["tags_json"] == []


def test_validate_translation_given_literal_words_expect_reusable() -> None:
    packet = {"definitions": [{"source_id": 3}], "is_expression": True}
    for translation in (
        "take literally",
        "not take literally",
        "literal-minded and direct",
        "a verbatim account",
    ):
        result = {
            "definitions": [{"source_id": 3, "translation": translation}],
            "lemma_primary": translation,
        }
        assert validate_translation(packet, result) is None


def test_validate_translation_given_imagery_annotation_expect_rejection() -> None:
    packet = {"definitions": [{"source_id": 3}], "is_expression": True}
    for translation in (
        "(lit. throw in the towel)",
        "[lit. throw in the towel]",
        "[literally throw in the towel]",
        "literally: throw in the towel",
        "literal translation: throw in the towel",
    ):
        result = {
            "definitions": [{"source_id": 3, "translation": translation}],
            "lemma_primary": "settle down",
        }
        assert validate_translation(packet, result) == "literal_only_expression"


def test_validate_translation_accepts_complete_meaning_with_literal_suffix() -> None:
    packet = {"definitions": [{"source_id": 3}], "is_expression": True}
    result = {
        "definitions": [
            {
                "source_id": 3,
                "translation": "give up (lit. throw in the towel)",
            }
        ],
        "lemma_primary": "give up",
    }

    assert validate_translation(packet, result) is None


def test_complete_existing_translation_given_literal_words_expect_reuse() -> None:
    article = build_expression_article(word_class="EXPR")
    article["lemmas"][0]["primary_translation"] = "take literally"
    article["body"]["definitions"][0]["elements"][0]["translation"] = "take literally"

    assert complete_existing_translation(article) is not None
