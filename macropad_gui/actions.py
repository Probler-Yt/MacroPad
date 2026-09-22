"""
Host actions: make a pad key open a URL, launch an app, or run a command.

The pad can't do any of that itself. All it can ever send is a key press.
So an action is two halves:

  on the pad    the control is bound to a spare key, one of F13 to F24,
                which no normal keyboard has and no desktop binds
  on the host   a small listener watches the pad's keyboard interface and,
                when it sees one of those keys, runs the action

The listener is optional and off unless you turn it on. When it isn't
running, action keys send F13 to F24 and nothing happens, and every other
key on the pad works exactly as before.

It reads the pad's keyboard interface through /dev/hidraw, the same way the
rest of the app talks to the pad, so the udev rule that already exists is
all the permission it needs. Reading hidraw doesn't take the key away from
the desktop; it just gets a copy.
"""

import errno
import fcntl
import glob
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import core

proto = core.proto

TRIGGERS = tuple(f"f{i}" for i in range(13, 25))
CODE_TO_TRIGGER = {proto.KEYCODES[t]: t for t in TRIGGERS}

# Linux's keyboard layer gives some of these keys a meaning of their own, so
# the desktop acts on them as well as the listener. F13, F20 and F21 were
# confirmed on Plasma 6; F22 and F23 are the touchpad on and off keys from
# the same table. They can still be chosen, but they're picked last.
CLAIMED = {
    "f13": "often opens System Settings",
    "f20": "often mutes the microphone",
    "f21": "often toggles the touchpad",
    "f22": "may switch the touchpad on",
    "f23": "may switch the touchpad off",
}
# the order the automatic pick tries them in
PREFERRED = tuple(sorted(TRIGGERS, key=lambda t: (t in CLAIMED, int(t[1:]))))
KINDS = ("url", "app", "command", "text")
PASTE_KEYS = ("ctrl+v", "ctrl+shift+v")


# ------------------------------------------------------------------ action

@dataclass(frozen=True)
class Action:
    """What the computer does when a trigger key arrives."""
    kind: str               # url | app | command | text
    target: str             # the URL, the command line, or the text to type
    name: str = ""          # for apps, the name people recognise
    paste: str = ""         # for text: the paste shortcut, ctrl+v unless set

    def validate(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown action kind '{self.kind}'")
        if not self.target.strip():
            raise ValueError({"url": "enter a web address",
                              "app": "pick an app",
                              "command": "enter a command",
                              "text": "enter the text to type"}[self.kind])
        if self.paste and self.paste not in PASTE_KEYS:
            raise ValueError(f"unknown paste shortcut '{self.paste}'")
        if self.kind == "url" and " " in self.target.strip():
            raise ValueError("a web address can't contain spaces")
        return self

    @property
    def url(self):
        """URLs without a scheme get https, so 'github.com' just works."""
        t = self.target.strip()
        if self.kind != "url" or re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I):
            return t
        return "https://" + t

    def label(self):
        """Short enough for a keycap."""
        if self.kind == "url":
            host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", self.url, flags=re.I)
            host = host.split("/")[0].removeprefix("www.")
            return f"Open {host}"
        if self.kind == "app":
            return self.name or self.target.split()[0].rsplit("/", 1)[-1]
        if self.kind == "text":
            line = self.target.strip().splitlines()[0]
            short = line if len(line) <= 16 else line[:15].rstrip() + "…"
            return f'Type "{short}"'
        first = self.target.strip().split()[0].rsplit("/", 1)[-1]
        return f"Run {first}"

    def argv(self):
        """What to execute. Shell only where the user wrote a shell command."""
        if self.kind == "url":
            return ["xdg-open", self.url]
        if self.kind == "app":
            try:
                return shlex.split(self.target)
            except ValueError:
                return ["sh", "-c", self.target]
        return ["sh", "-c", self.target]

    def run(self, _popen=None):
        """Start it and walk away. The listener must never block on an app."""
        if self.kind == "text":
            return type_text(self.target, self.paste or "ctrl+v")
        return (_popen or subprocess.Popen)(self.argv(), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True)

    def to_json(self):
        d = {"kind": self.kind, "target": self.target}
        if self.name:
            d["name"] = self.name
        if self.paste:
            d["paste"] = self.paste
        return d

    @classmethod
    def from_json(cls, d):
        return cls(kind=d["kind"], target=d["target"], name=d.get("name", ""),
                   paste=d.get("paste", "")).validate()


