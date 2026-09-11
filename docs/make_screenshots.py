#!/usr/bin/env python3
"""
Regenerate every image in docs/images from the real app, offscreen.

    QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=2 python3 docs/make_screenshots.py

Uses a fake pad and a throwaway config folder, so it never touches your real
pad or your saved state. Looks best with Noto Sans and Hack installed, which
are Plasma's defaults.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()

from PySide6.QtCore import QPointF, QRectF, Qt                      # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QImage,     # noqa: E402
                           QLinearGradient, QPainter, QPen)
from PySide6.QtSvg import QSvgRenderer                                # noqa: E402
from PySide6.QtWidgets import QApplication                            # noqa: E402

from macropad_gui import app as A, core, padview                      # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    Image = None

B = core.Binding
DEMO = {
    "key4": B(keys="super+ctrl+left"), "key8": B(keys="super+w"),
    "key12": B(keys="super+ctrl+right"),
    "key3": B(media="prev"), "key7": B(media="playpause"), "key11": B(media="next"),
    "key2": B(keys="ctrl+z"), "key6": B(keys="ctrl+shift+z"), "key10": B(keys="ctrl+s"),
    "key1": B(keys="printscreen"), "key5": B(keys="ctrl+alt+t"), "key9": B(media="mycomputer"),
    "dial1-left": B(media="volumedown"), "dial1-push": B(media="mute"),
    "dial1-right": B(media="volumeup"),
    "dial2-left": B(media="brightnessdown"), "dial2-push": B(media="calculator"),
    "dial2-right": B(media="brightnessup"),
}

READY = core.Diagnosis("ready", "Ready on /dev/hidraw10.",
                       device=core.Device("/dev/hidraw10", 3, True))


def fonts(app):
    ui = QFont("Noto Sans", 10)
    if "Noto Sans" in QFontDatabase.families():
        app.setFont(ui)
    if "Hack" in QFontDatabase.families():
        hack = QFont("Hack", 10)
        real = QFontDatabase.systemFont
        QFontDatabase.systemFont = staticmethod(
            lambda which: hack if which == QFontDatabase.FixedFont else real(which))


def window(diag=READY, bindings=None, note=None):
    with mock.patch.object(core, "_keyd_warning", return_value=note), \
         mock.patch.object(core, "diagnose", return_value=diag):
        w = A.Window()
    w.timer.stop()
    w.diag = diag
    w.note = note
    w.state = core.State(Path(tempfile.mkdtemp()) / "state.json")
    for c, b in (bindings or {}).items():
        w.state.record(c, b)
    w.resize(1000, 800)
    w._poll_fake = diag
    w.inspector.show_problem(diag, note)
    status_colour = {"ready": A.GOOD, "unplugged": padview.INK_DIM.name()}.get(diag.status, A.BAD)
    w.dot.setStyleSheet(f"background: {status_colour}; border-radius: 4px;")
    w.status.setText({"ready": "Pad ready on /dev/hidraw10",
                      "unplugged": "No pad connected",
                      "no-permission": "No permission to write to the pad",
                      "unsupported": "Unsupported pad"}[diag.status])
    w._refresh()
    return w


def select(w, control):
    w.pad.select(control)
    w._refresh()


def save(w, name, frame=True):
    w.show()
    QApplication.processEvents()
    img = w.grab().toImage()
    path = OUT / f"{name}.png"
    img.save(str(path))
    if frame and Image:
        _frame(path)
    w.desired.clear()
    w.close()
    print("wrote", path.relative_to(ROOT))


def _frame(path, radius=20, pad=56):
    """Rounded corners, a hairline edge and a soft shadow, on transparency."""
    shot = Image.open(path).convert("RGBA")
    w, h = shot.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius, fill=255)
    shot.putalpha(mask)
    edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle([0, 0, w - 1, h - 1], radius,
                                           outline=(255, 255, 255, 34), width=2)
    shot = Image.alpha_composite(shot, edge)

    canvas = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad + 14, pad + w, pad + h + 14], radius, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(shot, (pad, pad))
    canvas.save(path, optimize=True)


# ------------------------------------------------------------------ scenes

def scene_hero():
    w = window(bindings=DEMO)
    select(w, "key12")
    w._say("Wrote 3 changes to the pad.", A.GOOD)
    save(w, "hero")


def scene_first_launch():
    w = window()
    save(w, "first-launch")


def scene_select_and_type():
    w = window(bindings={k: v for k, v in DEMO.items() if k.startswith("dial")})
    select(w, "key2")
    w.inspector.keys_edit.setText("ctrl+z")
    w.inspector._keys_typed("ctrl+z")
    save(w, "step-type")


def scene_record():
    w = window(bindings={k: v for k, v in DEMO.items() if k.startswith("dial")})
    select(w, "key6")
    w.inspector.record.setChecked(True)
    w.inspector.keys_edit.setText("ctrl+shift+…")
    save(w, "step-record")


def scene_media():
    w = window(bindings={k: v for k, v in DEMO.items() if k.startswith("key")})
    select(w, "dial1-left")
    w.inspector.mode_media.click()
    w.inspector.media_buttons["volumedown"].click()
    save(w, "step-media")


def scene_nothing():
    b = dict(DEMO)
    b["key1"] = B(none=True)
    w = window(bindings=b)
    w.desired["key5"] = B(none=True)
    select(w, "key5")
    w._refresh()
    save(w, "step-nothing")


def scene_pending():
    w = window(bindings={k: v for k, v in DEMO.items()
                         if k not in ("key2", "key6", "key10")})
    for k in ("key2", "key6", "key10"):
        w.desired[k] = DEMO[k]
    select(w, "key10")
    w.inspector.keys_hint.setText("Sends Ctrl+S")
    w._refresh(editor=False)
    save(w, "step-pending")


def scene_written():
    w = window(bindings=DEMO)
    select(w, "key10")
    w._say("Wrote 3 changes to the pad.", A.GOOD)
    save(w, "step-written")


def scene_flat():
    w = window(bindings=DEMO)
    w.pad.set_orientation("flat")
    select(w, "dial2-push")
    save(w, "flat-view")


def scene_permission():
    d = core.Diagnosis(
        "no-permission", "/dev/hidraw10 isn't writable.",
        device=core.Device("/dev/hidraw10", 3, False),
        detail=["No udev rule lets you write to the pad yet. Running the installer "
                "again adds it, or paste these commands into a terminal:"],
        fix=[f"echo '{core.RULE_LINE}' | sudo tee /etc/udev/rules.d/60-macropad.rules",
             "sudo udevadm control --reload-rules && sudo udevadm trigger",
             "# then unplug and replug the pad"])
    w = window(diag=d, bindings=DEMO)
    select(w, "key4")
    save(w, "permission")


def scene_keyd():
    note = ("keyd, a key remapping service, is running and /etc/keyd/default.conf "
            "lists this pad. It can change keys on their way from the pad to your "
            "desktop, so a key may not do exactly what you set here. If you only "
            "set keyd up for this pad, you can switch it off with: "
            "sudo systemctl disable --now keyd")
    w = window(bindings=DEMO, note=note)
    save(w, "keyd-note")


def scene_unsupported():
    d = core.Diagnosis(
        "unsupported", "Found a 1189:8890 pad, which this app doesn't know yet.",
        detail=["So far only 1189:8840 (12 keys, 2 dials) has been worked out. "
                "Pads in this family differ in layout and protocol, so writing to "
                "yours with the wrong one could fail or scramble it. The app won't try.",
                "ch57x-keyboard-tool, a command line tool, supports several of these "
                "pads today. To get yours into this app, the README's \"Other pads\" "
                "section shows how to capture what the vendor software sends."])
    w = window(diag=d)
    save(w, "unsupported")


def scene_legend():
    """Four keycaps showing every state the drawing uses."""
    scale = 2
    names = [("Written", "on the pad", padview.Look("Ctrl+C", known=True), None),
             ("Unknown", "could be anything", padview.Look(), None),
             ("Changed", "not written yet", padview.Look("Mute", known=True, pending=True), None),
             ("Selected", "being edited", padview.Look("Ctrl+S", known=True), "sel")]
    cell, gap, top = 132, 26, 24
    width = len(names) * cell + (len(names) - 1) * gap + 2 * 30
    img = QImage(width * scale, 210 * scale, QImage.Format_ARGB32)
    img.fill(padview.BOARD)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(scale, scale)
    pv = padview.PadView()
    for i, (title, sub, look, sel) in enumerate(names):
        x = 30 + i * (cell + gap)
        rect = QRectF(x + (cell - 96) / 2, top, 96, 96)
        pv.looks = {f"key{i + 1}": look}
        pv.current = f"key{i + 1}" if sel else None
        pv._paint_key(p, f"key{i + 1}", rect)
        f = QFont(QApplication.font())
        f.setPixelSize(15)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(padview.TAPE if look.pending or sel else padview.INK)
        p.drawText(QRectF(x, top + 110, cell, 22), Qt.AlignHCenter, title)
        f.setWeight(QFont.Normal)
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(padview.INK_DIM)
        p.drawText(QRectF(x, top + 134, cell, 20), Qt.AlignHCenter, sub)
    p.end()
    path = OUT / "legend.png"
    img.save(str(path))
    if Image:
        _frame(path, radius=24, pad=40)
    print("wrote", path.relative_to(ROOT))


def scene_social():
    """1280x640: GitHub's social preview, which Reddit and Discord show for links."""
    W, H = 1280, 640
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(padview.WINDOW)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # faint board-grid, like a PCB's copper pour
    p.setPen(QPen(QColor(255, 255, 255, 10), 1))
    for x in range(0, W, 32):
        p.drawLine(x, 0, x, H)
    for y in range(0, H, 32):
        p.drawLine(0, y, W, y)

    pv = padview.PadView()
    pv.resize(460, 640)
    pv.looks = {c: padview.Look(b.label(), known=True) for c, b in DEMO.items()}
    pv.looks["key12"] = padview.Look("Meta+Ctrl+Right", known=True, pending=True)
    pv.current = "key12"
    p.save()
    p.translate(70, 0)
    p.setTransform(pv._transform(), True)
    board = QRectF(pv.board)
    path = __import__("PySide6.QtGui", fromlist=["QPainterPath"]).QPainterPath()
    path.addRoundedRect(board, 18, 18)
    p.fillPath(path, padview.BOARD)
    p.setPen(QPen(padview.INK_DIM, 1.2))
    p.drawPath(path)
    for key, rect in pv.keys.items():
        pv._paint_key(p, key, rect)
    for dial, centre in pv.dials.items():
        pv._paint_dial(p, dial, centre)
    p.restore()

    x0 = 600
    f = QFont(QApplication.font())
    f.setPixelSize(92)
    f.setWeight(QFont.Bold)
    p.setFont(f)
    p.setPen(padview.INK)
    p.drawText(QRectF(x0, 150, 640, 110), Qt.AlignLeft | Qt.AlignVCenter, "MacroPad")
    p.fillRect(QRectF(x0 + 4, 268, 120, 6), padview.TAPE)
    f.setPixelSize(34)
    f.setWeight(QFont.Normal)
    p.setFont(f)
    p.drawText(QRectF(x0, 300, 640, 140), Qt.AlignLeft | Qt.TextWordWrap,
               "Set up your 12 key, 2 knob USB macro pad on Linux.")
    f.setPixelSize(26)
    p.setFont(f)
    p.setPen(padview.INK_DIM)
    p.drawText(QRectF(x0, 430, 640, 40), Qt.AlignLeft, "No Windows. No VM. No Wine.")
    mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    mono.setPixelSize(22)
    p.setFont(mono)
    p.drawText(QRectF(x0, 540, 640, 30), Qt.AlignLeft, "github.com/Probler-Yt/MacroPad")
    p.end()
    img.save(str(OUT / "social-preview.png"))
    print("wrote docs/images/social-preview.png")


def scene_icon():
    r = QSvgRenderer(str(ROOT / "packaging" / "macropad.svg"))
    img = QImage(256, 256, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, 256, 256))
    p.end()
    img.save(str(OUT / "icon.png"))
    print("wrote docs/images/icon.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    fonts(app)
    A.apply_theme(app)
    for name, fn in list(globals().items()):
        if name.startswith("scene_"):
            fn()
