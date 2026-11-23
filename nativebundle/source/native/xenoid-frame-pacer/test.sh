#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$root/build"
clang++ -std=c++17 -O1 -g -Wall -Wextra -Werror "$root/test_pacer.cpp" -o "$root/build/test_pacer"
"$root/build/test_pacer"
clang++ -std=c++17 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
 -fno-omit-frame-pointer "$root/test_pacer.cpp" -o "$root/build/test_pacer_sanitized"
"$root/build/test_pacer_sanitized"
