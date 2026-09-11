"""Tests for the §0 shared traversal core and the example-translation pipeline.

These tests cover:
  - iter_definition_cores yielding correct raw example element refs for the hard
    cases in data/articles/1.json (same-source_id siblings, sub_definition
    flattening, post-filter index space).
  - extract_existing_translations round-tripping example English.
  - The example-translation prompt, parse/validate, embed, and run flow.
  - apply-review index-matching and threshold.
  - review.py defensive reads.
  - CLI wiring.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Committed fixture (real Ordbokene article 1) so the suite collects without a
# populated, git-ignored data/articles/. Refreshed from
# https://ord.uib.no/bm/article/1.json.
ARTICLE_1 = json.loads(
    (Path(__file__).resolve().parent / "testdata" / "article_1.json").read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# §0 — shared traversal core
# ---------------------------------------------------------------------------


def test_core_surviving_definitions_match_extract_definitions_output() -> None:
    """The core's text and resolved examples must match extract_definitions."""
    from ordbokene.extract import extract_definitions, iter_definition_cores

    cores = iter_definition_cores(ARTICLE_1)
    defs = extract_definitions(ARTICLE_1)

    assert len(cores) == len(defs)
    for core, defn in zip(cores, defs, strict=True):
        assert core["source_id"] == defn["source_id"]
        assert core["text"] == defn["text"]
        assert len(core["example_elements"]) == len(defn["examples"])


def test_core_article_1_source_ids_and_example_counts() -> None:
    """The committed article-1 fixture has 4 surviving defs with these example counts."""
    from ordbokene.extract import iter_definition_cores

    cores = iter_definition_cores(ARTICLE_1)
    assert [(c["source_id"], len(c["example_elements"])) for c in cores] == [
        (2, 3),
        (4, 1),
        (5, 2),
        (6, 1),
    ]


def test_core_same_source_id_siblings_preserve_order() -> None:
    """Two defs sharing a source_id must yield cores in document order.

    Uses a synthetic article so the invariant is pinned deterministically rather
    than depending on the live Ordbokene article 1, which can lose this shape.
    """
    from ordbokene.extract import iter_definition_cores

    raw = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 4,
                    "elements": [
                        {"type_": "explanation", "content": "(note for) sjette tone", "items": []},
                    ],
                },
                {
                    "type_": "definition",
                    "id": 4,
                    "elements": [
                        {"type_": "explanation", "content": "jamfør A-dur og a-moll", "items": []},
                    ],
                },
            ]
        }
    }
    cores = iter_definition_cores(raw)
    sid4 = [c for c in cores if c["source_id"] == 4]
    assert len(sid4) == 2
    assert sid4[0]["text"].startswith("(")  # first in document order
    assert "jamfør" in sid4[1]["text"]  # second in document order


def test_core_example_elements_are_raw_dict_refs() -> None:
    """example_elements must be the actual raw dicts from the article (identity)."""
    from ordbokene.extract import iter_definition_cores

    cores = iter_definition_cores(ARTICLE_1)
    first_core = cores[0]  # source_id=2, "bokstavtegn og språklyd a"
    assert len(first_core["example_elements"]) == 3
    # Each ref must be a dict with type_ == "example" and a quote.content.
    for el in first_core["example_elements"]:
        assert isinstance(el, dict)
        assert el.get("type_") == "example"
        assert isinstance(el.get("quote", {}).get("content"), str)


