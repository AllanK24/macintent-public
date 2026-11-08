#!/bin/bash
# Run with: /bin/bash scripts/dev /bin/bash scripts/bootstrap.sh
# Downloads go only into project directories; no installer shell scripts run.
set -euo pipefail
: "${MACINTENT_ROOT:?Run this through scripts/dev}"
ROOT="$MACINTENT_ROOT"
if [ "${MACINTENT_LAUNCHER_ACTIVE:-}" != 1 ] || [ "${HOME:-}" != "$ROOT/.local/home" ]; then
  echo "Run bootstrap through scripts/dev for workspace-contained storage" >&2
  exit 1
fi
if [ "$(pwd -P)" != "$ROOT" ]; then
  echo "Bootstrap must run from the project root" >&2
  exit 1
fi
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo "This bootstrap targets native Apple Silicon macOS" >&2
  exit 1
fi

verify_sha256() {
  local expected="$1" file="$2" actual
  actual="$(shasum -a 256 "$file")"
  actual="${actual%% *}"
  if [ "$actual" != "$expected" ]; then
    echo "Checksum mismatch: $file" >&2
    exit 1
  fi
}

UV_VERSION=0.12.21
MISE_VERSION=2026.9.18
WITH_NODE=0
if [ "$#" -eq 1 ] && [ "$1" = --with-node ]; then
  WITH_NODE=1
elif [ "$#" -ne 0 ]; then
  echo 'Usage: scripts/bootstrap.sh [--with-node]' >&2
  exit 2
fi
UV_ARCHIVE="$ROOT/.cache/bootstrap/uv-${UV_VERSION}-aarch64-apple-darwin.tar.gz"
MISE_BINARY="$ROOT/.cache/bootstrap/mise-${MISE_VERSION}-macos-arm64"
for file in "$UV_ARCHIVE" "$MISE_BINARY"; do
  if [ -L "$file" ] || [ -d "$file" ]; then
    echo "Refusing redirected bootstrap artifact: $file" >&2
    exit 1
  fi
done
UV_TEMP="$(mktemp "$ROOT/.cache/bootstrap/uv-download.XXXXXX")"
curl --fail --location --retry 3 --output "$UV_TEMP" \
  "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-apple-darwin.tar.gz"
verify_sha256 b88bda573e566ef9bced66b155fe0408626fbbc053aee1c30ba686f0728c9447 "$UV_TEMP"
mv "$UV_TEMP" "$UV_ARCHIVE"
EXTRACT="$(mktemp -d "$ROOT/.cache/bootstrap/uv-${UV_VERSION}.XXXXXX")"
tar -xzf "$UV_ARCHIVE" -C "$EXTRACT"
cp "$EXTRACT/uv-aarch64-apple-darwin/uv" "$ROOT/.local/bin/uv"
cp "$EXTRACT/uv-aarch64-apple-darwin/uvx" "$ROOT/.local/bin/uvx"
chmod u+x "$ROOT/.local/bin/uv" "$ROOT/.local/bin/uvx"
uv --version
if [ "$WITH_NODE" -eq 1 ]; then
  MISE_TEMP="$(mktemp "$ROOT/.cache/bootstrap/mise-download.XXXXXX")"
  curl --fail --location --retry 3 --output "$MISE_TEMP" \
    "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-macos-arm64"
  verify_sha256 484c135bd4329975d608d3f77e26c2ece5d2f5590f18ca71f44440294f8cfa6f "$MISE_TEMP"
  mv "$MISE_TEMP" "$MISE_BINARY"
  cp "$MISE_BINARY" "$ROOT/.local/bin/mise"
  chmod u+x "$ROOT/.local/bin/mise"
  mise --version
  mise trust "$ROOT/mise.toml"
  mise install node
  mise exec -- node --version
fi
uv python install 3.12.14
uv python find 3.12.14
