#!/usr/bin/env python3
"""
macropad-probe - work out how your macro pad presents itself to Linux.

Read-only. It looks at what the kernel already knows about your USB devices
and prints it. It does not write to the pad, change any setting, or touch
your saved keybinds.

Pure standard library - nothing to install.

Usage:
    python3 macropad-probe.py              # look for known macro pad vendors
    python3 macropad-probe.py 1189         # a specific vendor id
    python3 macropad-probe.py 1189:8840    # a specific device
    python3 macropad-probe.py --all        # every HID device on the system
"""

import os
import sys
import glob

HIDRAW_GLOB = "/sys/class/hidraw/hidraw*"

# Vendors known to ship these cheap configurable pads. 1189 is the one the
# MacroPad project targets - it appears as "Acer Communications" but isn't.
KNOWN_VENDORS = {"1189": "Holtek/generic macro pad (MacroPad project family)",
                 "1a81": "Holtek Semiconductor"}

# Product ids the upstream MacroPad project has code for.
KNOWN_PRODUCTS = {
    "8890": "Legacy protocol (tested upstream)",
    "8830": "Extended protocol", "8831": "Extended protocol",
    "8832": "Extended protocol", "8897": "Extended protocol",
    "8874": "Extended protocol",
}

B = "\033[1m"; DIM = "\033[2m"; G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; O = "\033[0m"


def read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as f:
            return f.read() if binary else f.read().strip()
    except OSError:
        return None


def parse_descriptor(raw):
    """
    Walk a HID report descriptor far enough to list report IDs and sizes.
    Not a full parser - we only need to know which reports exist and how big
    they are, which is what determines how we talk to the device.
    """
    out = {"usage_page": None, "reports": {}}
    i = 0
    report_id, size, count = 0, 0, 0
    current = None

    while i < len(raw):
        b = raw[i]
        if b == 0b11111110:                      # long item, skip it
            i += 2 + (raw[i + 1] if i + 1 < len(raw) else 0)
            continue

        tag, typ, length = b >> 4, (b >> 2) & 0x3, b & 0x3
        length = 4 if length == 3 else length
        data = int.from_bytes(raw[i + 1:i + 1 + length], "little") if length else 0

        if typ == 1:                             # global
            if tag == 0 and out["usage_page"] is None:
                out["usage_page"] = data
            elif tag == 7:
                size = data
            elif tag == 8:
                report_id = data
                out["reports"].setdefault(report_id, {"in": 0, "out": 0, "feature": 0})
            elif tag == 9:
                count = data
        elif typ == 0:                           # main
            kind = {8: "in", 9: "out", 11: "feature"}.get(tag)
            if kind:
                current = out["reports"].setdefault(
                    report_id, {"in": 0, "out": 0, "feature": 0})
                # Accumulate bits. Rounding each item to a whole byte here
                # would inflate the total - a 5-bit field followed by 3 bits
                # of padding is one byte, not two.
                current[kind] += size * count

        i += 1 + length

    # Now convert the accumulated bit counts to bytes, once per report.
    for sizes in out["reports"].values():
        for kind in ("in", "out", "feature"):
            sizes[kind] = (sizes[kind] + 7) // 8

    return out