def test_core_flattens_sub_definition_examples_into_parent_core() -> None:
    """Examples inside sub_definition elements must be flattened into the parent def."""
    from ordbokene.extract import iter_definition_cores

    raw = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "elements": [
                        {
                            "type_": "definition",
                            "id": 2,
                            "elements": [
                                {"type_": "explanation", "content": "main $", "items": []},
                                {"type_": "example", "quote": {"content": "first"}},
                                {
                                    "type_": "definition",
                                    "id": 1,
                                    "sub_definition": True,
                                    "elements": [
                                        {"type_": "explanation", "content": "label"},
                                        {"type_": "example", "quote": {"content": "sub"}},
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ]
        }
    }
    cores = iter_definition_cores(raw)
    assert len(cores) == 1
    assert len(cores[0]["example_elements"]) == 2
    # The sub_definition example must be a distinct raw element.
    el_refs = cores[0]["example_elements"]
    assert el_refs[0] is not el_refs[1]


def test_core_skips_top_level_explanations_without_source_id() -> None:
    """When a def is filtered out (null source_id), surviving def indices must
    not have a gap."""
    from ordbokene.extract import iter_definition_cores

    raw = {
        "body": {
            "definitions": [
                # Top-level explanation: source_id=None → filtered out.
                {"type_": "explanation", "content": "orphan", "items": []},
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "real", "items": []},
                        {"type_": "example", "quote": {"content": "ex1"}},
                    ],
                },
            ]
        }
    }
    cores = iter_definition_cores(raw)
    assert len(cores) == 1  # only the surviving def
    assert cores[0]["source_id"] == 2
    assert len(cores[0]["example_elements"]) == 1


def test_core_empty_text_definition_filtered_out() -> None:
    """An explanation that resolves to empty text must be filtered out."""
    from ordbokene.extract import iter_definition_cores

    raw = {
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "", "items": []},
                        {"type_": "explanation", "content": "real sense", "items": []},
                    ],
                }
            ]
        }
    }
    cores = iter_definition_cores(raw)
    assert len(cores) == 1
    assert cores[0]["text"] == "real sense"


# ---------------------------------------------------------------------------
# §4 — extract_existing_translations round-trip
# ---------------------------------------------------------------------------


def test_extract_existing_translations_includes_example_en() -> None:
    """An article with en on example elements must return examples in the result."""
    from ordbokene.extract import extract_existing_translations

    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "primary_translation": "walk"}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "move on foot",
                            "items": [],
                            "translation": "walk",
                        },
                        {
                            "type_": "example",
                            "quote": {"content": "han gikk hjem"},
                            "en": "he walked home",
                        },
                    ],
                }
            ]
        },
    }
    result = extract_existing_translations(raw)
    assert result is not None
    assert result["definitions"][0]["examples"] == ["he walked home"]


def test_extract_existing_translations_returns_none_when_no_translations_or_en() -> None:
    from ordbokene.extract import extract_existing_translations

    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå"}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "sense", "items": []},
                        {"type_": "example", "quote": {"content": "ex"}},
                    ],
                }
            ]
        },
    }
    assert extract_existing_translations(raw) is None


def test_extract_existing_translations_returns_non_none_with_only_example_en() -> None:
    """Export guard: even without definition translations, example en triggers non-None."""
    from ordbokene.extract import extract_existing_translations

    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå"}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "sense", "items": []},
                        {
                            "type_": "example",
                            "quote": {"content": "han gikk"},
                            "en": "he walked",
                        },
                    ],
                }
            ]
        },
    }
    result = extract_existing_translations(raw)
    assert result is not None
    assert result["definitions"][0]["translation"] == ""
    assert result["definitions"][0]["examples"] == ["he walked"]


def test_build_lemma_round_trips_example_en_from_articles() -> None:
    """Full round-trip: article with en → extract_existing_translations → build_lemma."""
    import translate
    from ordbokene.extract import extract_existing_translations

    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "move on foot",
                            "items": [],
                            "translation": "walk",
                        },
                        {
                            "type_": "example",
                            "quote": {"content": "han gikk hjem"},
                            "en": "he walked home",
                        },
                        {
                            "type_": "example",
                            "quote": {"content": "de gikk fort"},
                            "en": "they walked fast",
                        },
                    ],
                }
            ]
        },
    }
    existing = extract_existing_translations(raw)
    assert existing is not None
    lemma = translate.build_lemma(raw, existing, 1)
    examples = lemma["definitions"][0]["examples"]
    assert examples == [
        {"no": "han gikk hjem", "en": "he walked home"},
        {"no": "de gikk fort", "en": "they walked fast"},
    ]


