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


def test_resume_extracts_on_article_filesystem_and_logs_resume(tmp_path, monkeypatch, caplog):
    import io
    import tarfile

    articles = tmp_path / "articles"
    articles.mkdir()
    (articles / ".fetch-incomplete").touch()
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        info = tarfile.TarInfo("article/1.json")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"{}"))

    temp_paths = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, **_kwargs):
            temp_paths.extend(articles.parent.glob("*.tar.gz"))
            yield content.getvalue()

    monkeypatch.setattr(source.requests, "get", lambda *_args, **_kwargs: Response())
    original = source._flatten_into
    parents = []

    def flatten(src, dst):
        parents.append(src.parent)
        temp_paths.append(src)
        original(src, dst)

    monkeypatch.setattr(source, "_flatten_into", flatten)
    with caplog.at_level("INFO"):
        source.ensure_articles_dir(articles)
    assert parents == [articles.parent]
    assert len(temp_paths) == 2
    assert all(path.name.startswith(".fetch-") for path in temp_paths)
    assert "resuming interrupted download" in caplog.text.lower()
    assert (articles / "1.json").read_text() == "{}"


def test_resume_removes_stale_fetch_files_and_directories(tmp_path, monkeypatch):
    articles = tmp_path / "articles"
    articles.mkdir()
    (articles / ".fetch-incomplete").touch()
    stale_file = tmp_path / ".fetch-old.tar.gz"
    stale_file.write_text("old")
    stale_dir = tmp_path / ".fetch-olddir"
    stale_dir.mkdir()
    (stale_dir / "partial").write_text("old")
    monkeypatch.setattr(source, "_download_and_extract_articles", lambda _: None)

    source.ensure_articles_dir(articles)

    assert not stale_file.exists()
    assert not stale_dir.exists()
