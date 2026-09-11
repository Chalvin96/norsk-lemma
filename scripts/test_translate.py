import copy
import json
from argparse import Namespace
from pathlib import Path

import pytest
import translate
from ordbokene import client, pipeline
from ordbokene.build import build_lemma
from ordbokene.cli import _collect_untranslated
from ordbokene.client import validate_translation
from ordbokene.embed import embed_translations
from ordbokene.extract import extract_definitions, extract_existing_translations, source_packet
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


def test_parse_json_response_accepts_only_clean_json() -> None:
    clean = '{"10": {"lemma_primary": "fish"}}'
    fenced = '```json\n{"10": {"lemma_primary": "fish"}}\n```'
    embedded = 'Here you go: {"10": {"lemma_primary": "fish"}} done'

    expected = {0: {"lemma_primary": "fish"}}
    assert translate._parse_json_response(clean, [10]) == expected
    assert translate._parse_json_response(fenced, [10]) == {0: "json_parse_failed"}
    assert translate._parse_json_response(embedded, [10]) == {0: "json_parse_failed"}


def test_parse_json_response_reports_missing_and_parse_failures() -> None:
    assert translate._parse_json_response('{"11": {}}', [10]) == {0: "unexpected_article_id"}
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
    assert '"source_id":2,"text":"anchor hook"' in prompt
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

    assert '"source_id":3,"text":"move on foot"' in prompt
    # All Norwegian examples are shown as read-only sense context.
    assert "han gikk hjem" in prompt
    assert "fjerde eksempel" in prompt
    assert "should be capped" in prompt


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
    assert '"source_id":3,"text":"move on foot"' in prompt


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
    assert '"pos":"VERB"' in prompt


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


def test_build_lemma_given_expression_with_gloss_preserves_primary() -> None:
    # Expressions retain the definition-based memory hook in the existing field.
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
    assert lemma["lemmas"][0]["primary_translation"] == "give up"


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


def test_export_removes_stale_file_when_built_article_has_no_lemmas(tmp_path: Path) -> None:
    from argparse import Namespace

    from ordbokene.cli import cmd_export

    articles_dir = tmp_path / "articles"
    lemma_dir = tmp_path / "lemma"
    articles_dir.mkdir()
    lemma_dir.mkdir()
    (lemma_dir / "1.json").write_text('{"lemmas": [{"lemma": "old"}]}', encoding="utf-8")
    raw = {
        "article_id": 1,
        "lemmas": [],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "a redirect",
                            "items": [],
                        }
                    ],
                }
            ]
        },
    }
    (articles_dir / "1.json").write_text(json.dumps(raw), encoding="utf-8")

    cmd_export(
        Namespace(
            articles_dir=articles_dir,
            lemma_dir=lemma_dir,
            force=False,
            dry_run=False,
            limit=None,
        )
    )

    assert not (lemma_dir / "1.json").exists()


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
        {"source_id": 5, "text": "actual sense", "examples": [], "context": ["refleksivt"]},
    ]


