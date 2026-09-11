#!/usr/bin/env bash
# MacroPad installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Probler-Yt/MacroPad/main/install.sh | bash
#
# or, from a downloaded copy:   ./install.sh
#
# Safe to run again at any time: it updates in place and keeps your bindings.
# Everything goes in your home folder except one small permission file in
# /etc/udev/rules.d, which is the only step that asks for your password.

set -euo pipefail
NEED_REPLUG=""

REPO="Probler-Yt/MacroPad"
BRANCH="main"

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
HOME_DIR="$DATA/macropad"           # app + optional private Python
APP="$HOME_DIR/app"
BIN="$HOME/.local/bin"
DESKTOP="$DATA/applications/macropad.desktop"
ICON="$DATA/icons/hicolor/scalable/apps/macropad.svg"
RULE="/etc/udev/rules.d/60-macropad.rules"
RULE_LINE='KERNEL=="hidraw*", ATTRS{idVendor}=="1189", ATTRS{idProduct}=="8840", TAG+="uaccess"'

if [ -t 1 ]; then
    O=$'\e[38;5;208m'; G=$'\e[32m'; R=$'\e[31m'; B=$'\e[1m'; D=$'\e[2m'; X=$'\e[0m'
else
    O=""; G=""; R=""; B=""; D=""; X=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$O" "$X" "$B" "$*" "$X"; }
ok()   { printf '    %sok%s  %s\n' "$G" "$X" "$*"; }
die()  { printf '\n%sStopped:%s %s\n' "$R" "$X" "$*" >&2; exit 1; }

# When piped from curl, stdin is this script, so questions read the terminal.
ask() {
    local answer=""
    if { exec 3</dev/tty; } 2>/dev/null; then
        read -r -p "    $1 [Y/n] " answer <&3 || true
        exec 3<&-
    fi
    case "$answer" in [nN]*) return 1 ;; *) return 0 ;; esac
}

cat <<EOF

${O}  ┌────────────────┐${X}
${O}  │  ◯        ◯    │${X}   ${B}MacroPad${X}
${O}  │  ▢   ▢   ▢    │${X}   Set up your 12 key, 2 dial macro pad on Linux.
${O}  │  ▢   ▢   ${X}▣${O}    │${X}
${O}  └────────────────┘${X}   ${D}github.com/${REPO}${X}

EOF

[ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not with sudo. It will ask for your password itself when it needs it."
command -v python3 >/dev/null || die "Python 3 isn't installed. Install it with your distro's software centre, then run this again."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || die "Python 3.9 or newer is needed. You have $(python3 -V)."

. /etc/os-release 2>/dev/null || true
FAMILY=" ${ID:-} ${ID_LIKE:-} "

# -------------------------------------------------------------- 1. files
step "Getting the app"
SRC=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -d "$HERE/macropad_gui" ] && [ -f "$HERE/macropad.py" ]; then
    SRC="$HERE"
    ok "using the copy in $SRC"
else
    command -v curl >/dev/null || die "curl is needed to download the app."
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" \
        | tar -xz -C "$TMP" || die "Couldn't download the app. Check your internet connection."
    SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    [ -d "$SRC/macropad_gui" ] || die "The download didn't contain the app. Please report this at github.com/${REPO}/issues"
    ok "downloaded the latest version"
fi

mkdir -p "$HOME_DIR"
rm -rf "$APP.new"
mkdir -p "$APP.new"
for f in macropad.py macropad-probe.py macropad-capture.py macropad-handshake.py \
         macropad_gui packaging install.sh uninstall.sh LICENSE README.md; do
    if [ -e "$SRC/$f" ]; then cp -r "$SRC/$f" "$APP.new/"; fi
done
find "$APP.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$APP.old"
if [ -d "$APP" ]; then mv "$APP" "$APP.old"; fi
mv "$APP.new" "$APP"
rm -rf "$APP.old"
ok "installed to $APP"

# -------------------------------------------------------------- 2. Qt
step "Checking for Qt, the toolkit that draws the window"
PY=""
if python3 -c 'import PySide6.QtWidgets' 2>/dev/null; then
    PY="$(command -v python3)"
    ok "already installed"
elif command -v pacman >/dev/null && [[ "$FAMILY" == *" arch "* ]] && [ ! -e /run/ostree-booted ]; then
    say "    Your system can install it directly (package: pyside6)."
    if ask "Install it now? This asks for your password."; then
        sudo pacman -S --needed --noconfirm pyside6 \
            && python3 -c 'import PySide6.QtWidgets' && PY="$(command -v python3)" \
            && ok "installed pyside6"
    fi
fi

