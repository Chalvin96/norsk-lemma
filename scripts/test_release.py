import os
import shutil
import subprocess
import tarfile
from pathlib import Path


def test_release_given_build_only_expect_archives_with_attribution_and_no_publication(tmp_path: Path) -> None:
    root = tmp_path / "release"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name("release.sh"), scripts / "release.sh")
    for name in ("README.md", "LICENSE", "NOTICE", "docs/data-sources.md"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    lemma = root / "data/export/lemma"
    lemma.mkdir(parents=True)
    (lemma / "1.json").write_text('{"source_article_id":1,"lemmas":[]}')
    audio = root / "data/export/audio/lemma/google/test-voice"
    audio.mkdir(parents=True)
    (audio / "word.mp3").write_bytes(b"audio fixture")
    (root / "data/export/audio/manifest-test.json").write_text('{"items":[]}')
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text('#!/bin/sh\necho called >> "$CALL_LOG"\nexit 99\n')
    gh.chmod(0o755)
    call_log = tmp_path / "gh-calls"
    environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "CALL_LOG": str(call_log)}
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["bash", str(scripts / "release.sh"), "v1.2.3", "--build-only"],
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "No tag or GitHub release was created" in result.stdout
    assert not call_log.exists()
    assert subprocess.check_output(["git", "tag"], cwd=root) == b""
    with tarfile.open(root / "dist/release/norsk-lemma-v1.2.3.tar.gz") as archive:
        assert archive.extractfile("lemma/1.json").read() == b'{"source_article_id":1,"lemmas":[]}'
        assert archive.extractfile("audio/lemma/google/test-voice/word.mp3").read() == b"audio fixture"
        for name in ("LICENSE", "NOTICE", "docs/data-sources.md"):
            assert archive.extractfile(name).read() == name.encode()
    with tarfile.open(root / "dist/release/norsk-lemma-audio-google-v1.2.3.tar.gz") as archive:
        assert archive.extractfile("audio/manifest-test.json").read() == b'{"items":[]}'
        assert archive.extractfile("NOTICE").read() == b"NOTICE"


def test_release_given_unknown_option_expect_error_before_side_effects(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("release.sh")), "v1.2.3", "--build-onyl"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "unknown option" in result.stderr
    assert not (tmp_path / "dist").exists()
