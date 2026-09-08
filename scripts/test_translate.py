import json
from argparse import Namespace
from pathlib import Path

import translate
from ordbokene import pipeline
from ordbokene.review import (
    TranslationReviewItem,
    build_review_prompt,
    collect_translation_reviews,
    parse_review_response,
)


def test_download_release_rejects_mismatched_archive_before_replacing_lemmas(
    tmp_path: Path, monkeypatch
) -> None:
    from ordbokene import cli

    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    existing_path = lemma_dir / "1.json"
    existing_path.write_text('{"lemmas": []}', encoding="utf-8")

    class Response:
        ok = True
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict:
            return {
                "tag_name": "v2.0.0",
                "assets": [
                    {
                        "name": "norsk-lemma-v1.9.0.tar.gz",
                        "size": 1,
                        "browser_download_url": "https://example.invalid/archive",
                    }
                ],
            }

    monkeypatch.setattr(cli.requests, "get", lambda *args, **kwargs: Response())

    try:
        cli.cmd_hydrate(
            Namespace(
                articles_dir=tmp_path / "articles",
                lemma_dir=lemma_dir,
                dry_run=False,
                force=False,
                limit=None,
                tag="v2.0.0",
            )
        )
    except SystemExit as exc:
        assert "expected 'norsk-lemma-v2.0.0.tar.gz'" in str(exc)
    else:
        raise AssertionError("mismatched release archive was accepted")

    assert existing_path.read_text(encoding="utf-8") == '{"lemmas": []}'


def test_hydrate_given_install_replace_failure_expect_existing_export_restored(
    tmp_path: Path, monkeypatch
) -> None:
    import io
    import tarfile

    from ordbokene import cli

    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    existing_path = lemma_dir / "1.json"
    existing_path.write_text('{"lemmas": [{"lemma": "old"}]}', encoding="utf-8")

    archive_buffer = io.BytesIO()
    payload = b'{"lemmas": [{"lemma": "new"}]}'
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("lemma/2.json")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    archive_bytes = archive_buffer.getvalue()

    class ApiResponse:
        ok = True
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict:
            return {
                "tag_name": "v2.0.0",
                "assets": [
                    {
                        "name": "norsk-lemma-v2.0.0.tar.gz",
                        "size": len(archive_bytes),
                        "browser_download_url": "https://example.invalid/archive",
                    }
                ],
            }

    class DownloadResponse:
        def __enter__(self) -> "DownloadResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int) -> list[bytes]:
            return [archive_bytes]

    def fake_get(_url: str, **kwargs: object) -> ApiResponse | DownloadResponse:
        return DownloadResponse() if kwargs.get("stream") else ApiResponse()

    monkeypatch.setattr(cli.requests, "get", fake_get)
    real_replace = cli.os.replace
    replace_calls = 0

    def fail_install_once(source: str | Path, destination: str | Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated install failure")
        real_replace(source, destination)

    monkeypatch.setattr(cli.os, "replace", fail_install_once)

    try:
        cli.cmd_hydrate(
            Namespace(
                articles_dir=tmp_path / "articles",
                lemma_dir=lemma_dir,
                dry_run=False,
                force=False,
                limit=None,
                tag="v2.0.0",
            )
        )
    except OSError as exc:
        assert "simulated install failure" in str(exc)
    else:
        raise AssertionError("the forced installation failure was not raised")

    assert existing_path.read_text(encoding="utf-8") == '{"lemmas": [{"lemma": "old"}]}'
    assert sorted(path.name for path in lemma_dir.glob("*.json")) == ["1.json"]


def test_extract_senses_merges_sub_definition_examples_and_skips_subarticles() -> None:
    raw_dict = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "elements": [
                        {
                            "type_": "definition",
                            "id": 2,
                            "elements": [
                                {
                                    "type_": "explanation",
                                    "content": "main $",
                                    "items": [],
                                },
                                {"type_": "example", "quote": {"content": "first example"}},
                                {
                                    "type_": "definition",
                                    "id": 1,
                                    "sub_definition": True,
                                    "elements": [
                                        {
                                            "type_": "explanation",
                                            "content": "label only",
                                        },
                                        {
                                            "type_": "example",
                                            "quote": {"content": "sub example"},
                                        },
                                    ],
                                },
                            ],
                        },
                        {
                            "type_": "sub_article",
                            "article": {"body": {"definitions": []}},
                        },
                    ],
                }
            ]
        }
    }

    assert translate.extract_senses(raw_dict) == [
        {
            "source_id": 2,
            "text": "main $",
            "examples": ["first example", "sub example"],
        }
    ]


def test_extract_senses_preserves_repeated_source_id_order() -> None:
    raw_dict = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "first"},
                        {"type_": "explanation", "content": "second"},
                    ],
                }
            ]
        }
    }

    assert translate.extract_senses(raw_dict) == [
        {"source_id": 2, "text": "first", "examples": []},
        {"source_id": 2, "text": "second", "examples": []},
    ]


def test_extract_senses_skips_redirect_cross_references() -> None:
    raw_dict = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "actual meaning"},
                        {
                            "type_": "explanation",
                            "content": "se $",
                            "items": [
                                {
                                    "type_": "article_ref",
                                    "lemmas": [{"lemma": "gå"}],
                                }
                            ],
                        },
                        {
                            "type_": "explanation",
                            "content": "sjå $",
                            "items": [
                                {
                                    "type_": "article_ref",
                                    "lemmas": [{"lemma": "stå"}],
                                }
                            ],
                        },
                    ],
                }
            ]
        }
    }

    assert translate.extract_senses(raw_dict) == [
        {"source_id": 2, "text": "actual meaning", "examples": []},
    ]


def test_extract_senses_skips_structural_context_labels() -> None:
    raw_dict = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "$",
                            "items": [
                                {"type_": "domain", "id": "something"},
                            ],
                        },
                        {"type_": "explanation", "content": "actual sense"},
                    ],
                }
            ]
        }
    }

    assert translate.extract_senses(raw_dict) == [
        {"source_id": 2, "text": "actual sense", "examples": []},
    ]


def test_parse_json_response_accepts_clean_fenced_and_embedded_json() -> None:
    clean = '{"10": {"lemma_primary": "fish"}}'
    fenced = '```json\n{"10": {"lemma_primary": "fish"}}\n```'
    embedded = 'Here you go: {"10": {"lemma_primary": "fish"}} done'

    expected = {0: {"lemma_primary": "fish"}}
    assert translate._parse_json_response(clean, [10]) == expected
    assert translate._parse_json_response(fenced, [10]) == expected
    assert translate._parse_json_response(embedded, [10]) == expected


def test_parse_json_response_reports_missing_and_parse_failures() -> None:
    assert translate._parse_json_response('{"11": {}}', [10]) == {0: "missing_entry"}
    assert translate._parse_json_response("not json", [10]) == {0: "json_parse_failed"}


def test_build_prompt_puts_definitions_before_lemma_primary_instruction() -> None:
    prompt = translate.build_prompt(
        [
            {
                "article_id": 14904,
                "lemmas": ["fisk"],
                "hgno": 2,
                "tags": ["NOUN"],
                "is_expression": False,
                "definitions": [{"source_id": 2, "text": "anchor hook"}],
            }
        ]
    )

    assert "Do NOT translate only the headword spelling" in prompt
    assert "source_id 2: anchor hook" in prompt
    assert prompt.index('"definitions"') < prompt.index('"lemma_primary"')


