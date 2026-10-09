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
from PySide6.QtGui import (QRadialGradient, QBrush, QColor, QFont, QFontDatabase, QFontMetricsF,
                           QPainter, QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import core, themes
from .vectors import KNOB_ARC

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
    global THEME, K, G, GY, M, R_OUT, R_IN, R_KNOB, ROW_H, ROW_GAP, ROW_SEP
    global DIAL_W, KEY_R, LEGEND_R, RULE_X, LABEL_X
    global BYTE_PT, LABEL_PT, ROW_PT, ROW_BYTE_PT, BADGE
    THEME = theme
    for colour, value in ((WINDOW, theme.window), (BOARD, theme.board),
                          (INK, theme.ink), (INK_DIM, theme.ink_dim),
                          (HATCH, theme.hatch), (TAPE, theme.accent)):
        colour.setRgba(QColor(value).rgba())
    m = theme.metrics
    K, G, GY, M = m.key, m.gap_x, m.gap_y, m.margin
    R_OUT, R_IN, R_KNOB = m.r_out, m.r_in, m.r_knob
    ROW_H, ROW_GAP, ROW_SEP = m.row_h, m.row_gap, m.row_sep
    DIAL_W = m.dial_w
    KEY_R, LEGEND_R = m.key_radius, m.legend_radius
    RULE_X, LABEL_X = m.rule_x, m.label_x
    BYTE_PT, LABEL_PT = m.byte_pt, m.label_pt
    ROW_PT, ROW_BYTE_PT = m.row_pt, m.row_byte_pt
    BADGE = m.badge


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

# Geometry in board units; the view scales it to fit. These come from the
# active theme's Metrics and are refreshed by apply_theme, so a theme can
# change the pad's proportions and not just its colours.
_M = themes.Metrics()
K, G, GY, M = _M.key, _M.gap_x, _M.gap_y, _M.margin
R_OUT, R_IN, R_KNOB = _M.r_out, _M.r_in, _M.r_knob
ROW_H, ROW_GAP, ROW_SEP = _M.row_h, _M.row_gap, _M.row_sep
DIAL_W = _M.dial_w
KEY_R, LEGEND_R = _M.key_radius, _M.legend_radius
RULE_X, LABEL_X = _M.rule_x, _M.label_x
BYTE_PT, LABEL_PT, ROW_PT, ROW_BYTE_PT = (_M.byte_pt, _M.label_pt,
                                          _M.row_pt, _M.row_byte_pt)
BADGE = _M.badge

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
        self._sys_mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self._layout()

    @property
    def mono(self):
        """The theme's monospace face when it ships one, else the system's."""
        if THEME.mono_font and THEME.mono_font in _families():
            return QFont(THEME.mono_font)
        return self._sys_mono

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

        # Sides are named for the pad lying flat, so standing it upright
        # turns them: knobs on the right go along the top, knobs on the
        # left along the bottom.
        left = lay.knob_side == "left" and bool(lay.knobs)

        keys_w = grid_cols * (K + G) - G
        keys_h = grid_rows * (K + GY) - GY
        # how much room the knobs need across, and down from the first centre
        dials_w = lay.knobs * (DIAL_W + G) - G if lay.knobs else 0
        dials_h = (R_OUT + (lay.knobs - 1) * (stack + 20 + R_OUT) + stack
                   if lay.knobs else 0)

        if self.orientation == "upright":
            # knobs in a row, above the keys or below them. Whichever of the
            # two is narrower gets centred against the other.
            span = max(keys_w, dials_w)
            step = span / max(lay.knobs, 1)
            band = (R_OUT + stack + 28) if lay.knobs else 0
            grid_left = M + max(0.0, (span - keys_w) / 2)
            grid_top = M if left else M + band
            keys_bottom = grid_top + keys_h
            y = (keys_bottom + 28 + R_OUT) if left else (M + R_OUT)
            dial_centres = [QPointF(M + step * (i + 0.5), y)
                            for i in range(lay.knobs)]
        else:
            # knobs in a column, down whichever side the pad wears them.
            # Three knobs of legends are taller than three rows of keys, so
            # centre the shorter one rather than leaving a hole in the board.
            grid_top = M + max(0.0, (dials_h - keys_h) / 2)
            grid_left = M + (DIAL_W + 30 if left else 0)
            x = (M + DIAL_W / 2) if left else \
                (grid_left + keys_w + G + 30 + DIAL_W / 2)
            top = M + max(0.0, (keys_h - dials_h) / 2)
            dial_centres = [QPointF(x, top + R_OUT + i * (stack + 20 + R_OUT))
                            for i in range(lay.knobs)]

        self.cells = {}
        for r in range(grid_rows):
            for c in range(grid_cols):
                self.cells[(r, c)] = QRectF(grid_left + c * (K + G),
                                            grid_top + r * (K + GY), K, K)
        for key, r, c in _key_grid(lay, self.orientation):
            self.keys[key] = self.cells[(r, c)]

        for dial, centre in zip(lay.dial_ids, dial_centres):
            self.dials[dial] = centre
            top = centre.y() + R_OUT + ROW_GAP
            # With a boxed legend the rows butt together and the block's own
            # colour shows through as a hairline; otherwise they are loose.
            inset = 0.0 if THEME.legend_box else 6.0
            step = ROW_H + ROW_SEP
            for i, act in enumerate(core.DIAL_ACTIONS):
                self.rows[f"{dial}-{act}"] = QRectF(
                    centre.x() - DIAL_W / 2 + inset, top + i * step,
                    DIAL_W - 2 * inset, ROW_H)

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

    def _arc_outline(self, c, mirror):
        """
        One turn arc, built from the outline in the Figma file. Coordinates
        there are multiples of the knob's radius measured from its centre,
        so this works at any size. The clockwise arc is the same shape
        mirrored.
        """
        path = QPainterPath()
        s = R_KNOB
        for op, a in KNOB_ARC:
            pts = [QPointF(c.x() + (-a[i] if mirror else a[i]) * s,
                           c.y() + a[i + 1] * s)
                   for i in range(0, len(a), 2)]
            if op == "M":
                path.moveTo(pts[0])
            elif op == "L":
                path.lineTo(pts[0])
            elif op == "Q":
                path.quadTo(pts[0], pts[1])
            elif op == "C":
                path.cubicTo(pts[0], pts[1], pts[2])
            else:
                path.closeSubpath()
        return path

    def _segment(self, dial, act):
        """Clickable area for one dial action, in board units."""
        c = self.dials[dial]
        path = QPainterPath()
        if act == "push":
            path.addEllipse(c, (R_KNOB if THEME.knob_art else R_IN) - 2,
                            (R_KNOB if THEME.knob_art else R_IN) - 2)
            return path
        if THEME.knob_art == "figma":
            return self._arc_outline(c, mirror=(act == "right"))
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
            # handwriting reads small; a normal face does not need the bump
            f.setPixelSize(px + (2 if roomy and THEME.wobble else 0))
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

    # ----------------------------------------------------------- surfaces

    @staticmethod
    def _well_brush(rect, radial=False):
        """
        The tile and knob gradients. Figma calls the tile one a diamond
        gradient, which Qt has no equivalent for; a radial one reaching the
        corners is the closest honest approximation and reads the same, as a
        face that darkens towards its edge.
        """
        if not THEME.well:
            return QColor(THEME.board)
        c = rect.center()
        r = (math.hypot(rect.width(), rect.height()) / 2 if not radial
             else max(rect.width(), rect.height()) / 2)
        g = QRadialGradient(c, r)
        g.setColorAt(0.0, QColor(THEME.board))
        g.setColorAt(0.75 if radial else 0.745, QColor(THEME.board))
        g.setColorAt(0.9 if radial else 1.0, QColor(THEME.well))
        g.setColorAt(1.0, QColor(THEME.well))
        return QBrush(g)

    def _glow(self, p, path, colour):
        """
        A few wide, nearly transparent passes under a stroke, so an accent
        edge blooms the way it does in the design.
        """
        if not THEME.glow:
            return
        p.save()
        p.setBrush(Qt.NoBrush)
        for i, (width, alpha) in enumerate(((THEME.glow, 26),
                                            (THEME.glow * 0.6, 44),
                                            (THEME.glow * 0.3, 70))):
            c = QColor(colour)
            c.setAlpha(alpha)
            pen = QPen(c, width)
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawPath(path)
        p.restore()

    def _tape(self, p, at, length=30.0, width=9.0):
        """
        The marker for an edit that hasn't reached the pad yet. Normally a
        strip of Kapton across the corner; "star" themes stamp a little
        asterisk tile instead.
        """
        if THEME.badge == "star":
            self._star_badge(p, at, max(11.0, width * 1.6))
            return
        p.save()
        p.translate(at)
        p.rotate(45)
        tape = QColor(TAPE)
        tape.setAlpha(225)
        p.setPen(Qt.NoPen)
        p.setBrush(tape)
        p.drawRect(QRectF(-length / 2, -width / 2, length, width))
        p.restore()

    @staticmethod
    def _corner_path(rect, tl=0.0, tr=0.0, br=0.0, bl=0.0):
        """
        A rectangle with each corner rounded by its own radius, built as one
        continuous outline. Adding overlapping sub-paths instead would cancel
        under the odd-even fill rule and leave the shape hollow.
        """
        path = QPainterPath()
        path.moveTo(rect.left() + tl, rect.top())
        path.lineTo(rect.right() - tr, rect.top())
        if tr:
            path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + tr)
        path.lineTo(rect.right(), rect.bottom() - br)
        if br:
            path.quadTo(rect.right(), rect.bottom(),
                        rect.right() - br, rect.bottom())
        path.lineTo(rect.left() + bl, rect.bottom())
        if bl:
            path.quadTo(rect.left(), rect.bottom(), rect.left(), rect.bottom() - bl)
        path.lineTo(rect.left(), rect.top() + tl)
        if tl:
            path.quadTo(rect.left(), rect.top(), rect.left() + tl, rect.top())
        path.closeSubpath()
        return path

    def _star_badge(self, p, at, size=16.0):
        """A filled tile with an asterisk on it, for a knob row."""
        box = QRectF(at.x() - size / 2, at.y() - size / 2, size, size)
        self._flag_shape(p, box, THEME.badge_glyph or "*", QColor(TAPE),
                         QColor(THEME.accent_text), glow=True)

    def _corner_flag(self, p, rect, glyph, fill, pen_colour, italic=False, glow=False):
        """
        The tab tucked into a key's top right corner: square, with only the
        inner corner rounded, so it reads as folded into the tile.
        """
        size = min(BADGE, rect.width() * 0.26)
        posOffset = 0.05 # Slightly offset from the edges for DPI scaling edge cases, despite now being drawn between the button background and border. -Oaken
        box = QRectF(rect.right() - size - KEY_R * posOffset,
                     rect.top() + KEY_R * posOffset, size, size)
        self._flag_shape(p, box, glyph, fill, pen_colour, italic, glow)

    def _flag_shape(self, p, box, glyph, fill, pen_colour, italic=False, glow=False):
        p.save()
        r = box.width() * 0.28
        # square against the tile's own corner, rounded on the two inner ones
        path = self._corner_path(box, tl=0.0, tr=r, br=0.0, bl=r) # I screwed up the figma file, the rect is rotated -90deg. Opposite corners need to be rounded. -Oaken
        if glow:
            self._glow(p, path, TAPE)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawPath(path)
        f = QFont(self.mono)
        f.setPixelSize(max(7, round(box.height() * 0.95)))
        f.setWeight(QFont.ExtraBold)
        f.setItalic(italic)
        p.setFont(f)
        p.setPen(pen_colour)
        # the asterisk sits high in the em box; nudge it back to the middle
        #lift = box.height() * (0.18 if glyph == "*" else 0.0) # This is no longer needed after fixing the corner rounding. -Oaken
        p.drawText(box.translated(0, 0), Qt.AlignCenter, glyph)
        p.restore()

    def _paint_key(self, p, key, rect):
        look = self.looks.get(key, Look())
        path = QPainterPath()
        path.addRoundedRect(rect, KEY_R, KEY_R)
        unknown = not look.known and not look.pending
        if THEME.well:
            p.fillPath(path, self._well_brush(rect))
        if unknown:
            self._unknown_fill(p, path, rect, key)
        if key == self.current: # if key == self.current or look.pending:
            self._glow(p, path, TAPE)
        if look.pending: # Move this so it draws after the button background, but before the border is drawn. -Oaken
            if THEME.badge == "star":
                self._corner_flag(p, rect, THEME.badge_glyph or "*",
                                  QColor(TAPE), QColor(THEME.accent_text), glow=True)
            else:
                self._tape(p, QPointF(rect.right() - 7, rect.top() + 7))
        elif unknown and THEME.query_badge:
            self._corner_flag(p, rect, "?", QColor(THEME.hatch), INK_DIM,
                              italic=True)
        self._stroke(p, path, self._outline_pen(key, look.known or look.pending), key)

        f = QFont(self.mono)
        f.setPixelSize(max(6, round(BYTE_PT)))
        if THEME.mono_font:
            f.setItalic(True)
            f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(QColor(THEME.byte_ink) if THEME.byte_ink else INK_DIM)
        bi = THEME.metrics.byte_inset
        byte_at = (rect.adjusted(bi[0] * rect.width(), bi[1] * rect.height(),
                                 -bi[0] * rect.width(), 0)
                   if bi else rect.adjusted(6, 4, -6, -4))
        p.drawText(byte_at, Qt.AlignLeft | Qt.AlignTop,
                   f"0x{core.action_byte(key):02x}")

        f = self._legend_font(round(LABEL_PT))
        p.setFont(f)
        if key == self.current:
            p.setPen(TAPE)
        elif look.pending:
            # the design marks a change with the corner flag, not the words
            p.setPen(TAPE if not THEME.badge_glyph else INK)
        else:
            p.setPen(INK if look.known and not look.quiet else INK_DIM)
        text = self._lettering(look.text if (look.known or look.pending) else "Unknown")
        lb = THEME.metrics.label_band
        body = (QRectF(rect.left() + lb[0] * rect.width(),
                       rect.top() + lb[1] * rect.height(),
                       rect.width() * (1 - lb[0] - lb[2]),
                       rect.height() * (1 - lb[1] - lb[3]))
                if lb else rect.adjusted(8, 22, -8, -10))
        # Let 'Meta+Ctrl+Left' break after a '+' instead of being cut off.
        text = text.replace("+", "+\u200b")
        p.drawText(body, Qt.AlignCenter | Qt.TextWordWrap,
                   self._fit(text, f, body))

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
                if THEME.well:
                    p.fillPath(seg, self._well_brush(seg.boundingRect()))
                if not look.known and not look.pending:
                    self._unknown_fill(p, seg, seg.boundingRect(), control, 7.0)
                if control == self.current:
                    fill = QColor(TAPE)
                    fill.setAlpha(55 if not THEME.glow else 28)
                    p.fillPath(seg, fill)
                if control == self.current or look.pending:
                    self._glow(p, seg, TAPE)
                self._stroke(p, seg, self._outline_pen(control, look.known or look.pending),
                             control)

        push = f"{dial}-push"
        look = self.looks.get(push, Look())
        knob = QPainterPath()
        knob.addEllipse(c, R_KNOB, R_KNOB)
        if THEME.well:
            p.fillPath(knob, self._well_brush(knob.boundingRect(), radial=True))
        if not look.known and not look.pending:
            self._unknown_fill(p, knob, knob.boundingRect(), push, 7.0)
        if push == self.current:
            fill = QColor(TAPE)
            fill.setAlpha(55 if not THEME.glow else 28)
            p.fillPath(knob, fill)
        if push == self.current or look.pending:
            self._glow(p, knob, TAPE)
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
        if THEME.legend_box:
            self._paint_legend_box(p, dial)
        for act in core.DIAL_ACTIONS:
            control = f"{dial}-{act}"
            self._paint_row(p, control, act, self.rows[control])

    def _paint_legend_box(self, p, dial):
        """
        One knob's three rows sit on a plate the colour of the dividing
        lines. The rows are painted over it leaving the plate showing as a
        hairline between them, which is how the design draws its separators.
        """
        boxes = [self.rows[f"{dial}-{a}"] for a in core.DIAL_ACTIONS]
        rect = boxes[0].united(boxes[-1]).adjusted(-ROW_SEP, -ROW_SEP,
                                                   ROW_SEP, ROW_SEP)
        p.setPen(QPen(QColor(THEME.line), ROW_SEP))
        p.setBrush(QColor(THEME.line))
        p.drawRoundedRect(rect, LEGEND_R, LEGEND_R)

    def _paint_row(self, p, control, act, rect):
        look = self.looks.get(control, Look())
        is_cur = control == self.current
        if THEME.legend_box:
            self._paint_boxed_row(p, control, act, rect, look, is_cur)
            return
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
        prefix = "0x" if THEME.byte_prefix else ""
        p.drawText(rect.adjusted(0, 0, -4, 0), Qt.AlignRight | Qt.AlignVCenter,
                   f"{prefix}{core.action_byte(control):02x}")

        f = self._legend_font(12, roomy=False)
        p.setFont(f)
        p.setPen(TAPE if look.pending else
                 (INK if look.known and not look.quiet else INK_DIM))
        text = self._lettering(look.text if (look.known or look.pending) else "Unknown")
        # the action byte sits on the right; leave room for the wider 0x form
        body = rect.adjusted(22, 0, -36 if THEME.byte_prefix else -26, 0)
        p.drawText(body, Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(f).elidedText(text, Qt.ElideRight, body.width()))
        if look.pending:
            self._tape(p, QPointF(rect.left() - 4, rect.top() + 3), 14, 6)

    def _paint_boxed_row(self, p, control, act, rect, look, is_cur):
        """
        One row of a boxed legend: a filled tile, a turn or press icon, a
        vertical rule, the legend, and the action byte on the right.
        """
        first = act == core.DIAL_ACTIONS[0]
        last = act == core.DIAL_ACTIONS[-1]
        r = LEGEND_R
        path = self._corner_path(rect,
                                 tl=r if first else 0.0, tr=r if first else 0.0,
                                 br=r if last else 0.0, bl=r if last else 0.0)

        p.setPen(Qt.NoPen)
        p.setBrush(self._well_brush(rect) if THEME.well else QColor(THEME.board))
        p.drawPath(path)
        if not look.known and not look.pending:
            self._unknown_fill(p, path, rect, control, 7.0)
        if is_cur or look.pending:
            self._glow(p, path, TAPE)
            pen = QPen(QColor(TAPE), ROW_SEP * 1.6)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        elif control == self.hover:
            glow = QColor(INK)
            glow.setAlpha(18)
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawPath(path)

        colour = (QColor(TAPE) if is_cur
                  else (INK if look.known or look.pending and not look.quiet else INK_DIM)) # Set pending text to white to differentiate between selected and pending. -Oaken
        icon_c = QPointF(rect.left() + RULE_X / 2, rect.center().y())
        if act == "push":
            p.setPen(Qt.NoPen)
            p.setBrush(colour)
            p.drawEllipse(icon_c, rect.height() * 0.11, rect.height() * 0.11)
        else:
            self._paint_turn_icon(p, icon_c, act == "right", colour)

        p.setPen(QPen(QColor(THEME.line), ROW_SEP))
        p.drawLine(QPointF(rect.left() + RULE_X, rect.top()),
                   QPointF(rect.left() + RULE_X, rect.bottom()))

        mono = QFont(self.mono)
        mono.setPixelSize(max(6, round(ROW_BYTE_PT)))
        mono.setItalic(True)
        mono.setWeight(QFont.Bold)
        p.setFont(mono)
        p.setPen(QColor(THEME.byte_ink or THEME.ink_dim))
        p.drawText(rect.adjusted(0, 0, -RULE_X * 0.25, 0),
                   Qt.AlignRight | Qt.AlignVCenter,
                   f"0x{core.action_byte(control):02x}")

        f = self._legend_font(round(ROW_PT), roomy=False)
        p.setFont(f)
        p.setPen(colour)
        text = self._lettering(look.text if (look.known or look.pending)
                               else "Unknown")
        body = rect.adjusted(LABEL_X, 0, -RULE_X * 1.1, 0)
        p.drawText(body, Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(f).elidedText(text, Qt.ElideRight, body.width()))

    @staticmethod
    def _fit(text, font, rect):
        """Elide to at most two wrapped lines."""
        fm = QFontMetricsF(font)
        if fm.boundingRect(rect, Qt.TextWordWrap, text).height() <= rect.height():
            return text
        while text and fm.boundingRect(rect, Qt.TextWordWrap, text + "…").height() > rect.height():
            text = text[:-1]
        return text + "…"
