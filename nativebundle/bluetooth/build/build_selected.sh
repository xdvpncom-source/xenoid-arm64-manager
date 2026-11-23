#!/usr/bin/env bash
# Run only inside the separately provisioned, bounded Linux x86_64 builder.
# This script never deploys an artifact or operates on an Android container.
set -eo pipefail
if [[ $# != 2 || ! "$1" =~ ^(graph|build)$ ]]; then
  echo "usage: $0 graph|build ABSOLUTE_OWNED_SOURCE_ROOT" >&2
  exit 64
fi
PHASE="$1"
SOURCE_ROOT="$2"
[[ "$SOURCE_ROOT" = /* && -f "$SOURCE_ROOT/.xenoid-bluetooth-build-owner.json" ]] || {
  echo "owned source checkout marker missing" >&2; exit 65;
}
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo "matching AOSP host prebuilts require Linux x86_64" >&2; exit 65;
}
cd "$SOURCE_ROOT"
[[ -x prebuilts/clang/host/linux-x86/clang-r450784d/bin/clang ]] || exit 66
[[ -x prebuilts/rust/linux-x86/1.59.0/bin/rustc ]] || exit 66
[[ -f packages/modules/Bluetooth/system/test/rootcanal/Android.bp ]] || exit 66

# AOSP's partial-tree switch defers unrelated absent modules. Soong still
# emits fatal Ninja rules for any absent dependency of these selected targets.
# It does not disable CFI, LTO, license checks or generated input validation.
export ALLOW_MISSING_DEPENDENCIES=true
# Qualification captures commands from the selected Ninja target. The optional
# global IDE database is unnecessary and this AOSP version misplaces it when
# OUT_DIR is absolute.
unset SOONG_GEN_COMPDB SOONG_LINK_COMPDB_TO
export OUT_DIR="$SOURCE_ROOT/out-bluetooth-r74"
export DIST_DIR="$SOURCE_ROOT/dist-bluetooth-r74"
export USE_RBE=false USE_GOMA=false
export GOMAXPROCS=4
unset SANITIZE_HOST SANITIZE_TARGET SANITIZE_TARGET_DIAG
unset LLVM_PREBUILTS_BASE LLVM_PREBUILTS_VERSION RUST_PREBUILTS_BASE RUST_PREBUILTS_VERSION
mkdir -p "$DIST_DIR"
source build/envsetup.sh
lunch redroid_arm64-userdebug
# The deployed runtime uses the ARM64 service directly, not impl-sim.so.
TARGETS=(android.hardware.bluetooth@1.1-service.sim)
if [[ "$PHASE" == graph ]]; then
  # The normal UI initializes KatiEnabled before generating the Soong graph.
  # Standalone mode emits conflicting ICU host/device installation rules in
  # this Android 13 tree. Keep upstream duplicate-rule checks unchanged.
  # The initial public reproduction runs the normal bootstrap Go tests.
  build/soong/soong_ui.bash --make-mode --skip-ninja -j1 "${TARGETS[@]}" \
    2>&1 | tee "$DIST_DIR/graph.log"
else
  # Compile the selected Soong artifact through the upstream UI/environment.
  # The generated combined graph has only the Soong subninja and normal pools;
  # unrelated Kati packaging does not participate. Do not edit either graph.
  [[ -f "$OUT_DIR/soong/.soong.kati_enabled" && -s "$OUT_DIR/soong/build.ninja" ]] || exit 67
  [[ ! -e "$OUT_DIR/last_kati_suffix" && -s "$OUT_DIR/combined.ninja" ]] || exit 67
  python3 - "$OUT_DIR" <<'PY'
from pathlib import Path
import sys
out = Path(sys.argv[1])
includes = [s.strip() for s in (out / 'combined.ninja').read_text().splitlines()
            if s.strip().startswith(('include ', 'subninja '))]
assert includes == ['subninja ' + str(out / 'soong/build.ninja')]
PY
  TARGET_PREFIX="$OUT_DIR/soong/.intermediates/packages/modules/Bluetooth/system/test/rootcanal/${TARGETS[0]}/android_vendor.33_arm64_armv8-a_cfi"
  build/soong/soong_ui.bash --make-mode --skip-kati --skip-soong-tests -j1 showcommands \
    "$TARGET_PREFIX/${TARGETS[0]}" "$TARGET_PREFIX/unstripped/${TARGETS[0]}" \
    2>&1 | tee "$DIST_DIR/build.log"
fi
