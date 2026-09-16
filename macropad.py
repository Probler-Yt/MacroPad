#!/usr/bin/env python3
"""
macropad - configure Holtek-family USB macro pads from Linux.

A Linux port of the protocol from rOzzy1987/MacroPad (GPL-3.0), which
reverse-engineered the vendor's Windows software. All credit for working
out the wire format goes there. This is a clean-room reimplementation of
that protocol in Python, with the Windows-only HID layer replaced by
direct writes to /dev/hidraw.

Licensed GPL-3.0, same as the original.

Examples:
    macropad.py list
    macropad.py show
    macropad.py set key1 ctrl+c
    macropad.py set knob1-left volumedown
    macropad.py set key3 "ctrl+shift+n" --dry-run
"""

import argparse
import glob
import os
import sys

# --------------------------------------------------------------- protocol

MAGIC = 0xFE          # upstream's Extended protocol
MAGIC_NATIVE = 0xFD   # what 1189:8840 actually wants (confirmed by usb capture)
TERMINATOR = (0xFE, 0xFF)   # follows a config report to commit it
REPORT_LEN = 65       # 1 report id + 64 payload bytes

class KeyType:
    NONE = 0
    BASIC = 1         # keyboard keystrokes
    MULTIMEDIA = 2    # media keys
    MOUSE = 3
    LED = 8

# Modifier bitmask, matching the USB HID keyboard modifier byte.
MODIFIERS = {
    "ctrl": 0x01, "shift": 0x02, "alt": 0x04, "win": 0x08, "super": 0x08,
    "lctrl": 0x01, "lshift": 0x02, "lalt": 0x04, "lwin": 0x08,
    "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rwin": 0x80,
}

# Standard USB HID Keyboard/Keypad usage IDs. Verified against upstream:
# A=4, Z=29, Enter=40, Esc=41, Space=44, F1=58, F12=69, Right=79, Left=80.
KEYCODES = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYCODES[_c] = 4 + _i
for _i, _c in enumerate("1234567890"):
    KEYCODES[_c] = 30 + _i
KEYCODES.update({
    "enter": 40, "return": 40, "esc": 41, "escape": 41, "backspace": 42,
    "tab": 43, "space": 44, "minus": 45, "equal": 46, "leftbracket": 47,
    "rightbracket": 48, "backslash": 49, "semicolon": 51, "quote": 52,
    "grave": 53, "comma": 54, "period": 55, "slash": 56, "capslock": 57,
    "printscreen": 70, "scrolllock": 71, "pause": 72, "insert": 73,
    "home": 74, "pageup": 75, "delete": 76, "end": 77, "pagedown": 78,
    "right": 79, "left": 80, "down": 81, "up": 82,
})
for _i in range(1, 13):
    KEYCODES[f"f{_i}"] = 57 + _i          # F1 = 58 .. F12 = 69

# Media keys live on the Consumer usage page, sent as a 16-bit value.
MEDIA_KEYS = {
    "play": 0x00CD, "pause": 0x00CD, "playpause": 0x00CD,
    "next": 0x00B5, "prev": 0x00B6, "previous": 0x00B6,
    "stop": 0x00B7, "mute": 0x00E2,
    "volumeup": 0x00E9, "volup": 0x00E9,
    "volumedown": 0x00EA, "voldown": 0x00EA,
    "brightnessup": 0x006F, "brightnessdown": 0x0070,
    "calculator": 0x0192, "browser": 0x0196, "mail": 0x018A,
    "mycomputer": 0x0194,     # seen in capture: the vendor app's "My computer"
}

# Physical control -> protocol action byte.
#
# Keys are 1..15 directly. Knobs start at 0x10 - confirmed by usb capture on
# 1189:8840, where the two dials occupied 0x10-0x12 and 0x13-0x15.
#
# Upstream derives knob numbers as (enum - 10), putting the first knob at 13.
# That holds on pads with fewer keys but not here: this firmware reserves
# 1..15 for keys and begins knobs on the 0x10 boundary. If your pad disagrees,
# --raw-action lets you find its numbering without editing this file.
KNOB_BASE = 0x10

