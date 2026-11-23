#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
case "$(uname -s)" in Linux) ;; *) echo 'Linux headers and runtime required' >&2; exit 77;; esac
TMP_TEST="$(mktemp -d)"
trap 'rm -rf "$TMP_TEST"' EXIT
"${CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter test_loop_direct_io.c -o "$TMP_TEST/test"
for CASE in 0 1 2 3 4 5 6; do
  STATUS=0
  "$TMP_TEST/test" "$CASE" >"$TMP_TEST/output" 2>&1 || STATUS=$?
  EXPECTED=1
  if (( CASE < 2 )); then EXPECTED=0; fi
  [[ "$STATUS" = "$EXPECTED" ]] || { cat "$TMP_TEST/output" >&2; exit 1; }
  if (( CASE >= 3 )); then grep -q DETACH_OWNED "$TMP_TEST/output"; else ! grep -q DETACH_OWNED "$TMP_TEST/output"; fi
  if (( CASE < 2 )); then grep -q VERIFIED "$TMP_TEST/output"; else ! grep -q VERIFIED "$TMP_TEST/output"; fi
done
echo 'loop direct-I/O: 7 source failure-boundary cases passed'
