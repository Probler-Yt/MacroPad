"""
Core of the macro pad GUI. No Qt in here: the GUI imports this, and so do
the tests, so everything that matters can be checked without a display.

The pad is write-only, which shapes the whole design. There are two kinds
of truth and they live in separate files:

    profile   what you *want* on the pad. You edit it; the GUI edits it.
    state     what this tool last wrote *successfully*. Only the writer
              touches it. It is our shadow copy of the pad's memory.

A control we never wrote, or whose write failed partway, is UNKNOWN. The
pad could hold anything there, and the UI must say so rather than guess.

Every byte on the wire still comes from macropad.py. This module decides
what to send and remembers what was sent; it never builds a report itself.
"""

import errno
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

try:
    import macropad as proto
except ImportError:                      # running from inside the package dir
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import macropad as proto


# ------------------------------------------------------------------ device

VID, PID = "1189", "8840"
DEVICE_ID = f"{VID}:{PID}"

DIAL_ACTIONS = ("left", "push", "right")
WRITE_GAP = 0.02      # same pause the CLI leaves between reports


def action_byte(control):
    """
    The byte the pad uses for one control. Keys count from 1; knobs start
    at 0x10, three apiece. Confirmed on 1189:8840 both by writing and by
    reading back, and it is what the pad reports for its own slots.
    """
    control = control.lower()
    if control.startswith("key"):
        return int(control[3:])
    dial, act = control.split("-")
    return proto.KNOB_BASE + (int(dial[4:]) - 1) * 3 + DIAL_ACTIONS.index(act)


