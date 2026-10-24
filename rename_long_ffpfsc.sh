#!/usr/bin/env bash
# renames .ffpfsc files too long for ShadowMount (~63 bytes); dry run unless --apply
# usage: ./rename_long_ffpfsc.sh <dir> [--apply]
set -euo pipefail

# EDIT ME: TITLEID|new base name without .ffpfsc
MAP=(
  "PPSA00001|Example Title [PPSA00001] [01.009]"
  "PPSA00002|Another Example Title [PPSA00002] [01.004]"
)

APPLY=0
ROOT=""
for arg in "$@"; do
  case "$arg" in
    --apply|-a) APPLY=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,22p'; exit 0 ;;
    *) ROOT="$arg" ;;
  esac
done
if [[ -z "$ROOT" ]]; then ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
if [[ ! -d "$ROOT" ]]; then echo "ERROR: not a directory: $ROOT" >&2; exit 1; fi

if [[ "$APPLY" -eq 1 ]]; then echo ">>> APPLY mode — files WILL be renamed."
else echo ">>> DRY RUN — nothing will be changed. Re-run with --apply to rename."; fi
echo ">>> Searching under: $ROOT"
echo

done_n=0; miss_n=0; skip_n=0
for entry in "${MAP[@]}"; do
  tid="${entry%%|*}"
  newbase="${entry#*|}"
  f="$(find "$ROOT" -type f -iname "*${tid}*.ffpfsc" -print 2>/dev/null | head -1)"
  if [[ -z "$f" ]]; then
    echo "  [MISS] no .ffpfsc found for $tid"
    miss_n=$((miss_n + 1))
    continue
  fi
  dir="$(dirname "$f")"
  cur="$(basename "$f")"
  new="${newbase}.ffpfsc"
  newbytes=$(printf '%s' "$new" | wc -c | tr -d ' ')
  if [[ "$cur" == "$new" ]]; then
    echo "  [OK]   already named: $cur"
    skip_n=$((skip_n + 1))
    continue
  fi
  if [[ -e "$dir/$new" ]]; then
    echo "  [SKIP] target exists: $new"
    skip_n=$((skip_n + 1))
    continue
  fi
  echo "  $cur"
  echo "    -> $new   (${newbytes} bytes)"
  if [[ "$APPLY" -eq 1 ]]; then mv -n -- "$dir/$cur" "$dir/$new"; fi
  done_n=$((done_n + 1))
done

echo
echo ">>> ${done_n} to rename, ${skip_n} already-ok/skipped, ${miss_n} not found."
if [[ "$APPLY" -eq 0 && "$done_n" -gt 0 ]]; then
  echo ">>> Re-run with --apply to perform the ${done_n} rename(s)."
fi