def test_main_translation_examples_survive_embed_and_export_round_trip() -> None:
    import translate
    from ordbokene.embed import embed_translations
    from ordbokene.extract import extract_existing_translations

    article = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "move", "items": []},
                        {"type_": "example", "quote": {"content": "han gikk"}},
                    ],
                }
            ]
        },
    }
    embed_translations(
        article,
        {
            "lemma_primary": "walk",
            "definitions": [
                {
                    "source_id": 2,
                    "translation": "walk",
                    "examples": ["he walked"],
                }
            ],
        },
    )

    result = extract_existing_translations(article)
    assert result is not None
    exported = translate.build_lemma(article, result, 1)
    assert exported["definitions"][0]["examples"] == [{"no": "han gikk", "en": "he walked"}]


def test_embed_translations_rejects_unmatched_examples_before_mutating() -> None:
    from ordbokene.embed import embed_translations

    article = {
        "lemmas": [],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {"type_": "explanation", "content": "move", "items": []},
                        {"type_": "example", "quote": {"content": "han gikk"}, "en": "old"},
                        {"type_": "example", "quote": {"content": "hun løp"}, "en": "stale"},
                    ],
                }
            ]
        },
    }

    import pytest

    with pytest.raises(ValueError, match="example_cardinality_mismatch"):
        embed_translations(
            article,
            {"definitions": [{"source_id": 2, "translation": "walk", "examples": ["he walked"]}]},
        )

    examples = article["body"]["definitions"][0]["elements"][1:]
    assert [example["en"] for example in examples] == ["old", "stale"]


def test_extract_existing_translations_preserves_all_examples() -> None:
    from ordbokene.extract import extract_existing_translations

    elements: list[dict] = [
        {
            "type_": "explanation",
            "content": "sense",
            "items": [],
            "translation": "walk",
        }
    ]
    for i in range(6):
        elements.append({"type_": "example", "quote": {"content": f"ex{i}"}, "en": f"en{i}"})
    raw = {
        "article_id": 1,
        "lemmas": [{"id": 1, "lemma": "gå", "primary_translation": "walk"}],
        "body": {"definitions": [{"type_": "definition", "id": 2, "elements": elements}]},
    }
    result = extract_existing_translations(raw)
    assert result is not None
    assert len(result["definitions"][0]["examples"]) == 6


# ---------------------------------------------------------------------------
# §1 — examples.py: selection, prompt, parse, validate, embed, run
# ---------------------------------------------------------------------------


def _make_article(article_id: int = 1, examples: list[dict] | None = None) -> dict:
    """Build a minimal article with one definition and the given example elements."""
    ex_elements = examples or [
        {"type_": "example", "quote": {"content": "han gikk"}, "en": ""},
        {"type_": "example", "quote": {"content": "de løp"}, "en": ""},
    ]
    return {
        "article_id": article_id,
        "lemmas": [{"id": 1, "lemma": "gå", "hgno": 1, "paradigm_info": []}],
        "body": {
            "definitions": [
                {
                    "type_": "definition",
                    "id": 2,
                    "elements": [
                        {
                            "type_": "explanation",
                            "content": "move on foot",
                            "items": [],
                            "translation": "walk",
                        },
                        *ex_elements,
                    ],
                }
            ]
        },
    }


def test_select_articles_finds_untranslated_examples(tmp_path: Path) -> None:
    from ordbokene.examples import collect_pending_examples

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    pending = collect_pending_examples(tmp_path, force=False)
    assert len(pending) == 1
    aid, raw, defs = pending[0]
    assert aid == 1
    assert len(defs) == 1  # one definition with untranslated examples
    def_idx, gloss, exs = defs[0]
    assert def_idx == 0
    assert gloss == "move on foot"
    assert len(exs) == 2


def test_select_articles_force_includes_already_translated(tmp_path: Path) -> None:
    from ordbokene.examples import collect_pending_examples

    article = _make_article(
        examples=[
            {"type_": "example", "quote": {"content": "han gikk"}, "en": "he walked"},
        ]
    )
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    # Without force: nothing pending (already has en).
    assert collect_pending_examples(tmp_path, force=False) == []
    # With force: selected.
    pending = collect_pending_examples(tmp_path, force=True)
    assert len(pending) == 1