def test_build_prompt_includes_examples_under_sense() -> None:
    prompt = translate.build_prompt(
        [
            {
                "article_id": 1,
                "lemmas": ["gå"],
                "hgno": 0,
                "tags": ["VERB"],
                "pos": "VERB",
                "is_expression": False,
                "definitions": [
                    {
                        "source_id": 3,
                        "text": "move on foot",
                        "examples": [
                            "han gikk hjem",
                            "de gikk fort",
                            "tre forsok",
                            "fjerde eksempel",
                            "should be capped",
                        ],
                    }
                ],
            }
        ]
    )

    assert "source_id 3: move on foot" in prompt
    # Norwegian examples are SHOWN as read-only gloss context, capped at MAX_EXAMPLES=4.
    assert "example: han gikk hjem" in prompt
    assert "example: fjerde eksempel" in prompt
    assert "should be capped" not in prompt


def test_build_prompt_includes_definition_text_when_no_examples() -> None:
    prompt = translate.build_prompt(
        [
            {
                "article_id": 1,
                "lemmas": ["gå"],
                "hgno": 0,
                "tags": ["VERB"],
                "pos": "VERB",
                "is_expression": False,
                "definitions": [{"source_id": 3, "text": "move on foot"}],
            }
        ]
    )
    # Definition text is still present even with no examples shown.
    assert "source_id 3: move on foot" in prompt


def test_collect_translation_reviews_includes_primary_definitions_and_examples(
    tmp_path: Path,
) -> None:
    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    (lemma_dir / "123.json").write_text(
        json.dumps(
            {
                "lemmas": [{"lemma": "gå", "primary_translation": "walk"}],
                "definitions": [
                    {
                        "text": "flytte seg til fots",
                        "translation": "move on foot",
                        "examples": [{"no": "han gikk hjem", "en": "he walked home"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    items = collect_translation_reviews(lemma_dir)

    assert len(items) == 1
    assert items[0].article_id == 123
    assert items[0].lemma == "gå"
    assert items[0].primary_translation == "walk"
    assert items[0].definitions == [
        {
            "index": 0,
            "text": "flytte seg til fots",
            "translation": "move on foot",
            "examples": [{"no": "han gikk hjem", "en": "he walked home"}],
        }
    ]


def test_build_review_prompt_checks_all_translation_fields() -> None:
    prompt = build_review_prompt(
        [
            TranslationReviewItem(
                article_id=1,
                lemma="gå",
                primary_translation="walk",
                definitions=[
                    {
                        "index": 0,
                        "text": "flytte seg til fots",
                        "translation": "move on foot",
                        "examples": [{"no": "han gikk hjem", "en": "he walked home"}],
                    }
                ],
            )
        ]
    )

    assert "`primary_translation`" in prompt
    assert "definition `translation`" in prompt
    assert "example `en`" in prompt
    assert "primary_mismatch" in prompt
    assert "definitions[0].translation" in prompt
    assert "definitions[0].examples[0].en" in prompt


def test_parse_review_response_accepts_fenced_json() -> None:
    parsed = parse_review_response(
        '```json\n{"issues": [{"id": 0, "field": "primary_translation"}]}\n```'
    )

    assert parsed == {"issues": [{"id": 0, "field": "primary_translation"}]}


def test_build_prompt_includes_part_of_speech_when_present() -> None:
    prompt = translate.build_prompt(
        [
            {
                "article_id": 5,
                "lemmas": ["løpe"],
                "hgno": 0,
                "tags": ["VERB"],
                "pos": "VERB",
                "is_expression": False,
                "definitions": [{"source_id": 1, "text": "run"}],
            }
        ]
    )
    assert "part_of_speech: verb" in prompt


def test_build_prompt_omits_part_of_speech_when_absent() -> None:
    prompt = translate.build_prompt(
        [
            {
                "article_id": 5,
                "lemmas": ["løpe"],
                "hgno": 0,
                "tags": [],
                "is_expression": False,
                "definitions": [{"source_id": 1, "text": "run"}],
            }
        ]
    )
    assert "part_of_speech" not in prompt


def test_build_prompt_repeated_source_id_instruction_present() -> None:
    prompt = translate.build_prompt([])
    assert "source_id appears more than once" in prompt


def test_ensure_articles_dir_skips_download_when_articles_exist(
    tmp_path: Path, monkeypatch
) -> None:
    from ordbokene.source import ensure_articles_dir

    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    (articles_dir / "1.json").write_text("{}", encoding="utf-8")

    called = False

    def fake_download(_articles_dir: Path) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("ordbokene.source._download_and_extract_articles", fake_download)

    ensure_articles_dir(articles_dir)
    assert called is False


def test_flatten_into_moves_nested_json_to_dst(tmp_path: Path) -> None:
    from ordbokene.source import _flatten_into

    src = tmp_path / "extracted"
    # Simulate the archive's article/ subdirectory layout
    (src / "article").mkdir(parents=True)
    (src / "article" / "1.json").write_text('{"article_id": 1}', encoding="utf-8")
    (src / "article" / "2.json").write_text('{"article_id": 2}', encoding="utf-8")

    dst = tmp_path / "articles"
    dst.mkdir()

    _flatten_into(src, dst)

    assert (dst / "1.json").exists()
    assert (dst / "2.json").exists()
    assert not (dst / "article").exists() or not (dst / "article" / "1.json").exists()


def test_extract_senses_normalizes_whitespace() -> None:
    raw_dict = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 3,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "holdeplass for tog,  t-bane",
                            "items": [],
                        }
                    ],
                }
            ]
        }
    }
    senses = translate.extract_senses(raw_dict)
    assert senses[0]["text"] == "holdeplass for tog, t-bane"


def test_extract_senses_strips_leading_trailing_whitespace() -> None:
    raw_dict = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "  rase  ", "items": []},
                    ],
                }
            ]
        }
    }
    senses = translate.extract_senses(raw_dict)
    assert senses[0]["text"] == "rase"


