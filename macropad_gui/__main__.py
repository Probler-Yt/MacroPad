"""
    python3 -m macropad_gui            open the app
    python3 -m macropad_gui doctor     check the pad is connected and writable

The rest are for poking at the core from a terminal:
    python3 -m macropad_gui state
    python3 -m macropad_gui state
    python3 -m macropad_gui write key1 "ctrl+c"
    python3 -m macropad_gui write dial1-right --media volumeup
"""

import argparse
import sys

from . import core

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"; DIM = "\033[2m"; O = "\033[0m"


def cmd_doctor(_):
    d = core.diagnose()
    colour = G if d.ready else (Y if d.status == "unplugged" else R)
    print(f"{colour}{B}{d.headline}{O}")
    for line in d.detail:
        print(f"  {line}")
    if d.fix:
        print(f"\n{B}Fix{O}")
        for c in d.fix:
            print(f"  {DIM if c.startswith('#') else ''}{c}{O}")
    for w in d.warnings:
        print(f"\n{Y}Note:{O} {w}")
    return 0 if d.ready else 1


def cmd_state(_):
    s = core.State()
    print(f"{DIM}{s.path}{'' if s.path.exists() else '  (not created yet)'}{O}\n")
    for c in core.CONTROL_IDS:
        e = s.get(c)
        byte = f"0x{core.action_byte(c):02x}"
        if e.unknown:
            print(f"  {c:<12} {DIM}{byte}{O}  {Y}unknown{O}  {DIM}{e.reason}{O}")
        else:
            print(f"  {c:<12} {DIM}{byte}{O}  {e.binding.label():<24} "
                  f"{DIM}{e.written}{O}")
    return 0


def cmd_write(a):
    if a.control not in core.CONTROL_IDS:
        print(f"Unknown control. One of: {', '.join(core.CONTROL_IDS)}")
        return 1
    try:
        b = core.Binding(keys=a.keys or "", media=a.media or "",
                         delay=a.delay).validate()
    except ValueError as e:
        print(f"{R}{e}{O}")
        return 1

    d = core.diagnose()
    if not d.ready:
        return cmd_doctor(None)

    state = core.State()
    try:
        core.write_binding(d.device, a.control, b, state)
    except core.WriteError as e:
        print(f"{R}Write failed:{O} {e}")
        print("  " + ("The pad was partly written; this control is now marked "
                      "unknown." if e.pad_touched else "Nothing reached the pad."))
        return 1
    print(f"{G}Written.{O} {a.control} -> {b.label()}   {DIM}{state.path}{O}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="python3 -m macropad_gui")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="check the pad is connected and writable")
    sub.add_parser("state", help="show what this tool believes is on the pad")
    w = sub.add_parser("write", help="write one binding and record it")
    w.add_argument("control")
    w.add_argument("keys", nargs="?", help="e.g. 'ctrl+c' or 'a,b,c'")
    w.add_argument("--media", help="e.g. volumeup")
    w.add_argument("--delay", type=int, default=0)
    a = ap.parse_args()
    if a.cmd is None:
        try:
            from .app import run
        except ImportError as e:
            print(f"{R}Can't start the app:{O} {e}")
            print("Install Qt for Python:  sudo pacman -S pyside6")
            return 1
        return run()
    return {"doctor": cmd_doctor, "state": cmd_state, "write": cmd_write}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