# ------------------------------------------------------------------- store

class ActionStore:
    """
    trigger key -> Action, in ~/.config/macropad/actions.json.

    Keyed by trigger rather than by control, because that's what the
    listener sees: a key, not a position on the pad. It also means reading
    the pad back still makes sense, since a control bound to F17 can be
    looked up here.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else core.config_dir() / "actions.json"
        self.by_trigger = {}
        self._mtime = None
        self.reload()

    def reload(self):
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self.by_trigger, self._mtime = {}, None
            return
        if st.st_mtime == self._mtime:
            return
        data = json.loads(self.path.read_text())
        self.by_trigger = {t: Action.from_json(a)
                           for t, a in data.get("actions", {}).items()
                           if t in TRIGGERS}
        self._mtime = st.st_mtime

    def save(self):
        core._atomic_write_json(self.path, {
            "format": 1,
            "actions": {t: a.to_json() for t, a in sorted(self.by_trigger.items())}})
        self._mtime = self.path.stat().st_mtime

    def get(self, trigger):
        return self.by_trigger.get(trigger)

    def set(self, trigger, action):
        if trigger not in TRIGGERS:
            raise ValueError(f"{trigger} isn't a trigger key")
        self.by_trigger[trigger] = action.validate()

    def remove(self, trigger):
        self.by_trigger.pop(trigger, None)

    def prune(self, bound_keys):
        """
        Forget actions whose trigger no longer sits on any control. Only
        call this when every control's binding is known, or it may throw
        away an action that's still on the pad.
        """
        gone = [t for t in self.by_trigger if t not in bound_keys]
        for t in gone:
            del self.by_trigger[t]
        return gone


def trigger_of(binding):
    """The trigger key a binding sends, or None if it isn't one."""
    if binding and binding.keys and binding.keys.lower() in TRIGGERS:
        return binding.keys.lower()
    return None


def trigger_free(trigger, store, used_keys, current=None):
    """Can this control use this trigger without clashing with another?"""
    if trigger == current:
        return True
    return trigger not in store.by_trigger and trigger not in used_keys


def pick_trigger(store, used_keys, current=None, want=None):
    """
    A trigger key for a control.

    If the person asked for a particular one and it's free, that. Otherwise
    keep the one the control already has, so editing an action doesn't need
    a write to the pad. Otherwise the first free one, trying the keys the
    desktop leaves alone before the ones it's known to act on. None when all
    twelve are taken.
    """
    if want in TRIGGERS and trigger_free(want, store, used_keys, current):
        return want
    if current in TRIGGERS:
        return current
    for t in PREFERRED:
        if trigger_free(t, store, used_keys):
            return t
    return None


# -------------------------------------------------------------- typing text
#
# Typing a phrase is harder than it sounds on Linux. Keyboards send key
# positions, not letters, so typing "£" or "@" key by key means knowing the
# person's layout, and emoji can't be typed that way at all. So we do what
# text expanders like espanso do for anything longer than a word: put the
# text on the clipboard, then press the paste shortcut. That works for any
# text in any layout.
#
# Pressing the shortcut means sending key presses, which on Linux goes
# through /dev/uinput: a virtual keyboard. It needs one permission rule,
# the same one Steam installs for its controller support.

UINPUT = "/dev/uinput"
UINPUT_RULE = ('KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", '
               'OPTIONS+="static_node=uinput"')

_EV_SYN, _EV_KEY, _SYN_REPORT = 0, 1, 0
_UI_SET_EVBIT = 0x40045564          # _IOW('U', 100, int)
_UI_SET_KEYBIT = 0x40045565         # _IOW('U', 101, int)
_UI_DEV_CREATE = 0x5501             # _IO('U', 1)
_UI_DEV_DESTROY = 0x5502            # _IO('U', 2)
_KEY = {"ctrl": 29, "shift": 42, "v": 47}


