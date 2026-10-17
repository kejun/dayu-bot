#!/usr/bin/env sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_binary=$(mktemp "${TMPDIR:-/tmp}/dsbot-input-test.XXXXXX")
trap 'rm -f "$test_binary"' EXIT HUP INT TERM
"${CC:-cc}" -std=c11 -Wall -Wextra -Werror \
    -I"$project_dir/main" "$project_dir/main/input_policy.c" \
    "$project_dir/tests/test_input_policy.c" -o "$test_binary"
"$test_binary"