CONTROLS = {}
for _i in range(1, 16):
    CONTROLS[f"key{_i}"] = _i
for _knob in (1, 2, 3):
    base = KNOB_BASE + (_knob - 1) * 3
    # Order within a knob (left, push, right) follows upstream's enum and
    # matches the capture's three-consecutive-values pattern, but which
    # physical direction is which was not separately verified.
    CONTROLS[f"knob{_knob}-left"] = base
    CONTROLS[f"knob{_knob}-push"] = base + 1
    CONTROLS[f"knob{_knob}-right"] = base + 2
    CONTROLS[f"dial{_knob}-left"] = base          # friendlier aliases
    CONTROLS[f"dial{_knob}-push"] = base + 1
    CONTROLS[f"dial{_knob}-right"] = base + 2


def build_report(report_id, action, key_type, payload, layer=0, delay=0,
                 magic=MAGIC):
    """
    Assemble one Extended-protocol report.

    Layout (payload bytes, after the report id):
        0     0xFE magic
        1     action - which physical control
        2     layer number
        3     key type
        4-5   delay, little endian
        6-8   zero
        9     count of populated pairs
        10..  payload
    """
    data = bytearray(REPORT_LEN - 1)
    data[0] = magic
    data[1] = action
    data[2] = layer
    data[3] = key_type
    data[4] = delay & 0xFF
    data[5] = (delay >> 8) & 0xFF

    count = 0
    for i, b in enumerate(payload):
        if 10 + i >= len(data):
            break
        data[10 + i] = b
        if b != 0:
            count = (i >> 1) + 1
    data[9] = count

    return bytes([report_id]) + bytes(data)


def parse_keystroke(spec):
    """'ctrl+shift+n' -> [(modifier_mask, keycode)]. Comma separates a sequence."""
    sequence = []
    for chunk in spec.split(","):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("+")]
        mods, code = 0, None
        for p in parts:
            if p in MODIFIERS:
                mods |= MODIFIERS[p]
            elif p in KEYCODES:
                code = KEYCODES[p]
            else:
                raise ValueError(f"don't recognise '{p}' in '{spec}'")
        if code is None and mods == 0:
            raise ValueError(f"nothing usable in '{chunk}'")
        sequence.append((mods, code or 0))
    if not sequence:
        raise ValueError(f"couldn't parse '{spec}'")
    return sequence


def build_key(report_id, action, sequence, layer=0, delay=0):
    payload = bytearray()
    for mods, code in sequence:
        payload += bytes([mods, code])
    return build_report(report_id, action, KeyType.BASIC, payload, layer, delay)


def build_media(report_id, action, usage, layer=0):
    payload = bytes([0, usage & 0xFF, (usage >> 8) & 0xFF, 0])
    return build_report(report_id, action, KeyType.MULTIMEDIA, payload, layer)


# ------------------------------------------------------- native protocol
#
# Confirmed by capturing the vendor software over usbmon. Keystrokes use
# upstream's Extended layout with two corrections: the magic byte is 0xFD
# rather than 0xFE, and layers are numbered from 1, not 0. Sending layer 0
# is why our earlier reports were accepted and then discarded. Media keys
# use a different packing again - see native_media().
#
# Observed for key1 -> 'b':
#   03 fd 01 01 01 00 00 00 00 00 01 00 05    config
#   03 fd fe ff 00 ...                        commit

def native_commit(report_id):
    data = bytearray(REPORT_LEN - 1)
    data[0] = MAGIC_NATIVE
    data[1], data[2] = TERMINATOR
    return bytes([report_id]) + bytes(data)


def native_key(report_id, action, sequence, layer=1, delay=0):
    payload = bytearray()
    for mods, code in sequence:
        payload += bytes([mods, code])
    body = build_report(report_id, action, KeyType.BASIC, payload, layer, delay,
                        magic=MAGIC_NATIVE)
    return [body, native_commit(report_id)]


