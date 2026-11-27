#!/usr/bin/env bash
# Run from checkout root. Caller supplies a dedicated, private, existing output directory.
set -euo pipefail
umask 077
unset SSLKEYLOGFILE
ulimit -c 0
if [[ $# != 1 || ! -d $1 || -L $1 || ! -O $1 || $(stat -c %a -- "$1") != 700 ]]; then
    printf '%s\n' 'Usage: bash tools/run_local_static_publish.sh <operator-owned-0700-directory>' >&2; exit 2
fi
dir=$(cd -- "$1" && pwd -P)
exec 9>"$dir/.local-static.lock"
flock -n 9 || exit 75
prev="$dir/strict-oss-local.json"
tmp=$(mktemp "$dir/.local-static.XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT
args=(--apply)
if [[ -f $prev ]]; then args+=(--previous "$prev"); fi
# A failed pass never replaces the last promotable snapshot. Retain only the tool's
# sanitised JSON as an operator diagnostic (not a frontend map).
if "${PYTHON:-python}" -B -m tools.publish_local_static "${args[@]}" > "$tmp"; then
    mv -fT -- "$tmp" "$prev"
else
    mv -fT -- "$tmp" "$dir/strict-oss-local.failed.json"
    exit 1
fi
