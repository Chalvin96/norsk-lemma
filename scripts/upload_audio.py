#!/usr/bin/env python3
"""Upload pronunciation MP3s to the media bucket (any S3-compatible store).

Reads the audio manifest, uploads each clip under its manifest ``path`` key, skips
objects already present, and tags them with a long-lived immutable cache header.

Public URL of an uploaded file is the bucket host + the same path, e.g.

    https://media.umebocchi.my.id/audio/lemma/google/nb-NO-Chirp3-HD-Aoede/6f9920804fe0.mp3

Only the S3 access/secret keys are secret (keep them in .env). The endpoint,
bucket, and public base are public deployment config — no need to hide them.

Usage:
    uv run --group audio python scripts/upload_audio.py            # upload missing
    uv run --group audio python scripts/upload_audio.py --dry-run  # show what would upload
    uv run --group audio python scripts/upload_audio.py --force    # re-upload everything
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from ordbokene.audio import AUDIO_BASE_URL
from tqdm import tqdm

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data"
DEFAULT_MANIFEST = DATA_DIR / "audio" / "manifest-google-nb-NO-Chirp3-HD-Aoede.json"

CONTENT_TYPE = "audio/mpeg"
# Keys are content-addressed and never change, so cache them forever.
CACHE_CONTROL = "public, max-age=31536000, immutable"


def make_client():
    endpoint = os.environ.get("S3_ENDPOINT_URL")
    access_key = os.environ.get("S3_ACCESS_KEY_ID")
    secret_key = os.environ.get("S3_SECRET_ACCESS_KEY")
    missing = [
        name
        for name, value in (
            ("S3_ENDPOINT_URL", endpoint),
            ("S3_ACCESS_KEY_ID", access_key),
            ("S3_SECRET_ACCESS_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        sys.exit(f"error: missing env vars: {', '.join(missing)} (see .env.example)")

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.environ.get("S3_REGION", "us-east-1"),
    )


def object_exists(client, bucket: str, key: str) -> bool:
    """Return False for 403/AccessDenied so a restricted bucket triggers re-upload instead of abort."""
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound", "403", "AccessDenied", "Forbidden"):
            return False
        raise


def safe_key(key: str) -> bool:
    return (
        bool(key)
        and not key.startswith("/")
        and ".." not in key.split("/")
        and key.startswith("audio/")
        and key.endswith(".mp3")
    )


def load_items(manifest_path: Path) -> list[dict]:
    if not manifest_path.exists():
        sys.exit(f"error: manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest.get("items", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dry-run", action="store_true", help="list actions without uploading")
    parser.add_argument("--force", action="store_true", help="re-upload even if the object exists")
    parser.add_argument("--limit", type=int, default=0, help="upload at most N files (0 = all)")
    args = parser.parse_args()

    load_dotenv(REPO / ".env")

    items = load_items(args.manifest)
    if args.limit:
        items = items[: args.limit]

    client = bucket = None
    # Optional per-object ACL. Leave unset for stores that reject ACLs (R2,
    # Garage) and rely on a public-read bucket policy instead.
    extra_args = {"ContentType": CONTENT_TYPE, "CacheControl": CACHE_CONTROL}
    acl = os.environ.get("S3_OBJECT_ACL")
    if acl:
        extra_args["ACL"] = acl
    if not args.dry_run:
        bucket = os.environ.get("S3_BUCKET")
        if not bucket:
            sys.exit("error: missing env var: S3_BUCKET (see .env.example)")
        client = make_client()

    uploaded = skipped = missing_local = bad_key = 0
    for item in tqdm(items, desc="audio", unit="file"):
        key = item.get("path")
        if not key:
            continue
        if not safe_key(key):
            bad_key += 1
            tqdm.write(f"unsafe key, skipping: {key!r}")
            continue
        local = DATA_DIR / key
        if not local.exists():
            missing_local += 1
            tqdm.write(f"missing local file, skipping: {local}")
            continue

        if args.dry_run:
            tqdm.write(f"would upload {key}")
            uploaded += 1
            continue

        if not args.force and object_exists(client, bucket, key):
            skipped += 1
            continue

        client.upload_file(str(local), bucket, key, ExtraArgs=extra_args)
        uploaded += 1

    verb = "would upload" if args.dry_run else "uploaded"
    print(
        f"\n{verb}: {uploaded}  skipped (already present): {skipped}  "
        f"missing local: {missing_local}  unsafe keys: {bad_key}"
    )
    if uploaded and not args.dry_run:
        print(f"public base: {AUDIO_BASE_URL}/<path>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
