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
                if report[1] in (proto.MAGIC_INFO, proto.MAGIC_READ):
                    continue                      # a query, not a binding
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

    def test_capture_directions_are_separate(self):
        """A reply from the pad must never be mistaken for a binding."""
        with tempfile.TemporaryDirectory() as d:
            cap = Path(d) / "both.txt"
            cap.write_text(
                "[1] interrupt out ep=4\n    03fd010101000000000001000400\n"
                "[2] interrupt in ep=3\n    03fd020101000000000001001500\n"
                "[3] interrupt out ep=4\n    03fdfeff00000000\n")
            self.assertEqual(len(core.read_capture(cap)), 2)
            self.assertEqual(len(core.read_capture(cap, incoming=True)), 1)

            state = core.State(Path(d) / "s.json")
            got = core.import_capture(cap, state)
            self.assertEqual([(c, b.keys) for c, _, b in got], [("key1", "a")])
            self.assertTrue(state.get("key2").unknown)

    def test_uncommitted_configs_are_not_imported(self):
        with tempfile.TemporaryDirectory() as d:
            cap = Path(d) / "c.txt"
            cap.write_text("[1] interrupt out ep=4\n"
                           "    03fd010101000000000001000400\n")
            state = core.State(Path(d) / "s.json")
            self.assertEqual(core.import_capture(cap, state), [])
            self.assertTrue(state.get("key1").unknown)


class ReadingThePad(unittest.TestCase):
    """The vendor software's "view settings", captured and decoded."""

    def setUp(self):
        self.cap = CAPTURES / "1189-8840-read.txt"
        self.sent = core.read_capture(self.cap)
        self.got = core.read_capture(self.cap, incoming=True)

    def test_our_requests_match_the_vendors(self):
        info, read1, read2, read3 = self.sent
        # the vendor repeats the command byte; match it rather than assume
        self.assertEqual(proto.native_info(3)[:4], info[:4])
        for layer, sent in ((1, read1), (2, read2), (3, read3)):
            # the vendor's trailing bytes are uninitialised memory; ours are zero
            self.assertEqual(proto.native_read(3, layer)[:6], sent[:6])

    def test_info_reply(self):
        self.assertEqual(core.decode_info(self.got[0]), (12, 2))
        self.assertEqual(len(core.KEYS), 12)
        self.assertEqual(len(core.DIALS), 2)

    def test_layer_1_is_what_the_pad_holds(self):
        layer1 = {}
        for r in self.got:
            d = core.decode_config(r)
            if d and d[1] == 1:
                layer1[d[0]] = d[2]
        # dials, which is the mapping we could never confirm by writing alone
        self.assertEqual(layer1["dial1-left"], core.Binding(media="volumedown"))
        self.assertEqual(layer1["dial1-push"], core.Binding(media="mute"))
        self.assertEqual(layer1["dial1-right"], core.Binding(media="volumeup"))
        self.assertEqual(layer1["dial2-left"], core.Binding(media="brightnessdown"))
        self.assertEqual(layer1["dial2-push"], core.Binding(media="calculator"))
        self.assertEqual(layer1["dial2-right"], core.Binding(media="brightnessup"))
        # keys, including modifiers and an empty one
        self.assertEqual(layer1["key2"], core.Binding(keys="ctrl+alt+t"))
        self.assertEqual(layer1["key6"], core.Binding(keys="ctrl+shift+alt+super+f12"))
        self.assertEqual(layer1["key3"], core.Binding(media="prev"))
        self.assertTrue(layer1["key1"].none)

    def test_a_read_binding_re_encodes_to_the_same_write(self):
        """Whatever we read, writing it back must produce the same meaning."""
        for r in self.got:
            d = core.decode_config(r)
            if not d:
                continue
            control, layer, binding = d
            again = core.decode_config(
                binding.reports(3, core.action_byte(control), layer)[0])
            self.assertIsNotNone(again, binding)
            self.assertEqual(again[0], control)
            self.assertEqual(again[2], binding)

    def test_the_pad_stores_two_key_sequences(self):
        """
        Slots 0x0d to 0x0f hold two keystrokes each, in the layout we had
        assumed for sequences: count at byte 10, then (modifier, keycode)
        pairs. This is factory data for a 15 key sibling rather than
        something we watched being written, so it supports the encoding
        without proving the pad accepts it from us.
        """
        multi = [r for r in self.got
                 if r[1] == proto.MAGIC_READ and r[10] == 2 and r[4] != 2]
        self.assertTrue(multi)
        r = multi[0]
        self.assertEqual((r[11], r[13]), (0, 0))         # no modifiers
        self.assertTrue(r[12] and r[14])                  # two real keycodes

    def test_replies_cover_the_firmware_not_the_hardware(self):
        """
        The pad answers for 24 slots per layer: 15 keys and 3 knobs, which
        is the most this firmware supports. Ours has 12 keys and 2 knobs,
        so 6 of those slots are for hardware that isn't there. Those decode
        to no control, which is what we want: the app must not offer them.
        """
        configs = [r for r in self.got if r[1] == proto.MAGIC_READ]
        self.assertEqual(len(configs), 24 * 3)

        real = [r for r in configs if core.decode_config(r)]
        self.assertEqual(len(real), len(core.CONTROL_IDS) * 3)

        phantom = {r[2] for r in configs if not core.decode_config(r)}
        self.assertEqual(phantom, {0x0d, 0x0e, 0x0f,       # keys 13 to 15
                                   0x16, 0x17, 0x18})      # a third knob

    def test_reading_fills_in_the_state(self):
        from unittest import mock
        replies = [r for r in self.got if r[1] == proto.MAGIC_READ and r[3] == 1]
        dev = core.Device(path="/dev/null-pad", report_id=3, writable=True)
        with tempfile.TemporaryDirectory() as d:
            state = core.State(Path(d) / "s.json")
            with mock.patch.object(core, "_exchange", return_value=replies):
                got = core.read_into_state(dev, state)
            self.assertEqual(len(got), len(core.CONTROL_IDS))
            fresh = core.State(Path(d) / "s.json")
            self.assertFalse(any(fresh.get(c).unknown for c in core.CONTROL_IDS))
            self.assertEqual(fresh.get("dial2-push").binding,
                             core.Binding(media="calculator"))


