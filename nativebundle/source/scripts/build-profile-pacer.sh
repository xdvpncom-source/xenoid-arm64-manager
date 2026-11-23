#!/usr/bin/env bash
# Build the source-owned BufferQueue admission pacer with the normal NDK owner.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "${1:-arm64}" == arm64 ]] || { echo 'profile pacer requires ARM64' >&2; exit 2; }
source "$ROOT/scripts/android-sdk-root.sh"
SDK="$(xenoid_android_sdk_root)"
NDK="$(xenoid_ndk_require "$SDK")"
PREBUILT="$(xenoid_ndk_prebuilt "$NDK")"
CXX="$PREBUILT/bin/aarch64-linux-android29-clang++"
OUT="$ROOT/native/xenoid-frame-pacer/libgui_pacer.so"
TMP="$OUT.tmp.$$"
trap 'rm -f "$TMP"' EXIT
"$CXX" -std=c++17 -O2 -Wall -Wextra -Werror -fPIC -fvisibility=hidden \
  -shared -static-libstdc++ "$ROOT/native/xenoid-frame-pacer/pacer.cpp" \
  -Wl,-soname,libgui_pacer.so -Wl,-Bsymbolic-functions -Wl,-z,global \
  -Wl,--exclude-libs,ALL -ldl -llog -o "$TMP"
"$PREBUILT/bin/llvm-strip" --strip-unneeded "$TMP"
if "$PREBUILT/bin/llvm-readelf" -d "$TMP" | grep -F 'libc++_shared.so' >/dev/null; then
  echo 'unexpected shared NDK C++ runtime dependency' >&2; exit 1
fi
chmod 0755 "$TMP"
mv -f "$TMP" "$OUT"
trap - EXIT
printf '%s\n' "$OUT"
