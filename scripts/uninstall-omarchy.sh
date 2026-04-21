#!/usr/bin/env bash
# Remove voxtype-tui's Omarchy integration bits:
#   - sentinel-tagged lines from hypr/windows.conf and hypr/bindings.conf
#   - the ~/.local/bin/voxtype-tui wrapper
#   - hyprctl reload
#
# Safe to re-run (no-op if nothing's there).

set -euo pipefail

SENTINEL_PATTERN="voxtype-tui-managed"
HYPR_WINDOWS="$HOME/.config/hypr/windows.conf"
HYPR_BINDINGS="$HOME/.config/hypr/bindings.conf"
WRAPPER_PATH="$HOME/.local/bin/voxtype-tui"

# Delete the sentinel line AND the immediately-following managed line.
remove_managed_block() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    local tmp
    tmp=$(mktemp)
    awk -v sentinel="$SENTINEL_PATTERN" '
        /^[[:space:]]*$/ && hold != "" {
            # trailing blank that precedes the sentinel — print buffered
            # and fall through
        }
        {
            if ($0 ~ sentinel) {
                skip_next = 1
                next
            }
            if (skip_next) {
                skip_next = 0
                next
            }
            print
        }
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
}

remove_managed_block "$HYPR_WINDOWS"
remove_managed_block "$HYPR_BINDINGS"

if [[ -f "$WRAPPER_PATH" ]]; then
    rm -f "$WRAPPER_PATH"
fi

if command -v hyprctl >/dev/null 2>&1; then
    hyprctl reload >/dev/null 2>&1 || true
fi

echo "Uninstalled."
