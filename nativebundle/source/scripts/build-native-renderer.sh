#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/native/xenoid-renderer"
mkdir -p "$OUT"
exec "$ROOT/scripts/build-native-for-arch.sh" xenoid-renderer \
  "$OUT/xenoid_renderer.c" "$OUT/xenoid-renderer" arm64 static
