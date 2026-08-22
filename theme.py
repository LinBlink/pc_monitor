"""Colour themes, shared by the JPEG renderer and the HD web page.

A theme is a flat dict of the same tokens the renderer already used as module
constants — surfaces, ink, one hue per entity, three reserved status hues — plus
a corner radius, because "terminal" is as much about square corners as it is
about the colour green.

Keeping them here rather than in :mod:`render` is what lets the web page paint
itself from the same numbers: the palette becomes CSS custom properties and both
clients agree on what "green terminal" means without either one owning it.

Adding a theme is adding an entry to :data:`THEMES` with every token present —
:func:`palette` rebinds the renderer's globals wholesale, so a missing token
would silently leave the previous theme's colour in place.
"""

from __future__ import annotations

# The full token set. A theme that does not define all of these is rejected at
# import, which is cheaper than finding out one tile is the wrong colour.
TOKENS = (
    "PLANE", "SURFACE", "BORDER", "INK", "INK2", "MUTED", "GRID",
    "C_CPU", "C_GPU", "C_MEM", "C_DOWN", "C_UP", "C_FPS", "C_AI", "C_PWR",
    "C_DISK", "S_GOOD", "S_WARN", "S_CRIT", "RADIUS",
)

# --- dark: the original palette --------------------------------------------
# One fixed hue per entity in validated slot order, status hues never used as a
# series, all text in ink tokens.
DARK = {
    "PLANE": (13, 13, 13),
    "SURFACE": (26, 26, 25),
    "BORDER": (49, 49, 48),
    "INK": (255, 255, 255),
    "INK2": (195, 194, 183),
    "MUTED": (137, 135, 129),
    "GRID": (44, 44, 42),
    "C_CPU": (57, 135, 229),
    "C_GPU": (217, 89, 38),
    "C_MEM": (25, 158, 112),
    "C_DOWN": (201, 133, 0),
    "C_UP": (213, 81, 129),
    "C_FPS": (144, 133, 233),
    "C_AI": (0, 150, 163),
    "C_PWR": (166, 118, 84),
    "C_DISK": (127, 118, 191),
    "S_GOOD": (12, 163, 12),
    "S_WARN": (250, 178, 25),
    "S_CRIT": (208, 59, 59),
    "RADIUS": 7,
}

# --- term: phosphor green, square corners, ANSI-bright accents --------------
# The base is green — plane, panels, borders and body text all sit on the same
# hue, which is what makes it read as a terminal rather than as a dark theme
# with a green tint. The series hues are then deliberately *not* green: on a
# green ground a green series would disappear into it, so each entity takes a
# saturated ANSI colour instead and the screen ends up more colourful than the
# dark theme, not less.
TERM = {
    "PLANE": (4, 10, 6),
    "SURFACE": (9, 22, 13),
    "BORDER": (26, 74, 41),
    "INK": (198, 255, 214),
    "INK2": (0, 230, 130),
    "MUTED": (74, 140, 98),
    "GRID": (22, 60, 36),
    "C_CPU": (60, 214, 255),      # bright cyan
    "C_GPU": (255, 140, 42),      # amber
    "C_MEM": (0, 230, 118),       # green
    "C_DOWN": (255, 214, 51),     # yellow
    "C_UP": (255, 92, 158),       # magenta
    "C_FPS": (167, 139, 250),     # violet
    "C_AI": (0, 229, 214),        # teal
    "C_PWR": (255, 176, 92),      # light orange
    "C_DISK": (122, 162, 255),    # blue
    "S_GOOD": (0, 230, 118),
    "S_WARN": (255, 200, 0),
    "S_CRIT": (255, 82, 82),
    "RADIUS": 0,
}

THEMES = {"dark": DARK, "term": TERM}
# Display names, and the order the T key cycles through.
LABELS = {"dark": "深色", "term": "终端绿"}
NAMES = tuple(THEMES)
DEFAULT = "dark"

for _name, _palette in THEMES.items():
    _missing = set(TOKENS) - set(_palette)
    if _missing:
        raise ValueError(f"theme {_name} is missing {sorted(_missing)}")


def resolve(name: str | None) -> str:
    """A known theme name, falling back to the default for anything else."""
    return name if name in THEMES else DEFAULT


def palette(name: str | None) -> dict:
    """A copy of one theme's tokens, safe to hand to ``globals().update``."""
    return dict(THEMES[resolve(name)])


def cycle(name: str | None, step: int = 1) -> str:
    """The next theme along, wrapping — what a single "switch theme" key does."""
    idx = NAMES.index(resolve(name))
    return NAMES[(idx + step) % len(NAMES)]


def hex_tokens(name: str | None) -> dict[str, str]:
    """The colour tokens as ``#rrggbb``, for CSS. RADIUS comes back as ``Npx``."""
    out = {}
    for key, value in palette(name).items():
        if key == "RADIUS":
            out[key] = f"{int(value)}px"
        else:
            out[key] = "#%02x%02x%02x" % tuple(value)
    return out
