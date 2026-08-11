"""Generate the handheld launcher icon and the Windows exe icon.

Onion themes expect monochrome white glyphs on transparency, so the launcher icon
stays white and picks up whatever theme it is dropped into. The exe icon is the
same glyph on the dashboard's own dark surface, because a white-on-transparent
glyph disappears against a light taskbar. Both are drawn at 4x and downscaled
because ImageDraw has no antialiasing.
"""

from PIL import Image, ImageDraw

SIZE = 74
SS = 4
WHITE = (255, 255, 255, 255)

# Matches render.py's surface and CPU hue, so the icon and the dashboard agree.
APP_BG = (26, 26, 25, 255)
APP_ACCENT = (57, 135, 229, 255)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build() -> Image.Image:
    s = SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = SS  # one target pixel

    # Monitor body: rounded outline.
    body = (7 * u, 11 * u, 67 * u, 53 * u)
    d.rounded_rectangle(body, radius=6 * u, outline=WHITE, width=3 * u)

    # Stand.
    d.rounded_rectangle((33 * u, 53 * u, 41 * u, 62 * u), radius=1 * u, fill=WHITE)
    d.rounded_rectangle((22 * u, 61 * u, 52 * u, 65 * u), radius=2 * u, fill=WHITE)

    # Sparkline inside the screen — the "live telemetry" cue.
    pts = [(15, 41), (23, 33), (30, 38), (37, 24), (45, 31), (52, 22), (59, 27)]
    d.line([(x * u, y * u) for x, y in pts], fill=WHITE, width=3 * u, joint="curve")

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def build_app(size: int = 256) -> Image.Image:
    """The exe icon: same glyph, dark rounded tile, sparkline in the CPU hue."""
    s = size * 2
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 74.0  # keep the 74-unit geometry of the launcher glyph

    def box(a, b, c, e):
        return (round(a * u), round(b * u), round(c * u), round(e * u))

    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=round(12 * u), fill=APP_BG)
    d.rounded_rectangle(box(7, 13, 67, 55), radius=round(6 * u), outline=WHITE,
                        width=round(3 * u))
    d.rounded_rectangle(box(33, 55, 41, 63), radius=round(u), fill=WHITE)
    d.rounded_rectangle(box(22, 62, 52, 66), radius=round(2 * u), fill=WHITE)

    pts = [(15, 43), (23, 35), (30, 40), (37, 26), (45, 33), (52, 24), (59, 29)]
    d.line([(x * u, y * u) for x, y in pts], fill=APP_ACCENT, width=round(4 * u),
           joint="curve")

    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    build().save("icon.png")
    print("wrote icon.png")
    app = build_app(max(ICO_SIZES))
    app.save("app.ico", sizes=[(n, n) for n in ICO_SIZES])
    print(f"wrote app.ico ({', '.join(str(n) for n in ICO_SIZES)})")
