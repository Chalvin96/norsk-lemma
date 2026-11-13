import json
import sys
import types
from pathlib import Path

import pytest


class StubClient:
    def __init__(self, metadata):
        self.metadata = metadata
        self.uploads = []

    def head_object(self, **kwargs):
        return {"Metadata": self.metadata}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.uploads.append((filename, bucket, key, ExtraArgs))


@pytest.mark.parametrize(
    ("stored", "checksum", "uploaded", "replaced"),
    [
        ({"sha256": "new"}, "new", False, False),
        ({"sha256": "old"}, "new", True, True),
        ({}, "new", True, True),
        ({}, None, False, False),
    ],
)
def test_upload_checks_existing_object_checksum(
    tmp_path: Path, monkeypatch, capsys, stored, checksum, uploaded, replaced
):
    boto3 = types.ModuleType("boto3")
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = type("ClientError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    import upload_audio

    key = "audio/lemma/clip.mp3"
    local = tmp_path / "export" / key
    local.parent.mkdir(parents=True)
    local.write_bytes(b"clip")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"items": [{"path": key, "content_sha256": checksum}]}))
    client = StubClient(stored)
    monkeypatch.setattr(upload_audio, "DATA_DIR", tmp_path)
    monkeypatch.setattr(upload_audio, "make_client", lambda: client)
    monkeypatch.setenv("S3_BUCKET", "bucket")
    monkeypatch.setattr(sys, "argv", ["upload_audio.py", "--manifest", str(manifest)])

    assert upload_audio.main() == 0
    assert bool(client.uploads) is uploaded
    if uploaded:
        assert client.uploads[0][3]["Metadata"] == {"sha256": checksum}
    output = capsys.readouterr().out
    if replaced:
        assert "replaced: 1" in output
        assert "purge" in output.lower()
