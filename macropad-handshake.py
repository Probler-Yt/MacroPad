#!/usr/bin/env python3
"""
macropad-handshake - find out how your pad wants to be talked to.

Sends only the benign "version check" report upstream uses on connect:
all zeros, no configuration payload. It cannot change your keybinds.

It tries each report id with two different transfer methods, and checks
after every attempt whether the device survived. If the pad resets, the
script waits for it to come back before continuing.

Usage:
    python3 macropad-handshake.py /dev/hidraw10
"""

import fcntl
import glob
import os
import sys
import time

REPORT_IDS = [0, 2, 3]
PAYLOAD_LEN = 64

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"; DIM = "\033[2m"; O = "\033[0m"


def hidioc_sfeature(size):
    """_IOWR('H', 0x06, size) - the SET_FEATURE ioctl hidraw exposes."""
    return (3 << 30) | (size << 16) | (ord('H') << 8) | 0x06


def device_present(path):
    return os.path.exists(path)


def wait_for_device(path, timeout=6.0):
    """After a reset the node usually reappears with the same name."""
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(path):
            time.sleep(0.4)          # let udev reapply the ACL
            return True
        time.sleep(0.2)
    return False


def try_write(path, report_id, method):
    """
    Returns (ok, detail). 'ok' means the device accepted the transfer
    without erroring and was still present afterwards.
    """
    data = bytes([report_id]) + bytes(PAYLOAD_LEN)

    try:
        if method == "write":
            fd = os.open(path, os.O_WRONLY)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        else:  # feature report via ioctl
            fd = os.open(path, os.O_RDWR)
            try:
                buf = bytearray(data)
                fcntl.ioctl(fd, hidioc_sfeature(len(buf)), buf)
            finally:
                os.close(fd)
    except OSError as e:
        return False, f"{e.__class__.__name__}: {e.strerror} (errno {e.errno})"
    except Exception as e:
        return False, str(e)

    time.sleep(0.3)
    if not device_present(path):
        return False, "device vanished - firmware reset"

    return True, "accepted"


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        cands = sorted(glob.glob("/dev/hidraw*"))
        print("Pass the device path, e.g.:")
        print(f"  python3 {sys.argv[0]} /dev/hidraw10")
        print(f"\n{DIM}Available: {' '.join(cands)}{O}")
        return 1

    if not os.path.exists(path):
        print(f"{R}{path} doesn't exist.{O}")
        return 1
    if not os.access(path, os.W_OK):
        print(f"{R}{path} isn't writable.{O} Check your udev rule.")
        return 1

    print(f"{B}Handshake probe on {path}{O}")
    print(f"{DIM}Sending zero-filled version-check reports only.")
    print(f"Your saved keybinds are not touched.{O}\n")

    results = []

    for method in ("write", "feature"):
        label = "interrupt OUT (write)" if method == "write" else "SET_FEATURE (ioctl)"
        print(f"{B}{label}{O}")

        for rid in REPORT_IDS:
            if not device_present(path):
                print(f"  {DIM}waiting for device to come back...{O}")
                if not wait_for_device(path):
                    print(f"  {R}device did not return. Replug it and rerun.{O}")
                    return 1

            ok, detail = try_write(path, rid, method)
            mark = f"{G}OK   {O}" if ok else f"{R}FAIL {O}"
            print(f"  report id {rid}:  {mark} {detail}")
            results.append((method, rid, ok, detail))

            if not ok and "vanished" in detail:
                if not wait_for_device(path):
                    print(f"  {R}device did not come back.{O} Replug and rerun.")
                    return 1
        print()

    # ------------------------------------------------------------ verdict
    print(f"{B}Verdict{O}")
    good = [(m, r) for m, r, ok, _ in results if ok]

    if not good:
        print(f"  {R}Nothing was accepted.{O}")
        print("  This pad may not use the MacroPad protocol at all, or it")
        print("  wants a vendor-specific unlock sequence first.")
        print("\n  Worth trying: the other config interface, if the probe found one.")
        return 1

    print(f"  {G}Accepted combinations:{O}")
    for m, r in good:
        how = "write" if m == "write" else "feature"
        print(f"    report id {r} via {how}")

    m, r = good[0]
    print(f"\n  Upstream picks the first report id that accepts a write,")
    print(f"  so your pad looks like {B}protocol version {r}{O}.")
    print(f"\n  Try a real binding with:")
    extra = "" if m == "write" else "  (needs --feature support adding)"
    print(f"    python3 macropad.py set key1 ctrl+c --report-id {r}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
