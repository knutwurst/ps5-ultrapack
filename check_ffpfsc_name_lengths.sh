#!/usr/bin/env bash
# lists .ffpfs(c) names over ShadowMountPlus's ~64 byte limit (UTF-8 bytes). read-only
# usage: ./check_ffpfsc_name_lengths.sh [dir] [max]
set -euo pipefail

MAX=63
ROOT=""
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,20p'
      exit 0 ;;
    *[!0-9]*) ROOT="$arg" ;;     # contains a non-digit -> treat as path
    *) MAX="$arg" ;;             # all digits -> custom limit
  esac
done

if [[ -z "$ROOT" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: not a directory: $ROOT" >&2
  exit 1
fi

NEAR=$((MAX - 15))

echo ">>> Limit: $MAX bytes per filename (ShadowMountPlus ENAMETOOLONG; bytes, not chars)."
echo ">>> Searching under: $ROOT"
echo

# bytes, not chars: ENAMETOOLONG counts UTF-8, a TM sign costs 3

total=0
over=0
near=0
longest=0
longest_name=""

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

while IFS= read -r -d '' f; do
  total=$((total + 1))
  base="$(basename "$f")"
  len=$(printf '%s' "$base" | wc -c | tr -d ' ')   # UTF-8 byte length
  if (( len > longest )); then longest=$len; longest_name="$base"; fi
  if (( len > MAX )); then
    printf '%05d\tOVER\t%s\n' "$len" "$f" >> "$tmp"
    over=$((over + 1))
  elif (( len >= NEAR )); then
    printf '%05d\tNEAR\t%s\n' "$len" "$f" >> "$tmp"
    near=$((near + 1))
  fi
done < <(find "$ROOT" -type f \( -iname '*.ffpfsc' -o -iname '*.ffpfs' \) -print0)

if (( over > 0 )); then
  echo "=== TOO LONG (> $MAX) ==="
  sort -rn "$tmp" | awk -F'\t' '$2=="OVER"{ printf "  %3d bytes  %s\n", $1+0, $3 }'
  echo
fi
if (( near > 0 )); then
  echo "=== CLOSE TO THE LIMIT ($NEAR-$MAX) ==="
  sort -rn "$tmp" | awk -F'\t' '$2=="NEAR"{ printf "  %3d bytes  %s\n", $1+0, $3 }'
  echo
fi

echo ">>> $total file(s) scanned."
if (( total == 0 )); then
  echo ">>> WARNING: no .ffpfsc/.ffpfs files were read — empty folder, wrong path, or a"
  echo ">>>          permission-protected drive. On macOS, run this from a terminal with"
  echo ">>>          Full Disk Access (System Settings → Privacy & Security)."
  exit 0
fi
echo ">>> $over over the limit, $near close to it."
echo ">>> Longest name: $longest bytes — $longest_name"
if (( over == 0 )); then
  echo ">>> OK — no filename exceeds $MAX bytes."
fi