def test_select_sparse_examples_preserves_source_indices(tmp_path: Path) -> None:
    from ordbokene.examples import collect_pending_examples

    article = _make_article(
        examples=[
            {"type_": "example", "quote": {"content": "han gikk"}, "en": "he walked"},
            {"type_": "example", "quote": {"content": "de løp"}, "en": ""},
        ]
    )
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    pending = collect_pending_examples(tmp_path)
    selected = pending[0][2][0][2]
    assert len(selected) == 1
    assert selected[0][0] == 1
    assert selected[0][1]["quote"]["content"] == "de løp"


def test_sparse_example_backfill_does_not_overwrite_existing_translation(tmp_path: Path) -> None:
    from ordbokene.examples import build_example_prompt, run

    article = _make_article(
        examples=[
            {"type_": "example", "quote": {"content": "han gikk"}, "en": "he walked"},
            {"type_": "example", "quote": {"content": "de løp"}, "en": ""},
        ]
    )
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    def fake_request(_session, _config, batch):
        assert "  1: de løp" in build_example_prompt(batch)
        return {1: {0: {1: "they ran"}}}

    assert run(tmp_path, request_fn=fake_request) == 1
    data = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    examples = [
        e for e in data["body"]["definitions"][0]["elements"] if e.get("type_") == "example"
    ]
    assert [example["en"] for example in examples] == ["he walked", "they ran"]


def test_request_example_translations_round_trips_through_llm_seam(monkeypatch) -> None:
    """Drive the real request path (prompt build + HTTP + parse) via a fake session.

    Behavioral: given a model that returns positional translations, the parsed
    result maps each example index to its English. No assertions on prompt wording.
    """
    from types import SimpleNamespace

    from ordbokene.examples import request_example_translations

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    el0 = {"type_": "example", "quote": {"content": "han gikk hjem"}}
    el1 = {"type_": "example", "quote": {"content": "de gikk fort"}}
    batch = [(1, {}, [(0, "move on foot", [el0, el1])])]

    model_reply = json.dumps({"1": {"0": {"0": "he walked home", "1": "they walked fast"}}})

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"content": model_reply}}]}

    class FakeSession:
        def post(self, *a, **kw) -> FakeResponse:
            return FakeResponse()

    config = SimpleNamespace(model="fake", max_retries=1, retry_delay=0)
    result = request_example_translations(FakeSession(), config, batch)

    assert result == {1: {0: {0: "he walked home", 1: "they walked fast"}}}


def test_request_example_translations_codex_uses_cli(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from ordbokene.examples import request_example_translations_codex

    el0 = {"type_": "example", "quote": {"content": "han gikk hjem"}}
    batch = [(1, {}, [(0, "move on foot", [el0])])]
    calls = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"1": {"0": {"0": "he walked home"}}}), encoding="utf-8")
        return FakeCompleted()

    monkeypatch.setattr("ordbokene.examples.subprocess.run", fake_run)

    config = SimpleNamespace(model="gpt-5.4-low", max_retries=1, retry_delay=0)
    result = request_example_translations_codex(None, config, batch)

    assert result == {1: {0: {0: "he walked home"}}}
    args, kwargs = calls[0]
    assert args[:4] == ["codex", "exec", "--model", "gpt-5.4-low"]
    assert kwargs["input"].startswith("You are an expert Norwegian-to-English translator.")
    assert kwargs["text"] is True


def test_parse_example_response_maps_indices_to_example_translations() -> None:
    from ordbokene.examples import parse_example_response

    el0 = {"type_": "example", "quote": {"content": "han gikk hjem"}}
    el1 = {"type_": "example", "quote": {"content": "de gikk fort"}}
    batch = [
        (
            1,
            {},
            [(0, "walk", [el0, el1])],
        )
    ]
    content = json.dumps({"1": {"0": {"0": "he walked home", "1": "they walked fast"}}})
    result = parse_example_response(content, batch)
    assert result == {1: {0: {0: "he walked home", 1: "they walked fast"}}}


