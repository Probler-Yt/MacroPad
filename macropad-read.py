#!/usr/bin/env python3
"""
macropad-read - find out what the pad will tell us about itself.

Two ways of listening, neither of which writes a configuration:

  1. Feature reports. A HID "get feature" is a read. We ask for each
     report id in turn and print anything that comes back.

  2. Passive listening. Several programs can have a hidraw device open at
     once, and input reports go to all of them. So we can sit and watch
     while the vendor software talks to the pad, and see the pad's
     replies, with no kernel module and no root.

Neither of these changes your keybinds.

    python3 macropad-read.py                 # find the pad, do both
    python3 macropad-read.py /dev/hidraw10   # a specific node
    python3 macropad-read.py --listen 60     # listen for 60 seconds

To catch the pad answering the vendor software:

    1. Start:   python3 macropad-read.py --listen 60
    2. In another window:   wine MINI_KEYBOARD.exe
    3. Press whatever it calls "view settings" or "read".
    4. Come back here. Anything the pad said is printed and saved.

It writes macropad-read.txt in the same format as macropad-capture.py,
so the same tools can read it.
"""

import fcntl
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import macropad as proto                                   # noqa: E402

OUTFILE = "macropad-read.txt"
REPORT_IDS = (0, 1, 2, 3, 4)
LEN = 65

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"
DIM = "\033[2m"; O = "\033[0m"


def hidiocgfeature(size):
    """_IOC(READ|WRITE, 'H', 0x07, size) - hidraw's GET_FEATURE ioctl."""
    return (3 << 30) | (size << 16) | (ord("H") << 8) | 0x07


def pretty(data, width=16):
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"    {off:04x}  {hexs:<{width * 3}} {text}")
    return "\n".join(lines)


def try_features(path, out):
    """Ask the pad for each report id. Reading can't change anything."""
    print(f"{B}Feature reports{O}  {DIM}asking the pad for each report id{O}")
    found = 0
    for rid in REPORT_IDS:
        buf = bytearray(LEN)
        buf[0] = rid
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError as e:
            print(f"  {R}can't open {path}: {e.strerror}{O}")
            return 0
        try:
            fcntl.ioctl(fd, hidiocgfeature(len(buf)), buf, True)
        except OSError as e:
            print(f"  id {rid}:  {DIM}nothing ({e.strerror}){O}")
            continue
        finally:
            os.close(fd)

        if not any(buf[1:]):
            print(f"  id {rid}:  {DIM}all zeros{O}")
            continue
        found += 1
        print(f"  id {rid}:  {G}answered{O}")
        print(pretty(bytes(buf)))
        out.write(f"[feature] report id {rid}\n    {bytes(buf).hex()}\n")
    return found


def listen(path, seconds, out):
    print(f"\n{B}Listening for {seconds}s{O}  "
          f"{DIM}anything the pad sends shows up here{O}")
    print(f"{DIM}Now use the vendor software and press its read or view "
          f"settings button.{O}\n")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        print(f"{R}Can't open {path}: {e.strerror}{O}")
        return 0

    seen = 0
    end = time.time() + seconds
    try:
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                data = os.read(fd, 128)
            except BlockingIOError:
                continue
            except OSError as e:
                print(f"{R}read failed: {e.strerror}{O}")
                break
            if not data:
                continue
            seen += 1
            left = int(end - time.time())
            print(f"{G}<-{O} {len(data)} bytes  {DIM}({left}s left){O}")
            print(pretty(data))
            out.write(f"[{seen}] interrupt in\n    {data.hex()}\n")
            out.flush()
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped early{O}")
    finally:
        os.close(fd)
    return seen


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    seconds = 30
    if "--listen" in sys.argv:
        i = sys.argv.index("--listen")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            seconds = int(sys.argv[i + 1])

    if args:
        path = args[0]
    else:
        pads = proto.find_devices()
        if not pads:
            print(f"{R}No pad found.{O} Run macropad-probe.py to see what's there,")
            print("or pass the node yourself:  python3 macropad-read.py /dev/hidrawN")
            return 1
        if len(pads) > 1:
            print(f"{B}Several candidates:{O}")
            for p in pads:
                print(f"  {p['path']}  {p['vid']}:{p['pid']}")
            print("\nPick one and pass it as an argument.")
            return 1
        path = pads[0]["path"]

    if not os.access(path, os.R_OK):
        print(f"{R}{path} isn't readable.{O} Add the udev rule, or use sudo.")
        return 1

    print(f"{B}Reading from {path}{O}")
    print(f"{DIM}Nothing here writes a configuration.{O}\n")

    with open(OUTFILE, "w") as out:
        out.write(f"# read attempt on {path}\n\n")
        found = try_features(path, out)
        heard = listen(path, seconds, out)

    print("\n" + "-" * 62)
    if found or heard:
        print(f"{G}Got something.{O}  "
              f"{found} feature report(s), {heard} message(s) while listening.")
        print(f"Saved to {B}{OUTFILE}{O} - send that over.")
    else:
        print(f"{Y}The pad said nothing.{O}")
        print("That doesn't prove it can't be read, only that it wasn't")
        print("answering us. The next thing to try is capturing the vendor")
        print("software with macropad-capture.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
