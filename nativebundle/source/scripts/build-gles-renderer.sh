#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/native/xenoid-renderer"
mkdir -p "$OUT"
ORIGINAL="$(python3 "$ROOT/scripts/renderer-platform-library.py")"
LABEL=(--label "${XENOID_GLES_RENDERER-Mali-G78}")
[[ -n "${XENOID_GLES_RENDERER-Mali-G78}" ]] || LABEL=(--native)
python3 "$ROOT/scripts/profile-gles-renderer.py" \
  --original "$ORIGINAL" --output "$OUT/libGLESv2_angle.so" \
  --manifest "$OUT/renderer-label.json" "${LABEL[@]}"