def native_media(report_id, action, usage, layer=1):
    """
    Confirmed by capturing the vendor software binding keys 1-12 to media
    keys. Upstream's Extended media layout is NOT what this pad wants:

        upstream  03 fd 01 01 02 00 00 00 00 00 01 00 b5 00   (usage one byte late)
        captured  03 fd 01 01 02 00 00 00 00 00 02 b5 00 00   key1 -> next track

    The count byte is 2 even when the high byte is zero, and the 16-bit
    consumer usage follows it directly, little endian.
    """
    data = bytearray(REPORT_LEN - 1)
    data[0] = MAGIC_NATIVE
    data[1] = action
    data[2] = layer
    data[3] = KeyType.MULTIMEDIA
    data[9] = 2
    data[10] = usage & 0xFF
    data[11] = (usage >> 8) & 0xFF
    return [bytes([report_id]) + bytes(data), native_commit(report_id)]


# ----------------------------------------------------------- reading back
#
# The pad can be read, which we only found by capturing the vendor software's
# "view settings" button. Two commands, both answered on the interrupt IN
# endpoint of the same report id:
#
#   0xFB  what are you?    reply: 03 fb <keys> <knobs>
#   0xFA  send me a layer  reply: one report per control, magic 0xFA, then
#                                 nothing more
#
# Observed, reading layer 1:
#   03 fa 0f 03 01 02 ...          request (trailing bytes were uninitialised
#                                  memory in the vendor program; zeros work)
#   03 fa 02 01 01 00 ... 01 05 17 key2 is ctrl+alt+t
#   03 fa 10 01 02 00 ... 01 ea 00 dial 1 left is volume down

MAGIC_INFO = 0xFB
MAGIC_READ = 0xFA
KEY_SLOTS = 0x0F      # the firmware keeps 15 key slots whatever the pad has
KNOB_SLOTS = 3
LAYERS = 3


def native_info(report_id):
    """Ask the pad what it is. Reply carries the key and knob counts."""
    data = bytearray(REPORT_LEN - 1)
    data[0] = MAGIC_INFO
    return bytes([report_id]) + bytes(data)


def native_read(report_id, layer=1):
    """Ask the pad for every control in one layer."""
    data = bytearray(REPORT_LEN - 1)
    data[0] = MAGIC_READ
    data[1] = KEY_SLOTS
    data[2] = LAYERS
    data[3] = layer
    data[4] = 2
    return bytes([report_id]) + bytes(data)


# ------------------------------------------------------- legacy protocol
#
# The older format. Unlike Extended, a single binding is a *sequence* of
# reports: optionally select the layer, then one report per keystroke plus
# one, then commit to flash. Missing the commit leaves the pad mid-write,
# which is a good candidate for the firmware faults some devices show.

def _legacy(report_id, b0, b1, b2=0, b3=0, b4=0, b5=0):
    data = bytearray(REPORT_LEN - 1)
    data[0], data[1], data[2] = b0, b1, b2
    data[3], data[4], data[5] = b3, b4, b5
    return bytes([report_id]) + bytes(data)


def legacy_layer_select(report_id, layer):
    return _legacy(report_id, 161, layer)


def legacy_flash(report_id, led=False):
    return _legacy(report_id, 170, 161 if led else 170)


def legacy_key_sequence(report_id, action, sequence, layer=0):
    """Returns the full ordered list of reports for one key binding."""
    reports = []
    if report_id != 0:
        reports.append(legacy_layer_select(report_id, layer))

    n = len(sequence)
    for index in range(n + 1):
        type_byte = KeyType.BASIC
        if report_id != 0:
            type_byte |= (layer << 4) & 0xFF
        # Index 0 carries only the first entry's modifiers, no keycode.
        # That asymmetry is in the original; it is not a mistake here.
        if index == 0:
            mods, code = sequence[0][0], 0
        else:
            mods, code = sequence[index - 1]
        reports.append(_legacy(report_id, action, type_byte, n, index, mods, code))

    reports.append(legacy_flash(report_id))
    return reports


def legacy_media_sequence(report_id, action, usage, layer=0):
    reports = []
    if report_id != 0:
        reports.append(legacy_layer_select(report_id, layer))
    type_byte = KeyType.MULTIMEDIA
    if report_id != 0:
        type_byte |= (layer << 4) & 0xFF
    reports.append(_legacy(report_id, action, type_byte,
                           usage & 0xFF, (usage >> 8) & 0xFF))
    reports.append(legacy_flash(report_id))
    return reports