def test_build_lemma_assigns_prep_tag_to_pos_field() -> None:
    lemma = translate.build_lemma(_article_with_pos("PREP"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "PREP"


def test_build_lemma_assigns_pron_tag_to_pos_field() -> None:
    lemma = translate.build_lemma(_article_with_pos("PRON"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "PRON"


def test_build_lemma_assigns_conj_tag_to_pos_field() -> None:
    lemma = translate.build_lemma(_article_with_pos("CONJ"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "CONJ"


def test_build_lemma_assigns_interj_tag_to_pos_field() -> None:
    lemma = translate.build_lemma(_article_with_pos("INTERJ"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "INTERJ"


def test_build_lemma_assigns_det_tag_to_pos_field() -> None:
    lemma = translate.build_lemma(_article_with_pos("DET"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "DET"


def test_build_lemma_assigns_num_tag_to_pos_field() -> None:
    lemma = translate.build_lemma(_article_with_pos("NUM"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "NUM"


def test_build_lemma_pos_unknown_tag_falls_back_to_unknown() -> None:
    lemma = translate.build_lemma(_article_with_pos("ZZUNKNOWN"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "UNKNOWN"


def test_build_lemma_produces_lemma_with_translations_pos_and_word_forms() -> None:
    raw_dict = {
        "article_id": 14903,
        "lemmas": [
            {
                "id": 17301,
                "lemma": "fisk",
                "hgno": 1,
                "inflection_class": "m.,m1",
                "paradigm_info": [
                    {
                        "tags": ["NOUN", "Masc"],
                        "inflection": [
                            {"tags": ["Sing", "Ind"], "word_form": "fisk"},
                        ],
                    }
                ],
            }
        ],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "akvatisk dyr",
                            "items": [],
                        }
                    ],
                }
            ]
        },
    }

    llm_result = {
        "definitions": [
            {"source_id": 2, "translation": "aquatic animal"},
        ],
        "lemma_primary": "fish",
    }

    lemma = translate.build_lemma(raw_dict, llm_result, 14903)

    assert lemma["source_article_id"] == 14903
    assert lemma["cross_reference"] is None
    assert len(lemma["definitions"]) == 1
    assert lemma["definitions"][0]["text"] == "akvatisk dyr"
    assert lemma["definitions"][0]["translation"] == "aquatic animal"
    assert len(lemma["lemmas"]) == 1
    assert lemma["lemmas"][0]["lemma"] == "fisk"
    assert lemma["lemmas"][0]["pos"] == "NOUN"
    assert lemma["lemmas"][0]["primary_translation"] == "fish"
    assert lemma["lemmas"][0]["is_sub_article"] is False
    assert any(wf["word_form"] == "fisk" for wf in lemma["lemmas"][0]["word_forms"])


def test_build_lemma_given_empty_primary_single_sense_expect_gloss_fallback() -> None:
    # The translate step returned an empty lemma_primary; with one glossed sense
    # the definition translation is copied so the headword is not dropped.
    raw_dict = _article_with_definitions("akvatisk dyr")
    llm_result = {
        "definitions": [{"source_id": 100, "translation": "aquatic animal"}],
        "lemma_primary": "",
    }
    lemma = translate.build_lemma(raw_dict, llm_result, 1)
    assert lemma["lemmas"][0]["primary_translation"] == "aquatic animal"


def test_build_lemma_given_empty_primary_multi_sense_expect_first_gloss() -> None:
    # Multiple senses, empty lemma_primary: fall back to the leading sense.
    raw_dict = _article_with_definitions("dele ut", "gi råd")
    llm_result = {
        "definitions": [
            {"source_id": 100, "translation": "hand out"},
            {"source_id": 101, "translation": "advise"},
        ],
        "lemma_primary": None,
    }
    lemma = translate.build_lemma(raw_dict, llm_result, 1)
    assert lemma["lemmas"][0]["primary_translation"] == "hand out"


def test_build_lemma_given_empty_primary_no_gloss_expect_null() -> None:
    # No definition translation to derive from: primary stays null (warn tier).
    raw_dict = _article_with_definitions("udefinert")
    llm_result = {
        "definitions": [{"source_id": 100, "translation": "   "}],
        "lemma_primary": "",
    }
    lemma = translate.build_lemma(raw_dict, llm_result, 1)
    assert lemma["lemmas"][0]["primary_translation"] is None


def test_build_lemma_given_expression_with_gloss_expect_primary_forced_null() -> None:
    # An EXPR-tagged expression is forced to null primary_translation even though
    # the gloss fallback would otherwise compute one — no single-lemma label fits
    # an idiom. Guards the interaction between the new fallback and the is_expression
    # gate.
    raw_dict = {
        "article_id": 1,
        "lemmas": [
            {
                "id": 1,
                "lemma": "kaste inn håndkleet",
                "hgno": 1,
                "paradigm_info": [{"tags": ["EXPR"], "inflection": []}],
            },
        ],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 100,
                    "elements": [{"type_": "explanation", "content": "gi opp", "items": []}],
                }
            ]
        },
    }
    llm_result = {
        "definitions": [{"source_id": 100, "translation": "give up"}],
        "lemma_primary": "",
    }
    lemma = translate.build_lemma(raw_dict, llm_result, 1)
    assert lemma["lemmas"][0]["is_sub_article"] is True
    assert lemma["lemmas"][0]["primary_translation"] is None


def test_build_lemma_emits_cross_reference_when_definition_is_redirect() -> None:
    raw_dict = {
        "article_id": 99999,
        "lemmas": [{"id": 1, "lemma": "praktildkvede", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "se $",
                            "items": [
                                {
                                    "type_": "article_ref",
                                    "article_id": 25773,
                                    "lemmas": [{"lemma": "ildkvede"}],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }

    lemma = translate.build_lemma(raw_dict, "error", 99999)

    assert lemma["cross_reference"] == {
        "article_id": 25773,
        "lemma": "ildkvede",
    }
    assert lemma["definitions"] == []
    assert lemma["lemmas"][0]["primary_translation"] is None


def test_collect_pending_skips_existing(tmp_path: Path) -> None:
    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    (lemma_dir / "100.json").write_text("{}", encoding="utf-8")

    exploded = [(100, {"article_id": 100}), (200, {"article_id": 200})]
    pending = translate.collect_pending(exploded, lemma_dir, force=False)

    assert pending == [(200, {"article_id": 200})]


def test_collect_pending_force_returns_all(tmp_path: Path) -> None:
    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    (lemma_dir / "100.json").write_text("{}", encoding="utf-8")

    exploded = [(100, {"article_id": 100}), (200, {"article_id": 200})]
    pending = translate.collect_pending(exploded, lemma_dir, force=True)

    assert len(pending) == 2


def test_export_rebuilds_changed_articles_and_preserves_frequency_metadata(tmp_path: Path) -> None:
    from argparse import Namespace

    from ordbokene.build import build_lemma
    from ordbokene.cli import cmd_export
    from ordbokene.extract import extract_existing_translations
    from ordbokene.io import write_lemma

    articles_dir = tmp_path / "articles"
    lemma_dir = tmp_path / "lemma"
    articles_dir.mkdir()
    raw = {
        "article_id": 1,
        "lemmas": [
            {
                "id": 1,
                "lemma": "gå",
                "primary_translation": "walk",
                "paradigm_info": [],
            }
        ],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "move on foot",
                            "translation": "walk",
                            "items": [],
                        }
                    ],
                }
            ]
        },
    }
    article_path = articles_dir / "1.json"
    article_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    existing = extract_existing_translations(raw)
    assert existing is not None
    old = build_lemma(raw, existing, 1)
    old["lemmas"][0]["frequency_rank"] = 7
    write_lemma(lemma_dir, 1, old)

    raw["lemmas"][0]["primary_translation"] = "stroll"
    raw["body"]["definitions"][0]["elements"][0]["translation"] = "stroll"
    article_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    cmd_export(
        Namespace(
            articles_dir=articles_dir,
            lemma_dir=lemma_dir,
            force=False,
            dry_run=False,
            limit=None,
        )
    )

    updated = json.loads((lemma_dir / "1.json").read_text(encoding="utf-8"))
    assert updated["lemmas"][0]["primary_translation"] == "stroll"
    assert updated["definitions"][0]["translation"] == "stroll"
    assert updated["lemmas"][0]["frequency_rank"] == 7


def test_render_item_formats_fraction_as_numerator_over_denominator() -> None:
    item = {"numerator": 1, "denominator": 2}
    assert translate._render_item(item) == "1/2"


def test_render_item_expands_language_abbreviation_to_full_form(monkeypatch) -> None:
    monkeypatch.setitem(translate.ABBREVIATIONS, "lang_en", "English")
    item = {"id": "lang_en"}
    assert translate._render_item(item) == "English"


class FakeSession:
    def __init__(self) -> None:
        pass


def test_process_batch_writes_lemma_json(tmp_path: Path, monkeypatch) -> None:
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    lemma_dir = tmp_path / "lemma"

    article = {
        "article_id": 10,
        "edit_state": "Eksisterende",
        "lemmas": [
            {
                "id": 1,
                "lemma": "fisk",
                "hgno": 1,
                "paradigm_info": [{"tags": ["NOUN"], "inflection": []}],
            }
        ],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [{"type_": "explanation", "content": "dyr", "items": []}],
                }
            ]
        },
    }
    (articles_dir / "10.json").write_text(json.dumps(article), encoding="utf-8")

    fake_result = {
        0: {
            "definitions": [{"source_id": 2, "translation": "animal"}],
            "lemma_primary": "fish",
        }
    }
    # Patch pipeline.request_translations — that's where process_batch resolves the name.
    monkeypatch.setattr(pipeline, "request_translations", lambda *a: fake_result)

    args = Namespace(
        api_key="fake",
        model="test",
        batch_size=10,
        max_retries=3,
        retry_delay=60,
        error_log=tmp_path / "errors.log",
        lemma_dir=lemma_dir,
        dry_run=False,
    )

    batch = [(10, article)]
    written = translate.process_batch(FakeSession(), args, batch)

    assert written == 1
    lemma_file = lemma_dir / "10.json"
    assert lemma_file.exists()
    data = json.loads(lemma_file.read_text(encoding="utf-8"))
    assert data["source_article_id"] == 10
    assert data["lemmas"][0]["primary_translation"] == "fish"


def test_build_lemma_repeated_source_id_preserves_order() -> None:
    raw_dict = {
        "article_id": 116155,
        "lemmas": [{"id": 1, "lemma": "haste", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "det haster ikke", "items": []},
                        {"type_": "explanation", "content": "det er ikke så viktig", "items": []},
                    ],
                }
            ]
        },
    }
    llm_result = {
        "definitions": [
            {"source_id": 2, "translation": "not urgent"},
            {"source_id": 2, "translation": "not important"},
        ],
        "lemma_primary": "not pressing",
    }

    lemma = translate.build_lemma(raw_dict, llm_result, 116155)

    assert len(lemma["definitions"]) == 2
    assert lemma["definitions"][0]["translation"] == "not urgent"
    assert lemma["definitions"][1]["translation"] == "not important"


