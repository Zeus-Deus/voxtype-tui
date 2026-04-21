#!/usr/bin/env bash
# Install voxtype-tui's Omarchy integration:
#   - append a floating window rule to ~/.config/hypr/windows.conf
#   - append a keybind to ~/.config/hypr/bindings.conf
#   - hyprctl reload
#
# Both edits are wrapped with a sentinel comment so uninstall-omarchy.sh can
# find and remove them. Re-runnable safely (idempotent).
#
# This script assumes voxtype-tui is already on $PATH — install it first from
# the AUR: `yay -S voxtype-tui`. voxtype-tui is Arch-only at the moment; it is
# not published to PyPI.

set -euo pipefail

# Edit BIND_KEY if SUPER CTRL ALT, X conflicts with your own binding.
BIND_KEY="SUPER CTRL ALT, X"
BIND_DESC="Voxtype config"
WIN_SIZE="1100 750"
APP_ID="org.omarchy.voxtype-tui"

SENTINEL="# voxtype-tui-managed (do not edit this line manually)"
HYPR_WINDOWS="$HOME/.config/hypr/windows.conf"
HYPR_BINDINGS="$HOME/.config/hypr/bindings.conf"

die() { echo "error: $*" >&2; exit 1; }

# --- sanity checks ---

if ! command -v voxtype-tui >/dev/null 2>&1; then
    cat >&2 <<EOF
voxtype-tui not found on PATH.

Install it first from the AUR:
    yay -S voxtype-tui

Then re-run this script. (voxtype-tui is Arch-only at the moment; it is
not published to PyPI, so pipx will not work.)
EOF
    exit 1
fi

if [[ ! -d "$HOME/.config/omarchy" ]]; then
    cat >&2 <<EOF
Omarchy not detected at ~/.config/omarchy/.
This integration is Omarchy-specific. For other setups, just run:

    voxtype-tui
EOF
    exit 1
fi

if [[ ! -f "$HYPR_BINDINGS" ]]; then
    die "expected $HYPR_BINDINGS to exist (Omarchy should have created it)"
fi

# Bail if SUPER CTRL ALT, X (or whatever the user set BIND_KEY to) is
# already bound to something else. Ignore our own managed block.
ACTIVE_BINDINGS=$(grep -v "voxtype-tui-managed" "$HYPR_BINDINGS" || true)
CONFLICT_PATTERN="^[[:space:]]*bind[a-z]*[[:space:]]*=.*SUPER[[:space:]]+CTRL[[:space:]]+ALT[[:space:]]*,[[:space:]]*X[[:space:]]*,"
if echo "$ACTIVE_BINDINGS" | grep -qE "$CONFLICT_PATTERN"; then
    cat >&2 <<EOF
A binding already exists on $BIND_KEY in $HYPR_BINDINGS:

$(echo "$ACTIVE_BINDINGS" | grep -E "$CONFLICT_PATTERN" | head -1)

Either remove that binding or edit BIND_KEY at the top of this script to
something free, then re-run.

Other SUPER CTRL ALT keys known to be free (as of Omarchy 3.5.x) include:
  A-Z  except   T (time), B (battery), Z (reset zoom)
Consult ~/.config/hypr/bindings.conf for your current layout.
EOF
    exit 2
fi

# --- windows.conf: float rule (idempotent) ---

if ! grep -q "voxtype-tui-managed" "$HYPR_WINDOWS" 2>/dev/null; then
    {
        echo "$SENTINEL"
        echo "windowrule = float on, center on, size $WIN_SIZE, match:initial_class $APP_ID"
    } >> "$HYPR_WINDOWS"
fi

# --- bindings.conf: keybind (idempotent) ---

if ! grep -q "voxtype-tui-managed" "$HYPR_BINDINGS" 2>/dev/null; then
    {
        echo "$SENTINEL"
        echo "bindd = $BIND_KEY, $BIND_DESC, exec, omarchy-launch-or-focus-tui voxtype-tui"
    } >> "$HYPR_BINDINGS"
fi

# --- reload ---

if command -v hyprctl >/dev/null 2>&1; then
    hyprctl reload >/dev/null 2>&1 || true
fi

cat <<EOF
Installed.

  Keybind:  $BIND_KEY ($BIND_DESC)
  Window:   class=$APP_ID, size=$WIN_SIZE

Press $BIND_KEY in Hyprland to open voxtype-tui. Run
scripts/uninstall-omarchy.sh to remove.
EOF
