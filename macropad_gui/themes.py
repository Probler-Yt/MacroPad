"""
Themes: a palette, and a drawing style for the pad.

Most themes only change colours. Two change how the pad is drawn:

  Blueprint   an engineering drawing: white linework on cyanotype blue, a
              faint grid, unknown keys as dashed hidden lines, chain lines
              through the knob centres, and a title block in the corner
  Sketch      pencil on paper: lines that wander a little and are gone over
              twice, as a hand would, in a handwriting font

Nord and Catppuccin use their published palettes, both MIT licensed.
"""

from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Metrics:
    """
    The pad's proportions, in board units. The view scales the whole board
    to fit, so only the ratios between these matter.

    Knobs North's values come from its Figma file: each one is the design's
    measurement divided by the design's 291.67 unit key and multiplied by
    our 96, so the proportions are exactly the designer's.
    """
    key: float = 96.0             # a key tile, square
    gap_x: float = 16.0           # between key columns
    gap_y: float = 16.0           # between key rows
    margin: float = 30.0          # board edge to anything
    key_radius: float = 5.0
    r_out: float = 54.0           # outer reach of a knob's arcs
    r_in: float = 40.0
    r_knob: float = 30.0          # the knob body
    row_h: float = 22.0           # one legend row
    row_gap: float = 12.0         # knob bottom to first legend row
    row_sep: float = 0.0          # hairline between rows, 0 for none
    dial_w: float = 160.0         # legend block width
    legend_radius: float = 3.0
    rule_x: float = 0.0           # vertical rule inside the legend block
    label_x: float = 22.0         # where a legend's text starts
    byte_pt: float = 10.0         # action byte type size
    label_pt: float = 13.0        # key legend type size
    row_pt: float = 12.0          # knob legend type size
    row_byte_pt: float = 10.0
    badge: float = 13.0           # the changed / unknown corner flag
    # Where a key's legend sits, as fractions of the tile: left, top, right,
    # bottom. Empty keeps the fixed insets that suit a 96 unit key.
    label_band: tuple = ()
    byte_inset: tuple = ()        # same, for the action byte


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    dark: bool

    # palette, as hex
    window: str          # behind everything
    board: str           # the pad's face
    ink: str             # lines and legends
    ink_dim: str         # secondary text, unknown outlines
    hatch: str           # hatching, grid, quiet lines
    accent: str          # selection and unwritten changes
    accent_text: str     # text on an accent filled button
    good: str
    bad: str
    line: str            # widget borders
    field: str           # text boxes

    # drawing style
    unknown: str = "hatch"        # hatch, or "hidden": dashed outline
    grid: bool = False            # a faint drawing grid behind the pad
    title_block: bool = False     # sheet border and title block
    centre_lines: bool = False    # chain lines through knob centres
    wobble: float = 0.0           # how far a pencil line wanders, board units
    caps: bool = False            # legends in capitals, like drawing lettering
    font: str = ""                # legend font; empty means the system font
    mono_legends: bool = False    # draw legends in the monospace font
    badge: str = "tape"           # pending marker: "tape" strip or "star"
    legend_box: bool = False      # a border around each knob's three rows
    query_badge: bool = False     # a "?" in the corner of an unknown key
    byte_prefix: bool = False     # knob rows read 0x13 rather than 13
    badge_glyph: str = ""         # a character stamped in the corner flag
    byte_ink: str = ""            # action byte grey; empty means ink_dim
    well: str = ""                # gradient's far colour; empty means flat
    glow: float = 0.0             # neon bloom on accent strokes, board units
    knob_art: str = ""            # "figma" draws the outline from vectors.py
    button_radius: int = 4        # window buttons and fields
    pill_buttons: bool = False    # footer buttons outlined rather than bare
    outline_primary: bool = False # the write button outlined, not filled
    ui_pt: int = 0                # base type size; 0 keeps Qt's own
    title_pt: int = 18            # the inspector's heading
    mono_status: bool = False     # footer status in the mono face
    metrics: Metrics = field(default_factory=Metrics)
    ui_font: str = ""             # window chrome; empty means the system font
    mono_font: str = ""           # window chrome, monospace parts
    notes: tuple = field(default=())


SILKSCREEN = Theme(
    "silkscreen", "Silkscreen", True,
    window="#17191b", board="#202326", ink="#d8d4cb", ink_dim="#72767b",
    hatch="#34383c", accent="#eb8a2f", accent_text="#1b1206",
    good="#8db36b", bad="#e0584f", line="#3a3e42", field="#121416")