def test_audio_jobs_dedupe_by_text_and_tone_and_skip_expressions(tmp_path: Path) -> None:
    from ordbokene.audio import collect_audio_jobs

    lemma_dir = tmp_path / "lemma"
    _write_lemma_fixture(
        lemma_dir,
        1,
        _lemma_fixture(pronunciation=[{"source": "nb_uttale", "tone": 2, "tone_status": "known"}]),
        _lemma_fixture("ta høyde for", 11, is_sub_article=True),
    )
    _write_lemma_fixture(
        lemma_dir,
        2,
        _lemma_fixture(
            source_lemma_id=12,
            pronunciation=[{"source": "nb_uttale", "tone": 2, "tone_status": "known"}],
        ),
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
    _write_lemma_fixture(
        lemma_dir,
        1,
        _lemma_fixture("tanken", 1, pronunciation=[{"tone": 1, "tone_status": "known"}]),
        _lemma_fixture("tanken", 2, pronunciation=[{"tone": 2, "tone_status": "known"}]),
    )

    jobs = collect_audio_jobs(
        lemma_dir, provider="google", voice="nb-NO-Chirp3-HD-Aoede", language_code="nb-NO"
    )

    assert [(job.text, job.tone) for job in jobs] == [("tanken", 1), ("tanken", 2)]
    assert jobs[0].filename != jobs[1].filename


def test_exported_audio_tone_metadata_matches_schema() -> None:
    lemma_dir = Path(__file__).resolve().parents[1] / "data" / "export" / "lemma"
    assert lemma_dir.is_dir()

    checked = 0
    for path in sorted(lemma_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for lemma in data.get("lemmas", []):
            if not isinstance(lemma, dict):
                continue
            audio = lemma.get("audio")
            if not isinstance(audio, dict):
                continue
            for item in audio.get("lemma", []):
                if not isinstance(item, dict):
                    continue
                checked += 1
                status = item.get("tone_status")
                if status == "known":
                    assert item.get("tone") in (1, 2), (path, item)
                elif status in {"unknown", "none"}:
                    assert item.get("tone") is None, (path, item)
                elif status is not None:
                    raise AssertionError((path, item))
    assert checked > 90_000


def test_generate_audio_dry_run_lists_jobs_without_google_import(tmp_path: Path, capsys) -> None:
    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    _write_lemma_fixture(lemma_dir, 1, _lemma_fixture())

    rc = _run_audio(
        lemma_dir,
        audio_dir,
        tmp_path / "articles",
        dry_run=True,
        confirm_cost=False,
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


def test_generate_audio_retries_transient_synthesis_failure_until_success(
    tmp_path: Path, monkeypatch
) -> None:
    import generate_audio
    from ordbokene.audio import file_sha256

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    _write_lemma_fixture(lemma_dir, 1, _lemma_fixture())
    attempts = 0

    class ServiceUnavailable(Exception):
        pass

    def flaky_synthesis(_job: object, output_path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ServiceUnavailable("service temporarily unavailable")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"recovered audio")

    monkeypatch.setattr(generate_audio.time, "sleep", lambda _seconds: None)

    assert (
        _run_audio(
            lemma_dir,
            audio_dir,
            articles_dir,
            synthesize=flaky_synthesis,
            limit=1,
            confirm_cost=False,
        )
        == 0
    )

    job = _collect_audio_job(lemma_dir)
    manifest = json.loads(
        (audio_dir / f"manifest-google-{_AUDIO_VOICE}.json").read_text(encoding="utf-8")
    )
    assert attempts == 3
    assert manifest["items"][0]["content_sha256"] == file_sha256(
        audio_dir / "lemma" / "google" / _AUDIO_VOICE / job.filename
    )


def test_google_tts_timeout_propagates_and_outer_audio_policy_retries(
    tmp_path: Path, monkeypatch
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    import generate_audio
    from ordbokene.audio import AudioJob
    from ordbokene.google_tts import K_GOOGLE_TTS_TIMEOUT, synthesize_google_mp3

    calls: list[dict[str, object]] = []

    class DeadlineExceeded(Exception):
        pass

    class FakeClient:
        def synthesize_speech(self, **kwargs: object) -> object:
            calls.append(kwargs)
            if len(calls) <= 3:
                raise DeadlineExceeded("request timed out")
            return SimpleNamespace(audio_content=b"recovered audio")

    def build_message(**kwargs: object) -> dict[str, object]:
        return kwargs

    class FakeTextToSpeech:
        AudioEncoding = SimpleNamespace(MP3="MP3")
        AudioConfig = staticmethod(build_message)
        SynthesisInput = staticmethod(build_message)
        VoiceSelectionParams = staticmethod(build_message)
        TextToSpeechClient = FakeClient

    google_module = ModuleType("google")
    cloud_module = ModuleType("google.cloud")
    cloud_module.texttospeech = FakeTextToSpeech
    google_module.cloud = cloud_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)

    direct_output = tmp_path / "direct.mp3"
    direct_job = AudioJob(
        key=("bønner", None, None),
        text="bønner",
        provider="google",
        voice=_AUDIO_VOICE,
        language_code="nb-NO",
        filename="direct.mp3",
    )
    with pytest.raises(DeadlineExceeded, match="timed out"):
        synthesize_google_mp3(direct_job, direct_output)
    assert calls[-1]["retry"] is None
    assert calls[-1]["timeout"] == K_GOOGLE_TTS_TIMEOUT

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    _write_lemma_fixture(lemma_dir, 1, _lemma_fixture())
    monkeypatch.setattr(generate_audio.time, "sleep", lambda _seconds: None)

    assert (
        _run_audio(
            lemma_dir,
            audio_dir,
            articles_dir,
            synthesize=synthesize_google_mp3,
            limit=1,
            confirm_cost=False,
        )
        == 0
    )
    assert len(calls) == 4
    assert all(call["retry"] is None for call in calls)
    assert all(call["timeout"] == K_GOOGLE_TTS_TIMEOUT for call in calls)
    assert (audio_dir / "lemma" / "google" / _AUDIO_VOICE).exists()


def test_generate_audio_rejects_synthesis_without_output(tmp_path: Path) -> None:
    from ordbokene.audio import output_path_for_job

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    _write_lemma_fixture(lemma_dir, 1, _lemma_fixture())
    output_path = output_path_for_job(audio_dir, _collect_audio_job(lemma_dir))
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"previous audio")

    with pytest.raises(RuntimeError, match="without creating nonempty output"):
        _run_audio(
            lemma_dir,
            audio_dir,
            articles_dir,
            synthesize=lambda _job, _path: None,
            limit=1,
            force=True,
            confirm_cost=False,
        )

    assert output_path.read_bytes() == b"previous audio"
    assert not list(output_path.parent.glob(".*.pending"))
    assert not (audio_dir / f"manifest-google-{_AUDIO_VOICE}.json").exists()


def test_generate_audio_exhaustion_raises_without_hashing_or_manifest_update(
    tmp_path: Path, monkeypatch
) -> None:
    import generate_audio
    from ordbokene.audio import output_path_for_job

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    _write_lemma_fixture(lemma_dir, 1, _lemma_fixture())
    job = _collect_audio_job(lemma_dir)
    output_path = output_path_for_job(audio_dir, job)
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"previous audio")
    manifest_path = audio_dir / f"manifest-google-{_AUDIO_VOICE}.json"
    output_before = output_path.read_bytes()
    attempts = 0
    hash_calls = 0

    class ResourceExhausted(Exception):
        pass

    def exhausted_synthesis(_job: object, _output_path: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise ResourceExhausted("quota remains exhausted")

    real_file_sha256 = generate_audio.file_sha256

    def track_hash(path: Path) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return real_file_sha256(path)

    monkeypatch.setattr(generate_audio.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(generate_audio, "file_sha256", track_hash)

    with pytest.raises(ResourceExhausted, match="quota remains exhausted"):
        _run_audio(
            lemma_dir,
            audio_dir,
            articles_dir,
            synthesize=exhausted_synthesis,
            limit=1,
            force=True,
            confirm_cost=False,
        )

    assert attempts == 6
    assert hash_calls == 0
    assert output_path.read_bytes() == output_before
    assert not manifest_path.exists()


def test_generate_audio_no_pending_jobs_retains_valid_manifest_entries(tmp_path: Path) -> None:
    from ordbokene.audio import (
        file_sha256,
        output_path_for_job,
        write_manifest,
    )

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    _write_lemma_fixture(
        lemma_dir,
        1,
        _lemma_fixture(pronunciation=[{"source": "nb_uttale", "tone": 2, "tone_status": "known"}]),
    )
    job = _collect_audio_job(lemma_dir)
    output_path = output_path_for_job(audio_dir, job)
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"existing audio")
    job.content_sha256 = file_sha256(output_path)
    write_manifest(audio_dir, "google", job.voice, job.language_code, [job])

    def fail_synthesis(_job: object, _output_path: Path) -> None:
        raise AssertionError("valid existing audio should not be synthesized")

    assert _run_audio(lemma_dir, audio_dir, articles_dir, synthesize=fail_synthesis) == 0

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

    from ordbokene.audio import file_sha256, output_path_for_job, write_manifest

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    _write_lemma_fixture(lemma_dir, 2, _lemma_fixture(source_lemma_id=20))
    current_job = _collect_audio_job(lemma_dir)
    output_path = output_path_for_job(audio_dir, current_job)
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"existing audio")
    current_job.content_sha256 = file_sha256(output_path)
    old_job = replace(current_job, article_ids=[1], source_lemma_ids=[10])
    write_manifest(audio_dir, "google", current_job.voice, current_job.language_code, [old_job])

    def fail_synthesis(_job: object, _output_path: Path) -> None:
        raise AssertionError("valid existing audio should not be synthesized")

    assert _run_audio(lemma_dir, audio_dir, articles_dir, synthesize=fail_synthesis) == 0

    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    assert manifest["items"][0]["article_ids"] == [1, 2]
    assert manifest["items"][0]["source_lemma_ids"] == [10, 20]


def test_generate_audio_reconstructs_manifest_for_existing_mp3_without_tts(
    tmp_path: Path,
) -> None:
    from ordbokene.audio import file_sha256, output_path_for_job

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    _write_lemma_fixture(lemma_dir, 1, _lemma_fixture())
    job = _collect_audio_job(lemma_dir)
    output_path = output_path_for_job(audio_dir, job)
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"interrupted audio")
    expected_checksum = file_sha256(output_path)

    def fail_synthesis(_job: object, _output_path: Path) -> None:
        raise AssertionError("an existing MP3 without a manifest should be recovered")

    assert _run_audio(lemma_dir, audio_dir, articles_dir, synthesize=fail_synthesis) == 0

    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    assert manifest["items"][0]["file"] == job.filename
    assert manifest["items"][0]["content_sha256"] == expected_checksum
    assert manifest["items"][0]["source_lemma_ids"] == [10]


