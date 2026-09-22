"""
The window. Open it, change what a control does, write it, close it. The
pad keeps its bindings in its own memory, so nothing here needs to keep
running afterwards.
"""

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (QActionGroup, QColor, QFont, QFontDatabase,
                           QGuiApplication, QIcon,
                           QPalette)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget,
                               QCheckBox, QComboBox, QMenu)

from . import actions as act, core, keymap, padview, themes
from .padview import INK, INK_DIM, TAPE, WINDOW, Look, PadView

# Set by apply_theme; functions read these when they run, so they follow a
# theme change without anything else having to know.
GOOD = BAD = LINE = FIELD = ""

# Media keys as pairs, so opposites sit side by side.
MEDIA_GRID = [("playpause", "stop"), ("prev", "next"),
              ("volumedown", "volumeup"), ("mute", None),
              ("brightnessdown", "brightnessup"),
              ("calculator", "mail"), ("browser", "mycomputer")]

def stylesheet(t):
    """The whole window's look, from one theme."""
    tint = themes.mix
    return f"""
QWidget {{ color: {t.ink}; background: {t.window}; }}
QLabel[role="title"] {{ font-size: 18px; font-weight: 600; }}
QLabel[role="dim"] {{ color: {t.ink_dim}; }}
QLabel[role="caption"] {{ color: {t.ink_dim}; padding: 14px 0px 0px 0px; margin: 0px; }}
QLabel[role="value"] {{ font-size: 15px; }}
QPushButton {{ background: transparent; border: 1px solid {t.line};
              border-radius: 4px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {t.ink_dim}; }}
QPushButton:checked {{ border-color: {t.accent}; color: {t.accent}; }}
QPushButton:disabled {{ color: {tint(t.window, t.ink_dim, 0.55)}; border-color: {tint(t.window, t.line, 0.6)}; }}
QPushButton[role="primary"] {{ background: {t.accent}; color: {t.accent_text};
              border: none; font-weight: 600; padding: 8px 16px; }}
QPushButton[role="primary"]:hover {{ background: {tint(t.accent, t.ink, 0.15)}; }}
QPushButton[role="primary"]:disabled {{ background: {tint(t.window, t.line, 0.7)}; color: {tint(t.window, t.ink_dim, 0.7)}; }}
QPushButton[role="flat"] {{ border: none; color: {t.ink_dim}; padding: 6px 8px; }}
QPushButton[role="flat"]:hover {{ color: {t.ink}; }}
QPushButton[role="media"] {{ text-align: left; padding: 7px 10px; }}
QLineEdit {{ background: {t.field}; border: 1px solid {t.line}; border-radius: 4px;
            padding: 7px 8px; }}
QLineEdit:focus {{ border-color: {t.ink_dim}; }}
QLineEdit[recording="true"] {{ border-color: {t.accent}; background: {tint(t.field, t.accent, 0.12)}; }}
QPlainTextEdit {{ background: {t.field}; border: 1px solid {t.line}; border-radius: 4px; }}
QFrame[role="banner"] {{ border: 1px solid {t.line}; border-left: 3px solid {t.bad};
            border-radius: 4px; }}
QFrame[role="banner"][tone="quiet"] {{ border-left: 3px solid {t.ink_dim}; }}
QFrame[role="banner"] QLabel, QFrame[role="banner"] QPushButton {{ background: transparent; }}
QFrame[role="footer"] {{ border-top: 1px solid {t.line}; }}
QFrame[role="editbar"] {{ border-top: 1px solid {t.accent}; background: {tint(t.window, t.accent, 0.06)}; }}
QFrame[role="editbar"] QLabel {{ background: transparent; }}
QSpinBox {{ background: {t.field}; border: 1px solid {t.line}; border-radius: 4px;
            padding: 4px 6px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {t.ink_dim};
            border-radius: 3px; background: {t.field}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}
QComboBox {{ background: {t.field}; border: 1px solid {t.line}; border-radius: 4px;
            padding: 6px 8px; }}
QMenu {{ background: {t.field}; border: 1px solid {t.line}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; }}
QMenu::item:selected {{ background: {tint(t.field, t.accent, 0.25)}; }}
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
    action = Signal()                   # the banner's button
    host_action = Signal(str, object, object)   # control, Action or None, trigger
    test_action = Signal(object)
    background = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setFixedWidth(400)
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
        self.mode_action = _button("Action", checkable=True)
        self.mode_action.setToolTip("Open a web page, an app, or run a command")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for i, b in enumerate((self.mode_keys, self.mode_media, self.mode_none,
                               self.mode_action)):
            self.mode_group.addButton(b, i)
            b.setStyleSheet("padding: 6px 8px;")
            modes.addWidget(b, 1)
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
        ap = QWidget()
        al = QVBoxLayout(ap)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(6)
        kinds = QHBoxLayout()
        kinds.setSpacing(6)
        self.kind_group = QButtonGroup(self)
        self.kind_group.setExclusive(True)
        self.kind_buttons = {}
        for i, (kind, label) in enumerate((("url", "Web page"), ("app", "App"),
                                           ("command", "Command"), ("text", "Text"))):
            b = _button(label, "media", checkable=True)
            self.kind_group.addButton(b, i)
            self.kind_buttons[kind] = b
            kinds.addWidget(b)
        al.addLayout(kinds)
        self.action_edit = QLineEdit()
        self.app_pick = QComboBox()
        self.app_pick.setMaxVisibleItems(18)
        self._apps_loaded = False
        al.addWidget(self.action_edit)
        al.addWidget(self.app_pick)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("Kind regards,\nProbler")
        self.text_edit.setFixedHeight(84)
        self.paste_terminal = QCheckBox("Paste with Ctrl+Shift+V, for terminals")
        al.addWidget(self.text_edit)
        al.addWidget(self.paste_terminal)
        fix = QHBoxLayout()
        self.text_fix = _label("", "dim", wrap=True)
        self.text_fix_btn = _button("Copy fix")
        self.text_fix_btn.setToolTip("Copies the commands; paste them into a terminal")
        fix.addWidget(self.text_fix, 1)
        fix.addWidget(self.text_fix_btn, 0, Qt.AlignTop)
        al.addLayout(fix)
        self._text_fix_cmds = []
        sends = QHBoxLayout()
        sends.addWidget(_label("Sends", "dim"))
        self.trigger_pick = QComboBox()
        self.trigger_pick.setToolTip(
            "The spare key this control sends to your computer. Pick another "
            "if this one does something on your desktop already.")
        sends.addWidget(self.trigger_pick, 1)
        al.addLayout(sends)
        self.trigger_note = _label("", "dim", wrap=True)
        al.addWidget(self.trigger_note)
        row = QHBoxLayout()
        self.action_hint = _label("", "dim", wrap=True)
        self.test_btn = _button("Test")
        self.test_btn.setToolTip("Run it now, on this computer")
        row.addWidget(self.action_hint, 1)
        row.addWidget(self.test_btn, 0, Qt.AlignTop)
        al.addLayout(row)
        self.bg_check = QCheckBox("Run actions in the background")
        self.bg_check.setToolTip("Starts a small listener now and whenever you log in")
        self.bg_status = _label("", "dim", wrap=True)
        al.addSpacing(6)
        al.addWidget(self.bg_check)
        al.addWidget(self.bg_status)

        self.pages = (kp, mp, np_, ap)
        ev.addWidget(kp)
        ev.addWidget(mp)
        ev.addWidget(np_)
        ev.addWidget(ap)

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
        self.kind_group.idClicked.connect(self._kind_picked)
        self.action_edit.textEdited.connect(lambda _: self._action_edited())
        self.app_pick.activated.connect(lambda _: self._action_edited())
        self.trigger_pick.activated.connect(lambda _: self._action_edited())
        self.text_edit.textChanged.connect(self._action_edited)
        self.paste_terminal.toggled.connect(lambda _: self._action_edited())
        self.text_fix_btn.clicked.connect(self._copy_text_fix)
        self.test_btn.clicked.connect(self._test)
        self.bg_check.toggled.connect(lambda on: None if self._loading
                                      else self.background.emit(on))
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

    def show_control(self, control, entry, desired, can_write, all_unknown=False,
                     action=None, onpad_action=None, pending=None, bg=None,
                     triggers=None, trigger=None):
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
            trig = act.trigger_of(entry.binding)
            if onpad_action and trig:
                self.onpad.setText(onpad_action.label())
                self.onpad_why.setText(f"The pad sends {trig.upper()}, and this "
                                       "computer does the rest.")
                self.onpad_why.show()
            else:
                self.onpad.setText(entry.binding.label())
                self.onpad_why.hide()
            self.onpad.setStyleSheet("")

        shown = desired or (None if entry.unknown else entry.binding)
        self.show_background(bg)
        self._clear_action()
        self.set_triggers(triggers or [], trigger)
        if action is not None:
            self.mode_action.setChecked(True)
            self._page(3)
            self._show_action(action)
            self.keys_edit.clear()
            self._clear_media()
        elif shown is not None and shown.none:
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
        self.update_buttons(desired is not None if pending is None else pending,
                            can_write)
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
        if i == 3 and not self._loading:
            if not self.kind_group.checkedButton():
                self.kind_buttons["url"].setChecked(True)
                self._kind_picked(0)
            self.action_edit.setFocus()
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

    # ------------------------------------------------------------ actions

    _PLACEHOLDER = {"url": "github.com", "command": "~/bin/switch-audio.sh"}

    def _kind(self):
        b = self.kind_group.checkedButton()
        return next((k for k, btn in self.kind_buttons.items() if btn is b), "url")

    def set_triggers(self, options, current):
        """
        options: [(trigger, used_by or None)]. Taken keys are shown but can't
        be picked; keys the desktop is known to act on carry a warning.
        """
        was, self._loading = self._loading, True
        self.trigger_pick.clear()
        self.trigger_pick.addItem("Choose automatically", None)
        model = self.trigger_pick.model()
        for trig, used_by in options:
            text = trig.upper()
            if used_by:
                text += f"   used by {used_by}"
            elif trig in act.CLAIMED:
                text += f"   {act.CLAIMED[trig]}"
            self.trigger_pick.addItem(text, trig)
            if used_by:
                model.item(self.trigger_pick.count() - 1).setEnabled(False)
        i = self.trigger_pick.findData(current) if current else 0
        self.trigger_pick.setCurrentIndex(max(i, 0))
        self._loading = was
        self._note_trigger(current)

    def set_trigger(self, trigger):
        """Show which key was picked, without it counting as an edit."""
        was, self._loading = self._loading, True
        i = self.trigger_pick.findData(trigger)
        if i >= 0:
            self.trigger_pick.setCurrentIndex(i)
        self._loading = was
        self._note_trigger(trigger)

    def _note_trigger(self, trigger):
        if trigger in act.CLAIMED:
            self.trigger_note.setText(
                f"<span style='color:{TAPE.name()}'>{trigger.upper()} {act.CLAIMED[trigger]} "
                "on many desktops, so it may do that as well. Pick another if it "
                "does.</span>")
        else:
            self.trigger_note.setText("")

    def _clear_action(self):
        """Nothing carries over from the last control you looked at."""
        self.action_edit.clear()
        self.text_edit.clear()
        self.paste_terminal.setChecked(False)
        if self.app_pick.count():
            self.app_pick.setCurrentIndex(0)
        self.action_hint.setText("")
        self.test_btn.setEnabled(False)
        self._last_kind = None

    def _kind_picked(self, _i):
        kind = self._kind()
        # a URL isn't a command and vice versa, so switching kind starts afresh
        if not self._loading and getattr(self, "_last_kind", None) not in (None, kind):
            self.action_edit.clear()
            self.text_edit.clear()
        self._last_kind = kind
        is_app, is_text = kind == "app", kind == "text"
        self.app_pick.setVisible(is_app)
        self.action_edit.setVisible(not is_app and not is_text)
        self.text_edit.setVisible(is_text)
        self.paste_terminal.setVisible(is_text)
        self._show_text_problems(is_text)
        if is_app:
            self._load_apps()
        elif is_text:
            pass
        else:
            self.action_edit.setPlaceholderText(self._PLACEHOLDER[kind])
            self.action_edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont)
                                     if kind == "command" else self.font())
        if not self._loading:
            self._action_edited()

    def _load_apps(self):
        if self._apps_loaded:
            return
        self.app_pick.clear()
        self.app_pick.addItem("Pick an app...", None)
        for name, command in act.installed_apps():
            self.app_pick.addItem(name, (name, command))
        self._apps_loaded = True

    def _current_action(self):
        kind = self._kind()
        if kind == "text":
            text = self.text_edit.toPlainText()
            paste = "ctrl+shift+v" if self.paste_terminal.isChecked() else ""
            return act.Action("text", text, paste=paste) if text.strip() else None
        if kind == "app":
            data = self.app_pick.currentData()
            return act.Action("app", data[1], data[0]) if data else None
        text = self.action_edit.text().strip()
        return act.Action(kind, text) if text else None

    def _show_action(self, action):
        self.kind_buttons[action.kind].setChecked(True)
        self._kind_picked(0)
        if action.kind == "text":
            self.text_edit.setPlainText(action.target)
            self.paste_terminal.setChecked(action.paste == "ctrl+shift+v")
        elif action.kind == "app":
            self._load_apps()
            i = self.app_pick.findData((action.name, action.target))
            if i < 0:                                   # not installed any more
                self.app_pick.addItem(action.label(), (action.name, action.target))
                i = self.app_pick.count() - 1
            self.app_pick.setCurrentIndex(i)
        else:
            self.action_edit.setText(action.target)
        self.action_hint.setText(self._describe(action))
        self.test_btn.setEnabled(True)

    @staticmethod
    def _describe(action):
        return {"url": f"Opens {action.url}",
                "app": f"Starts {action.label()}",
                "command": "Runs this in a shell, as you",
                "text": "Types it wherever your cursor is"}[action.kind]

    def _action_edited(self):
        if self._loading or self.control is None:
            return
        action = self._current_action()
        want = self.trigger_pick.currentData()
        if action is None:
            self.action_hint.setText("")
            self.test_btn.setEnabled(False)
            self.host_action.emit(self.control, None, None)
            return
        try:
            action.validate()
        except ValueError as e:
            self.action_hint.setText(f"<span style='color:{BAD}'>{e}</span>")
            self.test_btn.setEnabled(False)
            self.host_action.emit(self.control, None, None)
            return
        self.action_hint.setText(self._describe(action))
        self.test_btn.setEnabled(True)
        self.host_action.emit(self.control, action, want)

    def _show_text_problems(self, visible):
        problems = act.text_problems() if visible else []
        self._text_fix_cmds = [c for _, cmds in problems for c in cmds
                               if not c.startswith("#")]
        if problems:
            why = " ".join(w for w, _ in problems)
            self.text_fix.setText(f"<span style='color:{TAPE.name()}'>{why}</span> "
                                  "Copy the fix, paste it into a terminal, then "
                                  "come back.")
        else:
            self.text_fix.setText("")
        self.text_fix.setVisible(bool(problems))
        self.text_fix_btn.setVisible(bool(self._text_fix_cmds))

    def _copy_text_fix(self):
        QGuiApplication.clipboard().setText("\n".join(self._text_fix_cmds))
        self.text_fix_btn.setText("Copied")
        QTimer.singleShot(1500, lambda: self.text_fix_btn.setText("Copy fix"))

    def _test(self):
        action = self._current_action()
        if action:
            self.test_action.emit(action)

    def show_background(self, running):
        """running: None unknown, False off, True on."""
        if running is None:
            return
        self._loading, was = True, self._loading
        self.bg_check.setChecked(act.autostart_enabled())
        self._loading = was
        if running:
            self.bg_status.setText("Running. Action keys work, and it starts "
                                   "again when you log in.")
        elif act.autostart_enabled():
            self.bg_status.setText("Set to start at login, but not running "
                                   "right now. Untick and tick to start it.")
        else:
            self.bg_status.setText("Off. Action keys send F13 to F24 and nothing "
                                   "happens until this is on. Everything else "
                                   "on the pad works as normal.")

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
        self.actions = act.ActionStore()     # trigger key -> host action
        self.pending_actions = {}            # control -> act.Action, unsaved
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
        self.theme_btn = _button("Theme", "flat")
        self.theme_btn.setToolTip("Change how MacroPad looks")
        self.theme_menu = QMenu(self)
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        current = self.settings.value("theme", themes.DEFAULT)
        for t in themes.THEMES.values():
            a = self.theme_menu.addAction(t.name)
            a.setCheckable(True)
            a.setChecked(t.key == current)
            a.setData(t.key)
            self.theme_group.addAction(a)
        self.theme_btn.setMenu(self.theme_menu)
        self.rotate = _button("Rotate view", "flat")
        self.import_btn = _button("Import capture…", "flat")
        self.write_all = _button("", "primary")
        fl.addWidget(self.dot)
        fl.addWidget(self.status)
        fl.addSpacing(16)
        fl.addWidget(self.message, 1)
        fl.addWidget(self.edit_btn)
        fl.addWidget(self.read_btn)
        fl.addWidget(self.theme_btn)
        fl.addWidget(self.rotate)
        fl.addWidget(self.import_btn)
        fl.addWidget(self.write_all)
        outer.addWidget(footer)
        self.setCentralWidget(root)

        self.pad.selected.connect(self._select)
        self.inspector.changed.connect(self._changed)
        self.inspector.revert.connect(self._revert)
        self.inspector.action.connect(self._identify)
        self.inspector.host_action.connect(self._host_action)
        self.inspector.test_action.connect(self._test_action)
        self.inspector.background.connect(self._background)
        self.inspector.write.connect(lambda c: self._write([c]))
        self.write_all.clicked.connect(lambda: self._write(self._pending()))
        self.edit_btn.clicked.connect(lambda: self._edit_layout(True))
        self.edit_done.clicked.connect(lambda: self._edit_layout(False, keep=True))
        self.edit_cancel.clicked.connect(lambda: self._edit_layout(False, keep=False))
        self.pad.rearranged.connect(self._rearranged)
        self.read_btn.clicked.connect(lambda: self._read(quiet=False))
        self.rotate.clicked.connect(self._rotate)
        self.theme_group.triggered.connect(lambda a: self._set_theme(a.data()))
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

    def _binding_now(self, c):
        """What the control sends once pending edits are written."""
        if c in self.desired:
            return self.desired[c]
        e = self.state.get(c)
        return None if e.unknown else e.binding

    def _stored_action(self, binding):
        trig = act.trigger_of(binding)
        return self.actions.get(trig) if trig else None

    def _action_for(self, c):
        return self.pending_actions.get(c) or self._stored_action(self._binding_now(c))

    def _trigger_options(self, control):
        """[(trigger, name of the control holding it, or None)] for the picker."""
        holders = {}
        for c in self.state.layout.control_ids:
            if c == control:
                continue
            b = self._binding_now(c)
            if b and b.keys and b.keys.lower() in act.TRIGGERS:
                holders[b.keys.lower()] = core.control_name(c)
        for t in self.actions.by_trigger:
            holders.setdefault(t, "another key")
        mine = act.trigger_of(self._binding_now(control)) if control else None
        return [(t, None if t == mine else holders.get(t)) for t in act.TRIGGERS]

    def _pending(self):
        out = []
        for c in self.state.layout.control_ids:
            e = self.state.get(c)
            b = self.desired.get(c)
            pad = b is not None and (e.unknown or e.binding != b)
            host = (c in self.pending_actions and
                    self._stored_action(self._binding_now(c)) != self.pending_actions[c])
            if pad or host:
                out.append(c)
        return out

    def _refresh(self, editor=True):
        pending = set(self._pending())
        looks = {}
        for c in self.state.layout.control_ids:
            e = self.state.get(c)
            if c in pending:
                a = self.pending_actions.get(c)
                text = a.label() if a else self._binding_now(c).label()
                looks[c] = Look(text, known=not e.unknown, pending=True)
            elif e.unknown:
                looks[c] = Look("", known=False)
            else:
                a = self._stored_action(e.binding)
                looks[c] = Look(a.label() if a else e.binding.label(), known=True,
                                quiet=e.binding.none)
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
        e = self.state.get(c) if c else None
        self.inspector.show_control(
            c, e, self.desired.get(c) if c in pending else None,
            can_write, all_unknown,
            action=self._action_for(c) if c else None,
            onpad_action=self._stored_action(e.binding) if e and not e.unknown else None,
            pending=c in pending,
            bg=bool(act.listener_pid()),
            triggers=self._trigger_options(c) if c else None,
            trigger=act.trigger_of(self._binding_now(c)) if c else None)

    def _select(self, control):
        self._refresh()

    def _changed(self, control, binding):
        self.pending_actions.pop(control, None)
        if binding is None:
            self.desired.pop(control, None)
        else:
            self.desired[control] = binding
        self._refresh(editor=False)

    def _host_action(self, control, action, want=None):
        """
        Point a control at a host action. It gets a spare F13 to F24 key on
        the pad; if it already has one, it keeps it, so changing what the
        action does never needs a write to the pad.
        """
        if action is None:
            self.pending_actions.pop(control, None)
            if act.trigger_of(self.desired.get(control)):
                self.desired.pop(control, None)
            self._refresh(editor=False)
            return

        used = set()
        for c in self.state.layout.control_ids:
            b = self._binding_now(c) if c != control else None
            if b and b.keys:
                used.add(b.keys.lower())
        current = act.trigger_of(self._binding_now(control))
        trig = act.pick_trigger(self.actions, used, current, want)
        if trig is None:
            self._say("All twelve action keys, F13 to F24, are taken. Free one "
                      "up by setting another action control to something else.", BAD)
            return

        want = core.Binding(keys=trig)
        e = self.state.get(control)
        if e.unknown or e.binding != want:
            self.desired[control] = want
        else:
            self.desired.pop(control, None)
        self.pending_actions[control] = action
        self.inspector.set_trigger(trig)
        self._refresh(editor=False)

    def _test_action(self, action):
        if action.kind == "text":
            problems = act.text_problems()
            if problems:
                self._say(problems[0][0], BAD)
                return
            # typing into MacroPad itself would be useless, so give time to
            # click somewhere else first
            for n in (3, 2, 1):
                QTimer.singleShot((3 - n) * 1000, lambda n=n: self._say(
                    f"Typing in {n}... click where you want it.", GOOD))
            QTimer.singleShot(3000, lambda: self._run_test(action))
            return
        self._run_test(action)

    def _run_test(self, action):
        try:
            action.run()
        except (OSError, subprocess.SubprocessError) as e:
            self._say(f"Couldn't run it: {getattr(e, 'strerror', None) or e}", BAD)
            return
        self._say(f"Ran it: {action.label()}.", GOOD)

    def _background(self, on):
        act.set_autostart(on)
        if on:
            act.start_listener()
            self._say("Background actions on. They'll start again when you log in.", GOOD)
        else:
            act.stop_listener()
            self._say("Background actions off.", GOOD)
        QTimer.singleShot(700, lambda: self.inspector.show_background(
            bool(act.listener_pid())))

    def _revert(self, control):
        self.pending_actions.pop(control, None)
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
            "unsupported-os": "Not supported on this system",
        }[self.diag.status])
        self.inspector.show_problem(self.diag, self.note)
        self.inspector.show_background(bool(act.listener_pid()))
        if self.diag.status != old:
            self._refresh(editor=False)
            if self.diag.ready and old in ("unplugged", "no-interface", None):
                self._read(quiet=True)

    # ------------------------------------------------------------- write

    def _write(self, controls):
        controls = [c for c in controls
                    if c in self.desired or c in self.pending_actions]
        if not controls:
            return
        self._poll()
        needs_pad = any(c in self.desired for c in controls)
        if needs_pad and not self.diag.ready:
            self._say(self.diag.headline, BAD)
            return

        done, host_changed = [], False
        for c in controls:
            b = self.desired.get(c)
            e = self.state.get(c)
            if b is not None and (e.unknown or e.binding != b):
                try:
                    core.write_binding(self.diag.device, c, b, self.state)
                except core.WriteError as err:
                    what = core.control_name(c)
                    tail = (f"{what} is now marked unknown, since part of it "
                            "reached the pad." if err.pad_touched
                            else "Nothing reached the pad.")
                    why = str(err).split(": ", 1)[-1]
                    self._say(f"Couldn't write {what}: {why}. {tail}", BAD)
                    self._refresh()
                    return
            self.desired.pop(c, None)
            action = self.pending_actions.pop(c, None)
            if action:
                trig = act.trigger_of(self._binding_now(c))
                if trig:
                    self.actions.set(trig, action)
                    host_changed = True
            done.append((c, b, action))
            QApplication.processEvents()

        # Forget actions whose key is no longer on the pad, but only when we
        # know every control; otherwise one might still be sitting there.
        ids = self.state.layout.control_ids
        if all(not self.state.get(x).unknown for x in ids):
            bound = {self.state.get(x).binding.keys.lower()
                     for x in ids if self.state.get(x).binding.keys}
            if self.actions.prune(bound):
                host_changed = True
        if host_changed:
            self.actions.save()

        if len(done) == 1:
            c, b, action = done[0]
            name = core.control_name(c)
            if action:
                msg = f"{name} is set: {action.label()}."
                if not act.listener_pid():
                    msg += " Turn on background actions for it to work."
            elif b is not None and b.none:
                msg = f"{name} now does nothing."
            else:
                msg = f"{name} is now {b.label()}."
            self._say(msg, GOOD)
        else:
            self._say(f"Wrote {len(done)} changes.", GOOD)
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

    def _set_theme(self, key):
        t = apply_theme(QApplication.instance(), key)
        self.settings.setValue("theme", t.key)
        self._poll()                 # redraws the status dot in the new colours
        self._refresh()
        self.pad.update()
        self._say(f"Theme: {t.name}.", GOOD)

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


def apply_theme(app, key=None):
    """Switch the whole app, window and drawing, to one theme."""
    global GOOD, BAD, LINE, FIELD
    t = themes.get(key or themes.DEFAULT)
    themes.load_fonts()
    padview.apply_theme(t)
    GOOD, BAD, LINE, FIELD = t.good, t.bad, t.line, t.field
    app.setStyle("Fusion")
    pal = QPalette()
    for role, colour in ((QPalette.Window, t.window), (QPalette.Base, t.field),
                         (QPalette.Text, t.ink), (QPalette.WindowText, t.ink),
                         (QPalette.Button, t.window), (QPalette.ButtonText, t.ink),
                         (QPalette.Highlight, t.accent),
                         (QPalette.HighlightedText, t.accent_text),
                         (QPalette.ToolTipBase, t.field), (QPalette.ToolTipText, t.ink),
                         (QPalette.PlaceholderText, t.ink_dim)):
        pal.setColor(role, QColor(colour))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(t))
    return t


ICON = Path(__file__).resolve().parent.parent / "packaging" / "macropad.svg"


def run():
    app = QApplication(sys.argv)
    app.setApplicationName("macropad")
    app.setDesktopFileName("macropad")
    # The launcher gives Plasma the icon; this covers the window itself, X11
    # sessions, and running straight from a git checkout.
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))
    apply_theme(app, QSettings("macropad", "macropad").value("theme", themes.DEFAULT))
    w = Window()
    w.resize(1000, 800)
    w.show()
    return app.exec()


if __name__ == "__main__":
    # `python -m macropad_gui.app` lands here. `python -m macropad_gui` is the
    # documented way in, but people reach for this one, so make it work.
    sys.exit(run())