if [ -z "$PY" ]; then
    # Anything else gets a private copy, so nothing system-wide changes.
    say "    Setting up a private copy of Qt just for this app (about 100 MB, one time)."
    VENV="$HOME_DIR/venv"
    if [ ! -x "$VENV/bin/python" ]; then
        if ! python3 -m venv "$VENV" 2>/dev/null; then
            rm -rf "$VENV"
            if command -v apt-get >/dev/null; then
                say "    Python needs its 'venv' add-on first. This asks for your password."
                sudo apt-get install -y python3-venv >/dev/null
                python3 -m venv "$VENV" || die "Couldn't create the private Python."
            else
                die "Couldn't create a private Python. Install your distro's python3-venv package and run this again."
            fi
        fi
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet --upgrade PySide6-Essentials \
        || die "Couldn't download Qt. Check your internet connection and run this again."
    PY="$VENV/bin/python"
    ok "Qt ready"

    # Qt's X11 backend needs one small system library some distros leave out.
    if [ "${XDG_SESSION_TYPE:-}" = "x11" ]; then
        if command -v apt-get >/dev/null && ! ldconfig -p | grep -q libxcb-cursor.so.0; then
            sudo apt-get install -y libxcb-cursor0 >/dev/null && ok "added libxcb-cursor0"
        elif command -v dnf >/dev/null && [ ! -e /run/ostree-booted ] && ! ldconfig -p | grep -q libxcb-cursor.so.0; then
            sudo dnf install -y xcb-util-cursor >/dev/null && ok "added xcb-util-cursor"
        fi
    fi
fi

# -------------------------------------------------------------- 3. launcher
step "Adding MacroPad to your app menu"
mkdir -p "$BIN" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
cat > "$BIN/macropad" <<EOF
#!/bin/sh
# Opens MacroPad. Anything after 'macropad' is passed on, e.g. 'macropad doctor'.
cd "$APP" && exec "$PY" -m macropad_gui "\$@"
EOF
chmod +x "$BIN/macropad"
cp "$APP/packaging/macropad.svg" "$ICON"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=MacroPad
GenericName=Macro pad configurator
Comment=Change what your macro pad's keys and dials do
Exec=$BIN/macropad
Icon=$ICON
Terminal=false
Categories=Utility;Settings;HardwareSettings;
Keywords=macro;keyboard;keypad;knob;dial;shortcut;hid;
StartupWMClass=macropad
EOF
command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "$DESKTOP")" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
# Plasma caches icon lookups; drop the cache so the new icon shows straight away.
rm -f "${XDG_CACHE_HOME:-$HOME/.cache}/icon-cache.kcache" 2>/dev/null || true
ok "MacroPad is in your app menu"

# -------------------------------------------------------------- 4. permission
step "Letting the app talk to the pad"
if [ -f "$RULE" ] && grep -qF "$RULE_LINE" "$RULE"; then
    ok "permission rule already in place"
else
    say "    Linux doesn't let apps write to USB devices by default. This adds one"
    say "    small rule so MacroPad can, for whoever is logged in at the computer."
    say "    ${D}It writes: $RULE${X}"
    if ask "Add it now? This asks for your password."; then
        sudo mkdir -p "$(dirname "$RULE")"
        printf '%s\n' "# MacroPad: let the logged-in user configure 1189:8840 macro pads." \
            "# Must sort before 73-seat-late.rules, which is where uaccess takes effect." \
            "$RULE_LINE" | sudo tee "$RULE" >/dev/null
        if command -v udevadm >/dev/null; then
            sudo udevadm control --reload-rules || true
            sudo udevadm trigger --subsystem-match=hidraw || true
        fi
        ok "rule added"
        NEED_REPLUG=1
    else
        say "    Skipped. The app will show you how to add it later."
    fi
fi

# A rule named 99-something (or with no number) is read too late to work.
for f in /etc/udev/rules.d/*.rules; do
    [ -e "$f" ] || continue
    [ "$f" = "$RULE" ] && continue
    if grep -qiE 'idVendor\}=="1189"' "$f" 2>/dev/null && grep -q uaccess "$f"; then
        base="$(basename "$f")"
        if [[ ! "$base" < "73-seat-late.rules" ]]; then
            say "    ${D}Note: $f also mentions this pad but is read too late to do"
            say "    anything. It's harmless. Remove it with: sudo rm $f${X}"
        fi
    fi
done

# -------------------------------------------------------------- done
printf '\n%s%sAll done.%s\n\n' "$G" "$B" "$X"
n=1
if [ -n "$NEED_REPLUG" ]; then
    say "  ${B}$n.${X} Unplug your macro pad and plug it back in."
    n=$((n + 1))
fi
say "  ${B}$n.${X} Open ${B}MacroPad${X} from your app menu."
say "     ${D}(or type${X} macropad ${D}in a terminal)${X}"
say ""
say "  To update later, run the same command again."
say "  To remove it:  ${D}$APP/uninstall.sh${X}"
say ""
case ":$PATH:" in *":$BIN:"*) ;; *)
    say "  ${D}Tip: $BIN isn't on your PATH, so the 'macropad' command won't work"
    say "  in a terminal yet. The app menu entry works regardless.${X}"
    say ""
    ;;
esac
