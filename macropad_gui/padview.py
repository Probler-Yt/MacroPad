"""
The pad, drawn the way a circuit board is labelled: outlines and printed
legends on a dark board, each control marked with its action byte. It's the
one place the app has any personality; everything else is plain Qt.

Visual states:
    written     solid outline, legend in board ink
    unknown     hatched, like a no-go zone - we genuinely don't know
    changed     a strip of orange tape on the corner: edited, not yet written
    selected    orange outline
"""

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF,
                           QPainter, QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import core

WINDOW = QColor("#17191b")
BOARD = QColor("#202326")
INK = QColor("#d8d4cb")          # silkscreen white, slightly warm
INK_DIM = QColor("#72767b")
HATCH = QColor("#34383c")
TAPE = QColor("#eb8a2f")          # Kapton orange

# Geometry in board units; the view scales it to fit.
K, G, M = 96.0, 16.0, 30.0        # key size, gap, board margin
R_OUT, R_IN, R_KNOB = 54.0, 40.0, 30.0
ROW_H, ROW_GAP = 22.0, 12.0
DIAL_W = 160.0

# Which dial is dial 1 (action bytes 0x10-0x12). Upright, dials on top:
# assumed left. Flat (the vendor app's view): assumed top.
DIALS_IN_ORDER = ("dial1", "dial2")


@dataclass
class Look:
    text: str = ""           # what's on the pad, or will be once written
    known: bool = False
    pending: bool = False
    quiet: bool = False      # known, but does nothing: legend drawn dimmed


def _key_grid(orientation):
    """[(key_id, row, col)] with row 0 at the top."""
    out = []
    if orientation == "upright":              # 4 rows x 3 cols, dials above
        for r in range(4):
            for c in range(3):
                out.append((f"key{(3 - r) + 1 + c * 4}", r, c))
    else:                                     # 3 rows x 4 cols, dials right
        for r in range(3):
            for c in range(4):
                out.append((f"key{r * 4 + c + 1}", r, c))
    return out


