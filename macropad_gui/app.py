"""
The window. Open it, change what a control does, write it, close it. The
pad keeps its bindings in its own memory, so nothing here needs to keep
running afterwards.
"""

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QGuiApplication, QIcon,
                           QPalette)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from . import core, keymap
from .padview import INK, INK_DIM, TAPE, WINDOW, Look, PadView

GOOD = "#8db36b"
BAD = "#e0584f"
LINE = "#3a3e42"
FIELD = "#121416"

# Media keys as pairs, so opposites sit side by side.
MEDIA_GRID = [("playpause", "stop"), ("prev", "next"),
              ("volumedown", "volumeup"), ("mute", None),
              ("brightnessdown", "brightnessup"),
              ("calculator", "mail"), ("browser", "mycomputer")]

STYLE = f"""
QWidget {{ color: {INK.name()}; background: {WINDOW.name()}; }}
QLabel[role="title"] {{ font-size: 18px; font-weight: 600; }}
QLabel[role="dim"] {{ color: {INK_DIM.name()}; }}
QLabel[role="caption"] {{ color: {INK_DIM.name()}; padding: 14px 0px 0px 0px; margin: 0px; }}
QLabel[role="value"] {{ font-size: 15px; }}
QPushButton {{ background: transparent; border: 1px solid {LINE};
              border-radius: 4px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {INK_DIM.name()}; }}
QPushButton:checked {{ border-color: {TAPE.name()}; color: {TAPE.name()}; }}
QPushButton:disabled {{ color: #4a4e52; border-color: #2a2e32; }}
QPushButton[role="primary"] {{ background: {TAPE.name()}; color: #1b1206;
              border: none; font-weight: 600; padding: 8px 16px; }}
QPushButton[role="primary"]:hover {{ background: #f39a45; }}
QPushButton[role="primary"]:disabled {{ background: #2c3034; color: #5a5e62; }}
QPushButton[role="flat"] {{ border: none; color: {INK_DIM.name()}; padding: 6px 8px; }}
QPushButton[role="flat"]:hover {{ color: {INK.name()}; }}
QPushButton[role="media"] {{ text-align: left; padding: 7px 10px; }}
QLineEdit {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 4px;
            padding: 7px 8px; }}
QLineEdit:focus {{ border-color: {INK_DIM.name()}; }}
QLineEdit[recording="true"] {{ border-color: {TAPE.name()}; background: #2a1d10; }}
QPlainTextEdit {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 4px; }}
QFrame[role="banner"] {{ border: 1px solid {LINE}; border-left: 3px solid {BAD};
            border-radius: 4px; }}
QFrame[role="banner"][tone="quiet"] {{ border-left: 3px solid {INK_DIM.name()}; }}
QFrame[role="banner"] QLabel, QFrame[role="banner"] QPushButton {{ background: transparent; }}
QFrame[role="footer"] {{ border-top: 1px solid {LINE}; }}
QFrame[role="editbar"] {{ border-top: 1px solid {TAPE.name()}; background: #1c1a17; }}
QFrame[role="editbar"] QLabel {{ background: transparent; }}
QSpinBox {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 4px;
            padding: 4px 6px; }}
"""


def _label(text="", role=None, wrap=False):
    lab = QLabel(text)
    lab.setIndent(0)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    return lab


def _button(text, role=None, checkable=False):
    b = QPushButton(text)
    if role:
        b.setProperty("role", role)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    return b


# ------------------------------------------------------------------ recorder

