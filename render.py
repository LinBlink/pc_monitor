"""Draw the telemetry dashboard, in landscape or portrait, for the Miyoo panel.

The handheld can be held either way, so there are two layouts rather than one
layout that gets rotated: a 640x480 landscape grid and a 480x640 portrait stack.
Rotation is applied afterwards purely to map the chosen layout onto the physical
panel, which is itself mounted upside down.

Everything lives on one screen, so tiles are dense: each one is drawn into an
arbitrary box and adapts to its height — sparklines are anchored to the bottom and
dropped when a short tile leaves no room — which lets both layouts reuse the same
tile renderers at different sizes.

Colour roles follow a validated categorical palette: one fixed hue per entity
(never cycled), status hues reserved for state readouts (FPS, battery), and all
text in ink tokens so identity always comes from a mark beside the text.
"""

from __future__ import annotations

import math

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

PAD, GAP, PADI = 8, 6, 10
TITLE_H = 22
SPARK_MAX = 46
SPARK_MIN = 14
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
        self.hero = _font(_FONT_BOLD, 52)
        self.hero_sm = _font(_FONT_BOLD, 40)
        self.value = _font(_FONT_BOLD, 32)
        self.value_sm = _font(_FONT_BOLD, 26)
        self.value_xs = _font(_FONT_BOLD, 21)
        self.label = _font(_FONT_BOLD, 15)
        self.row = _font(_FONT_BOLD, 13)
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


def fmt_bytes(n: float) -> str:
    """Daily totals climb through the units, so the unit follows the number."""
    for unit, step in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n / 1024:.0f} KB"