# ----------------------------------------------------------- device layer

KNOWN_VENDORS = ("1189",)

def find_devices():
    """Find hidraw nodes that expose a vendor-defined 64-byte config interface."""
    out = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        dev = os.path.join(node, "device")
        try:
            uevent = open(os.path.join(dev, "uevent")).read()
        except OSError:
            continue

        hid_id = next((l.split("=", 1)[1] for l in uevent.splitlines()
                       if l.startswith("HID_ID=")), "")
        if hid_id.count(":") != 2:
            continue
        _, v, p = hid_id.split(":")
        vid, pid = v[-4:].lower(), p[-4:].lower()
        if vid not in KNOWN_VENDORS:
            continue

        try:
            desc = open(os.path.join(dev, "report_descriptor"), "rb").read()
        except OSError:
            continue
        # Vendor-defined usage page: 0x06 followed by a page >= 0xFF00.
        if not (b"\x06\x00\xff" in desc or b"\x06\x01\xff" in desc):
            continue

        # Report id is declared by the 0x85 global item.
        rid = 0
        idx = desc.find(b"\x85")
        if idx != -1 and idx + 1 < len(desc):
            rid = desc[idx + 1]

        path = "/dev/" + os.path.basename(node)
        out.append({"path": path, "vid": vid, "pid": pid, "report_id": rid,
                    "writable": os.access(path, os.W_OK),
                    "name": next((l.split("=", 1)[1] for l in uevent.splitlines()
                                  if l.startswith("HID_NAME=")), "")})
    return out


def write_report(path, report):
    with open(path, "wb") as f:
        f.write(report)


# ------------------------------------------------------------------- cli

