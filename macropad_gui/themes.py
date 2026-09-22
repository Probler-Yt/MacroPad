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

from dataclasses import dataclass, field
from pathlib import Path


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

THEMES = {t.key: t for t in (SILKSCREEN, WHITE_BOARD, BLUEPRINT, SKETCH,
                             NORD, MOCHA, LATTE)}
DEFAULT = SILKSCREEN.key

FONTS = Path(__file__).resolve().parent / "fonts"


def get(key):
    return THEMES.get(key, THEMES[DEFAULT])


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
