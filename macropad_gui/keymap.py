"""
Turn a key press in the GUI into a name macropad.py understands.

The pad sends HID usage codes, which identify a physical key position, not
a character - your keyboard layout decides what that position types. So the
recorder uses the physical key too: on Linux (X11 and Wayland) Qt reports
the xkb keycode, which is the evdev code + 8. That keeps 'record' correct on
UK, German, AZERTY and so on. Letters and digits fall back to Qt's key
value if the scancode is missing.
"""

from PySide6.QtCore import Qt

import macropad as proto

# evdev KEY_* code -> macropad.KEYCODES name. Only keys macropad.py can send.
EVDEV = {
    1: "esc", 14: "backspace", 15: "tab", 28: "enter", 57: "space",
    12: "minus", 13: "equal", 26: "leftbracket", 27: "rightbracket",
    43: "backslash", 39: "semicolon", 40: "quote", 41: "grave",
    51: "comma", 52: "period", 53: "slash", 58: "capslock",
    99: "printscreen", 70: "scrolllock", 119: "pause",
    110: "insert", 102: "home", 104: "pageup", 111: "delete", 107: "end",
    109: "pagedown", 106: "right", 105: "left", 108: "down", 103: "up",
    87: "f11", 88: "f12",
}
for _i, _c in enumerate("1234567890"):
    EVDEV[2 + _i] = _c
for _row, _start in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    for _i, _c in enumerate(_row):
        EVDEV[_start + _i] = _c
for _i in range(1, 11):
    EVDEV[58 + _i] = f"f{_i}"                      # F1 = 59 .. F10 = 68
for _i in range(13, 25):
    EVDEV[170 + _i] = f"f{_i}"                     # F13 = 183 .. F24 = 194

for _name in EVDEV.values():
    assert _name in proto.KEYCODES, _name

MODIFIER_KEYS = {Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
                 Qt.Key_Super_L, Qt.Key_Super_R, Qt.Key_AltGr}

_QT_FALLBACK = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _QT_FALLBACK[getattr(Qt, f"Key_{_c.upper()}")] = _c
for _c in "0123456789":
    _QT_FALLBACK[getattr(Qt, f"Key_{_c}")] = _c
for _i in range(1, 13):
    _QT_FALLBACK[getattr(Qt, f"Key_F{_i}")] = f"f{_i}"


def key_name(event):
    """HID key name for a QKeyEvent, or None if the pad can't send it."""
    code = event.nativeScanCode() - 8
    if code in EVDEV:
        return EVDEV[code]
    return _QT_FALLBACK.get(Qt.Key(event.key()))


def modifier_names(event):
    m = event.modifiers()
    out = []
    if m & Qt.ControlModifier:
        out.append("ctrl")
    if m & Qt.ShiftModifier:
        out.append("shift")
    if m & Qt.AltModifier:
        out.append("alt")
    if m & Qt.MetaModifier:
        out.append("super")
    return out
