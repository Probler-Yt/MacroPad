"""
Run from the repo root:   python3 -m unittest -v

The first test class is the important one. It replays the packets the
vendor's own software sent (captured over usbmon) and checks that the
path the GUI uses - Binding -> macropad.py - produces the same bytes.
If anyone edits the protocol layer and breaks it, this fails.
"""

import errno
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from macropad_gui import core
import macropad as proto

CAPTURES = Path(__file__).parent / "captures"


def load_capture(path):
    """[(label, bytes)] from a macropad-capture.py output file."""
    out, label = [], None
    for line in path.read_text().splitlines():
        m = re.match(r"\[(\d+)\] (.*)", line)
        if m:
            label = m.group(0)
        elif line.startswith("    ") and label:
            out.append((label, bytes.fromhex(line.strip())))
    return out


KEYNAME = {}
for _name, _code in proto.KEYCODES.items():
    KEYNAME.setdefault(_code, _name)


class CaptureReplay(unittest.TestCase):

    def test_dial_capture_reproduced_exactly(self):
        packets = load_capture(CAPTURES / "1189-8840-dials.txt")
        self.assertEqual(len(packets), 12)

        for i in range(0, len(packets), 2):
            (_, config), (_, commit) = packets[i], packets[i + 1]
            # usbmon's text interface stops at 32 bytes; the rest is unseen.
            self.assertEqual(len(config), 32)

            action, layer, mods, code = config[2], config[3], config[11], config[12]
            self.assertEqual(mods, 0)
            binding = core.Binding(keys=KEYNAME[code])
            ours = binding.reports(3, action, layer)

            self.assertEqual(len(ours), 2, "config + commit")
            self.assertEqual(ours[0][:32], config, f"config for action 0x{action:02x}")
            self.assertEqual(ours[1][:32], commit, "commit")
            self.assertEqual(len(ours[0]), 65)
            self.assertFalse(any(ours[0][32:]), "bytes past the capture are zero")

    def test_dial_action_order_matches_capture(self):
        packets = load_capture(CAPTURES / "1189-8840-dials.txt")
        captured = [p[2] for _, p in packets[0::2]]
        ours = [core.action_byte(c) for c in core.CONTROL_IDS if c.startswith("dial")]
        self.assertEqual(ours, captured)

    def test_keys_are_1_to_12(self):
        self.assertEqual([core.action_byte(k) for k in core.KEYS], list(range(1, 13)))


class EveryCapture(unittest.TestCase):
    """Every config packet in every capture must decode and re-encode exactly."""

    def test_round_trip_all_captures(self):
        seen = 0
        for path in sorted(CAPTURES.glob("*.txt")):
            for report in core.read_capture(path):
                if core.is_commit(report):
                    self.assertEqual(proto.native_commit(3)[:len(report)], report)
                    continue
                decoded = core.decode_config(report)
                self.assertIsNotNone(decoded, f"{path.name}: {report.hex()}")
                control, layer, binding = decoded
                ours = binding.reports(3, core.action_byte(control), layer)[0]
                self.assertEqual(ours[:len(report)], report,
                                 f"{path.name} {control}: ours {ours[:16].hex()}")
                seen += 1
        self.assertGreaterEqual(seen, 21)

    def test_media_keys_capture(self):
        """Vendor app: keys 1-12 set to media keys, saved with one commit."""
        want = ["next", "stop", "prev", "playpause", "volumedown", "volumeup",
                "mute", "calculator", "mail", "mycomputer",
                "brightnessdown", "brightnessup"]
        with tempfile.TemporaryDirectory() as d:
            state = core.State(Path(d) / "s.json")
            got = core.import_capture(CAPTURES / "1189-8840-media-keys.txt", state)
            self.assertEqual([c for c, _, _ in got][:12], list(core.KEYS))
            self.assertEqual([b.media for _, _, b in got][:12], want)
            # dial 1, slots labelled anticlockwise / press / clockwise -> 1 / 2 / 3
            self.assertEqual(state.get("dial1-left").binding, core.Binding(keys="1"))
            self.assertEqual(state.get("dial1-push").binding, core.Binding(keys="2"))
            self.assertEqual(state.get("dial1-right").binding, core.Binding(keys="3"))
            self.assertTrue(state.get("dial2-left").unknown)

    def test_uncommitted_configs_are_not_imported(self):
        with tempfile.TemporaryDirectory() as d:
            cap = Path(d) / "c.txt"
            cap.write_text("[1] interrupt out ep=4\n"
                           "    03fd010101000000000001000400\n")
            state = core.State(Path(d) / "s.json")
            self.assertEqual(core.import_capture(cap, state), [])
            self.assertTrue(state.get("key1").unknown)