@dataclass(frozen=True)
class Layout:
    """
    The shape of one pad: how many keys and knobs, and how the keys are
    arranged when you look at it.

    keys and knobs come from the pad itself (the 0xFB query). Rows and
    columns do not: the pad knows how many keys it has, not how they are
    laid out, so that part is either known from the model or chosen by
    whoever owns it.

    Rows and columns describe the pad lying flat, the way its keys are
    printed. Standing it upright just turns the drawing.
    """
    keys: int = 12
    knobs: int = 2
    rows: int = 3
    cols: int = 4
    name: str = ""
    places: tuple = ()      # ((row, col), ...) per key; empty means left to right

    def __post_init__(self):
        if not 1 <= self.keys <= proto.KEY_SLOTS:
            raise ValueError(f"a pad can have 1 to {proto.KEY_SLOTS} keys")
        if not 0 <= self.knobs <= proto.KNOB_SLOTS:
            raise ValueError(f"a pad can have up to {proto.KNOB_SLOTS} knobs")
        if self.rows * self.cols < self.keys:
            raise ValueError(f"{self.rows}x{self.cols} can't hold "
                             f"{self.keys} keys")
        if self.places:
            if len(self.places) != self.keys:
                raise ValueError("places must give a spot for every key")
            if len(set(self.places)) != len(self.places):
                raise ValueError("two keys can't share a spot")
            for r, c in self.places:
                if not (0 <= r < self.rows and 0 <= c < self.cols):
                    raise ValueError(f"({r}, {c}) is outside {self.rows}x{self.cols}")

    def spots(self):
        """[(key_id, row, col)] with the pad lying flat."""
        if self.places:
            return [(f"key{i + 1}", r, c) for i, (r, c) in enumerate(self.places)]
        return [(f"key{i + 1}", i // self.cols, i % self.cols)
                for i in range(self.keys)]

    def rearranged(self, places):
        """The same pad with its keys in different spots."""
        return replace(self, places=tuple((int(r), int(c)) for r, c in places))

    def resized(self, rows=None, cols=None, keys=None, knobs=None):
        """
        Change the grid, keeping any arrangement that still fits and
        falling back to left to right for keys that no longer do.
        """
        rows = self.rows if rows is None else rows
        cols = self.cols if cols is None else cols
        keys = self.keys if keys is None else keys
        knobs = self.knobs if knobs is None else knobs
        if rows * cols < keys:                 # grow to fit rather than refuse
            rows = -(-keys // cols)

        taken, places = set(), []
        old = {i: p for i, p in enumerate(self.places)}
        free = [(r, c) for r in range(rows) for c in range(cols)]
        for i in range(keys):
            spot = old.get(i)
            if spot and spot[0] < rows and spot[1] < cols and spot not in taken:
                places.append(spot)
                taken.add(spot)
            else:
                places.append(None)
        for i, spot in enumerate(places):
            if spot is None:
                nxt = next(p for p in free if p not in taken)
                places[i] = nxt
                taken.add(nxt)
        return replace(self, rows=rows, cols=cols, keys=keys, knobs=knobs,
                       places=tuple(places) if self.places else ())

    @property
    def key_ids(self):
        return tuple(f"key{i}" for i in range(1, self.keys + 1))

    @property
    def dial_ids(self):
        return tuple(f"dial{i}" for i in range(1, self.knobs + 1))

    @property
    def control_ids(self):
        return self.key_ids + tuple(f"{d}-{a}" for d in self.dial_ids
                                    for a in DIAL_ACTIONS)

    @property
    def control_for_action(self):
        return {action_byte(c): c for c in self.control_ids}

    def describe(self):
        knobs = (f" and {self.knobs} knob{'s' if self.knobs != 1 else ''}"
                 if self.knobs else "")
        return f"{self.keys} keys in {self.rows}x{self.cols}{knobs}"

    def to_json(self):
        d = {"keys": self.keys, "knobs": self.knobs,
             "rows": self.rows, "cols": self.cols, "name": self.name}
        if self.places:
            d["places"] = [list(p) for p in self.places]
        return d

    @classmethod
    def from_json(cls, d):
        return cls(keys=int(d["keys"]), knobs=int(d["knobs"]),
                   rows=int(d["rows"]), cols=int(d["cols"]),
                   name=d.get("name", ""),
                   places=tuple(tuple(p) for p in d.get("places", ())))


# Pads we have laid eyes on. Anything else has to be detected or described
# by its owner, because a USB id alone does not pin down a layout: 1189:8840
# covers several different pads.
KNOWN_LAYOUTS = {
    ("1189", "8840"): Layout(12, 2, 3, 4, "12 keys, 2 knobs"),
}
DEFAULT_LAYOUT = KNOWN_LAYOUTS[(VID, PID)]

# Kept so older code and the CLI keep working; the app uses a Layout.
KEYS = DEFAULT_LAYOUT.key_ids
DIALS = DEFAULT_LAYOUT.dial_ids
CONTROL_IDS = DEFAULT_LAYOUT.control_ids
CONTROL_FOR_ACTION = DEFAULT_LAYOUT.control_for_action

for _cid in CONTROL_IDS:
    assert action_byte(_cid) == proto.CONTROLS[_cid], _cid

_DIAL_WORDS = {"left": "turn left", "push": "press", "right": "turn right"}


def control_name(control):
    """'key4' -> 'Key 4', 'dial1-left' -> 'Dial 1, turn left'."""
    if control.startswith("key"):
        return f"Key {control[3:]}"
    dial, act = control.split("-")
    return f"Dial {dial[4:]}, {_DIAL_WORDS[act]}"


# Media keys the GUI offers, in the order it offers them. Every id must be a
# name macropad.MEDIA_KEYS knows; the usage codes themselves live there.
MEDIA = [
    ("playpause", "Play / pause"), ("next", "Next track"),
    ("prev", "Previous track"), ("stop", "Stop"),
    ("volumeup", "Volume up"), ("volumedown", "Volume down"), ("mute", "Mute"),
    ("brightnessup", "Brightness up"), ("brightnessdown", "Brightness down"),
    ("calculator", "Calculator"), ("mail", "Email"),
    ("browser", "Browser"), ("mycomputer", "My computer"),
]
MEDIA_LABEL = dict(MEDIA)
for _m, _ in MEDIA:
    assert _m in proto.MEDIA_KEYS, f"macropad.py has no media key {_m}"
_MEDIA_BY_USAGE = {}
for _m, _ in MEDIA:
    _MEDIA_BY_USAGE.setdefault(proto.MEDIA_KEYS[_m], _m)

# Verified on 1189:8840 so far: single keys and shortcuts (capture + use),
# media keys (capture). Sequences and the delay field are upstream's layout
# and have not been seen on this pad - the GUI keeps them locked until a
# capture confirms them.
SEQUENCES_VERIFIED = False

_MOD_ORDER = [("ctrl", 0x01), ("shift", 0x02), ("alt", 0x04), ("super", 0x08),
              ("rctrl", 0x10), ("rshift", 0x20), ("ralt", 0x40), ("rwin", 0x80)]
_MOD_LABEL = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "super": "Meta",
              "win": "Meta", "lctrl": "Ctrl", "lshift": "Shift", "lalt": "Alt",
              "lwin": "Meta", "rctrl": "Right Ctrl", "rshift": "Right Shift",
              "ralt": "AltGr", "rwin": "Right Meta"}
_KEY_LABEL = {"pageup": "Page Up", "pagedown": "Page Down", "printscreen": "Print",
              "scrolllock": "Scroll Lock", "capslock": "Caps Lock",
              "backspace": "Backspace", "leftbracket": "[", "rightbracket": "]",
              "backslash": "\\", "semicolon": ";", "quote": "'", "grave": "`",
              "comma": ",", "period": ".", "slash": "/", "minus": "-", "equal": "=",
              "esc": "Esc", "escape": "Esc", "return": "Enter"}
_KEYNAME_BY_CODE = {}
for _name, _code in proto.KEYCODES.items():
    _KEYNAME_BY_CODE.setdefault(_code, _name)


def normalize_keys(text):
    """' Ctrl + C ,' -> 'ctrl+c'. Doesn't validate; Binding does that."""
    alias = {"meta": "super"}          # Plasma says Meta; macropad.py says super
    steps = []
    for chunk in text.lower().split(","):
        parts = [alias.get(p.strip(), p.strip()) for p in chunk.split("+") if p.strip()]
        if parts:
            steps.append("+".join(parts))
    return ",".join(steps)


def pretty_step(step):
    parts = []
    for p in step.split("+"):
        p = p.strip().lower()
        if p in _MOD_LABEL:
            parts.append(_MOD_LABEL[p])
        else:
            parts.append(_KEY_LABEL.get(p, p.upper() if len(p) <= 3 else p.title()))
    return "+".join(parts)


# ----------------------------------------------------------------- binding

@dataclass(frozen=True)
class Binding:
    """
    What one control does. Stored in the CLI's own syntax, so every entry
    in a profile is exactly a `macropad.py set <control> <binding>` command.

        Binding(keys="ctrl+c")
        Binding(keys="a,b,c", delay=50)
        Binding(media="volumeup")
        Binding(none=True)          the control does nothing

    The kind is explicit rather than guessed from the string, because
    'pause' is both a keyboard key and a media key.
    """
    keys: str = ""
    media: str = ""
    delay: int = 0
    none: bool = False

    def validate(self):
        if sum(map(bool, (self.keys, self.media, self.none))) != 1:
            raise ValueError("a binding is exactly one of keys, media or nothing")
        if self.media and self.media not in proto.MEDIA_KEYS:
            raise ValueError(f"unknown media key '{self.media}'")
        if self.keys:
            proto.parse_keystroke(self.keys)        # raises ValueError
        if not 0 <= self.delay <= 0xFFFF:
            raise ValueError("delay must be 0-65535 ms")
        if self.delay and not self.keys:
            raise ValueError("delay only applies to key sequences")
        return self

    @property
    def steps(self):
        return len(proto.parse_keystroke(self.keys)) if self.keys else 1

    @property
    def is_nothing(self):
        return self.none

    def reports(self, report_id, action, layer=1):
        """The exact reports to send, built entirely by macropad.py."""
        self.validate()
        if self.none:
            # A keystroke with no modifier and no key: count 0, pair 00 00.
            # This is ch57x-keyboard-tool's encoding for a modifier-only key,
            # with no modifier. Needs confirming on hardware (see README).
            return proto.native_key(report_id, action, [(0, 0)], layer, 0)
        if self.media:
            return proto.native_media(report_id, action,
                                      proto.MEDIA_KEYS[self.media], layer)
        return proto.native_key(report_id, action,
                                proto.parse_keystroke(self.keys),
                                layer, self.delay)

    @property
    def is_sequence(self):
        return bool(self.keys) and self.steps > 1

    def label(self):
        """What a person reads on the key: 'Ctrl+Shift+N', 'Volume up'."""
        if self.none:
            return "Nothing"
        if self.media:
            return MEDIA_LABEL.get(self.media, self.media)
        text = ", ".join(pretty_step(s) for s in self.keys.split(",") if s.strip())
        return text + (f" ({self.delay} ms)" if self.delay else "")

    def to_json(self):
        if self.none:
            return {"none": True}
        if self.media:
            return {"media": self.media}
        d = {"keys": self.keys}
        if self.delay:
            d["delay"] = self.delay
        return d

    @classmethod
    def from_json(cls, d):
        return cls(keys=d.get("keys", ""), media=d.get("media", ""),
                   delay=int(d.get("delay", 0)),
                   none=bool(d.get("none", False))).validate()


# ------------------------------------------------------------------- files

def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "macropad"


def _atomic_write_json(path, data):
    """Write-then-rename, so a crash mid-save never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


FORMAT = 2      # 1 had no layout, and one build saved rows and cols swapped


@dataclass
class Entry:
    """One control's line in the shadow state."""
    binding: Binding = None      # what we believe is on the pad
    written: str = ""            # ISO timestamp of the successful write
    unknown: bool = True         # True = pad contents not known
    reason: str = "not written by this app yet"


class State:
    """
    Shadow copy of the pad. Keyed by (layer, control). Only the writer
    should call record()/mark_unknown(); the GUI just reads it.
    """

    def __init__(self, path=None, layout=None):
        self.path = Path(path) if path else config_dir() / f"state-{VID}-{PID}.json"
        self.entries = {}                    # (layer, control) -> Entry
        self.layout = layout or DEFAULT_LAYOUT
        if self.path.exists():
            self._load()

    def _load(self):
        data = json.loads(self.path.read_text())
        version = data.get("format")
        if version not in (1, FORMAT):
            raise ValueError(f"{self.path}: unsupported format {version}")
        # Format 1 files either have no layout or have one from the build that
        # stored rows and columns the wrong way round. Ignore it either way;
        # the bindings are still good.
        if version == FORMAT and data.get("layout"):
            self.layout = Layout.from_json(data["layout"])
        for layer, controls in data.get("layers", {}).items():
            for control, e in controls.items():
                if e.get("unknown"):
                    entry = Entry(unknown=True, reason=e.get("reason", ""))
                else:
                    entry = Entry(binding=Binding.from_json(e["binding"]),
                                  written=e.get("written", ""), unknown=False,
                                  reason="")
                self.entries[(int(layer), control)] = entry

    def save(self):
        layers = {}
        for (layer, control), e in sorted(self.entries.items()):
            if e.unknown:
                d = {"unknown": True, "reason": e.reason}
            else:
                d = {"binding": e.binding.to_json(), "written": e.written}
            layers.setdefault(str(layer), {})[control] = d
        _atomic_write_json(self.path, {"format": FORMAT, "device": DEVICE_ID,
                                       "layout": self.layout.to_json(),
                                       "layers": layers})

    def get(self, control, layer=1):
        return self.entries.get((layer, control), Entry())

    def record(self, control, binding, layer=1):
        self.entries[(layer, control)] = Entry(
            binding=binding, unknown=False, reason="",
            written=datetime.now().isoformat(timespec="seconds"))

    def mark_unknown(self, control, reason, layer=1):
        self.entries[(layer, control)] = Entry(unknown=True, reason=reason)


# ---------------------------------------------------------------- diagnosis

@dataclass
class Device:
    path: str
    report_id: int
    writable: bool
    name: str = ""
    vid: str = VID
    pid: str = PID


@dataclass
class Diagnosis:
    status: str      # ready | unplugged | no-interface | no-permission | unsupported
    headline: str
    device: Device = None
    detail: list = field(default_factory=list)
    fix: list = field(default_factory=list)       # works in bash, zsh and fish
    warnings: list = field(default_factory=list)

    @property
    def ready(self):
        return self.status == "ready"


RULES_DIRS = ("/etc/udev/rules.d", "/run/udev/rules.d")
SYSTEM_RULES = ("/usr/lib/udev/rules.d", "/lib/udev/rules.d")
SUGGESTED_RULE = "60-macropad.rules"
RULE_LINE = (f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{VID}", '
             f'ATTRS{{idProduct}}=="{PID}", TAG+="uaccess"')


def _uaccess_cutoff():
    """
    The rules file that turns a uaccess tag into an actual ACL. On systemd
    this is 73-seat-late.rules. udev runs every rules file in one lexical
    order by filename, so a rule that adds the tag must sort before this one
    - 99-foo.rules is tagged but never granted, and so is an unnumbered
    foo.rules, since letters sort after digits.
    """
    for d in SYSTEM_RULES:
        for f in sorted(glob.glob(os.path.join(d, "*.rules"))):
            try:
                if 'RUN{builtin}+="uaccess"' in open(f).read():
                    return os.path.basename(f)
            except OSError:
                continue
    return "73-seat-late.rules"


def _matching_rules():
    """User rule files that mention our vendor id and uaccess."""
    hits = []
    for d in RULES_DIRS:
        for f in sorted(glob.glob(os.path.join(d, "*.rules"))):
            try:
                text = open(f).read()
            except OSError:
                continue
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("#"):
                    continue
                if re.search(rf'idVendor\}}=="{VID}"', s, re.I) and "uaccess" in s:
                    hits.append(f)
                    break
    return hits


def _udev_tags(devpath):
    try:
        out = subprocess.run(["udevadm", "info", "--query=property",
                              f"--name={devpath}"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    tags = set()
    for line in out.splitlines():
        if line.startswith(("TAGS=", "CURRENT_TAGS=")):
            tags |= {t for t in line.split("=", 1)[1].split(":") if t}
    return tags


def _on_usb_bus():
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            v = open(os.path.join(d, "idVendor")).read().strip().lower()
            p = open(os.path.join(d, "idProduct")).read().strip().lower()
        except OSError:
            continue
        if (v, p) == (VID, PID):
            return True
    return False


def _keyd_warning():
    """keyd sits between the pad and the desktop and can rewrite its output."""
    if not shutil.which("keyd"):
        return None
    try:
        active = subprocess.run(["systemctl", "is-active", "keyd"],
                                capture_output=True, text=True,
                                timeout=3).stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not active:
        return None
    for f in glob.glob("/etc/keyd/*.conf"):
        try:
            if DEVICE_ID in open(f).read().lower():
                return (f"keyd, a key remapping service, is running and {f} "
                        "lists this pad. It can change keys on their way from "
                        "the pad to your desktop, so a key may not do exactly "
                        "what you set here. If you only set keyd up for this "
                        "pad, you can switch it off with: "
                        "sudo systemctl disable --now keyd")
        except OSError:
            continue
    return None


def diagnose(keyd=True):
    """keyd=False skips the keyd check, which shells out - the GUI polls."""
    warnings = [w for w in (_keyd_warning() if keyd else None,) if w]
    everything = proto.find_devices()
    found = [d for d in everything if d["pid"] == PID]

    if not found and everything:
        other = everything[0]
        dev = Device(path=other["path"], report_id=other["report_id"],
                     writable=other["writable"], name=other.get("name", ""),
                     vid=other["vid"], pid=other["pid"])
        return Diagnosis(
            "unsupported",
            f"Found a {other['vid']}:{other['pid']} pad, which isn't one this "
            "app knows.",
            device=dev,
            detail=["Pads in this family differ in both layout and protocol, "
                    "so the app won't write to one it hasn't identified.",
                    "It can ask the pad about itself first. That only reads, "
                    "and if the pad doesn't answer properly nothing else "
                    "happens."],
            warnings=warnings)

    if not found:
        if _on_usb_bus():
            return Diagnosis(
                "no-interface",
                "Pad is plugged in, but its configuration interface is missing.",
                detail=["The USB device is present, but no hidraw node with the "
                        "vendor-defined config interface was found.",
                        "Usually a replug fixes this. If not, run "
                        "macropad-probe.py and check the interface list."],
                warnings=warnings)
        return Diagnosis("unplugged", "No pad connected.",
                         detail=[f"Looking for {DEVICE_ID} on USB."],
                         warnings=warnings)

    d = found[0]
    dev = Device(path=d["path"], report_id=d["report_id"],
                 writable=d["writable"], name=d.get("name", ""),
                 vid=d["vid"], pid=d["pid"])

    if dev.writable:
        return Diagnosis("ready", f"Ready on {dev.path}.", device=dev,
                         detail=[f"report id {dev.report_id}"], warnings=warnings)

    # Not writable: work out which of the three usual causes it is.
    cutoff = _uaccess_cutoff()
    rules = _matching_rules()
    late = [r for r in rules if os.path.basename(r) >= cutoff]
    early = [r for r in rules if os.path.basename(r) < cutoff]
    tags = _udev_tags(dev.path)
    tagged = bool(tags and "uaccess" in tags)

    diag = Diagnosis("no-permission", f"{dev.path} isn't writable.",
                     device=dev, warnings=warnings)

    if late and not early:
        bad = late[0]
        new = os.path.join(os.path.dirname(bad), SUGGESTED_RULE)
        diag.detail = [
            f"{bad} matches the pad, but it runs after {cutoff}, which is "
            "where access is actually granted. The device gets tagged, "
            "then nothing acts on the tag.",
            "Rename it so it sorts earlier."]
        diag.fix = [f"sudo mv {bad} {new}",
                    "sudo udevadm control --reload-rules && sudo udevadm trigger",
                    "# then unplug and replug the pad"]
    elif early and not tagged:
        diag.detail = [f"{early[0]} looks right, but the device hasn't picked "
                       "it up yet."]
        diag.fix = ["sudo udevadm control --reload-rules && sudo udevadm trigger",
                    "# then unplug and replug the pad"]
    elif early and tagged:
        diag.detail = [
            "The rule matched and the device is tagged, but no access was "
            "granted. That happens when you aren't in an active local desktop "
            "session - for example over SSH, or in a shell started with sudo."]
    else:
        diag.detail = ["No udev rule lets you write to the pad yet. Running "
                       "the installer again adds it, or paste these commands "
                       "into a terminal:"]
        diag.fix = [f"echo '{RULE_LINE}' | sudo tee /etc/udev/rules.d/{SUGGESTED_RULE}",
                    "sudo udevadm control --reload-rules && sudo udevadm trigger",
                    "# then unplug and replug the pad"]
    return diag


# ------------------------------------------------------------------ writer

class WriteError(Exception):
    """
    pad_touched is the thing the UI cares about: False means the pad is
    exactly as it was; True means some reports landed and the control's
    contents are now unknown.
    """
    def __init__(self, message, pad_touched, cause=None):
        super().__init__(message)
        self.pad_touched = pad_touched
        self.cause = cause


def _explain(err):
    return {
        errno.ENOENT: "the pad was unplugged",
        errno.ENODEV: "the pad was unplugged",
        errno.EACCES: "no permission to write to the pad",
        errno.EPERM: "no permission to write to the pad",
        errno.EPIPE: "the pad rejected the report",
        errno.EIO: "the pad stopped responding (it may have reset)",
        errno.ETIMEDOUT: "the pad didn't accept the report in time",
    }.get(err.errno, err.strerror or str(err))


def write_binding(device, control, binding, state, layer=1, _write=None):
    """
    Send one binding (config report + commit) and update the shadow state.
    Raises WriteError; on success the state file is already saved.
    """
    write = _write or proto.write_report
    reports = binding.reports(device.report_id, action_byte(control), layer)

    sent = 0
    try:
        for r in reports:
            write(device.path, r)
            sent += 1
            time.sleep(WRITE_GAP)
    except OSError as e:
        why = _explain(e)
        # If even the first write failed on open, nothing reached the pad.
        untouched = sent == 0 and e.errno in (errno.ENOENT, errno.ENODEV,
                                              errno.EACCES, errno.EPERM)
        if not untouched:
            state.mark_unknown(control, f"write interrupted after {sent} of "
                               f"{len(reports)} reports: {why}", layer)
            state.save()
        raise WriteError(f"{control}: {why}", pad_touched=not untouched, cause=e)

    state.record(control, binding, layer)
    state.save()


# --------------------------------------------------------------- decoding

def decode_info(report):
    """(keys, knobs) from a 0xFB reply, or None."""
    if len(report) < 4 or report[1] != proto.MAGIC_INFO:
        return None
    return report[2], report[3]


def decode_config(report):
    """
    The inverse of Binding.reports()[0]: (control, layer, Binding) from a
    native config report, or None if it isn't one we understand. Used to
    import captures and to prove round trips in the tests.
    """
    if len(report) < 13 or report[1] not in (proto.MAGIC_NATIVE, proto.MAGIC_READ):
        return None
    action, layer, kind = report[2], report[3], report[4]
    control = CONTROL_FOR_ACTION.get(action)
    if control is None:
        return None
    delay = report[5] | (report[6] << 8)
    count = report[10]

    if kind == proto.KeyType.MULTIMEDIA and count:
        media = _MEDIA_BY_USAGE.get(report[11] | (report[12] << 8))
        return (control, layer, Binding(media=media)) if media else None

    if count == 0 and kind in (proto.KeyType.NONE, proto.KeyType.BASIC):
        mods, code = report[11], report[12]
        if not mods and not code:
            return control, layer, Binding(none=True)
        return None

    if kind in (proto.KeyType.NONE, proto.KeyType.BASIC) and count:
        steps = []
        for i in range(count):
            off = 11 + 2 * i
            if off + 1 >= len(report):
                return None
            mods, code = report[off], report[off + 1]
            names = [n for n, bit in _MOD_ORDER if mods & bit]
            if code:
                if code not in _KEYNAME_BY_CODE:
                    return None
                names.append(_KEYNAME_BY_CODE[code])
            steps.append("+".join(names))
        return control, layer, Binding(keys=",".join(steps), delay=delay)
    return None


def is_commit(report):
    return len(report) >= 4 and report[1] == proto.MAGIC_NATIVE and \
        (report[2], report[3]) == proto.TERMINATOR


def read_capture(path, incoming=False):
    """
    Reports from a macropad-capture.py output file, in order.

    Captures can now hold both directions. By default we return only what
    was sent TO the pad, since that is what a binding looks like; pass
    incoming=True for what the pad sent back.
    """
    out = []
    want = " in " if incoming else " out "
    keep = False
    for line in Path(path).read_text().splitlines():
        if line.startswith("["):
            keep = want in f" {line.split(']', 1)[-1].strip()} "
        elif line.startswith("    ") and keep:
            try:
                out.append(bytes.fromhex(line.strip()))
            except ValueError:
                continue
    return out


def import_capture(path, state):
    """
    Record what a vendor-software session wrote, so the GUI knows what's on
    the pad. Only bindings followed by a commit count - the vendor app can
    send many configs and save once. Returns [(control, layer, Binding)].
    """
    pending, done = [], []
    for report in read_capture(path):
        if is_commit(report):
            done.extend(pending)
            pending = []
            continue
        decoded = decode_config(report)
        if decoded:
            pending = [p for p in pending if p[:2] != decoded[:2]] + [decoded]
    for control, layer, binding in done:
        state.record(control, binding, layer)
    if done:
        state.save()
    return done


# ---------------------------------------------------------------- reading

READ_TIMEOUT = 1.5       # total, per request
READ_QUIET = 0.25        # replies have stopped once this much silence passes


class ReadError(Exception):
    pass


def _exchange(path, request, want=None):
    """
    Send one request and collect replies until the pad goes quiet.

    Reading is the one place we write to the pad without changing it: both
    commands here are queries, byte for byte what the vendor software sends.
    """
    import select

    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        raise ReadError(_explain(e)) from e

    replies = []
    try:
        while select.select([fd], [], [], 0)[0]:      # drain anything stale
            try:
                os.read(fd, 128)
            except OSError:
                break
        try:
            os.write(fd, request)
        except OSError as e:
            raise ReadError(_explain(e)) from e

        deadline = time.monotonic() + READ_TIMEOUT
        last = time.monotonic()
        while time.monotonic() < deadline:
            if not select.select([fd], [], [], 0.05)[0]:
                if replies and time.monotonic() - last > READ_QUIET:
                    break
                continue
            try:
                data = os.read(fd, 128)
            except BlockingIOError:
                continue
            except OSError as e:
                raise ReadError(_explain(e)) from e
            if not data:
                continue
            last = time.monotonic()
            replies.append(data)
            if want is not None and len(replies) >= want:
                break
    finally:
        os.close(fd)
    return replies


def read_info(device):
    """(keys, knobs) as the pad reports them, or None if it doesn't answer."""
    for reply in _exchange(device.path, proto.native_info(device.report_id), want=1):
        got = decode_info(reply)
        if got:
            return got
    return None


def read_layer(device, layer=1):
    """{control: Binding} for one layer, as it is on the pad right now."""
    out = {}
    for reply in _exchange(device.path, proto.native_read(device.report_id, layer)):
        decoded = decode_config(reply)
        if not decoded:
            continue
        control, got_layer, binding = decoded
        if got_layer == layer:
            out[control] = binding
    return out


def read_into_state(device, state, layer=1):
    """Read the pad and make the shadow state match it. Returns what was read."""
    bindings = read_layer(device, layer)
    if not bindings:
        raise ReadError("the pad didn't answer")
    for control, binding in bindings.items():
        state.record(control, binding, layer)
    state.save()
    return bindings


# -------------------------------------------------------------- detection

@dataclass
class Detection:
    """What a read-only interrogation of an unrecognised pad turned up."""
    speaks: bool = False          # it answered our protocol properly
    keys: int = 0
    knobs: int = 0
    layers: int = 0
    bindings: dict = field(default_factory=dict)
    why: str = ""                 # when speaks is False, what went wrong

    @property
    def layout(self):
        """
        A first guess at the shape. The pad reports counts, not geometry,
        so rows and columns are a guess the owner should confirm.
        """
        if not self.speaks:
            return None
        cols = 3 if self.keys % 3 == 0 else (4 if self.keys % 4 == 0 else self.keys)
        rows = -(-self.keys // cols)
        return Layout(self.keys, self.knobs, rows, cols)


def detect(device):
    """
    Ask an unknown pad what it is, without configuring anything.

    Both commands are queries. We only conclude the pad speaks this
    protocol if it answers both in the right shape: a well formed info
    reply, and a layer whose controls are exactly the slots that reply
    implies. A pad from a different family will fail one of those and we
    leave it alone.
    """
    try:
        info = read_info(device)
    except ReadError as e:
        return Detection(why=f"it didn't answer: {e}")
    if not info:
        return Detection(why="it didn't answer the 'what are you' query")

    keys, knobs = info
    if not 1 <= keys <= proto.KEY_SLOTS or not 0 <= knobs <= proto.KNOB_SLOTS:
        return Detection(why=f"it reported {keys} keys and {knobs} knobs, "
                             "which isn't a pad this protocol can describe")

    probe = Layout(keys, knobs, rows=keys, cols=1)      # geometry irrelevant here
    expected = set(probe.control_for_action)

    try:
        replies = _exchange(device.path, proto.native_read(device.report_id, 1))
    except ReadError as e:
        return Detection(why=f"it answered the first query but not the second: {e}")

    seen, bindings = set(), {}
    for reply in replies:
        if len(reply) < 13 or reply[1] != proto.MAGIC_READ:
            continue
        action, layer = reply[2], reply[3]
        seen.add(action)
        if action not in expected or layer != 1:
            continue
        control = probe.control_for_action[action]
        decoded = decode_config(reply)
        if decoded:
            bindings[control] = decoded[2]

    missing = expected - seen
    if missing:
        return Detection(why=f"it described {keys} keys and {knobs} knobs but "
                             f"didn't report {len(missing)} of them, so this "
                             "isn't the protocol it speaks")

    return Detection(speaks=True, keys=keys, knobs=knobs,
                     layers=proto.LAYERS, bindings=bindings)


def detection_report(device, detected):
    """A block of text to paste into an issue when adding a new pad."""
    lines = [f"Device: {device.vid}:{device.pid}",
             f"Name:   {device.name or 'not reported'}",
             f"Node:   {device.path}, report id {device.report_id}", ""]
    if not detected.speaks:
        lines += ["This pad did NOT answer the 1189:8840 protocol.",
                  f"Reason: {detected.why}"]
        return "\n".join(lines)
    lines += [f"The pad reports {detected.keys} keys and {detected.knobs} knobs.",
              f"It answered a layer read for every slot that implies.", "",
              "Layer 1 as read from the pad:"]
    for control, binding in sorted(detected.bindings.items(),
                                   key=lambda kv: action_byte(kv[0])):
        lines.append(f"  0x{action_byte(control):02x}  {control:<12} {binding.label()}")
    return "\n".join(lines)
