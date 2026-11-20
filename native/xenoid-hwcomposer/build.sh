#!/bin/bash
# Usage: ./build.sh /path/to/android-ndk [output-directory]
set -euo pipefail
source_dir=$(cd "$(dirname "$0")" && pwd)
ndk_root=${1:-${ANDROID_NDK_HOME:-}}
[[ -n "$ndk_root" ]] || { echo 'Pass an Android NDK directory or set ANDROID_NDK_HOME.' >&2; exit 2; }
output_dir=${2:-$source_dir/build}
mkdir -p "$output_dir"
case $(uname -s) in
    Darwin) host=darwin-x86_64 ;;
    Linux) host=linux-x86_64 ;;
    *) echo 'Use an Android NDK host toolchain on Linux or macOS.' >&2; exit 2 ;;
esac
toolchain="$ndk_root/toolchains/llvm/prebuilt/$host"
compiler="$toolchain/bin/aarch64-linux-android29-clang"
flags=(-std=gnu11 -O2 -Wall -Wextra -Werror -fPIC -fvisibility=hidden -ffunction-sections -fdata-sections -fstack-protector-strong -D_FORTIFY_SOURCE=2 -I"$source_dir/../xenoid-gralloc")
link=(-Wl,--gc-sections -Wl,--exclude-libs,ALL -Wl,-z,defs -Wl,-z,relro -Wl,-z,now)
"$compiler" "${flags[@]}" -shared -Wl,-soname,hwcomposer.raven.so "${link[@]}" "$source_dir/hwcomposer_wrapper.c" -ldl -o "$output_dir/hwcomposer.raven.so"
"$toolchain/bin/llvm-strip" --strip-unneeded "$output_dir/hwcomposer.raven.so"
"$compiler" "${flags[@]}" "${link[@]}" "$source_dir/test_refresh.c" -ldl -o "$output_dir/test_refresh"
"$toolchain/bin/llvm-strip" --strip-unneeded "$output_dir/test_refresh"
printf '%s\n' "$output_dir/hwcomposer.raven.so" "$output_dir/test_refresh"