def test_parse_sparse_example_response_preserves_source_index() -> None:
    from ordbokene.examples import parse_example_response

    example = {"type_": "example", "quote": {"content": "de løp"}}
    batch = [(1, {}, [(0, "move fast", [(1, example)])])]

    result = parse_example_response(json.dumps({"1": {"0": {"1": "they ran"}}}), batch)

    assert result == {1: {0: {1: "they ran"}}}


def test_parse_example_response_index_mismatch_skips_def() -> None:
    from ordbokene.examples import parse_example_response

    el0 = {"type_": "example", "quote": {"content": "han gikk hjem"}}
    el1 = {"type_": "example", "quote": {"content": "de gikk fort"}}
    batch = [
        (
            1,
            {},
            [(0, "walk", [el0, el1])],
        )
    ]
    # Returns only 1 example instead of 2 → mismatch, skip the def.
    content = json.dumps({"1": {"0": {"0": "he walked home"}}})
    result = parse_example_response(content, batch)
    assert result == {1: {}}  # def 0 skipped, no padding


def test_validate_rejects_en_equals_no() -> None:
    from ordbokene.examples import _is_valid_translation

    assert not _is_valid_translation("han gikk", "han gikk", "walk")


def test_validate_rejects_en_equals_gloss() -> None:
    from ordbokene.examples import _is_valid_translation

    assert not _is_valid_translation("move on foot", "han gikk", "move on foot")


def test_validate_rejects_leaked_markers() -> None:
    from ordbokene.examples import _is_valid_translation

    assert not _is_valid_translation("he walked\tExample:", "han gikk", "walk")
    assert not _is_valid_translation("Example: he walked", "han gikk", "walk")
    assert not _is_valid_translation("example: he walked", "han gikk", "walk")


def test_validate_accepts_good_translation() -> None:
    from ordbokene.examples import _is_valid_translation

    assert _is_valid_translation("he walked home", "han gikk hjem", "walk")


def test_embed_example_translations_sets_en_on_raw_refs() -> None:
    from ordbokene.examples import embed_example_translations

    article = _make_article()
    translations = {1: {0: {0: "he walked", 1: "they ran"}}}
    embed_example_translations(article, translations)
    cores = [
        c for c in article["body"]["definitions"][0]["elements"] if c.get("type_") == "example"
    ]
    assert cores[0]["en"] == "he walked"
    assert cores[1]["en"] == "they ran"


def test_embed_skips_invalid_translations() -> None:
    from ordbokene.examples import embed_example_translations

    article = _make_article()
    # en == no → rejected for first example.
    translations = {1: {0: {0: "han gikk", 1: "they ran"}}}
    embed_example_translations(article, translations)
    cores = [
        c for c in article["body"]["definitions"][0]["elements"] if c.get("type_") == "example"
    ]
    assert cores[0]["en"] == ""  # rejected, stays empty
    assert cores[1]["en"] == "they ran"


def test_run_dry_run_does_not_write(tmp_path: Path) -> None:
    from ordbokene.examples import run

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    def fake_request(*a, **kw):
        return {1: {0: {0: "he walked", 1: "they ran"}}}

    count = run(tmp_path, dry_run=True, request_fn=fake_request)
    assert count == 0
    data = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    examples = [
        e for e in data["body"]["definitions"][0]["elements"] if e.get("type_") == "example"
    ]
    assert all(e["en"] == "" for e in examples)


def test_run_with_mock_llm_writes_en(tmp_path: Path) -> None:
    from ordbokene.examples import run

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    def fake_request(*a, **kw):
        return {1: {0: {0: "he walked", 1: "they ran"}}}

    count = run(tmp_path, request_fn=fake_request)
    assert count == 1
    data = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    examples = [
        e for e in data["body"]["definitions"][0]["elements"] if e.get("type_") == "example"
    ]
    assert examples[0]["en"] == "he walked"
    assert examples[1]["en"] == "they ran"


