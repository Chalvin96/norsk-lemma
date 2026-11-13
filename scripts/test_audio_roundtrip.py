"""Tests for the audio round-trip through articles/.

Audio must persist into articles/ (the source of truth) so that a re-export
reproduces it into lemma/ losslessly, instead of being wiped by
``export --force``. This mirrors the example/definition-translation flow.
"""

import json
from pathlib import Path

from ordbokene.audio import AudioJob, embed_audio_into_articles
from ordbokene.build import build_lemma


def _article(article_id: int, lemma_id: int) -> dict:
    return {
        "article_id": article_id,
        "lemmas": [
            {
                "lemma": "coulomb",
                "hgno": 0,
                "id": lemma_id,
                "paradigm_info": [],
                "pronunciation": [{"tone": 1, "tone_status": "known", "source": "nb_uttale"}],
            }
        ],
        "body": {"definitions": []},
    }


def _job(article_id: int, lemma_id: int) -> AudioJob:
    return AudioJob(
        key=("coulomb", None, 1),
        text="coulomb",
        provider="google",
        voice="nb-NO-Chirp3-HD-Aoede",
        language_code="nb-NO",
        filename="62eb544244cd.mp3",
        article_ids=[article_id],
        source_lemma_ids=[lemma_id],
        tone_status=None,
        tone=1,
        content_sha256="deadbeef",
    )


def test_embed_matches_by_source_lemma_id_not_tone_status(tmp_path: Path) -> None:
    """Embed keys on (article_id, source_lemma_id), so it survives the
    tone_status drift that breaks the manifest-key path."""
    articles = tmp_path / "articles"
    articles.mkdir()
    (articles / "8969.json").write_text(json.dumps(_article(8969, 10622)), encoding="utf-8")

    written = embed_audio_into_articles(articles, [_job(8969, 10622)])
    assert written == 1

    raw = json.loads((articles / "8969.json").read_text())
    audio = raw["lemmas"][0]["audio"]
    assert audio == {"lemma": [audio["lemma"][0]]}
    item = audio["lemma"][0]
    assert item["file"] == "62eb544244cd.mp3"
    assert item["content_sha256"] == "deadbeef"


def test_export_reproduces_embedded_audio(tmp_path: Path) -> None:
    """build_lemma must read the embedded audio back onto the lemma entry."""
    articles = tmp_path / "articles"
    articles.mkdir()
    (articles / "8969.json").write_text(json.dumps(_article(8969, 10622)), encoding="utf-8")
    embed_audio_into_articles(articles, [_job(8969, 10622)])

    raw = json.loads((articles / "8969.json").read_text())
    lemma = build_lemma(raw, {}, 8969)
    assert lemma["lemmas"][0]["audio"]["lemma"][0]["file"] == "62eb544244cd.mp3"


def test_no_audio_key_when_absent(tmp_path: Path) -> None:
    """Lemmas without embedded audio must not carry an audio key at all."""
    raw = _article(8969, 10622)
    lemma = build_lemma(raw, {}, 8969)
    assert "audio" not in lemma["lemmas"][0]


def test_embed_skips_unmatched_lemma_id(tmp_path: Path) -> None:
    """A job whose source_lemma_id is absent from the article writes nothing."""
    articles = tmp_path / "articles"
    articles.mkdir()
    (articles / "8969.json").write_text(json.dumps(_article(8969, 10622)), encoding="utf-8")

    written = embed_audio_into_articles(articles, [_job(8969, 99999)])
    assert written == 0
    raw = json.loads((articles / "8969.json").read_text())
    assert "audio" not in raw["lemmas"][0]


def test_conflicting_jobs_choose_same_clip_in_either_order(tmp_path: Path, capsys) -> None:
    from dataclasses import replace

    articles = tmp_path / "articles"
    articles.mkdir()
    path = articles / "8969.json"
    first = _job(8969, 10622)
    second = replace(first, filename="other.mp3", text="other", content_sha256=None)
    winners = []
    for jobs in ([first, second], [second, first]):
        path.write_text(json.dumps(_article(8969, 10622)), encoding="utf-8")
        embed_audio_into_articles(articles, jobs)
        winners.append(json.loads(path.read_text())["lemmas"][0]["audio"]["lemma"][0]["file"])
        assert "conflict" in capsys.readouterr().out.lower()
    assert winners == [first.filename, first.filename]


def test_current_pronunciation_job_wins_conflict(tmp_path: Path) -> None:
    from dataclasses import replace

    articles = tmp_path / "articles"
    articles.mkdir()
    path = articles / "8969.json"
    path.write_text(json.dumps(_article(8969, 10622)), encoding="utf-8")
    old = _job(8969, 10622)
    current = replace(
        old, key=("coulomb", "known", 2), tone_status="known", tone=2, filename="current.mp3"
    )
    embed_audio_into_articles(articles, [current, old], current_jobs=[current])
    item = json.loads(path.read_text())["lemmas"][0]["audio"]["lemma"][0]
    assert item["file"] == current.filename