WHITE_BOARD = Theme(
    "white-board", "White board", False,
    window="#e4e2dc", board="#f7f6f2", ink="#232323", ink_dim="#86847e",
    hatch="#d6d2c8", accent="#cf6a17", accent_text="#ffffff",
    good="#3f7d20", bad="#c0392b", line="#cbc7bd", field="#ffffff")

BLUEPRINT = Theme(
    "blueprint", "Blueprint", True,
    window="#123b6d", board="#123b6d", ink="#f2f5f8", ink_dim="#93b3d8",
    hatch="#285a92", accent="#ffd166", accent_text="#0b2545",
    good="#a8e6a1", bad="#ff9a8f", line="#3d6aa3", field="#0d2e57",
    unknown="hidden", grid=True, title_block=True, centre_lines=True,
    caps=True, mono_legends=True)

SKETCH = Theme(
    "sketch", "Sketch", False,
    window="#f3efe6", board="#f3efe6", ink="#2e2b28", ink_dim="#8c857a",
    hatch="#b9b1a3", accent="#c8553d", accent_text="#ffffff",
    good="#4f7a36", bad="#b03a2e", line="#cfc6b6", field="#fbf9f4",
    wobble=1.6, font="Architects Daughter")

NORD = Theme(
    "nord", "Nord", True,
    window="#2e3440", board="#3b4252", ink="#eceff4", ink_dim="#7b88a1",
    hatch="#434c5e", accent="#88c0d0", accent_text="#2e3440",
    good="#a3be8c", bad="#bf616a", line="#4c566a", field="#292e39")

MOCHA = Theme(
    "catppuccin-mocha", "Catppuccin Mocha", True,
    window="#181825", board="#1e1e2e", ink="#cdd6f4", ink_dim="#7f849c",
    hatch="#313244", accent="#cba6f7", accent_text="#1e1e2e",
    good="#a6e3a1", bad="#f38ba8", line="#45475a", field="#11111b")

LATTE = Theme(
    "catppuccin-latte", "Catppuccin Latte", False,
    window="#e6e9ef", board="#eff1f5", ink="#4c4f69", ink_dim="#8c8fa1",
    hatch="#ccd0da", accent="#8839ef", accent_text="#eff1f5",
    good="#40a02b", bad="#d20f39", line="#bcc0cc", field="#dce0e8")

# Designed in Figma by a member of the community. The palette is Silkscreen's
# own window and board with a hotter orange and neutral rather than warm greys,
# so what makes it its own theme is mostly the drawing switches below.

# :D -Oaken

_F = 96.0 / 291.67 # Scale factor from the Figma file: its key tile is 291.67 units, ours is 96.

KNOBS_NORTH = Theme(
    "knobs-north", "Knobs North", True,
    window="#17191b", board="#202326", ink="#ffffff", ink_dim="#b5b5b5",
    hatch="#2f2f2f", accent="#ff7700", accent_text="#ffffff",
    good="#87df9a", bad="#e0584f", line="#6e6e6e", field="#202326",
    badge="star", badge_glyph="*", legend_box=True, query_badge=True,
    byte_prefix=True, byte_ink="#505050",
    # tile and knob gradients, both #202326 falling to #131619 in the file
    well="#131619", glow=35 * _F, knob_art="figma", # "glow" value here was set way too low, making it not visible. Fixed. -Oaken
    font="Harmattan", mono_font="JetBrains Mono NL", ui_font="Harmattan",
    button_radius=7, pill_buttons=True, outline_primary=True, ui_pt=18, # ui_pt=15 was a little small, bumping slightly to 18pt. -Oaken
    title_pt=28, mono_status=True,
    metrics=Metrics(
        key=96.0,
        gap_x=88.0 * _F, gap_y=60.58 * _F, margin=74.0 * _F,
        key_radius=20.0 * _F,
        r_out=128.58 * _F, r_in=110.0 * _F, r_knob=92.5 * _F,
        row_h=75.0 * _F, row_gap=40.0 * _F, row_sep=2.0 * _F,
        dial_w=512.0 * _F, legend_radius=15.0 * _F,
        rule_x=73.0 * _F, label_x=94.0 * _F,
        byte_pt=24.0 * _F, label_pt=55.0 * _F,
        row_pt=40.0 * _F, row_byte_pt=20.0 * _F,
        badge=64.29 * _F,
        # The design sets line height to exactly the type size; Qt's is
        # looser, so the band is taller than the design's 100/300 while
        # staying centred on the same line.

        # Values changed to allow 2 lines to render per key with increased font size. Elipsis will show on second line instead of a third line. -Oaken
        label_band=(38 / 300, 0.10, 37 / 300, 0.01), # label_band=(38 / 300, 0.18, 37 / 300, 0.18) <-- old
        byte_inset=(19 / 300, 10 / 300)),
    notes=("Designed in Figma by a member of the community.",
           "Knobs along the top. North. Or east if you rotate...")) # Small tweak -Oaken

