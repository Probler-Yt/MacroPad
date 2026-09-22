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
import random
import zlib
from dataclasses import dataclass

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF,
                           QPainter, QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import core, themes

THEME = themes.get(themes.DEFAULT)

# The colours everything draws with. Switching theme changes these objects in
# place, so every widget and module holding one follows along.
WINDOW = QColor(THEME.window)
BOARD = QColor(THEME.board)
INK = QColor(THEME.ink)           # silkscreen white, slightly warm
INK_DIM = QColor(THEME.ink_dim)
HATCH = QColor(THEME.hatch)
TAPE = QColor(THEME.accent)       # Kapton orange, in the default theme


def apply_theme(theme):
    global THEME
    THEME = theme
    for colour, value in ((WINDOW, theme.window), (BOARD, theme.board),
                          (INK, theme.ink), (INK_DIM, theme.ink_dim),
                          (HATCH, theme.hatch), (TAPE, theme.accent)):
        colour.setRgba(QColor(value).rgba())


_FAMILIES = None


def _families():
    global _FAMILIES
    if _FAMILIES is None:
        _FAMILIES = set(QFontDatabase.families())
    return _FAMILIES


def _seed(tag):
    """A stable number for a name. Python's own hash() changes every run."""
    return zlib.crc32(tag.encode())


def _wobble_path(path, amount, seed, step=4.0):
    """
    A pencil version of a path. Resampled finely, then nudged sideways by
    two slow waves with random phases, so the line wanders without jitter.
    Seeded, so a key looks the same every time it's drawn.
    """
    rnd = random.Random(seed)
    out = QPainterPath()
    for poly in path.toSubpathPolygons():
        pts = list(poly)
        if len(pts) < 2:
            continue
        dense = []
        for a, b in zip(pts, pts[1:]):
            seg = QLineF(a, b)
            n = max(1, int(seg.length() / step))
            dense.extend(seg.pointAt(k / n) for k in range(n))
        dense.append(pts[-1])
        f1, f2 = rnd.uniform(0.035, 0.06), rnd.uniform(0.10, 0.16)
        p1, p2 = rnd.uniform(0, math.tau), rnd.uniform(0, math.tau)
        dist, moved = 0.0, []
        for i, pt in enumerate(dense):
            if i:
                dist += QLineF(dense[i - 1], pt).length()
            nxt, prv = dense[min(i + 1, len(dense) - 1)], dense[max(i - 1, 0)]
            dx, dy = nxt.x() - prv.x(), nxt.y() - prv.y()
            length = math.hypot(dx, dy) or 1.0
            off = amount * (0.65 * math.sin(dist * f1 + p1) + 0.35 * math.sin(dist * f2 + p2))
            moved.append(QPointF(pt.x() - dy / length * off, pt.y() + dx / length * off))
        out.moveTo(moved[0])
        for q in moved[1:]:
            out.lineTo(q)
    return out

# Geometry in board units; the view scales it to fit.
K, G, M = 96.0, 16.0, 30.0        # key size, gap, board margin
R_OUT, R_IN, R_KNOB = 54.0, 40.0, 30.0
ROW_H, ROW_GAP = 22.0, 12.0
DIAL_W = 160.0

@dataclass
class Look:
    text: str = ""           # what's on the pad, or will be once written
    known: bool = False
    pending: bool = False
    quiet: bool = False      # known, but does nothing: legend drawn dimmed


def _key_grid(layout, orientation):
    """
    [(key_id, row, col)] as drawn, with row 0 at the top.

    The layout describes the pad lying flat. Upright is the same pad turned
    a quarter turn anticlockwise, so a key at (r, c) in an R by C grid ends
    up at (C - 1 - c, r) in a C by R one.
    """
    flat = layout.spots()
    if orientation != "upright":
        return flat
    return [(k, layout.cols - 1 - c, r) for k, r, c in flat]


def _grid_size(layout, orientation):
    return ((layout.cols, layout.rows) if orientation == "upright"
            else (layout.rows, layout.cols))