def describe(hidraw_dir):
    """Gather everything interesting about one hidraw node."""
    name = os.path.basename(hidraw_dir)
    dev = os.path.join(hidraw_dir, "device")

    uevent = read(os.path.join(dev, "uevent")) or ""
    hid_id = ""
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            hid_id = line.split("=", 1)[1]

    # HID_ID looks like 0003:00001189:00008840
    vid = pid = "????"
    if hid_id.count(":") == 2:
        _, v, p = hid_id.split(":")
        vid, pid = v[-4:].lower(), p[-4:].lower()

    info = {
        "node": f"/dev/{name}",
        "vid": vid,
        "pid": pid,
        "name": read(os.path.join(dev, "uevent")) or "",
        "product": "",
        "interface": None,
        "descriptor": None,
        "writable": os.access(f"/dev/{name}", os.W_OK),
        "readable": os.access(f"/dev/{name}", os.R_OK),
    }

    for line in uevent.splitlines():
        if line.startswith("HID_NAME="):
            info["product"] = line.split("=", 1)[1]

    # Walk up to the USB interface to find bInterfaceNumber - this is the
    # Linux equivalent of the "mi_00"/"mi_01" path fragments the Windows
    # code matches on.
    p = os.path.realpath(dev)
    for _ in range(6):
        cand = os.path.join(p, "bInterfaceNumber")
        if os.path.exists(cand):
            info["interface"] = read(cand)
            break
        p = os.path.dirname(p)

    raw = read(os.path.join(dev, "report_descriptor"), binary=True)
    if raw:
        info["descriptor"] = parse_descriptor(raw)
        info["descriptor_len"] = len(raw)

    return info


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv

    want_vid = want_pid = None
    if args:
        if ":" in args[0]:
            want_vid, want_pid = args[0].lower().split(":")
        else:
            want_vid = args[0].lower()

    nodes = sorted(glob.glob(HIDRAW_GLOB))
    if not nodes:
        print(f"{R}No HID devices found at all.{O} Is anything plugged in?")
        return 1

    found = []
    for n in nodes:
        info = describe(n)
        if show_all:
            found.append(info)
        elif want_vid:
            if info["vid"] == want_vid and (not want_pid or info["pid"] == want_pid):
                found.append(info)
        elif info["vid"] in KNOWN_VENDORS:
            found.append(info)

    if not found:
        print(f"{Y}Nothing matched.{O}")
        print(f"Scanned {len(nodes)} HID devices. Try:  python3 {sys.argv[0]} --all")
        return 1

    print(f"{B}Found {len(found)} matching HID interface(s){O}\n")

    for info in found:
        vid, pid = info["vid"], info["pid"]
        print(f"{B}{info['node']}{O}  {vid}:{pid}")
        if info["product"]:
            print(f"  product        {info['product']}")
        if vid in KNOWN_VENDORS:
            print(f"  vendor         {DIM}{KNOWN_VENDORS[vid]}{O}")
        if pid in KNOWN_PRODUCTS:
            print(f"  {G}known device   {KNOWN_PRODUCTS[pid]}{O}")
        else:
            print(f"  {Y}unknown pid    not in upstream's device list{O}")

        if info["interface"] is not None:
            print(f"  usb interface  {info['interface']}  "
                  f"{DIM}(Windows calls this mi_{info['interface']}){O}")

        perm = f"{G}yes{O}" if info["writable"] else f"{R}no - needs a udev rule{O}"
        print(f"  writable       {perm}")

        d = info.get("descriptor")
        if d:
            up = d["usage_page"]
            up_name = {1: "Generic Desktop", 7: "Keyboard", 12: "Consumer"}.get(
                up, f"0x{up:04x} (vendor-defined)" if up else "?")
            print(f"  usage page     {up_name}")
            print(f"  descriptor     {info['descriptor_len']} bytes")
            if d["reports"]:
                print(f"  reports        {DIM}id  in  out  feature (bytes){O}")
                for rid, sz in sorted(d["reports"].items()):
                    star = ""
                    if sz["out"] >= 8 or sz["feature"] >= 8:
                        star = f"  {G}<- can receive config{O}"
                    print(f"                 {rid:<3} {sz['in']:<3} "
                          f"{sz['out']:<4} {sz['feature']:<7}{star}")
        print()

    # ------------------------------------------------------------- verdict
    print(f"{B}What this means{O}")

    cfg = [i for i in found
           if i.get("descriptor")
           and any(r["out"] >= 8 or r["feature"] >= 8
                   for r in i["descriptor"]["reports"].values())]

    if not cfg:
        print(f"  {Y}No interface here looks like it accepts configuration data.{O}")
        print("  The pad may expose its config interface only under a different")
        print("  vendor id, or it may not be configurable over HID at all.")
    else:
        print(f"  {G}{len(cfg)} interface(s) can receive configuration reports.{O}")
        for i in cfg:
            print(f"    {i['node']}  (usb interface {i['interface']})")
        print("  That's where a configuration tool would write.")

    unwritable = [i for i in cfg if not i["writable"]]
    if unwritable:
        print()
        print(f"  {Y}You don't have write permission yet.{O} Fix with a udev rule:")
        v = unwritable[0]["vid"]; p = unwritable[0]["pid"]
        # Must sort before 73-seat-late.rules, which is where systemd turns
        # the uaccess tag into an actual permission. A 99- rule gets tagged
        # and then silently ignored.
        print(f"    sudo nano /etc/udev/rules.d/60-macropad.rules")
        print(f'    KERNEL=="hidraw*", ATTRS{{idVendor}}=="{v}", '
              f'ATTRS{{idProduct}}=="{p}", TAG+="uaccess"')
        print("    sudo udevadm control --reload-rules && sudo udevadm trigger")
        print("  Then unplug and replug the pad.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