def test_run_parallel_translates_all_batches(tmp_path: Path) -> None:
    from ordbokene.examples import run

    for aid in range(1, 7):
        (tmp_path / f"{aid}.json").write_text(json.dumps(_make_article(aid)), encoding="utf-8")

    def fake_request(_session, _config, batch):
        return {aid: {0: {0: "he walked", 1: "they ran"}} for aid, _raw, _defs in batch}

    count = run(tmp_path, request_fn=fake_request, batch_size=1, workers=4)
    assert count == 6
    for aid in range(1, 7):
        data = json.loads((tmp_path / f"{aid}.json").read_text(encoding="utf-8"))
        ex = [e for e in data["body"]["definitions"][0]["elements"] if e.get("type_") == "example"]
        assert ex[0]["en"] == "he walked"


def test_run_stops_on_quota_error_and_is_resumable(tmp_path: Path) -> None:
    from ordbokene.examples import run

    for aid in range(1, 6):
        (tmp_path / f"{aid}.json").write_text(json.dumps(_make_article(aid)), encoding="utf-8")

    # Sequential (workers=1): the 3rd batch hits a quota error → stop before it.
    seen = []

    def fake_request(_session, _config, batch):
        aid = batch[0][0]
        seen.append(aid)
        if aid == 3:
            return "codex_quota: usage limit reached"
        return {aid: {0: {0: "en0", 1: "en1"}} for aid, _raw, _defs in batch}

    count = run(tmp_path, request_fn=fake_request, batch_size=1, workers=1)
    assert count == 2  # only articles 1 and 2 written before the stop
    # Article 3 left untranslated → a resume run picks it up.
    data3 = json.loads((tmp_path / "3.json").read_text(encoding="utf-8"))
    ex3 = [e for e in data3["body"]["definitions"][0]["elements"] if e.get("type_") == "example"]
    assert all(e["en"] == "" for e in ex3)

    def fake_ok(_session, _config, batch):
        return {aid: {0: {0: "en0", 1: "en1"}} for aid, _raw, _defs in batch}

    resumed = run(tmp_path, request_fn=fake_ok, batch_size=1, workers=1)
    assert resumed == 3  # articles 3, 4, 5 remained


def test_run_idempotent(tmp_path: Path) -> None:
    from ordbokene.examples import run

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    call_count = [0]

    def fake_request(*a, **kw):
        call_count[0] += 1
        return {1: {0: {0: "he walked", 1: "they ran"}}}

    run(tmp_path, request_fn=fake_request)
    # Second run: all examples now have en → nothing pending.
    count = run(tmp_path, request_fn=fake_request)
    assert count == 0
    assert call_count[0] == 1  # LLM only called once


def test_run_limit(tmp_path: Path) -> None:
    from ordbokene.examples import run

    for aid in range(1, 4):
        (tmp_path / f"{aid}.json").write_text(json.dumps(_make_article(aid)), encoding="utf-8")

    def fake_request(*a, **kw):
        return {}

    count = run(tmp_path, limit=2, request_fn=fake_request)
    assert count == 0  # nothing written because fake returns {}


def test_run_skips_on_llm_error(tmp_path: Path) -> None:
    from ordbokene.examples import run

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    def fake_request(*a, **kw):
        return "request_error: simulated"

    count = run(tmp_path, request_fn=fake_request)
    assert count == 0
    data = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    examples = [
        e for e in data["body"]["definitions"][0]["elements"] if e.get("type_") == "example"
    ]
    assert all(e["en"] == "" for e in examples)


# ---------------------------------------------------------------------------
# §6 — apply-review
# ---------------------------------------------------------------------------


