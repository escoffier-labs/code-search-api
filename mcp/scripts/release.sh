#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--publish" ]]; then
  cat <<'USAGE'
Usage: scripts/release.sh --publish

Runs verification, publishes the current package version to npm, packs the exact
npm artifact into /tmp, extracts it, and publishes that extracted package to
ClawHub with source provenance pointing at escoffier-labs/code-search-mcp.

Set SKIP_SMOKE=1 to skip the live code-search-api smoke test.
USAGE
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(node -p "require('$ROOT/package.json').version")"
SHA="$(git -C "$ROOT" rev-parse HEAD)"
PACK_DIR="/tmp/code-search-mcp-release-$VERSION"
TARBALL="/tmp/solomonneas-code-search-mcp-$VERSION.tgz"

cd "$ROOT"

npm run typecheck
npm test
npm run build

if [[ "${SKIP_SMOKE:-0}" != "1" ]]; then
  npm run smoke
fi

npm publish --access public

rm -rf "$PACK_DIR" "$TARBALL"
npm pack --pack-destination /tmp
mkdir -p "$PACK_DIR"
tar -xzf "$TARBALL" -C "$PACK_DIR" --strip-components=1

(
  cd "$PACK_DIR"
  npx clawhub --workdir . package publish . \
    --family code-plugin \
    --source-repo escoffier-labs/code-search-mcp \
    --source-commit "$SHA"
)
