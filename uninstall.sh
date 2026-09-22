#!/usr/bin/env bash
# Removes MacroPad. Your pad keeps its bindings; they live on the pad itself.
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/macropad"
RULE="/etc/udev/rules.d/60-macropad.rules"

ask() {
    local answer=""
    if { exec 3</dev/tty; } 2>/dev/null; then
        read -r -p "  $1 [y/N] " answer <&3 || true
        exec 3<&-
    fi
    case "$answer" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# stop the actions listener if it's running, and don't start it at login
LOCK="${XDG_RUNTIME_DIR:-$HOME/.cache}/macropad-actions.lock"
pid="$(cat "$LOCK" 2>/dev/null || true)"
case "$pid" in ''|*[!0-9]*) pid="" ;; esac
if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "macropad.*actions"; then
    kill "$pid" 2>/dev/null || true
fi
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/macropad-actions.desktop"

rm -rf "$DATA/macropad"
rm -f "$HOME/.local/bin/macropad" "$DATA/applications/macropad.desktop" \
      "$DATA/icons/hicolor/scalable/apps/macropad.svg"
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Removed the app and its menu entry."

if [ -d "$CONF" ] && ask "Also delete the app's memory of what's on your pad ($CONF)?"; then
    rm -rf "$CONF"
    echo "Deleted $CONF."
fi
if [ -f "$RULE" ] && ask "Also remove the USB permission rule? (asks for your password)"; then
    sudo rm -f "$RULE"
    sudo udevadm control --reload-rules
    echo "Removed $RULE."
fi
echo "Done. The pad keeps working with whatever you last wrote to it."