class VirtualKeyboard:
    """
    A tiny keyboard that only knows Ctrl, Shift and V. Created once and kept,
    because a brand new input device takes the compositor a moment to notice,
    and the first paste would otherwise go missing.
    """

    def __init__(self, path=UINPUT, _open=os.open, _ioctl=fcntl.ioctl,
                 _write=os.write, _sleep=time.sleep):
        import struct
        self._struct, self._write, self._sleep = struct, _write, _sleep
        self.fd = _open(path, os.O_WRONLY | os.O_NONBLOCK)
        _ioctl(self.fd, _UI_SET_EVBIT, _EV_KEY)
        for code in _KEY.values():
            _ioctl(self.fd, _UI_SET_KEYBIT, code)
        name = b"MacroPad virtual keyboard"
        # struct uinput_user_dev: name, input_id, ff_effects_max, 4 x 64 abs
        dev = struct.pack("<80sHHHHI256i", name, 0x03, 0x1189, 0x8841, 1, 0,
                          *([0] * 256))
        _write(self.fd, dev)
        _ioctl(self.fd, _UI_DEV_CREATE)
        _sleep(0.3)
        self._ioctl = _ioctl

    def _emit(self, typ, code, value):
        self._write(self.fd, self._struct.pack("@llHHi", 0, 0, typ, code, value))

    def press(self, combo):
        """Hold the modifiers, tap the key, let go, in that order."""
        keys = [_KEY[k] for k in combo.split("+")]
        for code in keys:
            self._emit(_EV_KEY, code, 1)
        self._emit(_EV_SYN, _SYN_REPORT, 0)
        self._sleep(0.02)
        for code in reversed(keys):
            self._emit(_EV_KEY, code, 0)
        self._emit(_EV_SYN, _SYN_REPORT, 0)

    def close(self):
        try:
            self._ioctl(self.fd, _UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


_keyboard = None


def _virtual_keyboard():
    global _keyboard
    if _keyboard is None:
        _keyboard = VirtualKeyboard()
    return _keyboard


def on_wayland():
    return bool(os.environ.get("WAYLAND_DISPLAY")) or \
        os.environ.get("XDG_SESSION_TYPE") == "wayland"


def _clipboard_tool():
    """The program that can set the clipboard in this session, or None."""
    import shutil
    if on_wayland():
        return ["wl-copy"] if shutil.which("wl-copy") else None
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def set_clipboard(text, _run=subprocess.run):
    tool = _clipboard_tool()
    if not tool:
        raise OSError(errno.ENOENT, "no clipboard tool installed")
    # wl-copy and xclip hand the text to a background copy of themselves and
    # return straight away, so this doesn't hang.
    _run(tool, input=text.encode(), check=True, timeout=5,
         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def type_text(text, paste="ctrl+v", _clip=None, _keyboard_factory=None):
    """Put the text on the clipboard and press paste."""
    (_clip or set_clipboard)(text)
    time.sleep(0.08)                  # let the clipboard change land first
    (_keyboard_factory or _virtual_keyboard)().press(paste)


def _install_hint(package):
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        text = ""
    family = " ".join(l.split("=", 1)[1].strip('"') for l in text.splitlines()
                      if l.startswith(("ID=", "ID_LIKE=")))
    if "arch" in family:
        return f"sudo pacman -S {package}"
    if "fedora" in family or "rhel" in family:
        return f"sudo dnf install {package}"
    if "debian" in family or "ubuntu" in family:
        return f"sudo apt install {package}"
    return f"# install the {package} package with your package manager"


def text_problems():
    """
    What stands between this computer and typing text, as
    [(what's wrong, [commands that fix it])]. Empty means ready.
    """
    out = []
    if not os.access(UINPUT, os.W_OK):
        out.append((
            "MacroPad needs permission to press the paste shortcut for you. "
            "It's the same permission Steam asks for.",
            [f"echo '{UINPUT_RULE}' | sudo tee /etc/udev/rules.d/60-macropad-uinput.rules",
             "sudo modprobe uinput",
             "sudo udevadm control --reload-rules && sudo udevadm trigger --sysname-match=uinput"]))
    if not _clipboard_tool():
        package = "wl-clipboard" if on_wayland() else "xclip"
        out.append((f"It also needs {package} to put the text on the clipboard.",
                    [_install_hint(package)]))
    return out


# ------------------------------------------------------------ installed apps

_FIELD_CODE = re.compile(r"^%[a-zA-Z]$")


def clean_exec(line):
    """
    Turn a .desktop Exec line into a command we can run. Drops the %U style
    field codes, and Flatpak's @@u / @@ markers, which only make sense to a
    launcher that is about to pass files.
    """
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    parts = [p for p in parts if not _FIELD_CODE.match(p) and not p.startswith("@@")]
    return shlex.join(p.replace("%%", "%") for p in parts)


def _app_dirs():
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    dirs = [home / "applications"]
    for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if d:
            dirs.append(Path(d) / "applications")
    dirs += [home / "flatpak/exports/share/applications",
             Path("/var/lib/flatpak/exports/share/applications")]
    return dirs


def installed_apps(dirs=None):
    """[(name, command)] for launchable apps, sorted, first definition wins."""
    seen, apps = set(), []
    for d in dirs or _app_dirs():
        for f in sorted(glob.glob(str(d / "*.desktop"))):
            ident = os.path.basename(f)
            if ident in seen:
                continue
            seen.add(ident)
            entry = _read_desktop_entry(f)
            if not entry or entry.get("Type") != "Application":
                continue
            if entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true":
                continue
            if entry.get("Name") and entry.get("Exec"):
                apps.append((entry["Name"], clean_exec(entry["Exec"])))
    return sorted(apps, key=lambda a: a[0].lower())


def _read_desktop_entry(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    entry, inside = {}, False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            inside = line == "[Desktop Entry]"
            continue
        if inside and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            entry.setdefault(k.strip(), v.strip())     # skip Name[fr] etc.
    return entry


# ----------------------------------------------------------------- listener

def _descriptor_keyboard_report_id(raw):
    """
    Does this HID descriptor describe a keyboard, and if so which report id
    carries it? (True, 0) means a keyboard with no report ids at all.
    """
    i, usage_page, usage, report_id = 0, 0, 0, 0
    stack, in_kbd = [], False
    while i < len(raw):
        b = raw[i]
        if b == 0xFE:                                   # long item
            i += 3 + (raw[i + 1] if i + 1 < len(raw) else 0)
            continue
        size = b & 3
        size = 4 if size == 3 else size
        tag, typ = b >> 4, (b >> 2) & 3
        data = int.from_bytes(raw[i + 1:i + 1 + size], "little")
        if typ == 1 and tag == 0:
            usage_page = data
        elif typ == 1 and tag == 8:
            report_id = data
        elif typ == 2 and tag == 0:
            usage = data
        elif typ == 0 and tag == 10:                    # collection
            stack.append(in_kbd)
            if usage_page == 1 and usage == 6:
                in_kbd = True
        elif typ == 0 and tag == 12:                    # end collection
            in_kbd = stack.pop() if stack else False
        elif typ == 0 and tag == 8 and in_kbd:          # an input item
            return True, report_id
        i += 1 + size
    return False, 0


def find_keyboard_nodes(vid=core.VID):
    """[(hidraw path, keyboard report id)] for every pad keyboard interface."""
    out = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        dev = os.path.join(node, "device")
        try:
            uevent = Path(dev, "uevent").read_text()
            raw = Path(dev, "report_descriptor").read_bytes()
        except OSError:
            continue
        hid_id = next((l.split("=", 1)[1] for l in uevent.splitlines()
                       if l.startswith("HID_ID=")), "")
        if hid_id.count(":") != 2 or hid_id.split(":")[1][-4:].lower() != vid:
            continue
        is_kbd, rid = _descriptor_keyboard_report_id(raw)
        if is_kbd:
            out.append(("/dev/" + os.path.basename(node), rid))
    return out


def pressed_codes(report, report_id):
    """
    The keycodes held down in one keyboard report, or None if this report
    isn't a keyboard one. Standard layout: [id], modifiers, reserved, six
    key slots.
    """
    if report_id:
        if not report or report[0] != report_id:
            return None
        report = report[1:]
    if len(report) < 3:
        return None
    return {c for c in report[2:8] if c}


class Listener:
    """
    Turns trigger key presses into actions. Fires once per press, on the
    way down, however long the key is held.
    """

    def __init__(self, store, log=print, run=None):
        self.store = store
        self.log = log
        self.run = run or (lambda action: action.run())
        self.held = set()

    def feed(self, codes):
        if codes is None:
            return []
        fired = []
        for code in codes - self.held:
            trigger = CODE_TO_TRIGGER.get(code)
            if not trigger:
                continue
            action = self.store.get(trigger)
            if not action:
                self.log(f"{trigger.upper()} pressed, but no action is set for it")
                continue
            self.log(f"{trigger.upper()}: {action.label()}")
            try:
                self.run(action)
                fired.append(trigger)
            except OSError as e:
                self.log(f"couldn't run {action.label()}: {e.strerror or e}")
        self.held = set(codes)
        return fired


def runtime_dir():
    d = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(d)


LOCK = "macropad-actions.lock"


def listen(debug=False, log=print):
    """
    Run until stopped. Survives the pad being unplugged, and picks up edits
    to actions.json without a restart.
    """
    lock_path = runtime_dir() / LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Actions are already running.")
        return 1
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("now", True))

    store = ActionStore()
    listener = Listener(store, log=log)
    log(f"Listening for F13 to F24 on the pad. {len(store.by_trigger)} action(s) set.")

    fds, waiting_said = {}, False
    try:
        while not stop["now"]:
            if not fds:
                for path, rid in find_keyboard_nodes():
                    try:
                        fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = (path, rid)
                    except OSError as e:
                        log(f"can't open {path}: {e.strerror}")
                if fds:
                    log("Pad found: " + ", ".join(p for p, _ in fds.values()))
                    waiting_said = False
                else:
                    if not waiting_said:
                        log("Waiting for the pad to be plugged in...")
                        waiting_said = True
                    time.sleep(2)
                    continue

            store.reload()
            ready, _, _ = select.select(list(fds), [], [], 1.0)
            for fd in ready:
                path, rid = fds[fd]
                try:
                    data = os.read(fd, 64)
                except BlockingIOError:
                    continue
                except OSError as e:
                    if e.errno in (errno.ENODEV, errno.EIO, errno.ENOENT):
                        log("Pad unplugged.")
                        for f in list(fds):
                            os.close(f)
                        fds.clear()
                        listener.held.clear()
                        break
                    raise
                if debug:
                    log(f"{path}: {data.hex(' ')}")
                listener.feed(pressed_codes(data, rid))
    except KeyboardInterrupt:
        pass
    finally:
        for f in fds:
            os.close(f)
        lock.close()
    log("Stopped.")
    return 0


# ---------------------------------------------------------------- autostart

def autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "macropad-actions.desktop"


def listener_command():
    """How to start the listener: the installed launcher if there is one."""
    wrapper = Path.home() / ".local/bin/macropad"
    if wrapper.exists():
        return [str(wrapper), "actions"], None
    return [sys.executable, "-m", "macropad_gui", "actions"], \
        str(Path(__file__).resolve().parent.parent)


def autostart_enabled():
    return autostart_path().exists()


def set_autostart(on):
    path = autostart_path()
    if not on:
        path.unlink(missing_ok=True)
        return
    argv, cwd = listener_command()
    lines = ["[Desktop Entry]", "Type=Application",
             "Name=MacroPad actions",
             "Comment=Runs the actions bound to your macro pad's keys",
             "Exec=" + " ".join(shlex.quote(a) for a in argv),
             "NoDisplay=true", "Terminal=false",
             "X-GNOME-Autostart-enabled=true"]
    if cwd:
        lines.append(f"Path={cwd}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def listener_pid():
    """The running listener's pid, checked against /proc so we never signal a stranger."""
    try:
        pid = int((runtime_dir() / LOCK).read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if b"actions" in cmd and b"macropad" in cmd else None


def start_listener():
    if listener_pid():
        return listener_pid()
    argv, cwd = listener_command()
    log = open(runtime_dir() / "macropad-actions.log", "a")
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    return p.pid


def stop_listener():
    pid = listener_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return pid
