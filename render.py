"""Draw the telemetry dashboard, in landscape or portrait, for the Miyoo panel.

The handheld can be held either way, so there are two layouts rather than one
layout that gets rotated: a 640x480 landscape grid and a 480x640 portrait stack.
Rotation is applied afterwards purely to map the chosen layout onto the physical
panel, which is itself mounted upside down.

Tiles are drawn into an arbitrary box and adapt to its height — the sparkline
is anchored to the bottom and dropped when a short tile leaves no room — so both
layouts reuse the same tile renderers.

Colour roles follow a validated categorical palette: one fixed hue per entity
(never cycled), status hues reserved for the FPS state, and all text in ink
tokens so identity always comes from a mark beside the text.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

LANDSCAPE = (640, 480)
PORTRAIT = (480, 640)

# --- ink & surfaces (dark mode) ---
PLANE = (13, 13, 13)
SURFACE = (26, 26, 25)
BORDER = (49, 49, 48)
INK = (255, 255, 255)
INK2 = (195, 194, 183)
MUTED = (137, 135, 129)
GRID = (44, 44, 42)

# --- one fixed hue per entity, in validated slot order ---
C_CPU = (57, 135, 229)
C_GPU = (217, 89, 38)
C_MEM = (25, 158, 112)
C_DOWN = (201, 133, 0)
C_UP = (213, 81, 129)
C_FPS = (144, 133, 233)

# --- reserved status hues (never used as a series) ---
S_GOOD = (12, 163, 12)
S_WARN = (250, 178, 25)
S_CRIT = (208, 59, 59)

PAD, GAP, PADI = 8, 8, 12
TITLE_H = 24
SPARK_MAX = 46
SPARK_MIN = 18
CHIP_H = 20
CHIP_GAP = 6
ARROW_W = 13

_FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
_FONT_REG = "C:/Windows/Fonts/msyh.ttc"


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    # Index 1 of the YaHei collections is the "UI" face; older copies lack it.
    try:
        return ImageFont.truetype(path, size, index=1)
    except (OSError, ValueError):
        return ImageFont.truetype(path, size, index=0)


class Fonts:
    def __init__(self):
        self.hero = _font(_FONT_BOLD, 60)
        self.value = _font(_FONT_BOLD, 34)
        self.value_sm = _font(_FONT_BOLD, 28)
        self.value_xs = _font(_FONT_BOLD, 22)
        self.label = _font(_FONT_BOLD, 15)
        self.sub = _font(_FONT_REG, 13)
        self.meta = _font(_FONT_REG, 12)
        self.tiny = _font(_FONT_REG, 11)


def blend(fg, bg, alpha: float):
    return tuple(int(round(f * alpha + b * (1 - alpha))) for f, b in zip(fg, bg))


def clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def fmt_rate(bps: float) -> str:
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def ellipsize(draw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def tile(d, box, fonts, title: str, meta: str = "") -> None:
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=8, fill=SURFACE, outline=BORDER, width=1)
    d.text((x0 + PADI, y0 + 7), title, font=fonts.label, fill=INK2)
    if meta:
        d.text((x1 - PADI, y0 + 10), meta, font=fonts.meta, fill=MUTED, anchor="ra")


def meter(d, box, frac: float, color) -> None:
    """Pill meter: track is a dark step of the series' own hue."""
    x0, y0, x1, y1 = box
    r = min(4, (y1 - y0) // 2)
    d.rounded_rectangle(box, radius=r, fill=blend(color, SURFACE, 0.22))
    frac = clamp01(frac)
    if frac <= 0:
        return
    w = max(2 * r, int(round((x1 - x0) * frac)))
    d.rounded_rectangle((x0, y0, x0 + w, y1), radius=r, fill=color)


def spark(img, box, values, color, vmax: float | None = None, ss: int = 3) -> None:
    """Sparkline: 10% area wash, 2px line, end dot with a 2px surface ring."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < 4 or h < 4 or len(values) < 2:
        return

    top = max(vmax if vmax is not None else max(values), 1e-9)
    n = len(values)

    layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    span_x, span_y = w * ss - 1, h * ss - 1
    pts = [(i * span_x / (n - 1), span_y - clamp01(v / top) * span_y)
           for i, v in enumerate(values)]
    ld.polygon([(0, h * ss)] + pts + [(w * ss, h * ss)], fill=color + (26,))
    ld.line(pts, fill=color + (255,), width=2 * ss, joint="curve")

    d = ImageDraw.Draw(img)
    d.line((x0, y1, x1, y1), fill=GRID, width=1)
    flat = layer.resize((w, h), Image.LANCZOS)
    img.paste(flat, (x0, y0), flat)

    ex = x0 + w - 1
    ey = y0 + (h - 1) - clamp01(values[-1] / top) * (h - 1)
    d.ellipse((ex - 6, ey - 6, ex + 6, ey + 6), fill=SURFACE)
    d.ellipse((ex - 4, ey - 4, ex + 4, ey + 4), fill=color)


def dot(d, x, y, color, r=4) -> None:
    d.ellipse((x, y, x + 2 * r, y + 2 * r), fill=color)


def _spark_box(box, top_limit: int):
    """Bottom-anchored sparkline area, or None when the tile is too short."""
    x0, _y0, x1, y1 = box
    bottom = y1 - PADI
    top = max(top_limit, bottom - SPARK_MAX)
    if bottom - top < SPARK_MIN:
        return None
    return (x0 + PADI, top, x1 - PADI, bottom)


def _cpu_tile(img, d, f, s, box) -> None:
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    roomy = (y1 - y0) >= 130
    c = s["cpu"]
    tile(d, box, f, "CPU", f"{c['cores']} 核 · {c['ghz']:.2f} GHz")

    y = y0 + TITLE_H
    d.text((ix0, y), f"{c['percent']:.0f}%", font=f.value if roomy else f.value_sm,
           fill=INK)
    d.text((ix1, y + (26 if roomy else 20)), f"峰值 {c['peak']:.0f}%",
           font=f.tiny, fill=MUTED, anchor="ra")
    y += 48 if roomy else 36
    mh = 10 if roomy else 8
    meter(d, (ix0, y, ix1, y + mh), c["percent"] / 100.0, C_CPU)
    y += mh + (10 if roomy else 6)

    sb = _spark_box(box, y)
    if sb:
        spark(img, sb, c["hist"], C_CPU, vmax=100.0)


def _fps_tile(img, d, f, s, box) -> None:
    x0, y0, x1, _y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    fps = s["fps"]
    v = fps["value"]

    if v is None:
        state, state_color = ("RTSS 未运行" if not fps["rtss"] else "无游戏"), S_WARN
    elif v >= 55:
        state, state_color = "流畅", S_GOOD
    elif v >= 30:
        state, state_color = "一般", S_WARN
    else:
        state, state_color = "卡顿", S_CRIT

    tile(d, box, f, "游戏 FPS")
    sw = d.textlength(state, font=f.meta)
    dot(d, ix1 - sw - 14, y0 + 13, state_color, 4)
    d.text((ix1, y0 + 10), state, font=f.meta, fill=INK2, anchor="ra")

    y = y0 + TITLE_H - 6
    if v is None:
        d.text((ix0, y), "—", font=f.hero, fill=MUTED)
        hint = ("请启动 MSI Afterburner / RTSS" if not fps["rtss"]
                else "前台没有游戏画面")
        d.text((ix0, y0 + 82), hint, font=f.sub, fill=INK2)
        for i, (name, pct) in enumerate(s["top"][:2]):
            d.text((ix0, y0 + 106 + i * 18),
                   ellipsize(d, f"{name}  {pct:.0f}%", f.tiny, ix1 - ix0),
                   font=f.tiny, fill=MUTED)
        return

    d.text((ix0, y), f"{v:.0f}", font=f.hero, fill=state_color)
    hw = d.textlength(f"{v:.0f}", font=f.hero)
    d.text((ix0 + 4 + hw, y0 + 62), "FPS", font=f.sub, fill=MUTED)
    d.text((ix0, y0 + 82),
           ellipsize(d, f"{fps['frametime_ms']:.1f} ms · {fps['process'] or ''}",
                     f.sub, ix1 - ix0),
           font=f.sub, fill=INK2)

    sb = _spark_box(box, y0 + 104)
    if sb:
        hist = fps["hist"]
        # The trend line keeps FPS's own fixed hue; only the dot + word above it
        # carry the status colour, so a mark's colour never shifts with its value.
        spark(img, sb, hist, C_FPS, vmax=max(60.0, max(hist)))


def _gpu_tile(img, d, f, s, box) -> None:
    x0, y0, x1, _y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    g = s["gpu"]
    tile(d, box, f, "GPU", g["name"] if g["ok"] else "未检测到")
    if not g["ok"]:
        d.text((ix0, y0 + TITLE_H + 6), "—", font=f.value, fill=MUTED)
        return

    y = y0 + TITLE_H
    d.text((ix0, y), f"{g['percent']:.0f}%", font=f.value_sm, fill=INK)
    d.text((ix1, y + 12), f"{g['temp_c']:.0f}°C · {g['power_w']:.0f} W",
           font=f.meta, fill=INK2, anchor="ra")
    y += 36
    meter(d, (ix0, y, ix1, y + 8), g["percent"] / 100.0, C_GPU)
    y += 10
    d.text((ix0, y),
           f"显存 {g['mem_used_gb']:.1f} / {g['mem_total_gb']:.1f} GB",
           font=f.tiny, fill=MUTED)
    y += 16
    frac = g["mem_used_gb"] / g["mem_total_gb"] if g["mem_total_gb"] else 0.0
    meter(d, (ix0, y, ix1, y + 8), frac, C_GPU)


def _mem_tile(img, d, f, s, box) -> None:
    x0, y0, x1, _y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    m = s["mem"]
    tile(d, box, f, "内存", f"共 {m['total_gb']:.0f} GB")

    y = y0 + TITLE_H
    d.text((ix0, y), f"{m['used_gb']:.1f} GB", font=f.value_sm, fill=INK)
    d.text((ix1, y + 12), f"{m['percent']:.0f}%", font=f.meta, fill=INK2,
           anchor="ra")
    y += 36
    meter(d, (ix0, y, ix1, y + 8), m["percent"] / 100.0, C_MEM)
    y += 8

    sb = _spark_box(box, y + 6)
    if sb:
        spark(img, sb, m["hist"], C_MEM, vmax=100.0)


def _net_tile(img, d, f, s, box) -> None:
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    n = s["net"]
    tile(d, box, f, "网络", ellipsize(d, n["nic"], f.meta, (x1 - x0) // 2))

    # Two small multiples: down and up each keep their own scale, so neither is
    # flattened by the other. Never a shared axis across wildly different rates.
    rows = (
        ("下载", "↓", n["down_bps"], n["down_hist"], n["down_peak"], C_DOWN),
        ("上传", "↑", n["up_bps"], n["up_hist"], n["up_peak"], C_UP),
    )
    top = y0 + TITLE_H + 2
    row_h = (y1 - PADI - top) // 2
    label_w = min(150, (x1 - x0) // 3)
    # A short row cannot stack label / value / peak, so it goes single-line and
    # drops the peak — which the sparkline's own scale already implies.
    stacked = row_h >= 50

    for i, (name, arrow, cur, hist, peak, color) in enumerate(rows):
        ry = top + i * row_h
        if stacked:
            dot(d, ix0, ry + 4, color, 4)
            d.text((ix0 + 16, ry), f"{arrow} {name}", font=f.meta, fill=INK2)
            d.text((ix0, ry + 16), fmt_rate(cur), font=f.value_xs, fill=INK)
            d.text((ix0, ry + 44), f"峰值 {fmt_rate(peak)}", font=f.tiny,
                   fill=MUTED)
        else:
            dot(d, ix0, ry + 9, color, 4)
            d.text((ix0 + 16, ry + 1), arrow, font=f.value_xs, fill=INK2)
            d.text((ix0 + 34, ry + 1), fmt_rate(cur), font=f.value_xs, fill=INK)
        sh = row_h - (8 if stacked else 6)
        if sh >= SPARK_MIN:
            spark(img, (ix0 + label_w, ry, ix1, ry + sh), hist, color,
                  vmax=max(peak, 64 * 1024))


def _chip_window(widths, idx: int, room: float) -> tuple[int, int]:
    """Widest run of chips around idx that fits, grown outwards from it."""
    lo = hi = idx
    used = widths[idx]
    while True:
        grew = False
        if hi + 1 < len(widths) and used + CHIP_GAP + widths[hi + 1] <= room:
            hi += 1
            used += CHIP_GAP + widths[hi]
            grew = True
        if lo - 1 >= 0 and used + CHIP_GAP + widths[lo - 1] <= room:
            lo -= 1
            used += CHIP_GAP + widths[lo]
            grew = True
        if not grew:
            return lo, hi


def _device_strip(d, f, x, y, avail: int, names, idx: int) -> None:
    """The devices the handheld found, current one as a filled pill.

    Only the handheld knows what is on the LAN, so the list arrives with the
    request. Neighbours are shown on both sides because LEFT / RIGHT wrap, and a
    window centred on the current device keeps it visible however long the list.
    """
    n = len(names)
    widths = [d.textlength(nm, font=f.meta) + 16 for nm in names]
    room = avail - (2 * ARROW_W if n > 1 else 0)

    lo, hi = _chip_window(widths, idx, room)
    if hi - lo + 1 < n:
        # Some are hidden, so a "+N" has to fit too — redo with room for it.
        lo, hi = _chip_window(widths, idx, room - 30)

    cx = x
    if n > 1:
        d.text((cx, y - 2), "‹", font=f.label, fill=INK2)
        cx += ARROW_W
    for i in range(lo, hi + 1):
        if i == idx:
            d.rounded_rectangle((cx, y - 3, cx + widths[i], y - 3 + CHIP_H),
                                radius=CHIP_H // 2, fill=blend(C_CPU, PLANE, 0.30))
        d.text((cx + 8, y), ellipsize(d, names[i], f.meta, widths[i] - 16),
               font=f.meta, fill=INK if i == idx else MUTED)
        cx += widths[i] + CHIP_GAP
    if n > 1:
        d.text((cx, y - 2), "›", font=f.label, fill=INK2)
        cx += ARROW_W
    hidden = n - (hi - lo + 1)
    if hidden:
        d.text((cx, y), f"+{hidden}", font=f.meta, fill=MUTED)


def _header(d, f, s, w, devices=(), dev_idx: int = 0) -> None:
    tw = d.textlength(s["time"], font=f.label)
    dot(d, w - PAD - 4 - tw - 16, 12, S_GOOD, 4)
    d.text((w - PAD - 4, 6), s["time"], font=f.label, fill=INK, anchor="ra")

    left = PAD + 4
    avail = (w - PAD - 4 - tw - 22) - left
    if devices:
        _device_strip(d, f, left, 7, avail, devices, dev_idx)
    else:
        d.text((left, 8), ellipsize(d, f"PC 监控 · {s['host']}", f.label, avail),
               font=f.label, fill=INK2)


def _draw_landscape(img, d, f, s, devices=(), dev_idx=0) -> None:
    w = LANDSCAPE[0]
    col = (w - 2 * PAD - GAP) // 2
    lx, rx = PAD, PAD + col + GAP
    _header(d, f, s, w, devices, dev_idx)
    _cpu_tile(img, d, f, s, (lx, 36, lx + col, 186))
    _fps_tile(img, d, f, s, (rx, 36, rx + col, 186))
    _gpu_tile(img, d, f, s, (lx, 194, lx + col, 306))
    _mem_tile(img, d, f, s, (rx, 194, rx + col, 306))
    _net_tile(img, d, f, s, (lx, 314, w - PAD, 472))


def _draw_portrait(img, d, f, s, devices=(), dev_idx=0) -> None:
    w = PORTRAIT[0]
    x0, x1 = PAD, w - PAD
    _header(d, f, s, w, devices, dev_idx)
    _cpu_tile(img, d, f, s, (x0, 34, x1, 142))
    _fps_tile(img, d, f, s, (x0, 150, x1, 288))
    _gpu_tile(img, d, f, s, (x0, 296, x1, 408))
    _mem_tile(img, d, f, s, (x0, 416, x1, 520))
    _net_tile(img, d, f, s, (x0, 528, x1, 632))


# orient counts quarter-turns clockwise as the user rotates the handheld, so the
# content is turned the opposite way to stay upright in their hands.
_CONTENT_ROTATION = {0: 0, 1: 270, 2: 180, 3: 90}


def draw_layout(snapshot: dict, fonts: Fonts, portrait: bool = False,
                devices=(), dev_idx: int = 0) -> Image.Image:
    """The layout at its natural size and upright — for humans looking at a screen."""
    img = Image.new("RGB", PORTRAIT if portrait else LANDSCAPE, PLANE)
    d = ImageDraw.Draw(img)
    draw = _draw_portrait if portrait else _draw_landscape
    draw(img, d, fonts, snapshot, devices, dev_idx)
    return img


def render(snapshot: dict, fonts: Fonts, orient: int = 0,
           panel_flip: bool = True, devices=(), dev_idx: int = 0) -> Image.Image:
    """The frame as the handheld should receive it, mapped onto the panel."""
    orient %= 4
    img = draw_layout(snapshot, fonts, portrait=orient in (1, 3),
                      devices=devices, dev_idx=dev_idx)

    rotation = _CONTENT_ROTATION[orient]
    if rotation:
        img = img.rotate(rotation, expand=True)
    if panel_flip:
        img = img.transpose(Image.ROTATE_180)
    return img