class RecorderEdit(QLineEdit):
    """
    A text field that can also listen for a key combo. Type 'ctrl+shift+n',
    or press Record and then press the combo itself.
    """
    recorded = Signal(str)

    def __init__(self):
        super().__init__()
        self.recording = False
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

    def set_recording(self, on):
        self.recording = on
        self.setProperty("recording", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        if on:
            self._before = self.text()
            self.clear()
            self.setPlaceholderText("Press the combo now")
            self.setFocus()
        else:
            self.setPlaceholderText("ctrl+shift+n")

    def event(self, e):
        # Catch Tab and friends too, which would otherwise move focus.
        if self.recording and e.type() == QEvent.KeyPress:
            self._record(e)
            return True
        if self.recording and e.type() == QEvent.ShortcutOverride:
            e.accept()
            return True
        return super().event(e)

    def _record(self, e):
        mods = keymap.modifier_names(e)
        if e.key() in keymap.MODIFIER_KEYS:
            self.setText("+".join(mods + ["…"]))
            return
        name = keymap.key_name(e)
        if name is None:
            self.setText("")
            self.setPlaceholderText("The pad can't send that key. Try another.")
            return
        combo = "+".join(mods + [name])
        self.set_recording(False)
        self.setText(combo)
        self.recorded.emit(combo)

    def focusOutEvent(self, e):
        if self.recording:
            self.set_recording(False)
            self.setText(self._before)
        super().focusOutEvent(e)


# ------------------------------------------------------------------ inspector

class Inspector(QWidget):
    """Right-hand panel: what the selected control does, and what to change it to."""
    changed = Signal(str, object)       # control, Binding or None (revert)
    write = Signal(str)
    revert = Signal(str)
    action = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedWidth(360)
        self.control = None
        self._loading = False
        self.editing_layout = False

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 16)
        v.setSpacing(4)

        # problem banner (no pad / no permission)
        self.banner = QFrame()
        self.banner.setProperty("role", "banner")
        bl = QVBoxLayout(self.banner)
        bl.setContentsMargins(12, 10, 12, 10)
        self.banner_head = _label("", wrap=True)
        self.banner_head.setStyleSheet("font-weight: 600;")
        self.banner_text = _label("", "dim", wrap=True)
        self.banner_fix = QPlainTextEdit()
        self.banner_fix.setReadOnly(True)
        self.banner_fix.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.banner_fix.setFixedHeight(84)
        self.banner_fix.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.banner_copy = _button("Copy commands")
        self.banner_copy.clicked.connect(self._copy)
        self.banner_action = _button("")
        self.banner_action.clicked.connect(lambda: self.action.emit())
        for w in (self.banner_head, self.banner_text, self.banner_fix,
                  self.banner_copy, self.banner_action):
            bl.addWidget(w)
        self.banner_copy.setSizePolicy(self.banner_copy.sizePolicy().horizontalPolicy(),
                                       self.banner_copy.sizePolicy().verticalPolicy())
        v.addWidget(self.banner)
        v.addSpacing(8)

        # empty state
        self.empty = _label("", "dim", wrap=True)
        v.addWidget(self.empty)

        # editor
        self.editor = QWidget()
        ev = QVBoxLayout(self.editor)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(4)
        self.title = _label("", "title")
        self.sub = _label("", "dim")
        ev.addWidget(self.title)
        ev.addWidget(self.sub)

        ev.addWidget(_label("On the pad now", "caption"))
        self.onpad = _label("", "value", wrap=True)
        self.onpad_why = _label("", "dim", wrap=True)
        ev.addWidget(self.onpad)
        ev.addWidget(self.onpad_why)

        ev.addWidget(_label("Change it to", "caption"))
        modes = QHBoxLayout()
        modes.setSpacing(6)
        self.mode_keys = _button("Shortcut", checkable=True)
        self.mode_media = _button("Media key", checkable=True)
        self.mode_none = _button("Nothing", checkable=True)
        self.mode_none.setToolTip("Make this control do nothing at all")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for i, b in enumerate((self.mode_keys, self.mode_media, self.mode_none)):
            self.mode_group.addButton(b, i)
            modes.addWidget(b)
        modes.addStretch()
        ev.addLayout(modes)
        ev.addSpacing(6)

        # shortcut page
        kp = QWidget()
        kl = QVBoxLayout(kp)
        kl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.keys_edit = RecorderEdit()
        self.keys_edit.setPlaceholderText("ctrl+shift+n")
        self.record = _button("Record", checkable=True)
        row.addWidget(self.keys_edit, 1)
        row.addWidget(self.record)
        kl.addLayout(row)
        self.keys_hint = _label("", "dim", wrap=True)
        kl.addWidget(self.keys_hint)
        # media page
        mp = QWidget()
        ml = QGridLayout(mp)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        self.media_group = QButtonGroup(self)
        self.media_group.setExclusive(True)
        self.media_buttons = {}
        for r, pair in enumerate(MEDIA_GRID):
            for col, mid in enumerate(pair):
                if mid is None:
                    continue
                b = _button(core.MEDIA_LABEL[mid], "media", checkable=True)
                self.media_group.addButton(b, [m for m, _ in core.MEDIA].index(mid))
                self.media_buttons[mid] = b
                ml.addWidget(b, r, col)
        np_ = _label("Pressing or turning it will do nothing. Handy for a key "
                     "you keep hitting by accident.", "dim", wrap=True)
        self.pages = (kp, mp, np_)
        ev.addWidget(kp)
        ev.addWidget(mp)
        ev.addWidget(np_)

        ev.addSpacing(10)
        actions = QHBoxLayout()
        self.write_btn = _button("Write to pad", "primary")
        self.revert_btn = _button("Revert", "flat")
        actions.addWidget(self.write_btn)
        actions.addWidget(self.revert_btn)
        actions.addStretch()
        ev.addLayout(actions)
        v.addWidget(self.editor)
        v.addStretch()

        self.mode_group.idClicked.connect(self._mode)
        self.keys_edit.textEdited.connect(self._keys_typed)
        self.keys_edit.recorded.connect(self._keys_typed)
        self.record.toggled.connect(self._toggle_record)
        self.media_group.idClicked.connect(self._media_picked)
        self.write_btn.clicked.connect(lambda: self.write.emit(self.control))
        self.revert_btn.clicked.connect(lambda: self.revert.emit(self.control))

        self.show_control(None, None, None, False)

    # -------------------------------------------------------------- banner

    def show_problem(self, diag, note=None):
        if diag is None or diag.ready:
            if not note:
                self.banner.hide()
                return
            diag = core.Diagnosis("note", "Heads up", detail=[note])
        quiet = diag.status in ("unplugged", "note", "unsupported")
        self.banner.setProperty("tone", "quiet" if quiet else "")
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner_head.setText(diag.headline)
        self.banner_text.setText("\n\n".join(diag.detail))
        cmds = [c for c in diag.fix if not c.startswith("#")]
        self.banner_fix.setPlainText("\n".join(cmds))
        lh = self.banner_fix.fontMetrics().lineSpacing()
        self.banner_fix.setFixedHeight(lh * max(1, len(cmds)) + 30)
        self.banner_fix.setVisible(bool(cmds))
        self.banner_copy.setVisible(bool(cmds))
        self.banner_action.setVisible(diag.status == "unsupported")
        self.banner_action.setText("Ask the pad what it is")
        if diag.fix and diag.fix[-1].startswith("#"):
            self.banner_text.setText(self.banner_text.text() + "\n\nThen unplug the pad and plug it back in.")
        self.banner.show()

    def _copy(self):
        QGuiApplication.clipboard().setText(self.banner_fix.toPlainText())
        self.banner_copy.setText("Copied")
        QTimer.singleShot(1500, lambda: self.banner_copy.setText("Copy commands"))

    # -------------------------------------------------------------- editor

    def show_control(self, control, entry, desired, can_write, all_unknown=False):
        self._loading = True
        self.control = control
        if control is None:
            self.editor.hide()
            self.empty.setText(
                ("Drag the keys around until the drawing matches your pad, "
                 "then press Done.\n\nUse the boxes below to change the "
                 "grid or the number of keys and knobs. This only changes "
                 "the picture, never the pad."
                 if self.editing_layout else
                 "Click a key or dial to change what it does.\n\n")
                + ("Hatched controls are ones this app hasn't been told about. "
                   "Press Read pad to ask the pad itself what it's holding."
                   if all_unknown and not self.editing_layout else ""))
            self.empty.show()
            self._loading = False
            return
        self.empty.hide()
        self.editor.show()
        self.title.setText(core.control_name(control))
        self.sub.setText(f"Action byte 0x{core.action_byte(control):02x}")

        if entry.unknown:
            self.onpad.setText("Unknown")
            self.onpad.setStyleSheet(f"color: {INK_DIM.name()};")
            self.onpad_why.setText(entry.reason[:1].upper() + entry.reason[1:] + ".")
            self.onpad_why.show()
        else:
            self.onpad.setText(entry.binding.label())
            self.onpad.setStyleSheet("")
            self.onpad_why.hide()

        shown = desired or (None if entry.unknown else entry.binding)
        if shown is not None and shown.none:
            self.mode_none.setChecked(True)
            self._page(2)
            self.keys_edit.clear()
            self._clear_media()
        elif shown is not None and shown.media:
            self.mode_media.setChecked(True)
            self._page(1)
            self.media_buttons[shown.media].setChecked(True)
            self.keys_edit.clear()
        else:
            self.mode_keys.setChecked(True)
            self._page(0)
            self.keys_edit.setText(shown.keys if shown else "")
            self._clear_media()
        self.keys_hint.setText("")
        self.update_buttons(desired is not None, can_write)
        self._loading = False

    def update_buttons(self, pending, can_write):
        self.write_btn.setEnabled(can_write and pending)
        self.revert_btn.setVisible(pending)

    def _clear_media(self):
        self.media_group.setExclusive(False)
        for b in self.media_buttons.values():
            b.setChecked(False)
        self.media_group.setExclusive(True)

    def _page(self, i):
        # Plain show/hide rather than QStackedWidget, which always reserves
        # the height of its tallest page.
        for j, page in enumerate(self.pages):
            page.setVisible(j == i)

    def _mode(self, i):
        self._page(i)
        if i == 2 and not self._loading and self.control is not None:
            self.changed.emit(self.control, core.Binding(none=True))
        if i == 0 and self.record.isChecked():
            self.keys_edit.setFocus()

    def _toggle_record(self, on):
        self.keys_edit.set_recording(on)
        self.keys_hint.setText(
            "Plasma keeps some combos for itself, like Meta shortcuts, so the "
            "app never sees them. Type those instead." if on else "")

    def _keys_typed(self, text):
        if self.record.isChecked() and not self.keys_edit.recording:
            self.record.setChecked(False)
        if self._loading or self.control is None:
            return
        text = core.normalize_keys(text)
        if not text:
            self.keys_hint.setText("")
            self.changed.emit(self.control, None)
            return
        try:
            b = core.Binding(keys=text).validate()
        except ValueError as e:
            self.keys_hint.setText(f"<span style='color:{BAD}'>{e}</span>")
            self.changed.emit(self.control, None)
            return
        if b.is_sequence and not core.SEQUENCES_VERIFIED:
            self.keys_hint.setText(
                f"<span style='color:{BAD}'>Sequences aren't verified on this "
                "pad yet, so they're switched off for now. One shortcut per "
                "control.</span>")
            self.changed.emit(self.control, None)
            return
        self.keys_hint.setText(f"Sends {b.label()}")
        self.changed.emit(self.control, b)

    def _media_picked(self, i):
        if self._loading or self.control is None:
            return
        self.changed.emit(self.control, core.Binding(media=core.MEDIA[i][0]))


