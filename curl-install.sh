#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${GCS_REPOSITORY:-wildesPepega/gamescope-clippy}"
if [[ ! "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    printf 'Invalid GCS_REPOSITORY; expected owner/repository.\n' >&2
    exit 2
fi

for tool in curl tar; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'Required command not found: %s\n' "$tool" >&2
        exit 1
    }
done

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
ARCHIVE="$TEMP_DIR/source.tar.gz"
URL="https://codeload.github.com/${REPOSITORY}/tar.gz/refs/heads/main"
curl --fail --location --silent --show-error "$URL" --output "$ARCHIVE"
tar -xzf "$ARCHIVE" -C "$TEMP_DIR"

SOURCE_DIR="$(find "$TEMP_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print -quit)"
[[ -n "$SOURCE_DIR" && -f "$SOURCE_DIR/install.sh" ]] || {
    printf 'Downloaded archive does not contain install.sh.\n' >&2
    exit 1
}
bash "$SOURCE_DIR/install.sh" "$@"
