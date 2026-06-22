import json
from argparse import Namespace
from pathlib import Path

import translate
from ordbokene import pipeline


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
                            "should be capped",
                        ],
                    }
                ],
            }
        ]
    )

    assert "source_id 3: move on foot" in prompt
    assert "example: han gikk hjem" in prompt
    assert "example: de gikk fort" in prompt
    assert "should be capped" not in prompt


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


def test_build_lemma_pos_prep() -> None:
    lemma = translate.build_lemma(_article_with_pos("PREP"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "PREP"


def test_build_lemma_pos_pron() -> None:
    lemma = translate.build_lemma(_article_with_pos("PRON"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "PRON"


def test_build_lemma_pos_conj() -> None:
    lemma = translate.build_lemma(_article_with_pos("CONJ"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "CONJ"


def test_build_lemma_pos_interj() -> None:
    lemma = translate.build_lemma(_article_with_pos("INTERJ"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "INTERJ"


def test_build_lemma_pos_det() -> None:
    lemma = translate.build_lemma(_article_with_pos("DET"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "DET"


def test_build_lemma_pos_num() -> None:
    lemma = translate.build_lemma(_article_with_pos("NUM"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "NUM"


def test_build_lemma_pos_unknown_tag_falls_back_to_unknown() -> None:
    lemma = translate.build_lemma(_article_with_pos("ZZUNKNOWN"), {}, 1)
    assert lemma["lemmas"][0]["pos"] == "UNKNOWN"


def test_build_lemma_produces_correct_shape() -> None:
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


def test_build_lemma_handles_redirect() -> None:
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


def test_render_item_handles_fraction() -> None:
    item = {"numerator": 1, "denominator": 2}
    assert translate._render_item(item) == "1/2"


def test_render_item_handles_language_abbreviation(monkeypatch) -> None:
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


def test_build_lemma_given_hgno_zero_preserves_zero() -> None:
    raw_dict = {
        "article_id": 200001,
        "lemmas": [{"id": 1, "lemma": "en", "hgno": 0, "paradigm_info": []}],
        "body": {"definitions": []},
    }
    lemma = translate.build_lemma(raw_dict, {}, 200001)
    assert lemma["lemmas"][0]["hgno"] == 0


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
