from pathlib import Path

import pytest
from ordbokene import source


def test_interrupted_fetch_resumes_without_overwriting_articles(
    tmp_path: Path, monkeypatch
) -> None:
    articles = tmp_path / "articles"
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "1.json").write_text("original")
    (extracted / "2.json").write_text("second")
    calls = 0

    def download(destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            (destination / "1.json").write_text("original")
            raise RuntimeError("interrupted")
        source._flatten_into(extracted, destination)

    monkeypatch.setattr(source, "_download_and_extract_articles", download)
    with pytest.raises(RuntimeError, match="interrupted"):
        source.ensure_articles_dir(articles)
    (articles / "1.json").write_text("enriched")
    source.ensure_articles_dir(articles)
    assert calls == 2
    assert (articles / "1.json").read_text() == "enriched"
    assert (articles / "2.json").read_text() == "second"
    assert not (articles / ".fetch-incomplete").exists()
