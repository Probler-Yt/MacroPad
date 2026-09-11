#!/usr/bin/env python3
"""
macropad-capture - watch what the vendor's software actually sends.

Uses the kernel's usbmon facility to record USB traffic going to your macro
pad, then decodes the outgoing reports so we can see the real wire format.

This is passive. It only observes - it never sends anything itself.

How to use it:

  1. Start this script:
         sudo python3 macropad-capture.py

  2. Leave it running. In another window, launch the vendor software
     under Wine:
         wine MINI_KEYBOARD.exe

  3. In that software, change ONE key to something distinctive - key 1 to
     the letter 'a' is ideal, because we know 'a' is HID code 0x04 and it
     will be easy to spot in the bytes.

  4. Click whatever it uses to apply or save.

  5. Come back here and press Ctrl+C.

It writes macropad-capture.txt, which has everything we need.
"""

import os
import re
import subprocess
import sys
import time

DEBUGFS = "/sys/kernel/debug/usb/usbmon"
OUTFILE = "macropad-capture.txt"

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"; DIM = "\033[2m"; O = "\033[0m"


def find_pad():
    """Locate the pad on the USB bus via lsusb. Returns (bus, device, vid, pid)."""
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return None

    candidates = []
    for line in out.splitlines():
        m = re.match(r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})", line)
        if not m:
            continue
        bus, dev, vid, pid = m.groups()
        if vid.lower() in ("1189", "1a81"):
            candidates.append((int(bus), int(dev), vid, pid, line.strip()))
    return candidates


def ensure_usbmon():
    if os.path.isdir(DEBUGFS):
        return True
    print(f"{DIM}Loading usbmon module...{O}")
    subprocess.run(["modprobe", "usbmon"], capture_output=True)
    if os.path.isdir(DEBUGFS):
        return True
    # debugfs may simply not be mounted yet
    subprocess.run(["mount", "-t", "debugfs", "none", "/sys/kernel/debug"],
                   capture_output=True)
    return os.path.isdir(DEBUGFS)


def decode(line, want_dev):
    """
    usbmon text format, roughly:
      <tag> <ts> <S|C> <type>:<bus>:<dev>:<ep> <flags> <len> = <hex data>

    We want submissions (S) carrying data toward our device: control
    transfers (Co) and interrupt out (Io).
    """
    parts = line.split()
    if len(parts) < 5:
        return None

    event = parts[2]
    addr = parts[3]
    if event != "S":
        return None

    bits = addr.split(":")
    if len(bits) < 4:
        return None
    xfer, bus, dev, ep = bits[0], bits[1], bits[2], bits[3]

    if int(dev) != want_dev:
        return None
    if not xfer.endswith("o"):        # 'o' = out, toward the device
        return None

    if "=" not in line:
        return None
    data = line.split("=", 1)[1].replace(" ", "").strip()
    if not data:
        return None

    kind = {"Co": "control out", "Io": "interrupt out",
            "Bo": "bulk out"}.get(xfer, xfer)

    setup = ""
    if xfer == "Co":
        # Control transfers carry an 8-byte setup packet in the flags field.
        for p in parts[4:9]:
            if re.fullmatch(r"[0-9a-f]{2,4}", p):
                setup += p + " "

    return {"kind": kind, "ep": ep, "setup": setup.strip(), "data": data}


def main():
    if os.geteuid() != 0:
        print(f"{R}Needs root{O} - usbmon is a kernel facility.")
        print(f"  sudo python3 {sys.argv[0]}")
        return 1

    cands = find_pad()
    if not cands:
        print(f"{R}No Holtek-family device found in lsusb.{O}")
        return 1

    if len(cands) > 1:
        print(f"{B}Several candidates:{O}")
        for i, c in enumerate(cands):
            print(f"  [{i}] {c[4]}")
        try:
            pick = int(input("Which one? ") or 0)
        except (ValueError, EOFError):
            pick = 0
        cands = [cands[pick]]

    bus, dev, vid, pid, desc = cands[0]
    print(f"{B}Watching{O} {desc}")
    print(f"{DIM}bus {bus}, device {dev}{O}\n")

    if not ensure_usbmon():
        print(f"{R}Couldn't set up usbmon.{O}")
        print("Try:  sudo modprobe usbmon")
        print("      sudo mount -t debugfs none /sys/kernel/debug")
        return 1

    path = os.path.join(DEBUGFS, f"{bus}u")
    if not os.path.exists(path):
        print(f"{R}{path} doesn't exist.{O} Is the bus number right?")
        return 1

    print(f"{G}Capturing.{O} Now, in another terminal:")
    print(f"  {B}wine MINI_KEYBOARD.exe{O}")
    print("\nChange ONE key to the letter 'a', then apply it.")
    print(f"{DIM}('a' is HID code 0x04 - easy to spot in the bytes.){O}")
    print(f"\nPress {B}Ctrl+C{O} when you're done.\n")
    print("-" * 62)

    packets = []
    try:
        with open(path, "r") as mon, open(OUTFILE, "w") as out:
            out.write(f"# capture of {desc}\n# bus {bus} device {dev}\n\n")
            for line in mon:
                rec = decode(line, dev)
                if not rec:
                    continue
                packets.append(rec)
                n = len(packets)
                hexstr = rec["data"]
                pretty = " ".join(hexstr[i:i + 2] for i in range(0, min(len(hexstr), 40), 2))
                print(f"{n:3}  {rec['kind']:<14} {pretty}"
                      + (" ..." if len(hexstr) > 40 else ""))
                out.write(f"[{n}] {rec['kind']} ep={rec['ep']}"
                          + (f" setup={rec['setup']}" if rec["setup"] else "")
                          + f"\n    {hexstr}\n")
                out.flush()
    except KeyboardInterrupt:
        pass
    except PermissionError:
        print(f"{R}Permission denied reading {path}.{O}")
        return 1

    print("\n" + "-" * 62)
    if not packets:
        print(f"{Y}Nothing captured.{O}")
        print("The software may not have written, or it may be talking to a")
        print("different device number. Check lsusb while Wine is running.")
        return 1

    print(f"{G}Captured {len(packets)} outgoing transfer(s){O} -> {OUTFILE}")

    hits = [p for p in packets if "04" in p["data"]]
    if hits:
        print(f"{DIM}{len(hits)} contain byte 0x04, which may be your 'a' keycode.{O}")

    print(f"\nSend me {B}{OUTFILE}{O} and I'll decode the format.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