class Detection(unittest.TestCase):
    """
    Detecting an unknown pad, using the real capture as the stand-in for a
    pad that speaks this protocol, and mangled versions for ones that don't.
    """

    def setUp(self):
        self.dev = core.Device(path="/dev/null-pad", report_id=3, writable=True,
                               name="Some Other Pad", vid="1189", pid="8890")
        got = core.read_capture(CAPTURES / "1189-8840-read.txt", incoming=True)
        self.info = [r for r in got if r[1] == proto.MAGIC_INFO]
        self.layer1 = [r for r in got if r[1] == proto.MAGIC_READ and r[3] == 1]

    def run_detect(self, info=None, layer=None):
        from unittest import mock
        calls = [info if info is not None else self.info,
                 layer if layer is not None else self.layer1]
        with mock.patch.object(core, "_exchange", side_effect=calls):
            return core.detect(self.dev)

    def test_a_pad_that_speaks_is_recognised(self):
        d = self.run_detect()
        self.assertTrue(d.speaks, d.why)
        self.assertEqual((d.keys, d.knobs), (12, 2))
        self.assertEqual(d.layout, core.Layout(12, 2, 4, 3))
        self.assertEqual(d.bindings["dial1-left"], core.Binding(media="volumedown"))

    def test_silence_is_not_taken_as_yes(self):
        d = self.run_detect(info=[])
        self.assertFalse(d.speaks)
        self.assertIn("didn't answer", d.why)

    def test_a_reply_that_is_not_ours_is_rejected(self):
        d = self.run_detect(info=[bytes.fromhex("03aa5501") + bytes(60)])
        self.assertFalse(d.speaks)

    def test_nonsense_counts_are_rejected(self):
        bad = bytearray(self.info[0])
        bad[2] = 99                                  # 99 keys
        d = self.run_detect(info=[bytes(bad)])
        self.assertFalse(d.speaks)
        self.assertIn("99 keys", d.why)

    def test_a_pad_that_answers_one_query_but_not_the_other(self):
        """The dangerous case: sounds right, then doesn't describe itself."""
        d = self.run_detect(layer=self.layer1[:4])
        self.assertFalse(d.speaks)
        self.assertIn("didn't report", d.why)

    def test_a_six_key_pad(self):
        info = bytearray(self.info[0])
        info[2], info[3] = 6, 1
        keep = {1, 2, 3, 4, 5, 6, 0x10, 0x11, 0x12}
        layer = [r for r in self.layer1 if r[2] in keep]
        d = self.run_detect(info=[bytes(info)], layer=layer)
        self.assertTrue(d.speaks, d.why)
        self.assertEqual(d.layout, core.Layout(6, 1, 2, 3))
        self.assertEqual(len(d.layout.control_ids), 9)

    def test_report_is_pasteable(self):
        text = core.detection_report(self.dev, self.run_detect())
        self.assertIn("1189:8890", text)
        self.assertIn("0x10  dial1-left", text)
        refused = core.detection_report(self.dev, self.run_detect(info=[]))
        self.assertIn("did NOT answer", refused)


