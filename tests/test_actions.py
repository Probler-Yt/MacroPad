"""
Host actions. The listener can't be tried against a real pad here, so the
parts that touch hardware are tested against the shapes of report the HID
spec allows, and the rest against a fake store and a fake runner.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import macropad as proto
from macropad_gui import actions as A, core


class Triggers(unittest.TestCase):

    def test_f13_to_f24_are_standard_hid_usages(self):
        self.assertEqual(proto.KEYCODES["f13"], 0x68)
        self.assertEqual(proto.KEYCODES["f24"], 0x73)
        self.assertEqual(len(A.TRIGGERS), 12)

    def test_a_trigger_binding_writes_like_any_key(self):
        config = core.Binding(keys="f17").reports(3, 0x05)[0]
        self.assertEqual(config[:14].hex(" "), "03 fd 05 01 01 00 00 00 00 00 01 00 6c 00")
        self.assertEqual(core.decode_config(config)[2], core.Binding(keys="f17"))

    def test_pick_keeps_a_controls_own_trigger(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        store.set("f15", A.Action("url", "x.com"))
        self.assertEqual(A.pick_trigger(store, {"f15"}, current="f15"), "f15")

    def test_pick_skips_anything_in_use(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        store.set("f13", A.Action("url", "x.com"))
        # f14 is on the pad as a plain key someone chose themselves
        self.assertEqual(A.pick_trigger(store, {"f13", "f14"}), "f15")

    def test_automatic_pick_leaves_the_troublemakers_till_last(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        order = []
        for _ in A.TRIGGERS:
            t = A.pick_trigger(store, set())
            order.append(t)
            store.set(t, A.Action("command", "true"))
        self.assertEqual(order[0], "f14")
        self.assertEqual(set(order[-5:]), set(A.CLAIMED))
        self.assertEqual(set(order), set(A.TRIGGERS))

    def test_a_chosen_key_is_honoured_when_free(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        self.assertEqual(A.pick_trigger(store, set(), want="f22"), "f22")

    def test_a_chosen_key_that_is_taken_is_not(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        store.set("f22", A.Action("url", "x.com"))
        self.assertNotEqual(A.pick_trigger(store, set(), want="f22"), "f22")
        # but a control can always keep, or re-choose, its own key
        self.assertEqual(A.pick_trigger(store, set(), current="f22", want="f22"), "f22")

    def test_moving_an_action_to_another_key(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        store.set("f14", A.Action("url", "x.com"))
        self.assertEqual(A.pick_trigger(store, {"f14"}, current="f14", want="f19"), "f19")

    def test_pick_runs_out_honestly(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        for t in A.TRIGGERS:
            store.set(t, A.Action("command", "true"))
        self.assertIsNone(A.pick_trigger(store, set()))


class Actions(unittest.TestCase):

    def test_urls_get_a_scheme_when_missing(self):
        self.assertEqual(A.Action("url", "github.com").url, "https://github.com")
        self.assertEqual(A.Action("url", "http://x.org").url, "http://x.org")
        self.assertEqual(A.Action("url", "mailto:a@b.c").url, "mailto:a@b.c")

    def test_labels_fit_a_keycap(self):
        self.assertEqual(A.Action("url", "https://www.github.com/Probler-Yt").label(),
                         "Open github.com")
        self.assertEqual(A.Action("app", "spotify", "Spotify").label(), "Spotify")
        self.assertEqual(A.Action("command", "~/bin/switch-audio.sh --next").label(),
                         "Run switch-audio.sh")

    def test_only_commands_go_through_a_shell(self):
        self.assertEqual(A.Action("url", "x.com").argv(), ["xdg-open", "https://x.com"])
        self.assertEqual(A.Action("app", "firefox --new-window").argv(),
                         ["firefox", "--new-window"])
        self.assertEqual(A.Action("command", "echo hi | wc").argv()[:2], ["sh", "-c"])

    def test_nonsense_is_refused(self):
        for bad in (A.Action("url", ""), A.Action("url", "not a url"),
                    A.Action("teleport", "x"), A.Action("command", "   ")):
            with self.assertRaises(ValueError, msg=bad):
                bad.validate()

    def test_running_detaches(self):
        seen = {}
        A.Action("url", "x.com").run(_popen=lambda argv, **kw: seen.update(argv=argv, **kw))
        self.assertEqual(seen["argv"], ["xdg-open", "https://x.com"])
        self.assertTrue(seen["start_new_session"])


class Store(unittest.TestCase):

    def test_round_trip_and_live_reload(self):
        path = Path(tempfile.mkdtemp()) / "a.json"
        a = A.ActionStore(path)
        a.set("f13", A.Action("url", "github.com"))
        a.save()
        b = A.ActionStore(path)
        self.assertEqual(b.get("f13"), A.Action("url", "github.com"))

        a.set("f14", A.Action("command", "true"))
        a.save()
        os.utime(path, (1, 1))              # make sure the mtime visibly moves
        b.reload()
        self.assertIn("f14", b.by_trigger)

    def test_prune_forgets_triggers_no_longer_on_the_pad(self):
        s = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        s.set("f13", A.Action("url", "a.com"))
        s.set("f14", A.Action("url", "b.com"))
        self.assertEqual(s.prune({"f14"}), ["f13"])
        self.assertEqual(list(s.by_trigger), ["f14"])


class DesktopApps(unittest.TestCase):

    def test_exec_lines_are_cleaned(self):
        self.assertEqual(A.clean_exec("firefox %u"), "firefox")
        self.assertEqual(A.clean_exec('code --new-window %F'), "code --new-window")
        self.assertEqual(
            A.clean_exec("/usr/bin/flatpak run --branch=stable --arch=x86_64 "
                         "--command=brave com.brave.Browser @@u %U @@"),
            "/usr/bin/flatpak run --branch=stable --arch=x86_64 "
            "--command=brave com.brave.Browser")

    def test_listing_apps(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.desktop").write_text("[Desktop Entry]\nType=Application\nName=Zed\n"
                                     "Name[fr]=Zède\nExec=zed %F\n")
        (d / "b.desktop").write_text("[Desktop Entry]\nType=Application\nName=Alpha\n"
                                     "Exec=alpha\n[Desktop Action new]\nExec=alpha --new\n")
        (d / "hidden.desktop").write_text("[Desktop Entry]\nType=Application\n"
                                          "Name=Secret\nExec=x\nNoDisplay=true\n")
        (d / "link.desktop").write_text("[Desktop Entry]\nType=Link\nName=L\nURL=x\n")
        self.assertEqual(A.installed_apps([d]), [("Alpha", "alpha"), ("Zed", "zed")])


class ReadingTheKeyboard(unittest.TestCase):
    """The pad's keyboard interface, in the forms the HID spec allows."""

    # a standard boot keyboard descriptor, no report ids
    BOOT = bytes.fromhex(
        "05010906a101050719e029e71500250175019508810295017508810195067508"
        "150025650507190029658100c0")
    # the same keyboard behind report id 1, followed by a consumer collection
    WITH_ID = bytes.fromhex(
        "05010906a1018501050719e029e7150025017501950881029506750815002565"
        "0507190029658100c0050c0901a1018502150026ff031900 2aff03 7510 9501 8100c0"
        .replace(" ", ""))
    VENDOR = bytes([6, 0, 255, 9, 1, 161, 1, 133, 2, 9, 1, 21, 0, 38, 255, 0,
                    117, 8, 149, 63, 129, 0, 192])

    def test_descriptors(self):
        self.assertEqual(A._descriptor_keyboard_report_id(self.BOOT), (True, 0))
        self.assertEqual(A._descriptor_keyboard_report_id(self.WITH_ID), (True, 1))
        self.assertEqual(A._descriptor_keyboard_report_id(self.VENDOR), (False, 0))

    def test_reports(self):
        f17 = proto.KEYCODES["f17"]
        self.assertEqual(A.pressed_codes(bytes([0, 0, f17, 0, 0, 0, 0, 0]), 0), {f17})
        self.assertEqual(A.pressed_codes(bytes([1, 0, 0, f17, 0, 0, 0, 0, 0]), 1), {f17})
        self.assertIsNone(A.pressed_codes(bytes([2, 0xe9, 0]), 1))    # a media report
        self.assertEqual(A.pressed_codes(bytes(8), 0), set())          # all released