def ellipsize(draw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def tile(d, box, fonts, title: str, meta: str = "") -> None:
    x0, y0, x1, _y1 = box
    d.rounded_rectangle(box, radius=7, fill=SURFACE, outline=BORDER, width=1)
    d.text((x0 + PADI, y0 + 5), title, font=fonts.label, fill=INK2)
    if meta:
        d.text((x1 - PADI, y0 + 8), meta, font=fonts.meta, fill=MUTED, anchor="ra")


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
    r = 5 if h >= 24 else 4
    d.ellipse((ex - r - 2, ey - r - 2, ex + r + 2, ey + r + 2), fill=SURFACE)
    d.ellipse((ex - r + 1, ey - r + 1, ex + r - 1, ey + r - 1), fill=color)


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


def _temp_color(temp_c: float | None):
    """Status hue for a CPU/GPU temperature, or muted when there is no reading."""
    if temp_c is None:
        return MUTED
    if temp_c >= 90:
        return S_CRIT
    if temp_c >= 80:
        return S_WARN
    return S_GOOD


# --- CPU ------------------------------------------------------------------

def _core_grid(img, d, f, box, pcts, mhz) -> None:
    """One cell per logical core: a usage bar with its own clock above it.

    Cores are laid out in at most two rows so a 16-thread CPU still gets cells
    wide enough for a clock reading; past that the clocks are dropped rather than
    shrunk into illegibility, because the bars are the part you read at a glance.
    """
    x0, y0, x1, y1 = box
    n = len(pcts)
    if not n:
        return
    rows = 1 if n <= 8 else 2
    cols = int(math.ceil(n / rows))
    cw = (x1 - x0) / cols
    ch = (y1 - y0) / rows
    show_mhz = bool(mhz) and cw >= 30 and ch >= 26
    bar_h = 9 if ch >= 22 else max(4, int(ch) - 4)

    for i, pct in enumerate(pcts):
        r, c = divmod(i, cols)
        cx = x0 + c * cw
        cy = y0 + r * ch
        cell_w = cw - 4
        if show_mhz:
            label = f"{mhz[i] / 1000:.1f}" if i < len(mhz) else "—"
            d.text((cx, cy), label, font=f.tiny, fill=MUTED)
            by = cy + ch - bar_h - 4
        else:
            by = cy + (ch - bar_h) / 2
        meter(d, (cx, by, cx + cell_w, by + bar_h), pct / 100.0, C_CPU)


def _cpu_tile(img, d, f, s, box) -> None:
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    c = s["cpu"]
    temp, power = c.get("temp_c"), c.get("power_w")

    meta = f"{c['cores']} 核 · {c['ghz']:.2f} GHz"
    tile(d, box, f, "CPU", meta)

    y = y0 + TITLE_H
    d.text((ix0, y), f"{c['percent']:.0f}%", font=f.value, fill=INK)
    vw = d.textlength(f"{c['percent']:.0f}%", font=f.value)

    # Temperature and package power sit beside the headline number: they are CPU
    # state, not a series of their own, so they take status ink and no hue.
    tx = ix0 + vw + 12
    if temp is not None:
        dot(d, tx, y + 12, _temp_color(temp), 4)
        d.text((tx + 13, y + 6), f"{temp:.0f}°C", font=f.value_xs, fill=INK2)
        tx += 13 + d.textlength(f"{temp:.0f}°C", font=f.value_xs) + 12
    if power is not None:
        d.text((tx, y + 6), f"{power:.0f} W", font=f.value_xs, fill=INK2)
    elif temp is None:
        d.text((tx, y + 10), "温度需 Afterburner", font=f.tiny, fill=MUTED)

    grid_top = y + 40
    if y1 - grid_top >= 22:
        _core_grid(img, d, f, (ix0, grid_top, ix1, y1 - 8),
                   c.get("core_pct") or [], c.get("core_mhz") or [])
    else:
        sb = _spark_box(box, grid_top)
        if sb:
            spark(img, sb, c["hist"], C_CPU, vmax=100.0)


# --- FPS ------------------------------------------------------------------

def _fps_state(fps: dict):
    v = fps["value"]
    if v is None:
        return ("RTSS 未运行" if not fps["rtss"] else "无游戏"), S_WARN
    if v >= 55:
        return "流畅", S_GOOD
    if v >= 30:
        return "一般", S_WARN
    return "卡顿", S_CRIT


def _fps_tile(img, d, f, s, box) -> None:
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    fps = s["fps"]
    v = fps["value"]
    state, state_color = _fps_state(fps)

    tile(d, box, f, "游戏 FPS")
    sw = d.textlength(state, font=f.meta)
    dot(d, ix1 - sw - 13, y0 + 11, state_color, 4)
    d.text((ix1, y0 + 8), state, font=f.meta, fill=INK2, anchor="ra")

    roomy = (y1 - y0) >= 88
    hero = f.hero if roomy else f.hero_sm
    y = y0 + TITLE_H - 4

    if v is None:
        d.text((ix0, y), "—", font=hero, fill=MUTED)
        hint = ("请启动 MSI Afterburner / RTSS" if not fps["rtss"]
                else "前台没有游戏画面")
        d.text((ix0, y + (54 if roomy else 42)), ellipsize(d, hint, f.sub, ix1 - ix0),
               font=f.sub, fill=INK2)
        return

    d.text((ix0, y), f"{v:.0f}", font=hero, fill=state_color)
    hw = d.textlength(f"{v:.0f}", font=hero)
    d.text((ix0 + 5 + hw, y + (30 if roomy else 22)), "FPS", font=f.sub, fill=MUTED)
    label = f"{fps['frametime_ms']:.1f} ms · {fps['process'] or ''}"
    d.text((ix0 + 5 + hw + 32, y + (30 if roomy else 22)),
           ellipsize(d, label, f.tiny, ix1 - ix0 - hw - 42), font=f.tiny, fill=MUTED)

    sb = _spark_box(box, y + (54 if roomy else 44))
    if sb:
        hist = fps["hist"]
        # The trend line keeps FPS's own fixed hue; only the dot + word above it
        # carry the status colour, so a mark's colour never shifts with its value.
        spark(img, sb, hist, C_FPS, vmax=max(60.0, max(hist)))


# --- GPU / memory ---------------------------------------------------------

def _gpu_tile(img, d, f, s, box) -> None:
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    g = s["gpu"]
    tile(d, box, f, "GPU", ellipsize(d, g["name"] if g["ok"] else "未检测到",
                                     f.meta, (x1 - x0) * 0.55))
    if not g["ok"]:
        d.text((ix0, y0 + TITLE_H + 4), "—", font=f.value, fill=MUTED)
        return

    y = y0 + TITLE_H
    d.text((ix0, y), f"{g['percent']:.0f}%", font=f.value_sm, fill=INK)
    vw = d.textlength(f"{g['percent']:.0f}%", font=f.value_sm)
    dot(d, ix0 + vw + 10, y + 10, _temp_color(g["temp_c"]), 4)
    d.text((ix0 + vw + 23, y + 4), f"{g['temp_c']:.0f}°C · {g['power_w']:.0f} W",
           font=f.meta, fill=INK2)

    y += 30
    meter(d, (ix0, y, ix1, y + 8), g["percent"] / 100.0, C_GPU)
    y += 12
    if y1 - y >= 26:
        d.text((ix0, y), f"显存 {g['mem_used_gb']:.1f} / {g['mem_total_gb']:.1f} GB",
               font=f.tiny, fill=MUTED)
        y += 15
        frac = g["mem_used_gb"] / g["mem_total_gb"] if g["mem_total_gb"] else 0.0
        meter(d, (ix0, y, ix1, y + 7), frac, C_GPU)


def _mem_slim(img, d, f, s, box) -> None:
    """Memory as a single labelled bar — it is the least volatile number here."""
    x0, y0, x1, y1 = box
    m = s["mem"]
    d.rounded_rectangle(box, radius=7, fill=SURFACE, outline=BORDER, width=1)
    cy = (y0 + y1) // 2
    d.text((x0 + PADI, cy), "内存", font=f.label, fill=INK2, anchor="lm")
    text = f"{m['used_gb']:.1f} / {m['total_gb']:.0f} GB"
    d.text((x0 + PADI + 42, cy), text, font=f.row, fill=INK, anchor="lm")
    tw = d.textlength(text, font=f.row)
    d.text((x1 - PADI, cy), f"{m['percent']:.0f}%", font=f.meta, fill=INK2,
           anchor="rm")
    bx0 = x0 + PADI + 52 + tw
    bx1 = x1 - PADI - 34
    if bx1 - bx0 > 30:
        meter(d, (bx0, cy - 4, bx1, cy + 4), m["percent"] / 100.0, C_MEM)


# --- process tables -------------------------------------------------------

def _proc_tile(img, d, f, box, title, rows, color, empty: str) -> None:
    """Top consumers as a small ranked table: mark, name, share."""
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    tile(d, box, f, title)

    if not rows:
        d.text((ix0, y0 + TITLE_H + 2), empty, font=f.tiny, fill=MUTED)
        return

    top = y0 + TITLE_H
    avail = y1 - 6 - top
    n = min(len(rows), 3)
    row_h = min(24, avail / max(1, n))
    for i, (name, pct) in enumerate(rows[:n]):
        ry = top + i * row_h
        dot(d, ix0, ry + row_h / 2 - 3, color, 3)
        pct_text = f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%"
        pw = d.textlength(pct_text, font=f.row)
        d.text((ix1, ry + row_h / 2), pct_text, font=f.row, fill=INK, anchor="rm")
        d.text((ix0 + 12, ry + row_h / 2),
               ellipsize(d, name, f.meta, ix1 - ix0 - pw - 22),
               font=f.meta, fill=INK2, anchor="lm")


# --- network --------------------------------------------------------------

def _net_tile(img, d, f, s, box) -> None:
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    n = s["net"]

    day = f"今日 ↓{fmt_bytes(n.get('day_down', 0))} ↑{fmt_bytes(n.get('day_up', 0))}"
    tile(d, box, f, "网络", day)

    # Two small multiples: down and up each keep their own scale, so neither is
    # flattened by the other. Never a shared axis across wildly different rates.
    rows = (
        ("下载", "↓", n["down_bps"], n["down_hist"], n["down_peak"], C_DOWN),
        ("上传", "↑", n["up_bps"], n["up_hist"], n["up_peak"], C_UP),
    )
    top = y0 + TITLE_H
    row_h = (y1 - 6 - top) // 2
    label_w = min(148, (x1 - x0) // 3)
    # A short row cannot stack label / value / peak, so it goes single-line and
    # drops the peak — which the sparkline's own scale already implies.
    stacked = row_h >= 46

    for i, (name, arrow, cur, hist, peak, color) in enumerate(rows):
        ry = top + i * row_h
        if stacked:
            dot(d, ix0, ry + 3, color, 4)
            d.text((ix0 + 15, ry - 1), f"{arrow} {name}", font=f.meta, fill=INK2)
            d.text((ix0, ry + 14), fmt_rate(cur), font=f.value_xs, fill=INK)
            d.text((ix0, ry + 40), f"峰值 {fmt_rate(peak)}", font=f.tiny, fill=MUTED)
        else:
            dot(d, ix0, ry + 8, color, 4)
            d.text((ix0 + 14, ry), arrow, font=f.value_xs, fill=INK2)
            d.text((ix0 + 31, ry), fmt_rate(cur), font=f.value_xs, fill=INK)
        sh = row_h - 6
        if sh >= SPARK_MIN:
            spark(img, (ix0 + label_w, ry, ix1, ry + sh), hist, color,
                  vmax=max(peak, 64 * 1024))


# --- header ---------------------------------------------------------------

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


def _battery(d, f, x, y, batt: dict) -> float:
    """Handheld battery as a glyph + percentage. Returns the width drawn."""
    pct = batt.get("percent")
    if pct is None:
        return 0.0
    charging = bool(batt.get("charging"))
    if charging:
        color = S_GOOD
    elif pct <= 15:
        color = S_CRIT
    elif pct <= 30:
        color = S_WARN
    else:
        color = INK2

    bw, bh = 22, 11
    d.rounded_rectangle((x, y, x + bw, y + bh), radius=3, outline=color, width=1)
    d.rectangle((x + bw + 1, y + 3, x + bw + 2, y + bh - 3), fill=color)
    fill_w = max(1, int((bw - 4) * clamp01(pct / 100.0)))
    d.rectangle((x + 2, y + 2, x + 2 + fill_w, y + bh - 2), fill=color)

    text = f"{pct:.0f}%" + ("⚡" if charging else "")
    d.text((x + bw + 7, y - 2), text, font=f.meta, fill=color)
    return bw + 7 + d.textlength(text, font=f.meta)


def _header(d, f, s, w, devices=(), dev_idx: int = 0, battery=None) -> None:
    right = w - PAD - 2
    tw = d.textlength(s["time"], font=f.label)
    d.text((right, 3), s["time"], font=f.label, fill=INK, anchor="ra")
    right -= tw + 12

    if battery:
        bw = _battery(d, f, right - 60, 7, battery)
        if bw:
            right -= bw + 12
    else:
        dot(d, right - 8, 8, S_GOOD, 4)
        right -= 14

    left = PAD + 2
    avail = right - left
    if devices:
        _device_strip(d, f, left, 5, avail, devices, dev_idx)
    else:
        d.text((left, 5), ellipsize(d, f"PC 监控 · {s['host']}", f.label, avail),
               font=f.label, fill=INK2)


# --- layouts --------------------------------------------------------------
# Everything is on one screen, so the boxes below are a fixed grid rather than
# anything adaptive: each row's height is chosen to be the least that keeps its
# tile legible at arm's length on a 3.5" panel.

def _draw_landscape(img, d, f, s, devices=(), dev_idx=0, battery=None) -> None:
    w = LANDSCAPE[0]
    x0, x1 = PAD, w - PAD
    half = (x1 - x0 - GAP) // 2
    mx = x0 + half + GAP
    _header(d, f, s, w, devices, dev_idx, battery)

    _cpu_tile(img, d, f, s, (x0, 28, x1, 152))
    _fps_tile(img, d, f, s, (x0, 158, x0 + half, 256))
    _gpu_tile(img, d, f, s, (mx, 158, x1, 256))
    _proc_tile(img, d, f, (x0, 262, x0 + half, 350), "CPU 占用前三",
               s.get("top", []), C_CPU, "暂无")
    _proc_tile(img, d, f, (mx, 262, x1, 350), "GPU 占用前三",
               s.get("gpu_top", []), C_GPU, "暂无")
    _net_tile(img, d, f, s, (x0, 356, x1, 442))
    _mem_slim(img, d, f, s, (x0, 448, x1, 472))


def _draw_portrait(img, d, f, s, devices=(), dev_idx=0, battery=None) -> None:
    w = PORTRAIT[0]
    x0, x1 = PAD, w - PAD
    half = (x1 - x0 - GAP) // 2
    mx = x0 + half + GAP
    _header(d, f, s, w, devices, dev_idx, battery)

    _cpu_tile(img, d, f, s, (x0, 28, x1, 168))
    # Portrait is too narrow for the FPS trend line, so this tile is only as tall
    # as its headline needs; the height goes to GPU, which can show its VRAM.
    _fps_tile(img, d, f, s, (x0, 174, x1, 246))
    _gpu_tile(img, d, f, s, (x0, 252, x1, 352))
    _proc_tile(img, d, f, (x0, 358, x0 + half, 446), "CPU 前三",
               s.get("top", []), C_CPU, "暂无")
    _proc_tile(img, d, f, (mx, 358, x1, 446), "GPU 前三",
               s.get("gpu_top", []), C_GPU, "暂无")
    _net_tile(img, d, f, s, (x0, 452, x1, 604))
    _mem_slim(img, d, f, s, (x0, 610, x1, 634))


# orient counts quarter-turns clockwise as the user rotates the handheld, so the
# content is turned the opposite way to stay upright in their hands.
_CONTENT_ROTATION = {0: 0, 1: 270, 2: 180, 3: 90}


def draw_layout(snapshot: dict, fonts: Fonts, portrait: bool = False,
                devices=(), dev_idx: int = 0, battery=None) -> Image.Image:
    """The layout at its natural size and upright — for humans looking at a screen."""
    img = Image.new("RGB", PORTRAIT if portrait else LANDSCAPE, PLANE)
    d = ImageDraw.Draw(img)
    draw = _draw_portrait if portrait else _draw_landscape
    draw(img, d, fonts, snapshot, devices, dev_idx, battery)
    return img


def render(snapshot: dict, fonts: Fonts, orient: int = 0,
           panel_flip: bool = True, devices=(), dev_idx: int = 0,
           battery=None) -> Image.Image:
    """The frame as the handheld should receive it, mapped onto the panel."""
    orient %= 4
    img = draw_layout(snapshot, fonts, portrait=orient in (1, 3),
                      devices=devices, dev_idx=dev_idx, battery=battery)

    rotation = _CONTENT_ROTATION[orient]
    if rotation:
        img = img.rotate(rotation, expand=True)
    if panel_flip:
        img = img.transpose(Image.ROTATE_180)
    return img