class PadView(QWidget):
    selected = Signal(str)
    rearranged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(360, 420)
        self.orientation = "upright"
        self.layout = core.DEFAULT_LAYOUT
        self.device_id = core.DEVICE_ID
        self.looks = {c: Look() for c in self.layout.control_ids}
        self.current = None
        self.hover = None
        self.editing = False
        self.drag = None          # key being moved
        self.drag_at = None       # where the pointer is, in board units
        self.drop = None          # (row, col) it would land in
        self._wobble = 0
        self._wobble_cache = {}
        self._wobbler = QTimer(self)
        self._wobbler.setInterval(130)
        self._wobbler.timeout.connect(self._tick)
        self.mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self._layout()

    # ------------------------------------------------------------ public

    def set_orientation(self, orientation):
        self.orientation = orientation
        self._layout()
        self.update()

    def set_layout(self, layout):
        """Redraw for a pad of a different shape."""
        self.layout = layout
        if self.current not in layout.control_ids:
            self.current = None
        self._layout()
        self.update()

    def set_looks(self, looks):
        self.looks = looks
        self.update()

    def select(self, control):
        self.current = control
        self.update()

    def set_editing(self, on):
        """
        Edit mode: keys can be dragged between cells. They wobble, which is
        the clearest way anyone has found to say "these can be moved".
        """
        self.editing = on
        self.drag = self.drop = self.drag_at = None
        self._wobbler.start() if on else self._wobbler.stop()
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def _tick(self):
        self._wobble = 1 - self._wobble
        self.update()

    # ------------------------------------------------------------ layout

    def _layout(self):
        self._wobble_cache = {}
        self.keys = {}              # key -> QRectF
        self.dials = {}             # dial -> centre QPointF
        self.rows = {}              # dial action -> legend row QRectF

        lay = self.layout
        grid_rows, grid_cols = _grid_size(lay, self.orientation)
        # from a knob's centre down to the bottom of its legend rows
        stack = R_OUT + ROW_GAP + len(core.DIAL_ACTIONS) * ROW_H

        if self.orientation == "upright":
            # knobs in a row above the keys
            span = max(grid_cols * (K + G) - G, lay.knobs * (DIAL_W + G))
            step = span / max(lay.knobs, 1)
            dial_centres = [QPointF(M + step * (i + 0.5), M + R_OUT)
                            for i in range(lay.knobs)]
            grid_top = M + (R_OUT + stack + 28 if lay.knobs else 0)
            grid_left = M
        else:
            # knobs in a column down the right
            grid_top, grid_left = M, M
            x = M + grid_cols * (K + G) + 30 + DIAL_W / 2
            dial_centres = [QPointF(x, M + R_OUT + i * (stack + 20 + R_OUT))
                            for i in range(lay.knobs)]

        self.cells = {}
        for r in range(grid_rows):
            for c in range(grid_cols):
                self.cells[(r, c)] = QRectF(grid_left + c * (K + G),
                                            grid_top + r * (K + G), K, K)
        for key, r, c in _key_grid(lay, self.orientation):
            self.keys[key] = self.cells[(r, c)]

        for dial, centre in zip(lay.dial_ids, dial_centres):
            self.dials[dial] = centre
            top = centre.y() + R_OUT + ROW_GAP
            for i, act in enumerate(core.DIAL_ACTIONS):
                self.rows[f"{dial}-{act}"] = QRectF(
                    centre.x() - DIAL_W / 2 + 6, top + i * ROW_H,
                    DIAL_W - 12, ROW_H)

        boxes = list(self.cells.values()) + list(self.rows.values())
        right = max(r.right() for r in boxes)
        bottom = max(r.bottom() for r in boxes)
        self.board = QRectF(0, 0, right + M, bottom + 46)

    def _transform(self):
        pad = 24
        avail = QRectF(self.rect()).adjusted(pad, pad, -pad, -pad)
        if THEME.title_block:                  # keep clear of the title block
            avail.adjust(0, 0, 0, -92)
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

    def _board_pos(self, pos):
        inv, ok = self._transform().inverted()
        return inv.map(QPointF(pos)) if ok else None

    def _cell_at(self, point):
        for spot, rect in self.cells.items():
            if rect.adjusted(-G / 2, -G / 2, G / 2, G / 2).contains(point):
                return spot
        return None

    def mouseMoveEvent(self, e):
        if self.editing:
            if self.drag:
                self.drag_at = self._board_pos(e.position())
                self.drop = self._cell_at(self.drag_at) if self.drag_at else None
                self.update()
            else:
                over = self._hit(e.position())
                key = over if over and over.startswith("key") else None
                self.setCursor(Qt.OpenHandCursor if key else Qt.ArrowCursor)
            return
        h = self._hit(e.position())
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        h = self._hit(e.position())
        if self.editing:
            if h and h.startswith("key"):
                self.drag = h
                self.drag_at = self._board_pos(e.position())
                self.setCursor(Qt.ClosedHandCursor)
                self.update()
            return
        if h:
            self.current = h
            self.selected.emit(h)
            self.update()

    def mouseReleaseEvent(self, e):
        if not (self.editing and self.drag):
            return
        moved, target = self.drag, self.drop
        self.drag = self.drag_at = self.drop = None
        self.setCursor(Qt.OpenHandCursor)
        if target:
            self._place(moved, target)
        self.update()

    def _place(self, key, target):
        """
        Drop a key into a cell. If another key is already there the two
        swap, so a drag never loses one.
        """
        spots = {k: (r, c) for k, r, c in _key_grid(self.layout, self.orientation)}
        if spots.get(key) == target:
            return
        for other, spot in spots.items():
            if spot == target:
                spots[other] = spots[key]
                break
        spots[key] = target

        if self.orientation == "upright":       # back to the flat arrangement
            spots = {k: (c, self.layout.cols - 1 - r) for k, (r, c) in spots.items()}
        places = [spots[f"key{i + 1}"] for i in range(self.layout.keys)]
        self.layout = self.layout.rearranged(places)
        self._layout()
        self.rearranged.emit(self.layout)

    # ------------------------------------------------------------ paint

    # ------------------------------------------------------------ paint

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), WINDOW)
        if THEME.grid:
            self._paint_grid(p)
        p.setTransform(self._transform())

        board = QPainterPath()
        board.addRoundedRect(self.board, 18, 18)
        p.fillPath(board, BOARD)
        # On themes where the pad is only an outline, draw that in full ink.
        edge = INK if THEME.board == THEME.window else INK_DIM
        self._stroke(p, board, QPen(edge, 1.2), "board")

        if self.editing:
            for spot, rect in self.cells.items():
                if spot == self.drop or not any(r is rect for r in self.keys.values()):
                    path = QPainterPath()
                    path.addRoundedRect(rect, 5, 5)
                    pen = QPen(TAPE if spot == self.drop else HATCH, 1.4)
                    pen.setStyle(Qt.DashLine)
                    p.setPen(pen)
                    p.setBrush(Qt.NoBrush)
                    p.drawPath(path)

        for key, rect in self.keys.items():
            if key == self.drag:
                continue
            p.save()
            if self.editing:
                # a small alternating tilt, the way icons shake when movable
                lean = 0.55 if (_seed(key) + self._wobble) % 2 else -0.55
                p.translate(rect.center())
                p.rotate(lean)
                p.translate(-rect.center())
            self._paint_key(p, key, rect)
            p.restore()

        for dial, centre in self.dials.items():
            self._paint_dial(p, dial, centre)

        if self.drag and self.drag_at:
            rect = QRectF(0, 0, K, K)
            rect.moveCenter(self.drag_at)
            p.save()
            p.setOpacity(0.92)
            shadow = QPainterPath()
            shadow.addRoundedRect(rect.adjusted(2, 4, 2, 4), 5, 5)
            p.fillPath(shadow, QColor(0, 0, 0, 90))
            p.fillPath(shadow, QColor(0, 0, 0, 60))
            fill = QPainterPath()
            fill.addRoundedRect(rect, 5, 5)
            p.fillPath(fill, BOARD)
            self._paint_key(p, self.drag, rect)
            p.restore()

        f = QFont(self.mono)
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(INK_DIM)
        p.drawText(QRectF(M, self.board.bottom() - 34, 200, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, self.device_id)

        if THEME.title_block:
            p.resetTransform()
            self._paint_sheet(p)

    # ------------------------------------------------------ theme drawing

    def _stroke(self, p, path, pen, tag):
        """
        Draw an outline. On a pencil theme the line wanders a little and is
        gone over a second time, lighter, the way a hand draws.
        """
        p.setBrush(Qt.NoBrush)
        if not THEME.wobble:
            p.setPen(pen)
            p.drawPath(path)
            return
        p.setPen(pen)
        p.drawPath(self._wobbled(path, THEME.wobble, tag))
        again = QPen(pen)
        c = QColor(pen.color())
        c.setAlpha(int(c.alpha() * 0.45))
        again.setColor(c)
        again.setWidthF(max(0.6, pen.widthF() * 0.7))
        p.setPen(again)
        p.drawPath(self._wobbled(path, THEME.wobble * 0.8, tag + "'"))

    def _wobbled(self, path, amount, tag):
        # Paths are in board units, so they only change when the layout
        # does; cache them rather than recompute on every hover.
        b = path.boundingRect()
        key = (tag, amount, round(b.x(), 1), round(b.y(), 1),
               round(b.width(), 1), round(b.height(), 1))
        got = self._wobble_cache.get(key)
        if got is None:
            got = self._wobble_cache[key] = _wobble_path(path, amount, _seed(tag))
        return got

    def _legend_font(self, px, roomy=True):
        """roomy: there's space to spare, as on a keycap; not on a dial legend."""
        if THEME.font and THEME.font in _families():
            f = QFont(THEME.font)
            f.setPixelSize(px + (2 if roomy else 0))   # handwriting reads small
        elif THEME.mono_legends:
            f = QFont(self.mono)
            f.setPixelSize(px - 1)
        else:
            f = QFont(self.font())
            f.setPixelSize(px)
        return f

    @staticmethod
    def _lettering(text):
        return text.upper() if THEME.caps else text

    def _paint_grid(self, p):
        """Drawing paper: fine lines, and a heavier one every fifth."""
        r = self.rect()
        fine, heavy = QColor(HATCH), QColor(HATCH)
        fine.setAlpha(70)
        heavy.setAlpha(150)
        step = 20
        for i, x in enumerate(range(0, r.width(), step)):
            p.setPen(QPen(heavy if i % 5 == 0 else fine, 1))
            p.drawLine(x, 0, x, r.height())
        for i, y in enumerate(range(0, r.height(), step)):
            p.setPen(QPen(heavy if i % 5 == 0 else fine, 1))
            p.drawLine(0, y, r.width(), y)

    def _paint_sheet(self, p):
        """A drawing border and a title block in the bottom right corner."""
        import getpass
        sheet = QRectF(self.rect()).adjusted(8, 8, -9, -9)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(INK, 1.4))
        p.drawRect(sheet)

        w, h = min(330.0, sheet.width() - 20), 84.0
        tb = QRectF(sheet.right() - w, sheet.bottom() - h, w, h)
        p.fillRect(tb, WINDOW)
        p.setPen(QPen(INK, 1.2))
        p.drawRect(tb)
        row = h / 3
        half = tb.left() + w / 2
        p.setPen(QPen(INK, 0.8))
        p.drawLine(QPointF(tb.left(), tb.top() + row), QPointF(tb.right(), tb.top() + row))
        p.drawLine(QPointF(tb.left(), tb.top() + 2 * row), QPointF(tb.right(), tb.top() + 2 * row))
        p.drawLine(QPointF(half, tb.top() + row), QPointF(half, tb.bottom()))

        try:
            who = getpass.getuser()
        except Exception:
            who = ""
        lay = self.layout
        knobs = f", {lay.knobs} KNOB{'S' if lay.knobs != 1 else ''}" if lay.knobs else ""
        cells = [
            (QRectF(tb.left(), tb.top(), w, row), "TITLE", "MACROPAD"),
            (QRectF(tb.left(), tb.top() + row, w / 2, row), "DWG NO", self.device_id),
            (QRectF(half, tb.top() + row, w / 2, row), "SCALE", "1:1   SHEET 1 OF 1"),
            (QRectF(tb.left(), tb.top() + 2 * row, w / 2, row), "DRAWN", who.upper()),
            (QRectF(half, tb.top() + 2 * row, w / 2, row), "LAYOUT",
             f"{lay.rows}X{lay.cols}{knobs}"),
        ]
        small, big = QFont(self.mono), QFont(self.mono)
        small.setPixelSize(7)
        big.setPixelSize(11)
        big.setBold(True)
        for rect, label, value in cells:
            p.setFont(small)
            p.setPen(INK_DIM)
            p.drawText(rect.adjusted(5, 2, -4, 0), Qt.AlignLeft | Qt.AlignTop, label)
            p.setFont(big)
            p.setPen(INK)
            p.drawText(rect.adjusted(5, 0, -4, -3), Qt.AlignLeft | Qt.AlignBottom,
                       QFontMetricsF(big).elidedText(value, Qt.ElideRight,
                                                     rect.width() - 10))

    def _centre_lines(self, p, c):
        """Chain lines through a round feature's centre: long dash, short dash."""
        pen = QPen(INK_DIM, 0.8)
        pen.setDashPattern([14, 3, 3, 3])
        p.setPen(pen)
        reach = R_OUT + 10
        p.drawLine(QPointF(c.x() - reach, c.y()), QPointF(c.x() + reach, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - reach), QPointF(c.x(), c.y() + reach))

    # ------------------------------------------------------------ pieces

    def _outline_pen(self, control, known):
        if control == self.current:
            return QPen(TAPE, 2.2)
        if control == self.hover:
            return QPen(INK, 1.6)
        pen = QPen(INK if known else INK_DIM, 1.1)
        if not known and THEME.unknown == "hidden":
            pen.setDashPattern([5, 3])          # hidden line, drawing convention
        return pen

    def _unknown_fill(self, p, path, rect, tag, step=9.0):
        if THEME.unknown == "hatch":
            self._hatch(p, path, rect, tag, step)

    def _hatch(self, p, path, rect, tag="", step=9.0):
        p.save()
        p.setClipPath(path)
        pen = QPen(HATCH, 1.4)
        x = rect.left() - rect.height()
        i = 0
        while x < rect.right():
            a = QPointF(x, rect.bottom())
            b = QPointF(x + rect.height(), rect.top())
            if THEME.wobble:
                line = QPainterPath(a)
                line.lineTo(b)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawPath(self._wobbled(line, THEME.wobble * 0.6, f"{tag}h{i}"))
            else:
                p.setPen(pen)
                p.drawLine(a, b)
            x += step
            i += 1
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
        unknown = not look.known and not look.pending
        if unknown:
            self._unknown_fill(p, path, rect, key)
        self._stroke(p, path, self._outline_pen(key, look.known or look.pending), key)

        f = QFont(self.mono)
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(INK_DIM)
        p.drawText(rect.adjusted(8, 6, -8, -6), Qt.AlignLeft | Qt.AlignTop,
                   f"0x{core.action_byte(key):02x}")

        f = self._legend_font(13)
        p.setFont(f)
        if look.pending:
            p.setPen(TAPE)
        else:
            p.setPen(INK if look.known and not look.quiet else INK_DIM)
        text = self._lettering(look.text if (look.known or look.pending) else "Unknown")
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
        if THEME.centre_lines:
            self._centre_lines(p, c)
        # Ring segments for turn left / turn right, centre for press.
        for act in core.DIAL_ACTIONS:
            control = f"{dial}-{act}"
            look = self.looks.get(control, Look())
            seg = self._segment(dial, act)
            if act != "push":
                if not look.known and not look.pending:
                    self._unknown_fill(p, seg, seg.boundingRect(), control, 7.0)
                if control == self.current:
                    fill = QColor(TAPE)
                    fill.setAlpha(55)
                    p.fillPath(seg, fill)
                self._stroke(p, seg, self._outline_pen(control, look.known or look.pending),
                             control)

        push = f"{dial}-push"
        look = self.looks.get(push, Look())
        knob = QPainterPath()
        knob.addEllipse(c, R_KNOB, R_KNOB)
        if not look.known and not look.pending:
            self._unknown_fill(p, knob, knob.boundingRect(), push, 7.0)
        if push == self.current:
            fill = QColor(TAPE)
            fill.setAlpha(55)
            p.fillPath(knob, fill)
        self._stroke(p, knob, self._outline_pen(push, look.known or look.pending), push)
        # knurling, the way a footprint marks a rotary encoder
        if not THEME.wobble:
            p.setPen(QPen(INK_DIM, 1.0))
            for i in range(24):
                a = math.radians(i * 15)
                p.drawLine(QPointF(c.x() + (R_KNOB - 4) * math.cos(a), c.y() + (R_KNOB - 4) * math.sin(a)),
                           QPointF(c.x() + (R_KNOB - 1) * math.cos(a), c.y() + (R_KNOB - 1) * math.sin(a)))

        f = self._legend_font(11)
        p.setFont(f)
        label = QRectF(c.x() - 23, c.y() - 8, 46, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(BOARD)
        p.drawRoundedRect(label, 3, 3)
        p.setPen(TAPE if push == self.current else INK_DIM)
        p.drawText(label, Qt.AlignCenter, self._lettering(f"Dial {dial[4:]}"))

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

        f = self._legend_font(12, roomy=False)
        p.setFont(f)
        p.setPen(TAPE if look.pending else
                 (INK if look.known and not look.quiet else INK_DIM))
        text = self._lettering(look.text if (look.known or look.pending) else "Unknown")
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