# ------------------------------------------------------------------ window

class Window(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MacroPad")
        self.settings = QSettings("macropad", "macropad")
        self.state = core.State()
        self.desired = {}                    # control -> Binding, unwritten edits
        self.diag = None
        # Pads the user has identified by asking them. Remembered so the app
        # doesn't go back to calling them unsupported on the next check.
        self.adopted = set(self.settings.value("adopted", []) or [])

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.pad = PadView()
        self.pad.set_layout(self.state.layout)
        self.pad.set_orientation(self.settings.value("orientation", "upright"))
        self.inspector = Inspector()
        body.addWidget(self.pad, 1)
        body.addWidget(self.inspector)
        outer.addLayout(body, 1)

        # editor bar, hidden unless you're rearranging the pad
        self.editbar = QFrame()
        self.editbar.setProperty("role", "editbar")
        eb = QHBoxLayout(self.editbar)
        eb.setContentsMargins(16, 8, 16, 8)
        eb.setSpacing(8)
        eb.addWidget(_label("Drag the keys to match your pad.", "dim"))
        eb.addSpacing(12)
        self.spin = {}
        for name, label, lo, hi in (("rows", "Rows", 1, 15),
                                    ("cols", "Columns", 1, 15),
                                    ("keys", "Keys", 1, 15),
                                    ("knobs", "Knobs", 0, 3)):
            eb.addWidget(_label(label, "dim"))
            box = QSpinBox()
            box.setRange(lo, hi)
            box.setFixedWidth(56)
            box.valueChanged.connect(self._resize_layout)
            self.spin[name] = box
            eb.addWidget(box)
        eb.addStretch()
        self.edit_done = _button("Done", "primary")
        self.edit_cancel = _button("Cancel", "flat")
        eb.addWidget(self.edit_cancel)
        eb.addWidget(self.edit_done)
        self.editbar.hide()
        outer.addWidget(self.editbar)

        footer = QFrame()
        footer.setProperty("role", "footer")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(16, 8, 16, 8)
        self.dot = QLabel()
        self.dot.setFixedSize(9, 9)
        self.status = _label("", "dim")
        self.message = _label("")
        self.edit_btn = _button("Edit layout", "flat")
        self.edit_btn.setToolTip("Rearrange the drawing to match your pad")
        self.read_btn = _button("Read pad", "flat")
        self.read_btn.setToolTip("Ask the pad what's on it right now")
        self.rotate = _button("Rotate view", "flat")
        self.import_btn = _button("Import capture…", "flat")
        self.write_all = _button("", "primary")
        fl.addWidget(self.dot)
        fl.addWidget(self.status)
        fl.addSpacing(16)
        fl.addWidget(self.message, 1)
        fl.addWidget(self.edit_btn)
        fl.addWidget(self.read_btn)
        fl.addWidget(self.rotate)
        fl.addWidget(self.import_btn)
        fl.addWidget(self.write_all)
        outer.addWidget(footer)
        self.setCentralWidget(root)

        self.pad.selected.connect(self._select)
        self.inspector.changed.connect(self._changed)
        self.inspector.revert.connect(self._revert)
        self.inspector.action.connect(self._identify)
        self.inspector.write.connect(lambda c: self._write([c]))
        self.write_all.clicked.connect(lambda: self._write(self._pending()))
        self.edit_btn.clicked.connect(lambda: self._edit_layout(True))
        self.edit_done.clicked.connect(lambda: self._edit_layout(False, keep=True))
        self.edit_cancel.clicked.connect(lambda: self._edit_layout(False, keep=False))
        self.pad.rearranged.connect(self._rearranged)
        self.read_btn.clicked.connect(lambda: self._read(quiet=False))
        self.rotate.clicked.connect(self._rotate)
        self.import_btn.clicked.connect(self._import)

        self.note = core._keyd_warning()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(2000)
        self._poll()
        self._refresh()
        # The pad can tell us what it holds, so ask rather than assume.
        if self.diag and self.diag.ready:
            QTimer.singleShot(0, lambda: self._read(quiet=True))

    # ------------------------------------------------------------- model

    def _pending(self):
        out = []
        for c in self.state.layout.control_ids:
            b = self.desired.get(c)
            if b is None:
                continue
            e = self.state.get(c)
            if e.unknown or e.binding != b:
                out.append(c)
        return out

    def _refresh(self, editor=True):
        pending = set(self._pending())
        looks = {}
        for c in self.state.layout.control_ids:
            e = self.state.get(c)
            if c in pending:
                looks[c] = Look(self.desired[c].label(), known=not e.unknown, pending=True)
            elif e.unknown:
                looks[c] = Look("", known=False)
            else:
                looks[c] = Look(e.binding.label(), known=True, quiet=e.binding.none)
        self.pad.set_looks(looks)

        n = len(pending)
        self.write_all.setText(f"Write {n} change{'s' if n != 1 else ''}")
        self.write_all.setVisible(n > 1)
        self.write_all.setEnabled(bool(self.diag and self.diag.ready))

        c = self.pad.current
        can_write = bool(self.diag and self.diag.ready)
        if not editor:
            self.inspector.update_buttons(c in pending, can_write)
            return
        all_unknown = all(self.state.get(x).unknown
                          for x in self.state.layout.control_ids)
        self.inspector.show_control(
            c, self.state.get(c) if c else None,
            self.desired.get(c) if c in pending else None,
            can_write, all_unknown)

    def _select(self, control):
        self._refresh()

    def _changed(self, control, binding):
        if binding is None:
            self.desired.pop(control, None)
        else:
            self.desired[control] = binding
        self._refresh(editor=False)

    def _revert(self, control):
        self.desired.pop(control, None)
        self._refresh()

    # ------------------------------------------------------------ device

    def _poll(self):
        old = self.diag.status if self.diag else None
        try:
            self.diag = core.diagnose(keyd=False)
        except Exception as e:            # never let polling kill the window
            self._say(f"Couldn't check the pad: {e}", BAD)
            return
        d = self.diag
        if d.status == "unsupported" and d.device and \
                f"{d.device.vid}:{d.device.pid}" in self.adopted:
            self.diag = core.Diagnosis("ready", f"Ready on {d.device.path}.",
                                       device=d.device, warnings=d.warnings)
        colour = {"ready": GOOD, "unplugged": INK_DIM.name()}.get(self.diag.status, BAD)
        self.dot.setStyleSheet(f"background: {colour}; border-radius: 4px;")
        if self.diag.device:
            self.pad.device_id = f"{self.diag.device.vid}:{self.diag.device.pid}"
        self.status.setText({
            "ready": f"Pad ready on {self.diag.device.path}" if self.diag.device else "Pad ready",
            "unplugged": "No pad connected",
            "no-interface": "Pad found, config interface missing",
            "no-permission": "No permission to write to the pad",
            "unsupported": "Unsupported pad",
        }[self.diag.status])
        self.inspector.show_problem(self.diag, self.note)
        if self.diag.status != old:
            self._refresh(editor=False)
            if self.diag.ready and old in ("unplugged", "no-interface", None):
                self._read(quiet=True)

    # ------------------------------------------------------------- write

    def _write(self, controls):
        controls = [c for c in controls if c in self.desired]
        if not controls:
            return
        self._poll()
        if not self.diag.ready:
            self._say(self.diag.headline, BAD)
            return

        done = []
        for c in controls:
            b = self.desired[c]
            try:
                core.write_binding(self.diag.device, c, b, self.state)
            except core.WriteError as e:
                what = core.control_name(c)
                tail = (f"{what} is now marked unknown, since part of it reached the pad."
                        if e.pad_touched else "Nothing reached the pad.")
                why = str(e).split(": ", 1)[-1]
                self._say(f"Couldn't write {what}: {why}. {tail}", BAD)
                self._refresh()
                return
            self.desired.pop(c, None)
            done.append((c, b))
            QApplication.processEvents()

        if len(done) == 1:
            c, b = done[0]
            if b.none:
                self._say(f"{core.control_name(c)} now does nothing.", GOOD)
            else:
                self._say(f"{core.control_name(c)} is now {b.label()}.", GOOD)
        else:
            self._say(f"Wrote {len(done)} changes to the pad.", GOOD)
        self._refresh()

    def _read(self, quiet=False):
        """
        Ask the pad what it holds and believe the answer. Anything you have
        changed but not written is left alone, since that is your intent
        rather than the pad's state.
        """
        if not (self.diag and self.diag.ready):
            if not quiet:
                self._say(self.diag.headline if self.diag else "No pad.", BAD)
            return
        self.read_btn.setEnabled(False)
        self.read_btn.setText("Reading...")
        QApplication.processEvents()
        try:
            got = core.read_into_state(self.diag.device, self.state)
        except core.ReadError as e:
            if not quiet:
                self._say(f"Couldn't read the pad: {e}", BAD)
        else:
            n = len(got)
            self._say(f"Read {n} control{'s' if n != 1 else ''} from the pad.", GOOD)
        finally:
            self.read_btn.setEnabled(True)
            self.read_btn.setText("Read pad")
        self._refresh()

    def _identify(self):
        """
        Interrogate a pad we don't recognise. Reads only. If it answers
        properly we adopt its shape and carry on; if not we say so and
        leave it alone.
        """
        dev = self.diag.device if self.diag else None
        if not dev:
            return
        self.inspector.banner_action.setEnabled(False)
        self.inspector.banner_action.setText("Asking...")
        QApplication.processEvents()
        try:
            found = core.detect(dev)
        finally:
            self.inspector.banner_action.setEnabled(True)
            self.inspector.banner_action.setText("Ask the pad what it is")

        if not found.speaks:
            self._say(f"That pad doesn't speak this protocol: {found.why}", BAD)
            return

        layout = found.layout
        self.state = core.State(layout=layout)
        self.state.layout = layout
        for control, binding in found.bindings.items():
            self.state.record(control, binding)
        self.state.save()
        self.pad.set_layout(layout)
        self.desired.clear()
        self.adopted.add(f"{dev.vid}:{dev.pid}")
        self.settings.setValue("adopted", sorted(self.adopted))
        self._poll()
        self._say(f"It says {layout.keys} keys and {layout.knobs} "
                  f"knob{'s' if layout.knobs != 1 else ''}. Check the drawing "
                  f"matches your pad.", GOOD)
        self._refresh()

    def _say(self, text, colour=None):
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {colour};" if colour else "")

    # ------------------------------------------------------------- misc

    def _edit_layout(self, on, keep=False):
        if on:
            self.editing_from = self.state.layout
            for name, value in (("rows", self.state.layout.rows),
                                ("cols", self.state.layout.cols),
                                ("keys", self.state.layout.keys),
                                ("knobs", self.state.layout.knobs)):
                self.spin[name].blockSignals(True)
                self.spin[name].setValue(value)
                self.spin[name].blockSignals(False)
            self._say("")
        elif not keep:
            self._set_layout(self.editing_from)
        else:
            self.state.save()
            lay = self.state.layout
            self._say(f"Layout saved: {lay.describe()}.", GOOD)
        self.editbar.setVisible(on)
        self.edit_btn.setEnabled(not on)
        self.read_btn.setEnabled(not on)
        self.inspector.editing_layout = on
        self.pad.select(None if on else self.pad.current)
        self.pad.set_editing(on)
        self._refresh()

    def _set_layout(self, layout):
        self.state.layout = layout
        self.pad.set_layout(layout)
        if self.pad.current not in layout.control_ids:
            self.pad.current = None
        self._refresh()

    def _rearranged(self, layout):
        self.state.layout = layout

    def _resize_layout(self):
        lay = self.state.layout.resized(
            rows=self.spin["rows"].value(), cols=self.spin["cols"].value(),
            keys=self.spin["keys"].value(), knobs=self.spin["knobs"].value())
        if lay.rows != self.spin["rows"].value():     # it had to grow to fit
            self.spin["rows"].blockSignals(True)
            self.spin["rows"].setValue(lay.rows)
            self.spin["rows"].blockSignals(False)
        self._set_layout(lay)
        self.pad.set_editing(True)

    def _rotate(self):
        o = "flat" if self.pad.orientation == "upright" else "upright"
        self.pad.set_orientation(o)
        self.settings.setValue("orientation", o)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import a capture of the vendor software",
            str(Path.cwd()), "Captures (*.txt);;All files (*)")
        if not path:
            return
        try:
            got = core.import_capture(path, self.state)
        except (OSError, ValueError) as e:
            self._say(f"Couldn't read that capture: {e}", BAD)
            return
        if not got:
            self._say("No saved bindings found in that capture.", BAD)
            return
        for c, _, _ in got:
            self.desired.pop(c, None)
        self._say(f"Imported {len(got)} bindings from {Path(path).name}.", GOOD)
        self._refresh()

    def closeEvent(self, e):
        n = len(self._pending())
        if not n:
            e.accept()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Unwritten changes")
        box.setText(f"{n} change{'s are' if n != 1 else ' is'} not on the pad yet.")
        write = box.addButton("Write and close", QMessageBox.AcceptRole)
        discard = box.addButton("Close without writing", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is write:
            self._write(self._pending())
            e.accept() if not self._pending() else e.ignore()
        elif box.clickedButton() is discard:
            e.accept()
        else:
            e.ignore()


def apply_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, colour in ((QPalette.Window, WINDOW), (QPalette.Base, QColor(FIELD)),
                         (QPalette.Text, INK), (QPalette.WindowText, INK),
                         (QPalette.Button, WINDOW), (QPalette.ButtonText, INK),
                         (QPalette.Highlight, TAPE), (QPalette.HighlightedText, QColor("#1b1206"))):
        pal.setColor(role, colour)
    app.setPalette(pal)
    app.setStyleSheet(STYLE)


ICON = Path(__file__).resolve().parent.parent / "packaging" / "macropad.svg"


def run():
    app = QApplication(sys.argv)
    app.setApplicationName("macropad")
    app.setDesktopFileName("macropad")
    # The launcher gives Plasma the icon; this covers the window itself, X11
    # sessions, and running straight from a git checkout.
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))
    apply_theme(app)
    w = Window()
    w.resize(1000, 800)
    w.show()
    return app.exec()