def test_build_lemma_pairs_norwegian_examples_with_english_by_index() -> None:
    raw_dict = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "move on foot", "items": []},
                        {"type_": "example", "quote": {"content": "han gikk hjem"}},
                        {"type_": "example", "quote": {"content": "de gikk fort"}},
                    ],
                }
            ]
        },
    }
    llm_result = {
        "definitions": [
            {
                "source_id": 2,
                "translation": "walk",
                "examples": ["he walked home", "they walked fast"],
            }
        ],
        "lemma_primary": "walk",
    }

    lemma = translate.build_lemma(raw_dict, llm_result, 1)

    examples = lemma["definitions"][0]["examples"]
    assert examples == [
        {"no": "han gikk hjem", "en": "he walked home"},
        {"no": "de gikk fort", "en": "they walked fast"},
    ]


def test_build_lemma_caps_output_examples_to_max_examples() -> None:
    raw_dict = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "move on foot", "items": []},
                        {"type_": "example", "quote": {"content": "first"}},
                        {"type_": "example", "quote": {"content": "second"}},
                        {"type_": "example", "quote": {"content": "third"}},
                        {"type_": "example", "quote": {"content": "fourth"}},
                        {"type_": "example", "quote": {"content": "fifth"}},
                    ],
                }
            ]
        },
    }
    llm_result = {
        "definitions": [
            {
                "source_id": 2,
                "translation": "walk",
                "examples": ["en first", "en second", "en third", "en fourth"],
            }
        ],
        "lemma_primary": "walk",
    }

    lemma = translate.build_lemma(raw_dict, llm_result, 1)

    examples = lemma["definitions"][0]["examples"]
    assert len(examples) == 4
    assert examples[0] == {"no": "first", "en": "en first"}
    assert examples[1] == {"no": "second", "en": "en second"}
    assert examples[3] == {"no": "fourth", "en": "en fourth"}


def test_build_lemma_pads_en_with_empty_when_model_returns_fewer() -> None:
    raw_dict = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "move on foot", "items": []},
                        {"type_": "example", "quote": {"content": "first"}},
                        {"type_": "example", "quote": {"content": "second"}},
                    ],
                }
            ]
        },
    }
    # Model returned only one example translation
    llm_result = {
        "definitions": [
            {
                "source_id": 2,
                "translation": "walk",
                "examples": ["en first"],
            }
        ],
        "lemma_primary": "walk",
    }

    lemma = translate.build_lemma(raw_dict, llm_result, 1)

    examples = lemma["definitions"][0]["examples"]
    assert examples == [
        {"no": "first", "en": "en first"},
        {"no": "second", "en": ""},
    ]


def test_build_lemma_repeated_source_id_pops_per_definition_examples_in_order() -> None:
    raw_dict = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "sense one", "items": []},
                        {"type_": "example", "quote": {"content": "ex one"}},
                        {"type_": "explanation", "content": "sense two", "items": []},
                        {"type_": "example", "quote": {"content": "ex two"}},
                    ],
                }
            ]
        },
    }
    llm_result = {
        "definitions": [
            {"source_id": 2, "translation": "one", "examples": ["en one"]},
            {"source_id": 2, "translation": "two", "examples": ["en two"]},
        ],
        "lemma_primary": "walk",
    }

    lemma = translate.build_lemma(raw_dict, llm_result, 1)

    assert len(lemma["definitions"]) == 2
    assert lemma["definitions"][0]["translation"] == "one"
    assert lemma["definitions"][0]["examples"] == [{"no": "ex one", "en": "en one"}]
    assert lemma["definitions"][1]["translation"] == "two"
    assert lemma["definitions"][1]["examples"] == [{"no": "ex two", "en": "en two"}]


def test_build_lemma_empty_queue_yields_empty_translation_and_examples() -> None:
    raw_dict = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "move on foot", "items": []},
                        {"type_": "example", "quote": {"content": "han gikk"}},
                    ],
                }
            ]
        },
    }
    # No matching source_id in the LLM result
    llm_result = {"definitions": [], "lemma_primary": ""}

    lemma = translate.build_lemma(raw_dict, llm_result, 1)

    assert lemma["definitions"][0]["translation"] == ""
    assert lemma["definitions"][0]["examples"] == [{"no": "han gikk", "en": ""}]


def test_build_lemma_reuse_path_emits_empty_en_for_examples() -> None:
    """R1: extract_existing_translations returns no examples key, so reused
    entries emit en="" for every example. This is deliberate."""
    raw_dict = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "move on foot", "items": []},
                        {"type_": "example", "quote": {"content": "han gikk hjem"}},
                    ],
                }
            ]
        },
    }
    # Simulates extract_existing_translations output: no "examples" key.
    llm_result = {
        "definitions": [{"source_id": 2, "translation": "walk"}],
        "lemma_primary": "walk",
    }

    lemma = translate.build_lemma(raw_dict, llm_result, 1)

    assert lemma["definitions"][0]["translation"] == "walk"
    assert lemma["definitions"][0]["examples"] == [{"no": "han gikk hjem", "en": ""}]


def test_build_lemma_given_hgno_zero_preserves_zero() -> None:
    raw_dict = {
        "article_id": 200001,
        "lemmas": [{"id": 1, "lemma": "en", "hgno": 0, "paradigm_info": []}],
        "body": {"definitions": []},
    }
    lemma = translate.build_lemma(raw_dict, {}, 200001)
    assert lemma["lemmas"][0]["hgno"] == 0


def test_pronunciation_tone_metadata_is_learner_facing() -> None:
    from enrich_pronunciation import PronEntry

    tone_1 = PronEntry(sampa=None, ipa="ˈɑ", tone=1, source="nb_g2p").to_dict()
    tone_2 = PronEntry(sampa=None, ipa="ˈɑ", tone=2, source="nb_g2p").to_dict()
    no_tone = PronEntry(sampa=None, ipa="ɑ", tone=None, source="nb_g2p").to_dict()
    trusted_no_tone = PronEntry(sampa=None, ipa="ɑ", tone=None, source="nb_uttale").to_dict()

    assert tone_1["tone_label"] == "Accent 1"
    assert tone_1["tone_status"] == "known"
    assert tone_2["tone_label"] == "Accent 2"
    assert tone_2["tone_status"] == "known"
    assert "tone_label" not in no_tone
    assert no_tone["tone_status"] == "unknown"
    assert trusted_no_tone["tone_status"] == "none"