# --------------------------------------------------------------- palettes
#
# A palette is colour and nothing else. Anything that decides how the pad is
# drawn - proportions, badges, knob art, typefaces - belongs to the style.

PALETTE_FIELDS = ("window", "board", "ink", "ink_dim", "hatch", "accent",
                  "accent_text", "good", "bad", "line", "field", "well",
                  "byte_ink", "dark")


def _palette(theme, **over):
    """Pull a palette out of a theme written the old way."""
    out = {f: getattr(theme, f) for f in PALETTE_FIELDS}
    out.update(over)
    return out


PALETTES = {
    "graphite": ("Graphite", _palette(SILKSCREEN)),
    "paper": ("Paper", _palette(WHITE_BOARD)),
    "nord": ("Nord", _palette(NORD)),
    "mocha": ("Catppuccin Mocha", _palette(MOCHA)),
    "latte": ("Catppuccin Latte", _palette(LATTE)),
    "neon": ("Neon", _palette(KNOBS_NORTH)),
    "blueprint": ("Blueprint", _palette(BLUEPRINT)),
    "graph": ("Graph paper", _palette(SKETCH)),
}

# ----------------------------------------------------------------- styles
#
# recolour lists the palettes a style is offered with. Blueprint and Sketch
# are left alone: a blueprint that isn't blue, or a pencil sketch that isn't
# on paper, is not the same idea.

OPEN = ("graphite", "paper", "nord", "mocha", "latte", "neon")

STYLES = {
    "silkscreen": ("Silkscreen", SILKSCREEN, OPEN),
    "knobs-north": ("Knobs North", KNOBS_NORTH, OPEN),
    "blueprint": ("Blueprint", BLUEPRINT, ("blueprint",)),
    "sketch": ("Hand drawn", SKETCH, ("graph",)),
}

# Keys as they were before styles and palettes were separated, so a saved
# setting from an older build still finds its theme.
LEGACY = {
    "silkscreen": ("silkscreen", "graphite"),
    "white-board": ("silkscreen", "paper"),
    "nord": ("silkscreen", "nord"),
    "catppuccin-mocha": ("silkscreen", "mocha"),
    "catppuccin-latte": ("silkscreen", "latte"),
    "blueprint": ("blueprint", "blueprint"),
    "sketch": ("sketch", "graph"),
    "knobs-north": ("knobs-north", "neon"),
}
_LEGACY_BY_PAIR = {v: k for k, v in LEGACY.items()}


def compose(style_key, palette_key):
    """One style wearing one palette."""
    style_name, base, _allowed = STYLES[style_key]
    palette_name, colours = PALETTES[palette_key]
    key = _LEGACY_BY_PAIR.get((style_key, palette_key),
                              f"{style_key}/{palette_key}")
    name = (style_name if len(STYLES[style_key][2]) == 1
            else f"{style_name} / {palette_name}")
    return replace(base, key=key, name=name, **colours)


THEMES = {}
for _s, (_sn, _base, _allowed) in STYLES.items():
    for _p in _allowed:
        _t = compose(_s, _p)
        THEMES[_t.key] = _t

DEFAULT = "silkscreen"


def parts(key):
    """(style, palette) for a theme key, old style or new."""
    if key in LEGACY:
        return LEGACY[key]
    if "/" in key:
        s, p = key.split("/", 1)
        if s in STYLES and p in PALETTES:
            return s, p
    return LEGACY[DEFAULT]

FONTS = Path(__file__).resolve().parent / "fonts"


def get(key):
    if key in THEMES:
        return THEMES[key]
    style, palette = parts(key or DEFAULT)
    if palette not in STYLES[style][2]:
        palette = STYLES[style][2][0]
    return THEMES[compose(style, palette).key]


def mix(a, b, t):
    """Blend two hex colours; t = 0 gives a, 1 gives b."""
    a, b = a.lstrip("#"), b.lstrip("#")
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def load_fonts():
    """Register the fonts shipped with the app. Safe to call more than once."""
    from PySide6.QtGui import QFontDatabase
    from . import padview
    for f in sorted(FONTS.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(f))
    padview._FAMILIES = None