class LayoutIsRemembered(unittest.TestCase):

    def test_state_keeps_the_pad_shape(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            lay = core.Layout(9, 2, 3, 3, "nine")
            st = core.State(path, layout=lay)
            st.record("key9", core.Binding(keys="f5"))
            st.save()
            again = core.State(path)
            self.assertEqual(again.layout, lay)
            self.assertEqual(len(again.layout.control_ids), 15)
            self.assertEqual(again.get("key9").binding, core.Binding(keys="f5"))

    def test_an_old_state_file_still_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps({
                "format": core.FORMAT, "device": core.DEVICE_ID,
                "layers": {"1": {"key1": {"binding": {"keys": "a"},
                                          "written": "2026-01-01T00:00:00"}}}}))
            st = core.State(path)
            self.assertEqual(st.layout, core.DEFAULT_LAYOUT)
            self.assertEqual(st.get("key1").binding, core.Binding(keys="a"))


class Arranging(unittest.TestCase):
    """Moving keys around the grid, and keeping it consistent when rotated."""

    def setUp(self):
        self.lay = core.DEFAULT_LAYOUT

    def test_the_default_matches_the_printed_pad(self):
        flat = {k: (r, c) for k, r, c in self.lay.spots()}
        self.assertEqual(flat["key1"], (0, 0))       # top left, reading across
        self.assertEqual(flat["key4"], (0, 3))
        self.assertEqual(flat["key12"], (2, 3))

    def test_upright_is_the_same_pad_turned(self):
        from macropad_gui import padview
        up = {k: (r, c) for k, r, c in padview._key_grid(self.lay, "upright")}
        self.assertEqual(up["key1"], (3, 0))         # bottom left when stood up
        self.assertEqual(up["key4"], (0, 0))
        self.assertEqual(up["key12"], (0, 2))
        self.assertEqual(len(set(up.values())), self.lay.keys)

    def test_swapping_two_keys(self):
        places = list(self.lay.spots())
        moved = self.lay.rearranged(
            [(2, 3) if k == "key1" else (0, 0) if k == "key12" else (r, c)
             for k, r, c in places])
        flat = {k: (r, c) for k, r, c in moved.spots()}
        self.assertEqual(flat["key1"], (2, 3))
        self.assertEqual(flat["key12"], (0, 0))
        self.assertEqual(len(set(flat.values())), 12)

    def test_resizing_keeps_what_still_fits(self):
        moved = self.lay.rearranged(
            [(2, 3) if k == "key1" else (0, 0) if k == "key12" else (r, c)
             for k, r, c in self.lay.spots()])
        self.assertEqual(moved.resized(cols=6).cols, 6)
        smaller = moved.resized(rows=2, cols=2, keys=4)
        self.assertEqual(len(smaller.spots()), 4)
        for _, r, c in smaller.spots():
            self.assertLess(r, 2)
            self.assertLess(c, 2)

    def test_a_grid_too_small_grows_instead_of_failing(self):
        grown = self.lay.resized(rows=1, cols=3)
        self.assertEqual((grown.rows, grown.cols), (4, 3))
        self.assertEqual(len(grown.spots()), 12)

    def test_layout_survives_a_save(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            moved = self.lay.rearranged(
                [(2, 3) if k == "key1" else (0, 0) if k == "key12" else (r, c)
                 for k, r, c in self.lay.spots()])
            st = core.State(path, layout=moved)
            st.save()
            self.assertEqual(core.State(path).layout, moved)


class OtherSystems(unittest.TestCase):

    def test_a_non_linux_system_gets_an_explanation(self):
        from unittest import mock
        with mock.patch.object(core, "on_linux", return_value=False):
            d = core.diagnose()
        self.assertEqual(d.status, "unsupported-os")
        self.assertFalse(d.ready)
        self.assertIsNone(d.device)

    def test_the_permission_rule_covers_the_whole_vendor(self):
        """A rule tied to one product id strands everyone with a sibling pad."""
        self.assertIn('idVendor}}=="1189"'.replace("}}", "}"), core.RULE_LINE)
        self.assertNotIn("idProduct", core.RULE_LINE)

    def test_an_unreadable_unknown_pad_is_told_how_to_fix_it(self):
        from unittest import mock
        other = [{"path": "/dev/hidraw4", "vid": "1189", "pid": "8890",
                  "report_id": 0, "writable": False, "name": ""}]
        with mock.patch.object(proto, "find_devices", return_value=other), \
             mock.patch.object(core, "_keyd_warning", return_value=None):
            d = core.diagnose()
        self.assertEqual(d.status, "unsupported")
        self.assertTrue(d.fix)
        self.assertIn("60-macropad.rules", d.fix[0])


class Layouts(unittest.TestCase):

    def test_action_bytes_agree_with_the_protocol_module(self):
        for c in core.DEFAULT_LAYOUT.control_ids:
            self.assertEqual(core.action_byte(c), proto.CONTROLS[c], c)

    def test_layouts_of_other_shapes(self):
        three = core.Layout(3, 1, 1, 3)
        self.assertEqual(three.control_ids,
                         ("key1", "key2", "key3",
                          "dial1-left", "dial1-push", "dial1-right"))
        self.assertEqual(core.action_byte("dial1-left"), 0x10)
        fifteen = core.Layout(15, 3, 5, 3)
        self.assertEqual(core.action_byte("dial3-right"), 0x18)
        self.assertEqual(len(fifteen.control_ids), 24)

    def test_impossible_layouts_are_refused(self):
        for bad in ((0, 2, 4, 3), (99, 2, 40, 3), (12, 9, 4, 3), (12, 2, 2, 3)):
            with self.assertRaises(ValueError, msg=bad):
                core.Layout(*bad)

    def test_round_trip(self):
        l = core.Layout(9, 2, 3, 3, "nine")
        self.assertEqual(core.Layout.from_json(l.to_json()), l)


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

    def test_nothing(self):
        b = core.Binding(none=True).validate()
        config, commit = b.reports(3, 0x04)
        # type 1 (keys), count 0, empty pair: press nothing
        self.assertEqual(config[:14].hex(" "), "03 fd 04 01 01 00 00 00 00 00 00 00 00 00")
        self.assertEqual(commit[:4], b"\x03\xfd\xfe\xff")
        self.assertFalse(any(config[11:]))
        self.assertEqual(b.label(), "Nothing")
        self.assertEqual(core.Binding.from_json(b.to_json()), b)
        self.assertEqual(core.decode_config(config), ("key4", 1, b))
        for bad in (core.Binding(none=True, keys="a"), core.Binding(none=True, media="mute"),
                    core.Binding(none=True, delay=5)):
            with self.assertRaises(ValueError):
                bad.validate()

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
        dev = [{"path": "/dev/hidraw10", "vid": "1189", "pid": "8840",
                "report_id": 3, "writable": False,
                "name": "USB Composite Device"}]
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
        # the device comes with it now, so the app can offer to interrogate it
        self.assertEqual(d.device.path, "/dev/hidraw4")
        self.assertEqual((d.device.vid, d.device.pid), ("1189", "8890"))

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
        from macropad_gui import actions as act_mod
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            qa = QApplication.instance() or QApplication([])
            w = app.Window()
            w.timer.stop()
            for c in core.CONTROL_IDS:
                w.pad.select(c)
                w._refresh()
                self.assertEqual(w.inspector.title.text(), core.control_name(c))
            # the drawing must cope with any shape the protocol allows
            for lay in (core.Layout(3, 1, 1, 3), core.Layout(15, 3, 5, 3),
                        core.Layout(4, 0, 1, 4)):
                w.pad.set_layout(lay)
                for orientation in ("upright", "flat"):
                    w.pad.set_orientation(orientation)
                    w.pad.grab()
                    for c in lay.control_ids:
                        self.assertIsNotNone(core.action_byte(c))
            # dragging a key in edit mode swaps it with whatever is there
            w.pad.set_layout(core.DEFAULT_LAYOUT)
            w._edit_layout(True)
            from macropad_gui import padview
            before = {k: (r, c) for k, r, c
                      in padview._key_grid(w.pad.layout, w.pad.orientation)}
            w.pad.drag = "key1"
            w.pad._place("key1", before["key12"])
            after = {k: (r, c) for k, r, c
                     in padview._key_grid(w.pad.layout, w.pad.orientation)}
            self.assertEqual(after["key1"], before["key12"])
            self.assertEqual(after["key12"], before["key1"])
            w._edit_layout(False, keep=False)
            self.assertEqual(w.state.layout, core.DEFAULT_LAYOUT)
            self.assertFalse(w.pad.editing)
            # an action typed for one key must not leak into the next one
            w.pad.select("key2"); w._refresh()
            w.inspector.mode_action.click()
            w.inspector.action_edit.setText("github.com")
            w.inspector._action_edited()
            w.pad.select("key3"); w._refresh()
            w.inspector.mode_action.click()
            self.assertEqual(w.inspector.action_edit.text(), "")
            # the picker: automatic avoids F13, and a chosen key is used
            w.desired.clear(); w.pending_actions.clear()
            w.pad.select("key4"); w._refresh()
            w._host_action("key4", act_mod.Action("url", "a.com"))
            self.assertEqual(w.desired["key4"].keys, "f14")
            w._host_action("key4", act_mod.Action("url", "a.com"), "f22")
            self.assertEqual(w.desired["key4"].keys, "f22")
            opts = dict(w._trigger_options("key6"))
            self.assertEqual(opts["f22"], "Key 4")          # taken, shown as whose
            self.assertIsNone(opts["f15"])
            w.desired.clear(); w.pending_actions.clear()
            w._changed("key1", core.Binding(media="mute"))
            self.assertEqual(w._pending(), ["key1"])
            # choosing Nothing marks the control as changed straight away
            w.pad.select("key5")
            w._refresh()
            w.inspector.mode_none.click()
            self.assertEqual(w.desired.get("key5"), core.Binding(none=True))
            self.assertIn("key5", w._pending())
            w.desired.clear()
            w.close()


class ThemedDrawing(unittest.TestCase):
    """Every theme, every shape, both ways up, switched live."""

    def test_switching_themes_live(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtWidgets import QApplication
            from macropad_gui import app, padview, themes
        except ImportError:
            self.skipTest("PySide6 not installed")
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            qa = QApplication.instance() or QApplication([])
            w = app.Window()
            w.timer.stop()
            for t in themes.THEMES.values():
                w._set_theme(t.key)
                self.assertEqual(padview.THEME.key, t.key)
                self.assertEqual(padview.INK.name(), t.ink)     # changed in place
                for lay in (core.DEFAULT_LAYOUT, core.Layout(15, 3, 3, 5)):
                    w.pad.set_layout(lay)
                    for o in ("upright", "flat"):
                        w.pad.set_orientation(o)
                        w.pad.grab()
            self.assertEqual(w.settings.value("theme"), list(themes.THEMES)[-1])
            w._set_theme(themes.DEFAULT)
            w.close()

    def test_pencil_lines_are_the_same_every_time(self):
        try:
            from PySide6.QtGui import QPainterPath
            from PySide6.QtCore import QRectF
            from macropad_gui import padview
        except ImportError:
            self.skipTest("PySide6 not installed")
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 96, 96), 5, 5)
        a = padview._wobble_path(path, 1.6, padview._seed("key4"))
        b = padview._wobble_path(path, 1.6, padview._seed("key4"))
        c = padview._wobble_path(path, 1.6, padview._seed("key5"))
        self.assertEqual([(p.x(), p.y()) for p in a.toFillPolygon()],
                         [(p.x(), p.y()) for p in b.toFillPolygon()])
        self.assertNotEqual([(p.x(), p.y()) for p in a.toFillPolygon()],
                            [(p.x(), p.y()) for p in c.toFillPolygon()])
        # it wanders, but only a little
        box = a.boundingRect()
        self.assertLess(abs(box.width() - 96), 5)