class PadView(QWidget):
    selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(360, 420)
        self.orientation = "upright"
        self.looks = {c: Look() for c in core.CONTROL_IDS}
        self.current = None
        self.hover = None
        self.mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self._layout()

    # ------------------------------------------------------------ public

    def set_orientation(self, orientation):
        self.orientation = orientation
        self._layout()
        self.update()

    def set_looks(self, looks):
        self.looks = looks
        self.update()

    def select(self, control):
        self.current = control
        self.update()

    # ------------------------------------------------------------ layout

    def _layout(self):
        self.keys = {}              # key -> QRectF
        self.dials = {}             # dial -> centre QPointF
        self.rows = {}              # dial action -> legend row QRectF

        if self.orientation == "upright":
            dial_centres = [QPointF(M + DIAL_W / 2, M + R_OUT),
                            QPointF(M + DIAL_W * 1.5, M + R_OUT)]
            grid_top = M + 2 * R_OUT + ROW_GAP + 3 * ROW_H + 28
            grid_left = M
        else:
            grid_top, grid_left = M, M
            x = M + 4 * K + 3 * G + 30 + DIAL_W / 2
            first = M + R_OUT
            second = first + R_OUT + ROW_GAP + 3 * ROW_H + 20 + R_OUT
            dial_centres = [QPointF(x, first), QPointF(x, second)]

        for key, r, c in _key_grid(self.orientation):
            self.keys[key] = QRectF(grid_left + c * (K + G),
                                    grid_top + r * (K + G), K, K)

        for dial, centre in zip(DIALS_IN_ORDER, dial_centres):
            self.dials[dial] = centre
            top = centre.y() + R_OUT + ROW_GAP
            for i, act in enumerate(core.DIAL_ACTIONS):
                self.rows[f"{dial}-{act}"] = QRectF(
                    centre.x() - DIAL_W / 2 + 6, top + i * ROW_H,
                    DIAL_W - 12, ROW_H)

        right = max([r.right() for r in self.keys.values()] +
                    [r.right() for r in self.rows.values()])
        bottom = max([r.bottom() for r in self.keys.values()] +
                     [r.bottom() for r in self.rows.values()])
        self.board = QRectF(0, 0, right + M, bottom + 46)

    def _transform(self):
        pad = 24
        avail = QRectF(self.rect()).adjusted(pad, pad, -pad, -pad)
        s = min(avail.width() / self.board.width(),
                avail.height() / self.board.height())
        s = max(0.4, min(s, 1.6))
        dx = avail.left() + (avail.width() - self.board.width() * s) / 2
        dy = avail.top() + (avail.height() - self.board.height() * s) / 2
        return QTransform(s, 0, 0, s, dx, dy)

    # ----------------------------------------------------------- hit test

    def _segment(self, dial, act):
        """Clickable area for one dial action, in board units."""
        c = self.dials[dial]
        path = QPainterPath()
        if act == "push":
            path.addEllipse(c, R_IN - 2, R_IN - 2)
            return path
        start, span = (100, 160) if act == "left" else (-80, 160)
        outer = QRectF(c.x() - R_OUT, c.y() - R_OUT, 2 * R_OUT, 2 * R_OUT)
        inner = QRectF(c.x() - R_IN, c.y() - R_IN, 2 * R_IN, 2 * R_IN)
        path.arcMoveTo(outer, start)
        path.arcTo(outer, start, span)
        path.arcTo(inner, start + span, -span)
        path.closeSubpath()
        return path

    def _hit(self, pos):
        inv, ok = self._transform().inverted()
        if not ok:
            return None
        p = inv.map(QPointF(pos))
        for key, rect in self.keys.items():
            if rect.contains(p):
                return key
        for control, rect in self.rows.items():
            if rect.contains(p):
                return control
        for dial in self.dials:
            for act in core.DIAL_ACTIONS:
                if self._segment(dial, act).contains(p):
                    return f"{dial}-{act}"
        return None

    def mouseMoveEvent(self, e):
        h = self._hit(e.position())
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mousePressEvent(self, e):
        h = self._hit(e.position())
        if h and e.button() == Qt.LeftButton:
            self.current = h
            self.selected.emit(h)
            self.update()

    # ------------------------------------------------------------ paint

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), WINDOW)
        p.setTransform(self._transform())

        board = QPainterPath()
        board.addRoundedRect(self.board, 18, 18)
        p.fillPath(board, BOARD)
        p.setPen(QPen(INK_DIM, 1.2))
        p.drawPath(board)

        for key, rect in self.keys.items():
            self._paint_key(p, key, rect)
        for dial, centre in self.dials.items():
            self._paint_dial(p, dial, centre)

        f = QFont(self.mono)
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(INK_DIM)
        p.drawText(QRectF(M, self.board.bottom() - 34, 200, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, core.DEVICE_ID)

    def _outline_pen(self, control, known):
        if control == self.current:
            return QPen(TAPE, 2.2)
        if control == self.hover:
            return QPen(INK, 1.6)
        return QPen(INK if known else INK_DIM, 1.1)

    def _hatch(self, p, path, rect, step=9.0):
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(HATCH, 1.4))
        x = rect.left() - rect.height()
        while x < rect.right():
            p.drawLine(QPointF(x, rect.bottom()),
                       QPointF(x + rect.height(), rect.top()))
            x += step
        p.restore()

    def _tape(self, p, at, length=30.0, width=9.0):
        """A strip of Kapton stuck across a corner."""
        p.save()
        p.translate(at)
        p.rotate(45)
        tape = QColor(TAPE)
        tape.setAlpha(225)
        p.setPen(Qt.NoPen)
        p.setBrush(tape)
        p.drawRect(QRectF(-length / 2, -width / 2, length, width))
        p.restore()

    def _paint_key(self, p, key, rect):
        look = self.looks.get(key, Look())
        path = QPainterPath()
        path.addRoundedRect(rect, 5, 5)
        if not look.known and not look.pending:
            self._hatch(p, path, rect)
        p.setBrush(Qt.NoBrush)
        p.setPen(self._outline_pen(key, look.known or look.pending))
        p.drawPath(path)

        f = QFont(self.mono)
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(INK_DIM)
        p.drawText(rect.adjusted(8, 6, -8, -6), Qt.AlignLeft | Qt.AlignTop,
                   f"0x{core.action_byte(key):02x}")

        f = QFont(self.font())
        f.setPixelSize(13)
        p.setFont(f)
        if look.pending:
            p.setPen(TAPE)
        else:
            p.setPen(INK if look.known and not look.quiet else INK_DIM)
        text = look.text if (look.known or look.pending) else "Unknown"
        body = rect.adjusted(8, 22, -8, -10)
        # Let 'Meta+Ctrl+Left' break after a '+' instead of being cut off.
        text = text.replace("+", "+\u200b")
        p.drawText(body, Qt.AlignCenter | Qt.TextWordWrap,
                   self._fit(text, f, body))

        if look.pending:
            self._tape(p, QPointF(rect.right() - 7, rect.top() + 7))

    def _paint_turn_icon(self, p, centre, clockwise, colour):
        r = 5.5
        box = QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r)
        pen = QPen(colour, 1.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        start, span = (120, 270) if not clockwise else (60, -270)
        p.drawArc(box, int(start * 16), int(span * 16))
        end = math.radians(start + span)
        tip = QPointF(centre.x() + r * math.cos(end), centre.y() - r * math.sin(end))
        # arrowhead along the tangent at the end of the arc
        d = 1 if not clockwise else -1
        tangent = end + d * math.pi / 2
        for spread in (0.5, -0.5):
            a = tangent + math.pi + spread
            p.drawLine(tip, QPointF(tip.x() + 3.5 * math.cos(a),
                                    tip.y() - 3.5 * math.sin(a)))

    def _paint_dial(self, p, dial, c):
        # Ring segments for turn left / turn right, centre for press.
        for act in core.DIAL_ACTIONS:
            control = f"{dial}-{act}"
            look = self.looks.get(control, Look())
            seg = self._segment(dial, act)
            if act != "push":
                if not look.known and not look.pending:
                    self._hatch(p, seg, seg.boundingRect(), 7.0)
                if control == self.current:
                    fill = QColor(TAPE)
                    fill.setAlpha(55)
                    p.fillPath(seg, fill)
                p.setPen(self._outline_pen(control, look.known or look.pending))
                p.setBrush(Qt.NoBrush)
                p.drawPath(seg)

        push = f"{dial}-push"
        look = self.looks.get(push, Look())
        knob = QPainterPath()
        knob.addEllipse(c, R_KNOB, R_KNOB)
        if not look.known and not look.pending:
            self._hatch(p, knob, knob.boundingRect(), 7.0)
        if push == self.current:
            fill = QColor(TAPE)
            fill.setAlpha(55)
            p.fillPath(knob, fill)
        p.setPen(self._outline_pen(push, look.known or look.pending))
        p.setBrush(Qt.NoBrush)
        p.drawPath(knob)
        # knurling, the way a footprint marks a rotary encoder
        p.setPen(QPen(INK_DIM, 1.0))
        for i in range(24):
            a = math.radians(i * 15)
            p.drawLine(QPointF(c.x() + (R_KNOB - 4) * math.cos(a), c.y() + (R_KNOB - 4) * math.sin(a)),
                       QPointF(c.x() + (R_KNOB - 1) * math.cos(a), c.y() + (R_KNOB - 1) * math.sin(a)))

        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        label = QRectF(c.x() - 21, c.y() - 8, 42, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(BOARD)
        p.drawRoundedRect(label, 3, 3)
        p.setPen(TAPE if push == self.current else INK_DIM)
        p.drawText(label, Qt.AlignCenter, f"Dial {dial[4:]}")

        # Legend rows: the silkscreen text next to the part.
        for act in core.DIAL_ACTIONS:
            control = f"{dial}-{act}"
            self._paint_row(p, control, act, self.rows[control])

    def _paint_row(self, p, control, act, rect):
        look = self.looks.get(control, Look())
        is_cur = control == self.current
        if is_cur or control == self.hover:
            bg = QColor(TAPE if is_cur else INK)
            bg.setAlpha(40 if is_cur else 18)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(rect, 3, 3)

        colour = TAPE if (look.pending or is_cur) else (INK if look.known else INK_DIM)
        icon_c = QPointF(rect.left() + 10, rect.center().y())
        if act == "push":
            p.setPen(Qt.NoPen)
            p.setBrush(colour)
            p.drawEllipse(icon_c, 2.6, 2.6)
        else:
            self._paint_turn_icon(p, icon_c, act == "right", colour)

        mono = QFont(self.mono)
        mono.setPixelSize(10)
        p.setFont(mono)
        p.setPen(INK_DIM)
        p.drawText(rect.adjusted(0, 0, -4, 0), Qt.AlignRight | Qt.AlignVCenter,
                   f"{core.action_byte(control):02x}")

        f = QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(TAPE if look.pending else
                 (INK if look.known and not look.quiet else INK_DIM))
        text = look.text if (look.known or look.pending) else "Unknown"
        body = rect.adjusted(22, 0, -26, 0)
        p.drawText(body, Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(f).elidedText(text, Qt.ElideRight, body.width()))
        if look.pending:
            self._tape(p, QPointF(rect.left() - 4, rect.top() + 3), 14, 6)

    @staticmethod
    def _fit(text, font, rect):
        """Elide to at most two wrapped lines."""
        fm = QFontMetricsF(font)
        if fm.boundingRect(rect, Qt.TextWordWrap, text).height() <= rect.height():
            return text
        while text and fm.boundingRect(rect, Qt.TextWordWrap, text + "…").height() > rect.height():
            text = text[:-1]
        return text + "…"