class BindingModel(unittest.TestCase):

    def test_round_trip(self):
        for b in (core.Binding(keys="ctrl+shift+n"),
                  core.Binding(keys="a,b,c", delay=250),
                  core.Binding(media="volumeup")):
            self.assertEqual(core.Binding.from_json(json.loads(json.dumps(b.to_json()))), b)

    def test_rejects_nonsense(self):
        bad = [core.Binding(), core.Binding(keys="a", media="mute"),
               core.Binding(media="nope"), core.Binding(keys="ctrl+wat"),
               core.Binding(media="mute", delay=10), core.Binding(keys="a", delay=-1)]
        for b in bad:
            with self.assertRaises(ValueError, msg=repr(b)):
                b.validate()

    def test_normalize(self):
        self.assertEqual(core.normalize_keys(" Ctrl + C ,"), "ctrl+c")
        self.assertEqual(core.normalize_keys("a, b"), "a,b")
        self.assertEqual(core.normalize_keys("Meta+Ctrl+Left"), "super+ctrl+left")

    def test_labels(self):
        self.assertEqual(core.Binding(keys="ctrl+shift+n").label(), "Ctrl+Shift+N")
        self.assertEqual(core.Binding(keys="super+pageup").label(), "Meta+Page Up")
        self.assertEqual(core.Binding(media="mycomputer").label(), "My computer")

    def test_pause_is_unambiguous(self):
        key = core.Binding(keys="pause").reports(3, 1)[0]
        media = core.Binding(media="pause").reports(3, 1)[0]
        self.assertEqual(key[4], proto.KeyType.BASIC)
        self.assertEqual(media[4], proto.KeyType.MULTIMEDIA)