class Listening(unittest.TestCase):

    def setUp(self):
        self.store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        self.store.set("f13", A.Action("url", "github.com"))
        self.ran = []
        self.logs = []
        self.l = A.Listener(self.store, log=self.logs.append, run=self.ran.append)
        self.f13, self.f14 = proto.KEYCODES["f13"], proto.KEYCODES["f14"]

    def test_fires_once_per_press_however_long_it_is_held(self):
        self.l.feed({self.f13})
        self.l.feed({self.f13})           # still held: a repeat report
        self.l.feed(set())                # released
        self.l.feed({self.f13})           # pressed again
        self.assertEqual(len(self.ran), 2)

    def test_ignores_ordinary_keys_and_unset_triggers(self):
        self.l.feed({proto.KEYCODES["a"]})
        self.l.feed({self.f14})
        self.assertEqual(self.ran, [])
        self.assertIn("no action is set", self.logs[-1])

    def test_ignores_reports_that_arent_keyboard_ones(self):
        self.assertEqual(self.l.feed(None), [])

    def test_a_failing_action_doesnt_kill_the_listener(self):
        def boom(_):
            raise FileNotFoundError(2, "No such file")
        l = A.Listener(self.store, log=self.logs.append, run=boom)
        self.assertEqual(l.feed({self.f13}), [])
        self.assertIn("couldn't run", self.logs[-1])