def test_pronunciation_skip_check_backfills_tone_metadata() -> None:
    from enrich_pronunciation import add_missing_tone_metadata, all_non_null_enriched

    article = {
        "lemmas": [
            {
                "pronunciation": [{"ipa": "ˈɑ", "tone": 1, "source": "nb_uttale"}],
                "paradigm_info": [
                    {
                        "inflection": [
                            {
                                "word_form": "test",
                                "pronunciation": [{"ipa": "ɑ", "tone": None, "source": "nb_g2p"}],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    assert all_non_null_enriched(article) is True
    assert add_missing_tone_metadata(article) is True
    assert article["lemmas"][0]["pronunciation"][0]["tone_label"] == "Accent 1"
    assert article["lemmas"][0]["pronunciation"][0]["tone_status"] == "known"
    inf_pron = article["lemmas"][0]["paradigm_info"][0]["inflection"][0]["pronunciation"][0]
    assert "tone_label" not in inf_pron
    assert inf_pron["tone_status"] == "unknown"
    assert add_missing_tone_metadata(article) is False


def test_enrich_article_backfills_tone_label_without_reresolving(tmp_path, monkeypatch) -> None:
    import enrich_pronunciation

    article_path = tmp_path / "article.json"
    article_path.write_text(
        json.dumps(
            {
                "lemmas": [
                    {
                        "lemma": "test",
                        "pronunciation": [{"ipa": "ˈɑ", "tone": 1, "source": "nb_uttale"}],
                        "paradigm_info": [
                            {
                                "inflection": [
                                    {
                                        "word_form": "test",
                                        "pronunciation": [
                                            {"ipa": "ˈɑ", "tone": 2, "source": "nb_uttale"}
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fail_resolve(*_args):
        raise AssertionError("resolve should not run for schema-only backfill")

    monkeypatch.setattr(enrich_pronunciation, "resolve", fail_resolve)

    counts = enrich_pronunciation.enrich_article(article_path, force=False)
    data = json.loads(article_path.read_text(encoding="utf-8"))

    assert counts == {}
    assert data["lemmas"][0]["pronunciation"][0]["tone_label"] == "Accent 1"
    assert data["lemmas"][0]["pronunciation"][0]["tone_status"] == "known"
    inf_pron = data["lemmas"][0]["paradigm_info"][0]["inflection"][0]["pronunciation"][0]
    assert inf_pron["tone_label"] == "Accent 2"
    assert inf_pron["tone_status"] == "known"


def test_enrich_article_only_resolves_missing_pronunciations(tmp_path, monkeypatch) -> None:
    import enrich_pronunciation

    article_path = tmp_path / "article.json"
    existing = {"ipa": "ˈɑ", "tone": 1, "source": "nb_uttale"}
    article_path.write_text(
        json.dumps(
            {
                "lemmas": [
                    {
                        "lemma": "test",
                        "paradigm_info": [
                            {
                                "tags": ["NOUN"],
                                "inflection": [
                                    {"word_form": "known", "pronunciation": [existing]},
                                    {"word_form": "missing", "pronunciation": []},
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    g2p_calls = []

    def fake_g2p_entries(wordforms):
        g2p_calls.append(wordforms)
        return {
            "missing": enrich_pronunciation.PronEntry(
                sampa=None,
                ipa="ˈmɪsɪŋ",
                tone=2,
                source="nb_g2p",
                prosody_trusted=False,
                needs_review=True,
            )
        }

    monkeypatch.setattr(enrich_pronunciation, "g2p_entries", fake_g2p_entries)

    counts = enrich_pronunciation.enrich_article(article_path, force=False)
    data = json.loads(article_path.read_text(encoding="utf-8"))
    forms = {
        inf["word_form"]: inf["pronunciation"][0]
        for inf in data["lemmas"][0]["paradigm_info"][0]["inflection"]
    }

    assert g2p_calls == [["missing"]]
    assert counts["nb_g2p"] == 1
    assert forms["known"]["source"] == "nb_uttale"
    assert forms["known"]["tone_label"] == "Accent 1"
    assert forms["known"]["tone_status"] == "known"
    assert forms["missing"]["source"] == "nb_g2p"
    assert forms["missing"]["tone_label"] == "Accent 2"
    assert forms["missing"]["tone_status"] == "known"


def test_resolve_skips_nb_g2p_for_multiword_or_bracket_forms(monkeypatch) -> None:
    import enrich_pronunciation

    def fail_transcribe(_words):
        raise AssertionError("nb-g2p should not run for expression wordforms")

    monkeypatch.setattr(enrich_pronunciation.nb_g2p, "transcribe_words", fail_transcribe)

    assert enrich_pronunciation.resolve("ta høyde for", None) is None
    assert enrich_pronunciation.resolve("[fikse|ordne] biffen", None) is None


def test_wrap_sub_as_article_given_null_properties_does_not_crash() -> None:
    sub = {
        "article_id": 133359,
        "lemmas": [],
        "body": {},
        "article_type": "SUB_ARTICLE",
        "word_class": "",
        "properties": None,
    }
    result = translate._wrap_sub_as_article(100495, sub)
    assert result["edit_state"] == "Eksisterende"


def test_explode_skips_pa_vent_articles(tmp_path: Path) -> None:
    pending = {"article_id": 1, "edit_state": "På vent", "lemmas": [], "body": {}}
    normal = {"article_id": 2, "edit_state": "Eksisterende", "lemmas": [], "body": {}}
    (tmp_path / "1.json").write_text(json.dumps(pending))
    (tmp_path / "2.json").write_text(json.dumps(normal))

    result = translate.explode(tmp_path)

    ids = [entry[0] for entry in result]
    assert 1 not in ids
    assert 2 in ids


def test_explode_includes_inline_sub_article_without_standalone_file(tmp_path: Path) -> None:
    sub = {
        "article_id": 999,
        "lemmas": [],
        "body": {},
        "article_type": "SUB_ARTICLE",
        "word_class": "",
        "properties": None,
    }
    parent = {
        "article_id": 100,
        "edit_state": "Eksisterende",
        "lemmas": [],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "elements": [{"type_": "sub_article", "article": sub}],
                }
            ]
        },
    }
    (tmp_path / "100.json").write_text(json.dumps(parent))

    result = translate.explode(tmp_path)

    ids = [entry[0] for entry in result]
    assert 100 in ids
    assert 999 in ids


def test_explode_skips_inline_sub_article_when_standalone_file_exists(tmp_path: Path) -> None:
    sub = {
        "article_id": 999,
        "lemmas": [],
        "body": {},
        "article_type": "SUB_ARTICLE",
        "word_class": "",
        "properties": None,
    }
    parent = {
        "article_id": 100,
        "edit_state": "Eksisterende",
        "lemmas": [],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "elements": [{"type_": "sub_article", "article": sub}],
                }
            ]
        },
    }
    standalone = {"article_id": 999, "edit_state": "Eksisterende", "lemmas": [], "body": {}}
    (tmp_path / "100.json").write_text(json.dumps(parent))
    (tmp_path / "999.json").write_text(json.dumps(standalone))

    result = translate.explode(tmp_path)

    # 999 appears exactly once (from its own standalone file, not as an exploded inline)
    ids = [entry[0] for entry in result]
    assert ids.count(999) == 1


def test_extract_senses_skips_colon_ending_structural_label() -> None:
    raw_dict = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 5,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "refleksivt:",
                            "items": [{"type_": "grammar", "id": "refl"}],
                        },
                        {"type_": "explanation", "content": "actual sense"},
                    ],
                }
            ]
        }
    }

    assert translate.extract_senses(raw_dict) == [
        {"source_id": 5, "text": "actual sense", "examples": []},
    ]


def test_audio_jobs_dedupe_by_text_and_tone_and_skip_expressions(tmp_path: Path) -> None:
    from ordbokene.audio import collect_audio_jobs

    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    (lemma_dir / "1.json").write_text(
        json.dumps(
            {
                "source_article_id": 1,
                "lemmas": [
                    {
                        "lemma": "bønner",
                        "source_lemma_id": 10,
                        "is_sub_article": False,
                        "word_forms": [
                            {
                                "word_form": "bønner",
                                "pronunciation": [
                                    {"source": "nb_uttale", "tone": 2, "tone_status": "known"}
                                ],
                            }
                        ],
                    },
                    {
                        "lemma": "ta høyde for",
                        "source_lemma_id": 11,
                        "is_sub_article": True,
                        "word_forms": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (lemma_dir / "2.json").write_text(
        json.dumps(
            {
                "source_article_id": 2,
                "lemmas": [
                    {
                        "lemma": "bønner",
                        "source_lemma_id": 12,
                        "is_sub_article": False,
                        "word_forms": [
                            {
                                "word_form": "bønner",
                                "pronunciation": [
                                    {"source": "nb_uttale", "tone": 2, "tone_status": "known"}
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    jobs = collect_audio_jobs(
        lemma_dir, provider="google", voice="nb-NO-Chirp3-HD-Aoede", language_code="nb-NO"
    )

    assert [job.text for job in jobs] == ["bønner"]
    assert jobs[0].key == ("bønner", "known", 2)
    assert jobs[0].article_ids == [1, 2]
    assert jobs[0].source_lemma_ids == [10, 12]
    assert jobs[0].filename.endswith(".mp3")
    assert "/" not in jobs[0].filename


def test_audio_jobs_keep_tonal_homographs_separate(tmp_path: Path) -> None:
    from ordbokene.audio import collect_audio_jobs

    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    (lemma_dir / "1.json").write_text(
        json.dumps(
            {
                "source_article_id": 1,
                "lemmas": [
                    {
                        "lemma": "tanken",
                        "source_lemma_id": 1,
                        "is_sub_article": False,
                        "word_forms": [
                            {
                                "word_form": "tanken",
                                "pronunciation": [{"tone": 1, "tone_status": "known"}],
                            }
                        ],
                    },
                    {
                        "lemma": "tanken",
                        "source_lemma_id": 2,
                        "is_sub_article": False,
                        "word_forms": [
                            {
                                "word_form": "tanken",
                                "pronunciation": [{"tone": 2, "tone_status": "known"}],
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    jobs = collect_audio_jobs(
        lemma_dir, provider="google", voice="nb-NO-Chirp3-HD-Aoede", language_code="nb-NO"
    )

    assert [(job.text, job.tone) for job in jobs] == [("tanken", 1), ("tanken", 2)]
    assert jobs[0].filename != jobs[1].filename


def test_write_audio_enriched_lemmas_does_not_mutate_source_lemma_dir(tmp_path: Path) -> None:
    from ordbokene.audio import collect_audio_jobs, write_audio_enriched_lemmas

    lemma_dir = tmp_path / "lemma"
    output_dir = tmp_path / "lemma-with-audio"
    lemma_dir.mkdir()
    lemma_path = lemma_dir / "1.json"
    lemma_path.write_text(
        json.dumps(
            {
                "source_article_id": 1,
                "lemmas": [
                    {
                        "lemma": "bønner",
                        "source_lemma_id": 10,
                        "is_sub_article": False,
                        "word_forms": [
                            {
                                "word_form": "bønner",
                                "pronunciation": [
                                    {"source": "nb_uttale", "tone": 2, "tone_status": "known"}
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    from dataclasses import replace

    jobs = collect_audio_jobs(
        lemma_dir, provider="google", voice="nb-NO-Chirp3-HD-Aoede", language_code="nb-NO"
    )
    jobs[0] = replace(jobs[0], content_sha256="abc123")

    changed = write_audio_enriched_lemmas(lemma_dir, output_dir, jobs)

    assert changed == 1
    assert "audio" not in json.loads(lemma_path.read_text(encoding="utf-8"))["lemmas"][0]
    audio = json.loads((output_dir / "1.json").read_text(encoding="utf-8"))["lemmas"][0]["audio"][
        "lemma"
    ][0]
    assert audio["file"] == jobs[0].filename
    assert audio["path"] == f"audio/lemma/google/nb-NO-Chirp3-HD-Aoede/{jobs[0].filename}"
    assert audio["url"] == f"https://media.umebocchi.my.id/{audio['path']}"
    assert audio["content_sha256"] == "abc123"
    assert audio["tone"] == 2


def test_generate_audio_dry_run_lists_jobs_without_google_import(tmp_path: Path, capsys) -> None:
    import generate_audio

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    lemma_dir.mkdir()
    (lemma_dir / "1.json").write_text(
        json.dumps(
            {
                "source_article_id": 1,
                "lemmas": [
                    {
                        "lemma": "bønner",
                        "source_lemma_id": 10,
                        "is_sub_article": False,
                        "word_forms": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc = generate_audio.run(
        lemma_dir=lemma_dir,
        audio_dir=audio_dir,
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
        dry_run=True,
        limit=None,
        force=False,
        confirm_cost=False,
        price_per_million_chars=30.0,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "pending api calls: 1" in captured.out
    assert "estimated cost:" in captured.out
    assert "bønner" in captured.out


def test_manifest_checksum_index_preserves_first_duplicate() -> None:
    from ordbokene.audio import manifest_checksum_index

    manifest = {
        "items": [
            {"file": "duplicate.mp3", "content_sha256": "first"},
            {"file": "duplicate.mp3", "content_sha256": "last"},
        ]
    }

    assert manifest_checksum_index(manifest)["duplicate.mp3"] == "first"


def test_generate_audio_no_pending_jobs_retains_valid_manifest_entries(tmp_path: Path) -> None:
    import generate_audio
    from ordbokene.audio import (
        collect_audio_jobs,
        file_sha256,
        output_path_for_job,
        write_manifest,
    )

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    lemma_dir.mkdir()
    articles_dir.mkdir()
    (lemma_dir / "1.json").write_text(
        json.dumps(
            {
                "source_article_id": 1,
                "lemmas": [
                    {
                        "lemma": "bønner",
                        "source_lemma_id": 10,
                        "is_sub_article": False,
                        "word_forms": [
                            {
                                "word_form": "bønner",
                                "pronunciation": [
                                    {"source": "nb_uttale", "tone": 2, "tone_status": "known"}
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    job = collect_audio_jobs(
        lemma_dir,
        provider="google",
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
    )[0]
    output_path = output_path_for_job(audio_dir, job)
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"existing audio")
    job.content_sha256 = file_sha256(output_path)
    write_manifest(audio_dir, "google", job.voice, job.language_code, [job])

    def fail_synthesis(_job: object, _output_path: Path) -> None:
        raise AssertionError("valid existing audio should not be synthesized")

    assert (
        generate_audio.run(
            lemma_dir=lemma_dir,
            audio_dir=audio_dir,
            voice=job.voice,
            language_code=job.language_code,
            dry_run=False,
            limit=None,
            force=False,
            confirm_cost=True,
            price_per_million_chars=30.0,
            articles_dir=articles_dir,
            synthesize=fail_synthesis,
        )
        == 0
    )

    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    assert manifest["items"] == [
        {
            "text": "bønner",
            "file": job.filename,
            "path": job.relative_path,
            "url": job.public_url,
            "article_ids": [1],
            "source_lemma_ids": [10],
            "pronunciation_source": "nb_uttale",
            "tone_status": "known",
            "tone": 2,
            "content_sha256": job.content_sha256,
        }
    ]


def test_generate_audio_reconciles_current_associations_for_retained_job(tmp_path: Path) -> None:
    from dataclasses import replace

    import generate_audio
    from ordbokene.audio import collect_audio_jobs, file_sha256, output_path_for_job, write_manifest

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    lemma_dir.mkdir()
    articles_dir.mkdir()
    (lemma_dir / "2.json").write_text(
        json.dumps(
            {
                "source_article_id": 2,
                "lemmas": [{"lemma": "bønner", "source_lemma_id": 20, "word_forms": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    current_job = collect_audio_jobs(
        lemma_dir,
        provider="google",
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
    )[0]
    output_path = output_path_for_job(audio_dir, current_job)
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"existing audio")
    current_job.content_sha256 = file_sha256(output_path)
    old_job = replace(current_job, article_ids=[1], source_lemma_ids=[10])
    write_manifest(audio_dir, "google", current_job.voice, current_job.language_code, [old_job])

    def fail_synthesis(_job: object, _output_path: Path) -> None:
        raise AssertionError("valid existing audio should not be synthesized")

    assert (
        generate_audio.run(
            lemma_dir=lemma_dir,
            audio_dir=audio_dir,
            voice=current_job.voice,
            language_code=current_job.language_code,
            dry_run=False,
            limit=None,
            force=False,
            confirm_cost=True,
            price_per_million_chars=30.0,
            articles_dir=articles_dir,
            synthesize=fail_synthesis,
        )
        == 0
    )

    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    assert manifest["items"][0]["article_ids"] == [1, 2]
    assert manifest["items"][0]["source_lemma_ids"] == [10, 20]


def test_generate_audio_reconstructs_manifest_for_existing_mp3_without_tts(
    tmp_path: Path,
) -> None:
    import generate_audio
    from ordbokene.audio import collect_audio_jobs, file_sha256, output_path_for_job

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    lemma_dir.mkdir()
    articles_dir.mkdir()
    (lemma_dir / "1.json").write_text(
        json.dumps(
            {
                "source_article_id": 1,
                "lemmas": [{"lemma": "bønner", "source_lemma_id": 10, "word_forms": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    job = collect_audio_jobs(
        lemma_dir,
        provider="google",
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
    )[0]
    output_path = output_path_for_job(audio_dir, job)
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"interrupted audio")
    expected_checksum = file_sha256(output_path)

    def fail_synthesis(_job: object, _output_path: Path) -> None:
        raise AssertionError("an existing MP3 without a manifest should be recovered")

    assert (
        generate_audio.run(
            lemma_dir=lemma_dir,
            audio_dir=audio_dir,
            voice=job.voice,
            language_code=job.language_code,
            dry_run=False,
            limit=None,
            force=False,
            confirm_cost=True,
            price_per_million_chars=30.0,
            articles_dir=articles_dir,
            synthesize=fail_synthesis,
        )
        == 0
    )

    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    assert manifest["items"][0]["file"] == job.filename
    assert manifest["items"][0]["content_sha256"] == expected_checksum
    assert manifest["items"][0]["source_lemma_ids"] == [10]


def test_generate_audio_limited_resumed_runs_retain_previous_manifest_entries(
    tmp_path: Path,
) -> None:
    import generate_audio
    from ordbokene.audio import (
        collect_audio_jobs,
        file_sha256,
        output_path_for_job,
        write_manifest,
    )

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    lemma_dir.mkdir()
    articles_dir.mkdir()
    lemmas = []
    for source_lemma_id, text in [(10, "bønner"), (20, "fisk"), (30, "gå")]:
        lemmas.append(
            {
                "lemma": text,
                "source_lemma_id": source_lemma_id,
                "is_sub_article": False,
                "word_forms": [],
            }
        )
    (lemma_dir / "1.json").write_text(
        json.dumps({"source_article_id": 1, "lemmas": lemmas}, ensure_ascii=False),
        encoding="utf-8",
    )
    jobs = collect_audio_jobs(
        lemma_dir,
        provider="google",
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
    )
    existing_job = jobs[0]
    existing_path = output_path_for_job(audio_dir, existing_job)
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(b"existing audio")
    existing_job.content_sha256 = file_sha256(existing_path)
    write_manifest(
        audio_dir, "google", existing_job.voice, existing_job.language_code, [existing_job]
    )

    def fake_synthesis(job: object, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"synthesized {job.text}".encode())

    for _ in range(2):
        assert (
            generate_audio.run(
                lemma_dir=lemma_dir,
                audio_dir=audio_dir,
                voice="nb-NO-Chirp3-HD-Aoede",
                language_code="nb-NO",
                dry_run=False,
                limit=1,
                force=False,
                confirm_cost=False,
                price_per_million_chars=30.0,
                articles_dir=articles_dir,
                synthesize=fake_synthesis,
            )
            == 0
        )

    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    assert {item["text"] for item in manifest["items"]} == {"bønner", "fisk", "gå"}
    preserved = next(item for item in manifest["items"] if item["text"] == "bønner")
    assert preserved["content_sha256"] == existing_job.content_sha256
    assert preserved["source_lemma_ids"] == [10]


def test_generate_audio_enrich_only_dry_run_does_not_write_articles(tmp_path: Path) -> None:
    import generate_audio

    articles_dir = tmp_path / "articles"
    audio_dir = tmp_path / "audio"
    articles_dir.mkdir()
    audio_dir.mkdir()
    article_path = articles_dir / "1.json"
    article_path.write_text(
        json.dumps(
            {
                "article_id": 1,
                "lemmas": [{"id": 10, "lemma": "bønner"}],
                "body": {"definitions": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest_path = audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json"
    manifest_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "text": "bønner",
                        "file": "clip.mp3",
                        "source_lemma_ids": [10],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = article_path.read_bytes()

    assert (
        generate_audio.run(
            lemma_dir=tmp_path / "lemma",
            audio_dir=audio_dir,
            voice="nb-NO-Chirp3-HD-Aoede",
            language_code="nb-NO",
            dry_run=True,
            limit=None,
            force=False,
            confirm_cost=False,
            price_per_million_chars=30.0,
            articles_dir=articles_dir,
            enrich_only=True,
        )
        == 0
    )
    assert article_path.read_bytes() == before


def test_generate_audio_with_fake_synthesizer_writes_manifest_and_enriches_articles(
    tmp_path: Path,
) -> None:
    import generate_audio

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    lemma_dir.mkdir()
    articles_dir.mkdir()
    (lemma_dir / "1.json").write_text(
        json.dumps(
            {
                "source_article_id": 1,
                "lemmas": [
                    {
                        "lemma": "bønner",
                        "source_lemma_id": 10,
                        "is_sub_article": False,
                        "word_forms": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (articles_dir / "1.json").write_text(
        json.dumps(
            {
                "article_id": 1,
                "lemmas": [{"lemma": "bønner", "id": 10}],
                "body": {"definitions": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_synthesize(_job, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake mp3")

    rc = generate_audio.run(
        lemma_dir=lemma_dir,
        audio_dir=audio_dir,
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
        dry_run=False,
        limit=1,
        force=False,
        confirm_cost=False,
        price_per_million_chars=30.0,
        articles_dir=articles_dir,
        synthesize=fake_synthesize,
    )

    assert rc == 0
    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    item = manifest["items"][0]
    assert item["file"].endswith(".mp3")
    assert item["path"] == f"audio/lemma/google/nb-NO-Chirp3-HD-Aoede/{item['file']}"
    assert item["content_sha256"]
    # Audio fields are embedded into articles/ as the source of truth; export propagates them to lemma/.
    audio = json.loads((articles_dir / "1.json").read_text(encoding="utf-8"))["lemmas"][0]["audio"][
        "lemma"
    ][0]
    assert audio["file"] == item["file"]
    assert audio["content_sha256"] == item["content_sha256"]


def test_generate_audio_manifest_tracks_all_articles_for_shared_audio(tmp_path: Path) -> None:
    import generate_audio

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    lemma_dir.mkdir()
    articles_dir.mkdir()

    for article_id, source_lemma_id in [(1, 10), (2, 20)]:
        (lemma_dir / f"{article_id}.json").write_text(
            json.dumps(
                {
                    "source_article_id": article_id,
                    "lemmas": [
                        {
                            "lemma": "bønner",
                            "source_lemma_id": source_lemma_id,
                            "is_sub_article": False,
                            "word_forms": [],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (articles_dir / f"{article_id}.json").write_text(
            json.dumps(
                {
                    "article_id": article_id,
                    "lemmas": [{"lemma": "bønner", "id": source_lemma_id}],
                    "body": {"definitions": []},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def fake_synthesize(_job, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake mp3")

    rc = generate_audio.run(
        lemma_dir=lemma_dir,
        audio_dir=audio_dir,
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
        dry_run=False,
        limit=2,
        force=False,
        confirm_cost=False,
        price_per_million_chars=30.0,
        articles_dir=articles_dir,
        synthesize=fake_synthesize,
    )

    assert rc == 0
    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert item["article_ids"] == [1, 2]
    assert item["source_lemma_ids"] == [10, 20]

    for article_id in [1, 2]:
        article = json.loads((articles_dir / f"{article_id}.json").read_text(encoding="utf-8"))
        audio = article["lemmas"][0]["audio"]["lemma"][0]
        assert audio["file"] == item["file"]


def test_ordbokene_cli_has_audio_subcommand() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(["audio", "--voice", "nb-NO-Chirp3-HD-Aoede", "--dry-run"])

    assert args.command == "audio"
    assert args.voice == "nb-NO-Chirp3-HD-Aoede"
    assert args.dry_run is True


def test_ordbokene_cli_has_review_subcommand() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(
        ["review", "--review-model", "openai/gpt-5.4", "--limit", "7", "--dry-run"]
    )

    assert args.command == "review"
    assert args.review_model == "openai/gpt-5.4"
    assert args.limit == 7
    assert args.dry_run is True


def test_release_script_builds_both_archives() -> None:
    script = Path("scripts/release.sh").read_text(encoding="utf-8")

    assert "norsk-lemma-${tag}.tar.gz" in script
    assert "norsk-lemma-audio-google-${tag}.tar.gz" in script
    assert "data/export/audio" in script
    assert "gh release create" in script


def test_request_translations_uses_selected_harness(monkeypatch) -> None:
    """config.harness picks the PROVIDERS entry; result keyed back by batch index."""
    from argparse import Namespace

    from ordbokene import client

    captured = {}

    def fake_provider(session, llm_config, prompt, *, max_tokens=0):
        captured["harness"] = llm_config.harness
        captured["model"] = llm_config.model
        return '{"7": {"lemma_primary": "fish", "definitions": []}}'

    monkeypatch.setitem(client.PROVIDERS, "codex", fake_provider)

    batch = [(7, {"article_id": 7, "lemmas": [{"lemma": "fisk"}], "body": {"definitions": []}})]
    config = Namespace(
        model="gpt-x", harness="codex", max_retries=1, retry_delay=0, reasoning_effort="high"
    )
    result = client.request_translations(None, config, batch)

    assert captured == {"harness": "codex", "model": "gpt-x"}
    assert result[0] == {"lemma_primary": "fish", "definitions": []}


def test_request_translations_defaults_to_openrouter(monkeypatch) -> None:
    from argparse import Namespace

    from ordbokene import client

    seen = {}

    def fake_provider(session, llm_config, prompt, *, max_tokens=0):
        seen["harness"] = llm_config.harness
        return "{}"

    monkeypatch.setitem(client.PROVIDERS, "openrouter", fake_provider)

    batch = [(7, {"article_id": 7, "lemmas": [{"lemma": "fisk"}], "body": {"definitions": []}})]
    config = Namespace(model="m", max_retries=1, retry_delay=0)  # no harness attr
    client.request_translations(None, config, batch)
    assert seen["harness"] == "openrouter"


def test_parser_translate_accepts_harness_and_effort() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(
        ["translate", "--harness", "codex", "--reasoning-effort", "high"]
    )
    assert args.harness == "codex"
    assert args.reasoning_effort == "high"


def test_parser_translate_harness_defaults_to_openrouter() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(["translate"])
    assert args.harness == "openrouter"
    assert args.reasoning_effort is None


def test_parser_rejects_unknown_harness() -> None:
    import pytest
    from ordbokene.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["translate", "--harness", "gpt4all"])


def test_request_translations_unknown_harness_returns_error() -> None:
    from argparse import Namespace

    from ordbokene import client

    batch = [(7, {"article_id": 7, "lemmas": [{"lemma": "x"}], "body": {"definitions": []}})]
    config = Namespace(model="m", harness="bogus", max_retries=1, retry_delay=0)
    result = client.request_translations(None, config, batch)
    assert result[0].startswith("unknown_harness")


def test_request_translations_warns_when_effort_ignored(monkeypatch, caplog) -> None:
    import logging
    from argparse import Namespace

    from ordbokene import client

    def fake_provider(session, cfg, prompt, *, max_tokens=0):
        return "{}"

    monkeypatch.setitem(client.PROVIDERS, "claude", fake_provider)
    batch = [(7, {"article_id": 7, "lemmas": [{"lemma": "x"}], "body": {"definitions": []}})]
    config = Namespace(
        model="m", harness="claude", max_retries=1, retry_delay=0, reasoning_effort="high"
    )
    with caplog.at_level(logging.WARNING):
        client.request_translations(None, config, batch)
    assert any("reasoning-effort is ignored" in r.message for r in caplog.records)


def test_request_translation_review_uses_selected_harness(monkeypatch) -> None:
    """Review routes through the same PROVIDERS registry, at temperature 0.0."""
    from argparse import Namespace

    from ordbokene import review
    from ordbokene.review import TranslationReviewItem

    captured = {}

    def fake_provider(session, cfg, prompt, *, max_tokens=0, temperature=0.1):
        captured["harness"] = cfg.harness
        captured["model"] = cfg.model
        captured["temperature"] = temperature
        return '{"issues": []}'

    monkeypatch.setitem(review.PROVIDERS, "codex", fake_provider)
    items = [
        TranslationReviewItem(
            article_id=7,
            lemma="fisk",
            primary_translation="fish",
            definitions=[{"text": "vanndyr", "translation": "aquatic animal"}],
        )
    ]
    config = Namespace(
        review_model="gpt-x", harness="codex", max_retries=1, retry_delay=0, reasoning_effort="high"
    )
    result = review.request_translation_review(None, config, items)

    assert captured == {"harness": "codex", "model": "gpt-x", "temperature": 0.0}
    assert result == {"issues": []}


def test_request_translation_review_unknown_harness_returns_error() -> None:
    from argparse import Namespace

    from ordbokene import review
    from ordbokene.review import TranslationReviewItem

    items = [
        TranslationReviewItem(
            article_id=7,
            lemma="fisk",
            primary_translation="fish",
            definitions=[],
        )
    ]
    config = Namespace(review_model="m", harness="bogus", max_retries=1, retry_delay=0)
    result = review.request_translation_review(None, config, items)
    assert isinstance(result, str) and result.startswith("unknown_harness")


def test_parser_review_accepts_harness() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(
        ["review", "--harness", "droid", "--reasoning-effort", "medium"]
    )
    assert args.harness == "droid"
    assert args.reasoning_effort == "medium"


def _article_with_pos(pos_tag: str) -> dict:
    return {
        "article_id": 1,
        "lemmas": [
            {
                "id": 1,
                "lemma": "test",
                "hgno": 1,
                "paradigm_info": [{"tags": [pos_tag], "inflection": []}],
            }
        ],
        "body": {"definitions": []},
    }


def _article_with_definitions(*contents: str) -> dict:
    return {
        "article_id": 1,
        "lemmas": [
            {
                "id": 1,
                "lemma": "test",
                "hgno": 1,
                "paradigm_info": [{"tags": ["NOUN"], "inflection": []}],
            },
        ],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 100 + i,
                    "elements": [{"type_": "explanation", "content": c, "items": []}],
                }
                for i, c in enumerate(contents)
            ]
        },
    }
