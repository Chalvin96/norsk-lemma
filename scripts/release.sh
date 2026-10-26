#!/usr/bin/env bash
#
# Cut a release: tag HEAD, build the archives locally, publish a GitHub release
# with both tarballs attached.
#
# Audio (data/export/audio/) is gitignored and lives only on this machine, so the
# release MUST be built here — CI has no audio to bundle. That is by design:
# the repo stays light to clone, the heavy MP3s ship as release assets.
#
# Usage:
#   scripts/release.sh v1.4.0 [--build-only] [--allow-dirty]
#
# Produces (under dist/release/, gitignored):
#   norsk-lemma-<tag>.tar.gz                 README + lemma/ + audio/lemma/ (Flyt import bundle)
#   norsk-lemma-audio-google-<tag>.tar.gz    README + data/export/audio/ (audio-only, incl. manifest)
#
set -euo pipefail

tag="${1:-}"
build_only=false
allow_dirty=false
if [[ -z "$tag" ]]; then
  echo "usage: $0 <tag> [--build-only] [--allow-dirty]" >&2
  exit 1
fi
if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: tag must look like v1.2.3 (got: $tag)" >&2
  exit 1
fi

shift
for option in "$@"; do
  case "$option" in
    --build-only) build_only=true ;;
    --allow-dirty) allow_dirty=true ;;
    *) echo "error: unknown option: $option" >&2; exit 1 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# --- preconditions -----------------------------------------------------------
if [[ "$build_only" == false ]]; then
  command -v gh >/dev/null 2>&1 || { echo "error: gh CLI not found" >&2; exit 1; }
  gh auth status >/dev/null 2>&1 || { echo "error: gh not authenticated — run 'gh auth login'" >&2; exit 1; }
fi

if [[ ! -d data/export/audio/lemma ]]; then
  echo "error: data/export/audio/lemma missing — run scripts/generate_audio.py first" >&2
  exit 1
fi
# Refuse if the tag already exists so a failed prior run can't leave a tag we'd silently reuse.
if [[ "$build_only" == false ]] && { git rev-parse -q --verify "refs/tags/${tag}" >/dev/null \
  || [[ -n "$(git ls-remote --tags origin "refs/tags/${tag}" 2>/dev/null)" ]]; }; then
  echo "error: tag ${tag} already exists (local or remote)" >&2
  exit 1
fi
# Refuse a dirty tree so the archive and the release tag can't diverge.
if [[ "$allow_dirty" == false ]] && ! git diff --quiet HEAD --; then
  echo "error: working tree has uncommitted changes (pass --allow-dirty to override)" >&2
  exit 1
fi

echo "Releasing ${tag} from $(git rev-parse --short HEAD) on branch $(git rev-parse --abbrev-ref HEAD)."
echo "Audio source: $(find data/export/audio/lemma -name '*.mp3' | wc -l | tr -d ' ') mp3 files."

# --- build archives ----------------------------------------------------------
out="${repo_root}/dist/release"
mkdir -p "$out"
main_archive="${out}/norsk-lemma-${tag}.tar.gz"
audio_archive="${out}/norsk-lemma-audio-google-${tag}.tar.gz"

echo "Building $(basename "$main_archive") ..."
tar -czf "$main_archive" -C "$repo_root" README.md LICENSE NOTICE docs/data-sources.md \
  -C "$repo_root/data/export" lemma audio/lemma

# Audio-only archive. Lay audio at the archive root (audio/lemma/...) so the
# `path` field in the data resolves identically here and in the main archive.
echo "Building $(basename "$audio_archive") ..."
tar -czf "$audio_archive" -C "$repo_root" README.md LICENSE NOTICE docs/data-sources.md -C "$repo_root/data/export" audio

echo "Archives:"
du -h "$main_archive" "$audio_archive"

if [[ "$build_only" == true ]]; then
  echo "Build complete. No tag or GitHub release was created."
  exit 0
fi

# --- publish -----------------------------------------------------------------
# Let gh create the tag and release together so a failure leaves no dangling remote tag.
echo "Creating GitHub release ${tag} at $(git rev-parse --short HEAD) ..."
gh release create "$tag" "$main_archive" "$audio_archive" \
  --target "$(git rev-parse HEAD)" \
  --title "$tag" \
  --generate-notes

echo "Done. Release ${tag} published with both archives attached."
