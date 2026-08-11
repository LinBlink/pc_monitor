"""Generate the 74x74 launcher icon for the handheld.

Onion themes expect monochrome white glyphs on transparency, so the icon stays
white and picks up whatever theme it is dropped into. Drawn at 4x and downscaled
because ImageDraw has no antialiasing.
"""

from PIL import Image, ImageDraw

SIZE = 74
SS = 4
WHITE = (255, 255, 255, 255)


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


if __name__ == "__main__":
    build().save("icon.png")
    print("wrote icon.png")