def test_generate_audio_limited_resumed_runs_retain_previous_manifest_entries(
    tmp_path: Path,
) -> None:
    from ordbokene.audio import (
        collect_audio_jobs,
        file_sha256,
        output_path_for_job,
        write_manifest,
    )

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    _write_lemma_fixture(
        lemma_dir,
        1,
        *[
            _lemma_fixture(text, source_lemma_id)
            for source_lemma_id, text in [(10, "bønner"), (20, "fisk"), (30, "gå")]
        ],
    )
    jobs = collect_audio_jobs(
        lemma_dir, provider="google", voice=_AUDIO_VOICE, language_code="nb-NO"
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
            _run_audio(
                lemma_dir,
                audio_dir,
                articles_dir,
                limit=1,
                confirm_cost=False,
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


def test_generate_audio_backfills_legacy_tone_status_without_renaming_audio(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from ordbokene.audio import (
        file_sha256,
        output_path_for_job,
        write_manifest,
    )

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()
    _write_lemma_fixture(
        lemma_dir,
        1,
        _lemma_fixture(pronunciation=[{"source": "nb_uttale", "tone": 2, "tone_status": "known"}]),
    )
    current = _collect_audio_job(lemma_dir)
    legacy = replace(
        current,
        filename="legacy-bonner.mp3",
        tone_status=None,
        key=("bønner", None, None),
        article_ids=[1],
        source_lemma_ids=[10],
    )
    path = output_path_for_job(audio_dir, legacy)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"legacy audio")
    checksum = file_sha256(path)
    legacy.content_sha256 = checksum
    write_manifest(audio_dir, "google", current.voice, current.language_code, [legacy])

    def fail_synthesis(_job: object, _output_path: Path) -> None:
        raise AssertionError("legacy audio must be reused during metadata backfill")

    assert _run_audio(lemma_dir, audio_dir, articles_dir, synthesize=fail_synthesis) == 0

    manifest = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )
    item = manifest["items"][0]
    assert item["file"] == "legacy-bonner.mp3"
    assert item["tone_status"] == "known"
    assert item["pronunciation_source"] == "nb_uttale"
    assert item["content_sha256"] == checksum
    assert path.read_bytes() == b"legacy audio"


def test_generate_audio_enrich_only_refreshes_legacy_manifest_metadata(
    tmp_path: Path,
) -> None:
    import generate_audio
    from ordbokene.audio import AudioJob, write_manifest

    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    audio_dir.mkdir()
    articles_dir.mkdir()
    _write_lemma_fixture(
        lemma_dir,
        1,
        _lemma_fixture(pronunciation=[{"source": "nb_uttale", "tone": 2, "tone_status": "known"}]),
    )
    legacy = AudioJob(
        key=("bønner", None, None),
        text="bønner",
        provider="google",
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
        filename="legacy-bonner.mp3",
        tone_status=None,
        tone=None,
        source_lemma_ids=[10],
    )
    write_manifest(audio_dir, "google", legacy.voice, legacy.language_code, [legacy])

    assert (
        generate_audio.run(
            lemma_dir=lemma_dir,
            audio_dir=audio_dir,
            voice=legacy.voice,
            language_code=legacy.language_code,
            dry_run=False,
            limit=None,
            force=False,
            confirm_cost=True,
            price_per_million_chars=30.0,
            articles_dir=articles_dir,
            enrich_only=True,
        )
        == 0
    )

    item = json.loads(
        (audio_dir / "manifest-google-nb-NO-Chirp3-HD-Aoede.json").read_text(encoding="utf-8")
    )["items"][0]
    assert item["file"] == "legacy-bonner.mp3"
    assert item["tone_status"] == "known"
    assert item["pronunciation_source"] == "nb_uttale"


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
    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"
    _write_lemma_fixture(lemma_dir, 1, _lemma_fixture())
    _write_article_fixture(articles_dir, 1, 10)

    def fake_synthesize(_job, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake mp3")

    rc = _run_audio(
        lemma_dir,
        audio_dir,
        articles_dir,
        limit=1,
        confirm_cost=False,
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
    lemma_dir = tmp_path / "lemma"
    audio_dir = tmp_path / "audio"
    articles_dir = tmp_path / "articles"

    for article_id, source_lemma_id in [(1, 10), (2, 20)]:
        _write_lemma_fixture(
            lemma_dir,
            article_id,
            _lemma_fixture(source_lemma_id=source_lemma_id),
        )
        _write_article_fixture(articles_dir, article_id, source_lemma_id)

    def fake_synthesize(_job, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake mp3")

    rc = _run_audio(
        lemma_dir,
        audio_dir,
        articles_dir,
        limit=2,
        confirm_cost=False,
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


def test_audio_cli_defaults_match_standalone_entrypoint() -> None:
    import generate_audio
    from ordbokene.cli import build_parser

    unified = build_parser().parse_args(["audio"])
    standalone = generate_audio.build_parser().parse_args([])

    for name in (
        "audio_dir",
        "voice",
        "language_code",
        "enrich_only",
        "confirm_cost",
        "price_per_million_chars",
        "list_voices",
        "workers",
    ):
        assert getattr(unified, name) == getattr(standalone, name)


def test_pronunciation_cli_defaults_match_standalone_entrypoint(monkeypatch) -> None:
    import enrich_pronunciation
    from ordbokene.cli import build_parser

    captured = {}

    def capture_run(input_dir, leksika_path, newwords_path, **options) -> None:
        captured.update(
            leksika=leksika_path,
            newwords=newwords_path,
            workers=options["workers"],
        )

    monkeypatch.setattr(enrich_pronunciation, "run", capture_run)
    enrich_pronunciation.main([])
    unified = build_parser().parse_args(["pronounce"])

    assert captured == {
        "leksika": Path(unified.leksika),
        "newwords": Path(unified.newwords),
        "workers": unified.workers,
    }


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
        return json.dumps({"7": _translation_result()})

    monkeypatch.setitem(client.PROVIDERS, "codex", fake_provider)

    batch = [(7, _translation_article(7))]
    config = Namespace(
        model="gpt-x", harness="codex", max_retries=1, retry_delay=0, reasoning_effort="high"
    )
    result = client.request_translations(None, config, batch)

    assert captured == {"harness": "codex", "model": "gpt-x"}
    assert result[0] == _translation_result()


def test_request_translations_defaults_to_openrouter(monkeypatch) -> None:
    from argparse import Namespace

    from ordbokene import client

    seen = {}

    def fake_provider(session, llm_config, prompt, *, max_tokens=0):
        seen["harness"] = llm_config.harness
        return "{}"

    monkeypatch.setitem(client.PROVIDERS, "openrouter", fake_provider)

    batch = [(7, _translation_article(7))]
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


def _translation_article(aid=1, expression=False, examples=0):
    elements = [{"type_": "explanation", "content": "få en usedvanlig sterk interesse for noe"}]
    elements += [
        {
            "type_": "example",
            "quote": {"content": f"eksempel {aid}.{i}"},
            "en": f"example {aid}.{i}",
        }
        for i in range(examples)
    ]
    lemma = {
        "id": aid,
        "lemma": "bli bitt av basillen" if expression else "interesse",
        "paradigm_info": [{"tags": ["EXPR" if expression else "NOUN"]}],
    }
    definition = {"type_": "definition", "id": 7, "elements": elements}
    return {"article_id": aid, "lemmas": [lemma], "body": {"definitions": [definition]}}


def _translation_result(translation="develop an unusually strong interest in something"):
    return {
        "definitions": [{"source_id": 7, "translation": translation}],
        "lemma_primary": "develop a strong interest",
    }


def _translation_config(tmp_path, **overrides):
    cfg = Namespace(
        model="fixture", harness="codex", max_retries=2, retry_delay=0, cache_dir=tmp_path / "cache"
    )
    vars(cfg).update(overrides)
    return cfg


@pytest.mark.parametrize("expression", [False, True])
def test_partial_batch_retries_only_failed_and_resume_hits_cache(tmp_path, monkeypatch, expression):
    calls = []

    def provider(_session, cfg, prompt, **_kwargs):
        packets = json.loads(prompt.split("Articles to translate:\n")[1])
        ids = [p["article_id"] for p in packets]
        calls.append(ids)
        assert cfg.response_schema["required"] == [str(aid) for aid in ids]
        assert cfg.max_retries == 1
        # Record 2 is missing on the first attempt; record 1 must not be paid for again.
        return json.dumps(
            {str(aid): _translation_result() for aid in ids if len(calls) > 1 or aid == 1}
        )

    monkeypatch.setitem(client.PROVIDERS, "codex", provider)
    batch = [(aid, _translation_article(aid, expression, 6)) for aid in (1, 2)]
    cfg = _translation_config(tmp_path)
    assert client.request_translations(None, cfg, batch) == {
        0: _translation_result(),
        1: _translation_result(),
    }
    assert calls == [[1, 2], [2]]
    assert len(list(cfg.cache_dir.rglob("*.json"))) == 2
    # Change only generated fields: the source fingerprint remains stable.
    embed_translations(batch[0][1], _translation_result())
    assert client.request_translations(None, cfg, batch) == {
        0: _translation_result(),
        1: _translation_result(),
    }
    assert calls == [[1, 2], [2]]
    batch[0][1]["body"]["definitions"][0]["elements"][0]["content"] += " (ofte)"
    client.request_translations(None, cfg, batch)
    assert calls[-1] == [1]
    monkeypatch.setattr(client, "PIPELINE_VERSION", "next-version")
    client.request_translations(None, cfg, batch)
    assert calls[-1] == [1, 2]
    for path in (cfg.cache_dir / "1").glob("*.json"):
        path.write_text("truncated", encoding="utf-8")
    client.request_translations(None, cfg, batch)
    assert calls[-1] == [1]  # Only the corrupt record is requested again.


@pytest.mark.parametrize(
    "bad,error",
    [
        ({"definitions": [], "lemma_primary": "interest"}, "definition_cardinality_mismatch"),
        (
            {"definitions": [{"source_id": 8, "translation": "meaning"}], "lemma_primary": "x"},
            "source_id_mismatch",
        ),
        (_translation_result("  "), "blank_definition"),
        (_translation_result("(lit. be bitten by the bacillus)"), "literal_only_expression"),
        (_translation_result("literally: be bitten by the bacillus"), "literal_only_expression"),
        (
            _translation_result()
            | {"lemma_primary": "develop a strong interest (lit. be bitten by the bacillus)"},
            "literal_in_primary",
        ),
        (
            _translation_result()
            | {"lemma_primary": "(lit. be bitten by the bacillus) develop an interest"},
            "literal_in_primary",
        ),
        (
            _translation_result()
            | {"lemma_primary": "literal translation: be bitten by the bacillus"},
            "literal_in_primary",
        ),
        (
            _translation_result()
            | {"lemma_primary": "develop an interest [literally bitten by a bacillus]"},
            "literal_in_primary",
        ),
        (_translation_result() | {"lemma_primary": ""}, "blank_primary"),
        (_translation_result() | {"examples": ["foreign record"]}, "invalid_record_shape"),
    ],
)
def test_invalid_records_never_cached(tmp_path, monkeypatch, bad, error):
    monkeypatch.setitem(client.PROVIDERS, "codex", lambda *_a, **_kw: json.dumps({"1": bad}))
    assert client.request_translations(
        None, _translation_config(tmp_path), [(1, _translation_article(expression=True))]
    ) == {0: error}
    assert not list((tmp_path / "cache").rglob("*.json"))


def test_repeated_source_ids_require_one_record_per_occurrence():
    raw = _translation_article()
    raw["body"]["definitions"][0]["elements"].append(
        {"type_": "explanation", "content": "annen sans"}
    )
    packet = source_packet(1, raw)
    assert validate_translation(packet, _translation_result()) == "definition_cardinality_mismatch"
    valid = _translation_result()
    valid["definitions"].append({"source_id": 7, "translation": "another sense"})
    assert validate_translation(packet, valid) is None


@pytest.mark.parametrize(
    "lemma",
    [
        "se seg om etter noe/noen",
        "fleske til",
        "dissosiativ lidelse",
        "xx",
        "forverre seg",
        "ta hjem seieren",
        "vanntette skott",
        "gjøre tjeneste som",
        "hemmelig tjeneste",
        "stå til tjeneste",
        "stå/være i tjeneste hos",
    ],
)
def test_source_packet_rejects_corpus_expression_without_definitions(lemma):
    raw = {
        "article_id": 1,
        "word_class": "EXPR",
        "lemmas": [{"lemma": lemma, "paradigm_info": [{"tags": ["EXPR"]}]}],
        "body": {"definitions": []},
    }

    with pytest.raises(ValueError, match="^no_source_definitions$"):
        source_packet(1, raw)


def test_legacy_fresh_bypasses_cache_read_and_replaces_cached_result(tmp_path, monkeypatch):
    import sys

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "translate.py",
            "--fresh",
            "--model",
            "fixture",
            "--cache-dir",
            str(cache_dir),
            "--articles-dir",
            str(tmp_path),
        ],
    )
    fresh = translate.parse_args()
    assert fresh.reuse_existing_translations is False
    assert fresh.reuse_cached_translations is False

    calls = 0

    def provider(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        result = _translation_result()
        result["lemma_primary"] = f"meaning {calls}"
        return json.dumps({"1": result})

    monkeypatch.setitem(client.PROVIDERS, "openrouter", provider)
    raw = _translation_article()
    reusable = copy.copy(fresh)
    reusable.reuse_cached_translations = True

    assert (
        client.request_translations(None, reusable, [(1, raw)])[0]["lemma_primary"] == "meaning 1"
    )
    assert client.request_translations(None, fresh, [(1, raw)])[0]["lemma_primary"] == "meaning 2"
    assert (
        client.request_translations(None, reusable, [(1, raw)])[0]["lemma_primary"] == "meaning 2"
    )
    assert calls == 2


def test_windows_arg_prompt_limit_counts_utf16_and_stops_before_launch(monkeypatch):
    from ordbokene import llm

    calls = []
    monkeypatch.setattr(llm.os, "name", "nt")
    monkeypatch.setattr(llm.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(llm.LlmError) as excinfo:
        llm.complete_cli(
            llm.HARNESSES["opencode"],
            llm.LlmConfig(model="m", retry_delay=0),
            "\U0001f600" * 14_001,
        )

    assert excinfo.value.code == "opencode_prompt_too_large"
    assert "28002 UTF-16 code units" in str(excinfo.value)
    assert calls == []


def test_examples_retained_across_regeneration_and_export_without_new_schema():
    raw = _translation_article(expression=True, examples=7)
    packet = source_packet(1, raw)
    assert len(packet["definitions"][0]["examples"]) == 7
    translated = _translation_result(
        "develop an unusually strong interest in something (lit. be bitten by the bacillus)"
    )
    assert validate_translation(packet, translated) is None
    embed_translations(raw, translated)
    for response in (translated, extract_existing_translations(raw)):
        exported = build_lemma(raw, response, 1)
        assert len(exported["definitions"][0]["examples"]) == 7
        assert exported["definitions"][0]["examples"][-1]["en"] == "example 1.6"
        assert set(exported["definitions"][0]) == {"text", "translation", "examples"}
        assert exported["lemmas"][0]["primary_translation"] == translated["lemma_primary"]
    before = copy.deepcopy(raw)
    embed_translations(raw, _translation_result("") | {"lemma_primary": ""})
    assert raw == before


def test_definition_call_rejects_cross_record_example_output():
    packet = source_packet(1, _translation_article(examples=1))
    bad = _translation_result()
    bad["definitions"][0]["examples"] = ["another record's English"]
    assert validate_translation(packet, bad) == "invalid_definition_shape"


def test_example_only_sibling_cannot_attach_to_previous_sense():
    raw = _translation_article(examples=1)
    raw["body"]["definitions"].append(
        {
            "type_": "definition",
            "id": 8,
            "elements": [{"type_": "example", "quote": {"content": "unrelated example"}}],
        }
    )
    assert extract_definitions(raw)[0]["examples"] == ["eksempel 1.0"]
    with pytest.raises(ValueError, match="unattached_or_reordered"):
        source_packet(1, raw)


def test_reference_inherits_only_explicit_sense_and_own_examples(tmp_path):
    target = _translation_article(2, examples=1)
    target["body"]["definitions"].append(
        {
            "type_": "definition",
            "id": 99,
            "elements": [{"type_": "explanation", "content": "unrelated sense"}],
        }
    )
    (tmp_path / "2.json").write_text(json.dumps(target), encoding="utf-8")
    raw = _translation_article(examples=1)
    explanation = raw["body"]["definitions"][0]["elements"][0]
    explanation.update(
        content="$",
        items=[
            {
                "type_": "article_ref",
                "article_id": 2,
                "definition_id": 7,
                "lemmas": [{"lemma": "interesse"}],
            }
        ],
    )
    packet = source_packet(1, raw, tmp_path)
    assert packet["definitions"][0]["source_id"] == 7
    assert packet["definitions"][0]["examples"] == ["eksempel 1.0"]
    assert "unrelated" not in packet["definitions"][0]["text"]
    del explanation["items"][0]["definition_id"]
    with pytest.raises(ValueError, match="unresolved_semantic_reference"):
        source_packet(1, raw, tmp_path)
    explanation["content"] = "til forskjell fra $"
    assert (
        source_packet(1, raw, tmp_path)["definitions"][0]["text"] == "til forskjell fra interesse"
    )


def test_strict_json_rejects_duplicate_keys_and_foreign_ids():
    assert client._parse_json_response('{"1":{},"1":{}}', [1]) == {0: "json_parse_failed"}
    assert client._parse_json_response('{"2":{}}', [1]) == {0: "unexpected_article_id"}


def test_partial_embedded_record_is_pending_and_default_batch_is_small():
    raw = _translation_article()
    raw["lemmas"][0]["primary_translation"] = "interest"
    assert _collect_untranslated([(1, raw)], False) == [(1, raw)]


def test_hydration_matches_source_examples_and_retains_new_senses(tmp_path, monkeypatch):
    from ordbokene import cli

    articles = tmp_path / "articles"
    lemmas = tmp_path / "lemma"
    articles.mkdir()
    lemmas.mkdir()
    raw = _translation_article(examples=6)
    raw["body"]["definitions"].append(
        {
            "type_": "definition",
            "id": 8,
            "elements": [
                {"type_": "explanation", "content": "ny betydning", "translation": "new sense"}
            ],
        }
    )
    (articles / "1.json").write_text(json.dumps(raw), encoding="utf-8")
    (lemmas / "1.json").write_text(
        json.dumps(
            {
                "lemmas": [{"primary_translation": "interest"}],
                "definitions": [
                    {
                        "text": extract_definitions(raw)[0]["text"],
                        "translation": "interest",
                        "examples": [
                            {"no": "another record", "en": "foreign English"},
                            {"no": "eksempel 1.4", "en": "updated example"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_download_lemma_release", lambda *_args: None)
    cli.cmd_hydrate(
        Namespace(articles_dir=articles, lemma_dir=lemmas, force=True, dry_run=False, limit=None)
    )
    hydrated = json.loads((articles / "1.json").read_text(encoding="utf-8"))
    embedded = extract_existing_translations(hydrated)
    assert embedded["definitions"][0]["examples"] == [
        "example 1.0",
        "example 1.1",
        "example 1.2",
        "example 1.3",
        "updated example",
        "example 1.5",
    ]
    assert embedded["definitions"][1]["translation"] == "new sense"


def test_quota_preserves_prior_success_and_stops_retries(tmp_path, monkeypatch):
    from ordbokene.llm import LlmError

    calls = []

    def provider(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return json.dumps({"1": _translation_result()})
        raise LlmError("quota", "limit reached")

    monkeypatch.setitem(client.PROVIDERS, "codex", provider)
    cfg = _translation_config(tmp_path, max_retries=3)
    received = client.request_translations(
        None, cfg, [(1, _translation_article()), (2, _translation_article(2))]
    )
    assert received[0] == _translation_result()
    assert received[1] == "quota: limit reached"
    assert len(calls) == 2
    assert len(list(cfg.cache_dir.rglob("*.json"))) == 1


def test_reference_without_display_lemma_is_not_silently_discarded():
    raw = _translation_article()
    raw["body"]["definitions"][0]["elements"][0].update(
        content="$", items=[{"type_": "article_ref", "article_id": 2}]
    )
    with pytest.raises(ValueError, match="unresolved_semantic_reference"):
        source_packet(1, raw)


@pytest.mark.parametrize(
    "bad",
    [
        {"definitions": []},
        {"definitions": [{"source_id": 99, "translation": "wrong record"}]},
        {"definitions": [{"source_id": 7, "translation": "interest", "examples": ["one"]}]},
    ],
)
def test_build_rejects_misaligned_records_without_mutating_source(bad):
    raw = _translation_article(examples=2)
    before = copy.deepcopy(raw)
    with pytest.raises(ValueError, match="mismatch"):
        build_lemma(raw, bad, 1)
    assert raw == before


def _reference_article(aid, target, sid=7):
    raw = _translation_article(aid, examples=1)
    raw["body"]["definitions"][0]["elements"][0].update(
        content="$",
        items=[
            {
                "type_": "article_ref",
                "article_id": target,
                "definition_id": sid,
                "lemmas": [{"lemma": "alias"}],
            }
        ],
    )
    return raw


def test_unscoped_reference_requires_one_extracted_definition(tmp_path):
    raw = _reference_article(1, 2, None)
    target = _translation_article(2)
    path = tmp_path / "2.json"
    path.write_text(json.dumps(target), encoding="utf-8")
    packet = source_packet(1, raw, tmp_path)
    assert packet["definitions"][0]["references"] == [[2, 7]]
    assert packet["definitions"][0]["examples"] == ["eksempel 1.0"]
    target["body"]["definitions"][0]["elements"].append(
        {"type_": "explanation", "content": "another meaning under the SAME source ID"}
    )
    path.write_text(json.dumps(target), encoding="utf-8")
    with pytest.raises(ValueError, match="unscoped_target_not_single_definition target=2:None"):
        source_packet(1, raw, tmp_path)


@pytest.mark.parametrize("marker", ["t_forsk_f", "mots", "fork"])
def test_typed_lexical_reference_never_loads_target(tmp_path, marker):
    raw = _reference_article(1, 999, None)
    explanation = raw["body"]["definitions"][0]["elements"][0]
    explanation["content"] = "$ $"
    explanation["items"].insert(0, {"type_": "entity", "id": marker})
    packet = source_packet(1, raw, tmp_path)
    assert packet["definitions"][0]["text"] == f"{translate.ABBREVIATIONS[marker]} alias"
    assert "references" not in packet["definitions"][0]


def test_reference_chain_provenance_and_terminal_cache_invalidation(tmp_path, monkeypatch):
    raw = _reference_article(1, 2)
    target = _translation_article(3)
    for aid, entry in [(2, _reference_article(2, 3)), (3, target)]:
        (tmp_path / f"{aid}.json").write_text(json.dumps(entry), encoding="utf-8")
    packet = source_packet(1, raw, tmp_path)
    assert packet["definitions"][0]["references"] == [[2, 7], [3, 7]]
    calls = []

    def provider(*_args, **_kwargs):
        calls.append(1)
        return json.dumps({"1": _translation_result()})

    monkeypatch.setitem(client.PROVIDERS, "codex", provider)
    cfg = _translation_config(tmp_path, articles_dir=tmp_path)
    client.request_translations(None, cfg, [(1, raw)])
    client.request_translations(None, cfg, [(1, raw)])
    assert len(calls) == 1
    target["body"]["definitions"][0]["elements"][0]["content"] += " (ofte)"
    (tmp_path / "3.json").write_text(json.dumps(target), encoding="utf-8")
    client.request_translations(None, cfg, [(1, raw)])
    assert len(calls) == 2
    (tmp_path / "3.json").write_text(json.dumps(_reference_article(3, 2)), encoding="utf-8")
    with pytest.raises(ValueError, match="cycle target=2:7"):
        source_packet(1, raw, tmp_path)


def test_local_target_definition_does_not_follow_extra_cyclic_alias(tmp_path):
    target = _translation_article(2)
    target["body"]["definitions"][0]["elements"].append(
        _reference_article(2, 2)["body"]["definitions"][0]["elements"][0]
    )
    (tmp_path / "2.json").write_text(json.dumps(target), encoding="utf-8")
    packet = source_packet(1, _reference_article(1, 2), tmp_path)
    assert packet["definitions"][0]["text"] == "få en usedvanlig sterk interesse for noe"
    assert packet["definitions"][0]["references"] == [[2, 7]]


def test_explicit_subdefinition_is_selected_without_parent_or_target_examples(tmp_path):
    target = _translation_article(2, examples=1)
    sub = {
        "type_": "definition",
        "id": 9,
        "sub_definition": True,
        "elements": [{"type_": "explanation", "content": "specific subordinate meaning"}],
    }
    target["body"]["definitions"][0]["elements"].append(sub)
    (tmp_path / "2.json").write_text(json.dumps(target), encoding="utf-8")
    packet = source_packet(1, _reference_article(1, 2, 9), tmp_path)
    assert packet["definitions"][0]["text"] == "specific subordinate meaning"
    assert packet["definitions"][0]["references"] == [[2, 9]]
    assert packet["definitions"][0]["examples"] == ["eksempel 1.0"]
    sub["elements"] = [{"type_": "explanation", "content": "brukt som adverb:"}]
    (tmp_path / "2.json").write_text(json.dumps(target), encoding="utf-8")
    with pytest.raises(ValueError, match="no_target_definition target=2:9"):
        source_packet(1, _reference_article(1, 2, 9), tmp_path)


def test_reference_depth_missing_ids_and_multiple_targets_fail_closed(tmp_path):
    for aid in range(2, 12):
        (tmp_path / f"{aid}.json").write_text(
            json.dumps(_reference_article(aid, aid + 1)), encoding="utf-8"
        )
    with pytest.raises(ValueError, match="depth_limit target=10:7"):
        source_packet(1, _reference_article(1, 2), tmp_path)
    with pytest.raises(ValueError, match="missing_target_article target=99:7"):
        source_packet(1, _reference_article(1, 99), tmp_path)
    with pytest.raises(ValueError, match="unmatched_definition_id target=2:99"):
        source_packet(1, _reference_article(1, 2, 99), tmp_path)
    raw = _reference_article(1, 2)
    explanation = raw["body"]["definitions"][0]["elements"][0]
    explanation["content"] = "$, $"
    explanation["items"].append({"type_": "article_ref", "article_id": 3, "definition_id": 7})
    with pytest.raises(ValueError, match="multi_target"):
        source_packet(1, raw, tmp_path)


def test_translate_id_file_limits_writes_and_rejects_missing_ids(tmp_path, monkeypatch):
    from ordbokene import cli

    ids_file = tmp_path / "ids.json"
    ids_file.write_text("[2]", encoding="utf-8")
    args = cli.build_parser().parse_args(
        [
            "translate",
            "--articles-dir",
            str(tmp_path),
            "--article-ids-file",
            str(ids_file),
            "--force",
        ]
    )
    monkeypatch.setattr(cli, "ensure_articles_dir", lambda *_args: None)
    monkeypatch.setattr(
        cli, "explode", lambda *_args: [(aid, _translation_article(aid)) for aid in (1, 2)]
    )

    def request(_session, _args, batch):
        assert [aid for aid, _ in batch] == [2]
        return {0: _translation_result()}

    monkeypatch.setattr(cli, "request_translations", request)
    cli.cmd_translate(args)
    assert (tmp_path / "2.json").exists() and not (tmp_path / "1.json").exists()
    ids_file.write_text("[999]", encoding="utf-8")
    with pytest.raises(ValueError, match="selected article IDs not found"):
        cli.cmd_translate(args)


_AUDIO_VOICE = "nb-NO-Chirp3-HD-Aoede"


def _write_lemma_fixture(
    lemma_dir: Path,
    article_id: int,
    *lemmas: dict,
) -> None:
    lemma_dir.mkdir(parents=True, exist_ok=True)
    (lemma_dir / f"{article_id}.json").write_text(
        json.dumps(
            {"source_article_id": article_id, "lemmas": list(lemmas)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _lemma_fixture(
    text: str = "bønner",
    source_lemma_id: int = 10,
    *,
    pronunciation: list[dict] | None = None,
    is_sub_article: bool = False,
) -> dict:
    return {
        "lemma": text,
        "source_lemma_id": source_lemma_id,
        "is_sub_article": is_sub_article,
        "word_forms": []
        if pronunciation is None
        else [{"word_form": text, "pronunciation": pronunciation}],
    }


def _write_article_fixture(articles_dir: Path, article_id: int, source_lemma_id: int) -> None:
    articles_dir.mkdir(parents=True, exist_ok=True)
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


def _collect_audio_job(lemma_dir: Path):
    from ordbokene.audio import collect_audio_jobs

    return collect_audio_jobs(
        lemma_dir,
        provider="google",
        voice=_AUDIO_VOICE,
        language_code="nb-NO",
    )[0]


def _run_audio(
    lemma_dir: Path,
    audio_dir: Path,
    articles_dir: Path,
    *,
    synthesize=None,
    limit: int | None = None,
    force: bool = False,
    confirm_cost: bool = True,
    **kwargs: object,
) -> int:
    import generate_audio

    options = {
        "lemma_dir": lemma_dir,
        "audio_dir": audio_dir,
        "voice": _AUDIO_VOICE,
        "language_code": "nb-NO",
        "dry_run": False,
        "limit": limit,
        "force": force,
        "confirm_cost": confirm_cost,
        "price_per_million_chars": 30.0,
        "articles_dir": articles_dir,
    }
    if synthesize is not None:
        options["synthesize"] = synthesize
    options.update(kwargs)
    return generate_audio.run(**options)
