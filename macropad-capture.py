#!/usr/bin/env python3
"""
macropad-capture - watch what the vendor's software sends, and what the pad
says back.

Uses the kernel's usbmon facility to record USB traffic to and from your
macro pad, then decodes the reports so we can see the real wire format.

This is passive. It only observes - it never sends anything itself.

How to use it:

  1. Start this script:
         sudo python3 macropad-capture.py

  2. Leave it running. In another window, launch the vendor software
     under Wine:
         wine MINI_KEYBOARD.exe

  3. Do ONE thing in that software, then come back and press Ctrl+C.

     To learn how a binding is written: change ONE key to the letter 'a'
     and apply it. ('a' is HID code 0x04, easy to spot in the bytes.)

     To learn whether the pad can be read back: press whatever the
     software calls "view settings" or "read". Anything the pad sends is
     recorded and marked with a <- arrow.

  4. It writes macropad-capture.txt, which has everything we need.
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


def kernel_has_usbmon():
    """
    'y' built into the kernel, 'm' a loadable module, None not built at all.
    Read from the running kernel's own config where it's available.
    """
    try:
        import gzip
        with gzip.open("/proc/config.gz", "rt") as f:
            text = f.read()
    except OSError:
        try:
            text = open(f"/boot/config-{os.uname().release}").read()
        except OSError:
            return "?"
    for line in text.splitlines():
        if line.startswith("CONFIG_USB_MON="):
            return line.split("=", 1)[1].strip()
    return None


def debugfs_mounted():
    try:
        return " /sys/kernel/debug " in open("/proc/mounts").read()
    except OSError:
        return False


def ensure_usbmon():
    """
    Get /sys/kernel/debug/usb/usbmon to exist, and explain it if we can't.

    Two separate things have to be true: the usbmon facility has to be in
    the kernel, and debugfs has to be mounted for it to appear. A missing
    module and an unmounted debugfs look identical until you check both.
    """
    if os.path.isdir(DEBUGFS):
        return True

    built = kernel_has_usbmon()

    if built == "m":
        print(f"{DIM}Loading usbmon module...{O}")
        subprocess.run(["modprobe", "usbmon"], capture_output=True)

    if not debugfs_mounted():
        print(f"{DIM}Mounting debugfs...{O}")
        subprocess.run(["mount", "-t", "debugfs", "none", "/sys/kernel/debug"],
                       capture_output=True)

    if os.path.isdir(DEBUGFS):
        return True

    # Still nothing. Say which of the two is actually missing.
    print(f"\n{R}Can't get at usbmon.{O}  {DIM}kernel {os.uname().release}{O}")
    if built is None:
        print("Your kernel was built without usbmon (CONFIG_USB_MON is not set),")
        print("so USB capture isn't available on it at all.")
        print(f"\n{B}What to do{O}")
        print("  Boot a kernel that has it. On Arch and CachyOS the LTS kernel does:")
        print("    sudo pacman -S linux-lts linux-lts-headers")
        print("  Then pick it in the boot menu and run this again.")
    elif built == "m":
        mod = f"/lib/modules/{os.uname().release}/kernel/drivers/usb/mon"
        print("Your kernel lists usbmon as a module, but it wouldn't load.")
        if not os.path.isdir(mod):
            print(f"{mod} is missing, which usually means the kernel was")
            print("updated and not rebooted into yet.")
            print(f"\n{B}What to do{O}\n  Reboot, then run this again.")
        else:
            print(f"\n{B}What to do{O}")
            print("  sudo depmod -a && sudo modprobe usbmon")
    else:
        print("usbmon should be built in, but the folder still isn't there.")
        print(f"\n{B}What to do{O}")
        print("  sudo mount -t debugfs none /sys/kernel/debug")
        print("  sudo ls /sys/kernel/debug/usb")
    return False


def decode(line, want_dev):
    """
    usbmon text format, roughly:
      <tag> <ts> <S|C|E> <type><dir>:<bus>:<dev>:<ep> <flags> <len> = <hex>

    Direction is the second letter: 'o' toward the device, 'i' from it.
    Data rides on the submission (S) of an OUT transfer and on the
    completion (C) of an IN transfer, so we want both.
    """
    parts = line.split()
    if len(parts) < 5:
        return None

    event, addr = parts[2], parts[3]
    bits = addr.split(":")
    if len(bits) < 4:
        return None
    xfer, ep = bits[0], bits[3]

    try:
        if int(bits[2]) != want_dev:
            return None
    except ValueError:
        return None

    outgoing = xfer.endswith("o")
    if outgoing and event != "S":        # OUT data rides on the submission
        return None
    if not outgoing and event != "C":    # IN data rides on the completion
        return None

    if "=" not in line:                  # no payload on this line
        return None
    data = line.split("=", 1)[1].replace(" ", "").strip()
    if not data:
        return None

    kind = {"Co": "control out", "Ci": "control in",
            "Io": "interrupt out", "Ii": "interrupt in",
            "Bo": "bulk out", "Bi": "bulk in"}.get(xfer, xfer)

    setup = ""
    if xfer.startswith("C"):
        # Control transfers carry an 8-byte setup packet in the flags field.
        for p in parts[4:9]:
            if re.fullmatch(r"[0-9a-f]{2,4}", p):
                setup += p + " "

    return {"kind": kind, "ep": ep, "setup": setup.strip(), "data": data,
            "outgoing": outgoing}


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
    print("\nDo ONE thing in it, then come back here.")
    print(f"{DIM}  ->  the software talking to the pad")
    print(f"  <-  the pad talking back{O}")
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
                arrow = f"{DIM}->{O}" if rec["outgoing"] else f"{G}<-{O}"
                print(f"{n:3} {arrow} {rec['kind']:<14} {pretty}"
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
        print("The software may not have talked to the pad, or it may be on a")
        print("different device number. Check lsusb while Wine is running.")
        return 1

    sent = sum(1 for p in packets if p["outgoing"])
    got = len(packets) - sent
    print(f"{G}Captured {sent} sent, {got} received{O} -> {OUTFILE}")
    if got:
        print(f"{G}The pad answered back.{O} It can be read, not only written.")

    print(f"\nSend me {B}{OUTFILE}{O} and I'll decode the format.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