def hexdump(b):
    lines = []
    for off in range(0, len(b), 16):
        chunk = b[off:off + 16]
        lines.append(f"  {off:04x}  " + " ".join(f"{x:02x}" for x in chunk))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Configure a Holtek-family macro pad.")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="show detected pads")
    sub.add_parser("controls", help="list control and key names you can use")

    s = sub.add_parser("set", help="bind a control")
    s.add_argument("control",
                   help="e.g. key1, dial1-left, or action:16 for a raw byte")
    s.add_argument("binding", help="e.g. 'ctrl+c', 'f5', 'volumeup', 'a,b,c'")
    s.add_argument("--layer", type=int, default=1,
                   help="layer number, 1-indexed on this hardware")
    s.add_argument("--delay", type=int, default=0, help="ms between keys")
    s.add_argument("--device", help="hidraw path, if you have more than one")
    s.add_argument("--report-id", type=int, help="override the detected report id")
    s.add_argument("--dry-run", action="store_true",
                   help="show the bytes without writing")
    s.add_argument("--legacy", action="store_true",
                   help="use the older multi-report protocol with a flash commit")
    s.add_argument("--commit", action="store_true",
                   help="follow an extended-protocol report with a flash commit")
    s.add_argument("--extended", action="store_true",
                   help="use upstream's 0xFE extended format instead of native")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 0

    if args.cmd == "controls":
        print("Keys:")
        print("  " + ", ".join(f"key{i}" for i in range(1, 16)))
        print("\nDials (knobN-* and dialN-* are the same thing):")
        for k in (1, 2, 3):
            b = KNOB_BASE + (k - 1) * 3
            print(f"  dial{k}-left  0x{b:02x}    dial{k}-push  0x{b+1:02x}"
                  f"    dial{k}-right 0x{b+2:02x}")
        print("\nOr a raw action byte:  action:16   action:0x10")
        print("\nModifiers:")
        print("  " + ", ".join(sorted(set(MODIFIERS))))
        print("\nMedia:")
        print("  " + ", ".join(sorted(MEDIA_KEYS)))
        print("\nKeys: a-z, 0-9, f1-f12, and:")
        named = [k for k in KEYCODES if len(k) > 1 and not k.startswith("f")]
        print("  " + ", ".join(sorted(named)))
        return 0

    devices = find_devices()

    if args.cmd == "list":
        if not devices:
            print("No macro pad config interface found.")
            print("Run macropad-probe.py to see what's connected.")
            return 1
        for d in devices:
            state = "writable" if d["writable"] else "NOT WRITABLE - needs udev rule"
            print(f"{d['path']}  {d['vid']}:{d['pid']}  report id {d['report_id']}  [{state}]")
            if d["name"]:
                print(f"    {d['name']}")
        return 0

    if args.cmd == "set":
        if args.device:
            dev = {"path": args.device, "report_id": args.report_id or 3, "writable": True}
        else:
            if not devices:
                print("No pad found. Try --device /dev/hidrawN")
                return 1
            if len(devices) > 1:
                print("Several candidates; pick one with --device:")
                for d in devices:
                    print("  " + d["path"])
                return 1
            dev = devices[0]

        report_id = args.report_id if args.report_id is not None else dev["report_id"]

        control = args.control.lower()
        if control.startswith("action:"):
            try:
                action = int(control.split(":", 1)[1], 0)
            except ValueError:
                print("Use action:N, e.g. action:16 or action:0x10")
                return 1
            if not 0 < action < 256:
                print("Action must be 1-255.")
                return 1
        elif control in CONTROLS:
            action = CONTROLS[control]
        else:
            print(f"Unknown control '{control}'. See: macropad.py controls")
            return 1

        binding = args.binding.lower().strip()
        is_media = binding in MEDIA_KEYS

        if args.extended or args.legacy:
            pass
        if args.legacy:
            if is_media:
                reports = legacy_media_sequence(report_id, action,
                                                MEDIA_KEYS[binding], args.layer)
                kind = f"legacy media ({binding})"
            else:
                try:
                    seq = parse_keystroke(args.binding)
                except ValueError as e:
                    print(f"Error: {e}")
                    return 1
                reports = legacy_key_sequence(report_id, action, seq, args.layer)
                kind = f"legacy keystroke ({len(seq)} step{'s' if len(seq) != 1 else ''})"
        elif args.extended:
            if is_media:
                reports = [build_media(report_id, action,
                                       MEDIA_KEYS[binding], args.layer)]
                kind = f"extended media ({binding})"
            else:
                try:
                    seq = parse_keystroke(args.binding)
                except ValueError as e:
                    print(f"Error: {e}")
                    return 1
                reports = [build_key(report_id, action, seq, args.layer, args.delay)]
                kind = f"extended keystroke ({len(seq)} step{'s' if len(seq) != 1 else ''})"
            if args.commit:
                reports.append(legacy_flash(report_id))
                kind += " + flash commit"
        else:
            # Native - the format confirmed by usb capture. Layer defaults to 1.
            layer = args.layer if args.layer else 1
            if is_media:
                reports = native_media(report_id, action, MEDIA_KEYS[binding], layer)
                kind = f"media ({binding})"
            else:
                try:
                    seq = parse_keystroke(args.binding)
                except ValueError as e:
                    print(f"Error: {e}")
                    return 1
                reports = native_key(report_id, action, seq, layer, args.delay)
                kind = f"keystroke ({len(seq)} step{'s' if len(seq) != 1 else ''})"

        print(f"{control} -> {args.binding}   [{kind}]")
        print(f"device {dev['path']}, report id {report_id}, "
              f"{len(reports)} report{'s' if len(reports) != 1 else ''}")
        for i, r in enumerate(reports):
            print(f"  [{i + 1}] " + " ".join(f"{x:02x}" for x in r[:14]) + " ...")

        if args.dry_run:
            print("\nDry run - nothing written.")
            return 0

        if not dev.get("writable", False):
            print(f"\n{dev['path']} isn't writable. Add the udev rule first.")
            return 1

        import time
        for i, r in enumerate(reports):
            try:
                write_report(dev["path"], r)
            except OSError as e:
                print(f"\nReport {i + 1}/{len(reports)} failed: {e}")
                if i > 0:
                    print("Earlier reports were sent - the pad may be part-configured.")
                return 1
            time.sleep(0.02)   # the firmware needs a moment between reports

        print("\nWritten. Press the control to test it.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