def test_apply_review_writes_suggested_en(tmp_path: Path) -> None:
    from ordbokene.examples import apply_review

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    review = {
        "issues": [
            {
                "id": 0,
                "article_id": 1,
                "field": "definitions[0].examples[0].en",
                "severity": "medium",
                "suggested_en": "he walked home",
            }
        ]
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    count = apply_review(tmp_path, review_path)
    assert count == 1
    data = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    examples = [
        e for e in data["body"]["definitions"][0]["elements"] if e.get("type_") == "example"
    ]
    assert examples[0]["en"] == "he walked home"


def test_apply_review_threshold_skips_low(tmp_path: Path) -> None:
    from ordbokene.examples import apply_review

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    review = {
        "issues": [
            {
                "id": 0,
                "article_id": 1,
                "field": "definitions[0].examples[0].en",
                "severity": "low",
                "suggested_en": "he walked home",
            }
        ]
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    count = apply_review(tmp_path, review_path, severity_threshold="medium")
    assert count == 0


def test_apply_review_rejects_invalid_field_path(tmp_path: Path) -> None:
    from ordbokene.examples import apply_review

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    review = {
        "issues": [
            {
                "id": 0,
                "article_id": 1,
                "field": "primary_translation",  # not an example field
                "severity": "high",
                "suggested_en": "walk",
            }
        ]
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    count = apply_review(tmp_path, review_path)
    assert count == 0


def test_apply_review_range_check_skips_out_of_range(tmp_path: Path) -> None:
    from ordbokene.examples import apply_review

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    review = {
        "issues": [
            {
                "id": 0,
                "article_id": 1,
                "field": "definitions[5].examples[0].en",  # def_index out of range
                "severity": "high",
                "suggested_en": "he walked home",
            }
        ]
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    count = apply_review(tmp_path, review_path)
    assert count == 0


def test_apply_review_idempotent(tmp_path: Path) -> None:
    from ordbokene.examples import apply_review

    article = _make_article()
    (tmp_path / "1.json").write_text(json.dumps(article), encoding="utf-8")

    review = {
        "issues": [
            {
                "id": 0,
                "article_id": 1,
                "field": "definitions[0].examples[0].en",
                "severity": "medium",
                "suggested_en": "he walked home",
            }
        ]
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    apply_review(tmp_path, review_path)
    apply_review(tmp_path, review_path)  # second run should be safe

    data = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    examples = [
        e for e in data["body"]["definitions"][0]["elements"] if e.get("type_") == "example"
    ]
    assert examples[0]["en"] == "he walked home"


# ---------------------------------------------------------------------------
# §8 — review.py defensive reads
# ---------------------------------------------------------------------------


def test_collect_reviews_coerces_bare_string_examples(tmp_path: Path) -> None:
    from ordbokene.review import collect_translation_reviews

    lemma_dir = tmp_path / "lemma"
    lemma_dir.mkdir()
    (lemma_dir / "1.json").write_text(
        json.dumps(
            {
                "lemmas": [{"lemma": "gå", "primary_translation": "walk"}],
                "definitions": [
                    {
                        "text": "move on foot",
                        "translation": "walk",
                        "examples": ["han gikk hjem", "de gikk fort"],  # bare strings
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    items = collect_translation_reviews(lemma_dir)
    assert len(items) == 1
    assert items[0].definitions[0]["examples"] == [
        {"no": "han gikk hjem", "en": ""},
        {"no": "de gikk fort", "en": ""},
    ]


def test_enrich_review_issues_with_article_id() -> None:
    from ordbokene.review import TranslationReviewItem, _enrich_with_article_ids

    result = {"issues": [{"id": 0, "field": "definitions[0].examples[0].en"}]}
    items = [
        TranslationReviewItem(
            article_id=42,
            lemma="gå",
            primary_translation="walk",
            definitions=[],
        )
    ]
    enriched = _enrich_with_article_ids(result, items)
    assert enriched["issues"][0]["article_id"] == 42


# ---------------------------------------------------------------------------
# §7 — CLI wiring
# ---------------------------------------------------------------------------


def test_cli_has_translate_examples_subcommand() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(
        [
            "translate-examples",
            "--provider",
            "codex",
            "--model",
            "gpt-5.4-low",
            "--limit",
            "5",
            "--dry-run",
        ]
    )
    assert args.command == "translate-examples"
    assert args.provider == "codex"
    assert args.model == "gpt-5.4-low"
    assert args.limit == 5
    assert args.dry_run is True


def test_cli_has_apply_review_subcommand() -> None:
    from ordbokene.cli import build_parser

    args = build_parser().parse_args(
        ["apply-review", "--review", "review.json", "--severity-threshold", "high"]
    )
    assert args.command == "apply-review"
    assert args.severity_threshold == "high"