class ShadowState(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.dev = core.Device(path="/dev/null-pad", report_id=3, writable=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_everything_starts_unknown(self):
        s = core.State(self.path)
        self.assertTrue(all(s.get(c).unknown for c in core.CONTROL_IDS))

    def test_successful_write_is_recorded_and_persisted(self):
        sent = []
        s = core.State(self.path)
        b = core.Binding(keys="ctrl+c")
        core.write_binding(self.dev, "key4", b, s, _write=lambda p, r: sent.append(r))

        self.assertEqual([r[:4] for r in sent], [b"\x03\xfd\x04\x01", b"\x03\xfd\xfe\xff"])
        again = core.State(self.path)
        self.assertFalse(again.get("key4").unknown)
        self.assertEqual(again.get("key4").binding, b)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")], [])

    def test_failed_commit_leaves_control_unknown(self):
        s = core.State(self.path)
        s.record("key1", core.Binding(keys="a"))
        calls = []

        def flaky(path, report):
            calls.append(report)
            if len(calls) == 2:
                raise OSError(errno.EIO, "I/O error")

        with self.assertRaises(core.WriteError) as ctx:
            core.write_binding(self.dev, "key1", core.Binding(keys="b"), s, _write=flaky)
        self.assertTrue(ctx.exception.pad_touched)
        e = core.State(self.path).get("key1")
        self.assertTrue(e.unknown)
        self.assertIn("1 of 2", e.reason)

    def test_unplugged_before_first_write_changes_nothing(self):
        s = core.State(self.path)
        s.record("key1", core.Binding(keys="a"))
        s.save()

        def gone(path, report):
            raise FileNotFoundError(errno.ENOENT, "No such file")

        with self.assertRaises(core.WriteError) as ctx:
            core.write_binding(self.dev, "key1", core.Binding(keys="b"), s, _write=gone)
        self.assertFalse(ctx.exception.pad_touched)
        self.assertEqual(core.State(self.path).get("key1").binding, core.Binding(keys="a"))


class UdevOrdering(unittest.TestCase):
    """udev sorts rule files by name; the uaccess tag must land before the cutoff."""

    def test_ordering(self):
        cutoff = "73-seat-late.rules"
        self.assertLess("60-macropad.rules", cutoff)
        self.assertLess("70-macropad.rules", cutoff)
        self.assertGreaterEqual("99-macropad.rules", cutoff)
        self.assertGreaterEqual("macropad.rules", cutoff)    # letters sort after digits

    def test_diagnose_without_hardware_does_not_crash(self):
        d = core.diagnose()
        self.assertIn(d.status, {"ready", "unplugged", "no-interface", "no-permission"})


if __name__ == "__main__":
    unittest.main()


class PermissionDiagnosis(unittest.TestCase):
    """The three ways a pad ends up unwritable, with the system faked out."""

    def diag(self, rules, tags):
        from unittest import mock
        dev = [{"path": "/dev/hidraw10", "pid": "8840", "report_id": 3,
                "writable": False, "name": "USB Composite Device"}]
        with mock.patch.object(proto, "find_devices", return_value=dev), \
             mock.patch.object(core, "_matching_rules", return_value=rules), \
             mock.patch.object(core, "_udev_tags", return_value=tags), \
             mock.patch.object(core, "_uaccess_cutoff", return_value="73-seat-late.rules"), \
             mock.patch.object(core, "_keyd_warning", return_value=None):
            return core.diagnose()

    def test_rule_numbered_too_late(self):
        d = self.diag(["/etc/udev/rules.d/99-macropad.rules"], {"uaccess", "seat"})
        self.assertEqual(d.status, "no-permission")
        self.assertIn("sudo mv /etc/udev/rules.d/99-macropad.rules "
                      "/etc/udev/rules.d/60-macropad.rules", d.fix)

    def test_rule_not_loaded_yet(self):
        d = self.diag(["/etc/udev/rules.d/60-macropad.rules"], set())
        self.assertTrue(any("reload-rules" in c for c in d.fix))
        self.assertFalse(any(c.startswith("sudo mv") for c in d.fix))

    def test_no_rule(self):
        d = self.diag([], set())
        self.assertTrue(d.fix[0].startswith("echo 'KERNEL==\"hidraw*\""))
        self.assertIn("60-macropad.rules", d.fix[0])

    def test_other_family_pad_is_refused_not_ignored(self):
        from unittest import mock
        other = [{"path": "/dev/hidraw4", "vid": "1189", "pid": "8890",
                  "report_id": 0, "writable": True, "name": ""}]
        with mock.patch.object(proto, "find_devices", return_value=other), \
             mock.patch.object(core, "_keyd_warning", return_value=None):
            d = core.diagnose()
        self.assertEqual(d.status, "unsupported")
        self.assertIn("1189:8890", d.headline)
        self.assertIsNone(d.device)

    def test_rule_fine_but_not_local_session(self):
        d = self.diag(["/etc/udev/rules.d/60-macropad.rules"], {"uaccess"})
        self.assertEqual(d.fix, [])
        self.assertIn("SSH", d.detail[0])


class GuiSmoke(unittest.TestCase):
    """Builds the window offscreen. Skipped if Qt for Python isn't installed."""

    def test_window_builds_and_selects(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtWidgets import QApplication
            from macropad_gui import app
        except ImportError:
            self.skipTest("PySide6 not installed")
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            qa = QApplication.instance() or QApplication([])
            w = app.Window()
            w.timer.stop()
            for c in core.CONTROL_IDS:
                w.pad.select(c)
                w._refresh()
                self.assertEqual(w.inspector.title.text(), core.control_name(c))
            w._changed("key1", core.Binding(media="mute"))
            self.assertEqual(w._pending(), ["key1"])
            w.desired.clear()
            w.close()