class Autostart(unittest.TestCase):

    def test_toggle_writes_and_removes_an_autostart_entry(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            A.set_autostart(True)
            text = A.autostart_path().read_text()
            self.assertIn("Exec=", text)
            self.assertIn("actions", text)
            self.assertTrue(A.autostart_enabled())
            A.set_autostart(False)
            self.assertFalse(A.autostart_enabled())

    def test_never_signals_a_process_that_isnt_ours(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}):
            (Path(d) / A.LOCK).write_text(str(os.getpid()))   # this test runner
            self.assertIsNone(A.listener_pid())


if __name__ == "__main__":
    unittest.main()


class TypingText(unittest.TestCase):
    """The phrase feature: clipboard, then a virtual keyboard presses paste."""

    def fake_keyboard(self):
        import struct
        self.ioctls, self.writes, self.slept = [], [], []
        kb = A.VirtualKeyboard(
            _open=lambda path, flags: 99,
            _ioctl=lambda fd, req, arg=None: self.ioctls.append((req, arg)),
            _write=lambda fd, data: self.writes.append(data),
            _sleep=self.slept.append)
        return kb, struct

    def test_the_text_action(self):
        a = A.Action("text", "Kind regards,\nProbler").validate()
        self.assertEqual(a.label(), 'Type "Kind regards,"')
        self.assertEqual(A.Action.from_json(a.to_json()), a)
        long = A.Action("text", "Thanks for reaching out, I'll reply soon")
        self.assertLessEqual(len(long.label()), 24)
        with self.assertRaises(ValueError):
            A.Action("text", "hi", paste="ctrl+p").validate()

    def test_the_device_is_set_up_the_way_the_kernel_expects(self):
        kb, struct = self.fake_keyboard()
        requests = [r for r, _ in self.ioctls]
        self.assertEqual(requests[0], A._UI_SET_EVBIT)
        self.assertEqual(sorted(a for r, a in self.ioctls if r == A._UI_SET_KEYBIT),
                         [29, 42, 47])                       # ctrl, shift, v only
        self.assertEqual(requests[-1], A._UI_DEV_CREATE)
        self.assertEqual(len(self.writes[0]), 1116)          # struct uinput_user_dev
        self.assertIn(0.3, self.slept)                       # waits to be noticed

    def test_ctrl_v_is_pressed_and_released_in_order(self):
        kb, struct = self.fake_keyboard()
        self.writes.clear()
        kb.press("ctrl+v")
        events = [struct.unpack("@llHHi", w)[2:] for w in self.writes]
        self.assertEqual(events, [(1, 29, 1), (1, 47, 1), (0, 0, 0),
                                  (1, 47, 0), (1, 29, 0), (0, 0, 0)])

    def test_terminal_paste_holds_shift_too(self):
        kb, struct = self.fake_keyboard()
        self.writes.clear()
        kb.press("ctrl+shift+v")
        downs = [struct.unpack("@llHHi", w)[3] for w in self.writes
                 if struct.unpack("@llHHi", w)[2:] [0] == 1 and struct.unpack("@llHHi", w)[4] == 1]
        self.assertEqual(downs, [29, 42, 47])

    def test_clipboard_first_then_paste(self):
        order = []
        class Kb:
            def press(self, combo):
                order.append(("press", combo))
        A.type_text("hello £ @ 👋", "ctrl+v",
                    _clip=lambda t: order.append(("clip", t)),
                    _keyboard_factory=Kb)
        self.assertEqual(order, [("clip", "hello £ @ 👋"), ("press", "ctrl+v")])

    def test_clipboard_tool_by_session(self):
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}), \
             mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n):
            self.assertEqual(A._clipboard_tool(), ["wl-copy"])
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "", "XDG_SESSION_TYPE": "x11"}), \
             mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/xclip" if n == "xclip" else None):
            self.assertEqual(A._clipboard_tool(), ["xclip", "-selection", "clipboard"])

    def test_problems_come_with_fixes(self):
        with mock.patch.object(A.os, "access", return_value=False), \
             mock.patch.object(A, "_clipboard_tool", return_value=None):
            probs = A.text_problems()
        self.assertEqual(len(probs), 2)
        self.assertIn("60-macropad-uinput.rules", probs[0][1][0])
        with mock.patch.object(A.os, "access", return_value=True), \
             mock.patch.object(A, "_clipboard_tool", return_value=["wl-copy"]):
            self.assertEqual(A.text_problems(), [])

    def test_the_listener_runs_text_actions_too(self):
        store = A.ActionStore(Path(tempfile.mkdtemp()) / "a.json")
        store.set("f16", A.Action("text", "gg ez"))
        typed = []
        with mock.patch.object(A, "type_text", side_effect=lambda t, p: typed.append((t, p))):
            A.Listener(store, log=lambda *_: None).feed({proto.KEYCODES["f16"]})
        self.assertEqual(typed, [("gg ez", "ctrl+v")])


class Themes(unittest.TestCase):

    def test_every_theme_is_complete_and_valid(self):
        import re
        from macropad_gui import themes
        colours = ("window", "board", "ink", "ink_dim", "hatch", "accent",
                   "accent_text", "good", "bad", "line", "field")
        for t in themes.THEMES.values():
            for c in colours:
                self.assertRegex(getattr(t, c), r"^#[0-9a-f]{6}$", f"{t.key}.{c}")
            self.assertIn(t.unknown, ("hatch", "hidden"))
        self.assertIn(themes.DEFAULT, themes.THEMES)
        self.assertEqual(themes.get("no such theme").key, themes.DEFAULT)

    def test_mixing(self):
        from macropad_gui import themes
        self.assertEqual(themes.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(themes.mix("#123456", "#abcdef", 0), "#123456")

    def test_the_sketch_font_ships_with_its_licence(self):
        from macropad_gui import themes
        self.assertTrue((themes.FONTS / "ArchitectsDaughter-Regular.ttf").exists())
        self.assertIn("Open Font License", (themes.FONTS / "OFL.txt").read_text())
