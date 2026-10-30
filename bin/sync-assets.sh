#!/usr/bin/env bash
# Syncs the logo from the www project into this repo. www is the source of truth for
# these images; connector carries a renamed copy because the plugin ZIP must contain its icons.
#
# Files are only written when source and destination differ (byte-for-byte).
# Requires no external tools beyond standard coreutils.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WWW="$REPO/../www"

if [[ ! -d "$WWW" ]]; then
  echo "✗ www not found at $WWW" >&2
  exit 1
fi

# ─── mapping: www path → path in this repo ───────────────────────────────────
SYNC_MAP=(
  "public/icon-512.png:plugin/assets/logo.png"
)

echo "Syncing assets (www → $(basename "$REPO")) ..."
copied=0
for entry in "${SYNC_MAP[@]}"; do
  src="$WWW/${entry%%:*}"
  dst="$REPO/${entry##*:}"
  if [[ ! -f "$src" ]]; then
    echo "  ⚠ missing in www: ${entry%%:*}" >&2
    continue
  fi
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    echo "  ⊜ ${entry##*:} unchanged"
  else
    cp "$src" "$dst"
    echo "  ✓ ${entry##*:}"
    ((copied++))
  fi
done
echo "  ($copied files updated)"
