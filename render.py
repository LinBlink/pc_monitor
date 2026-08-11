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
import time

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
C_AI = (0, 150, 163)
C_PWR = (166, 118, 84)
C_DISK = (127, 118, 191)

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

# Candidates in preference order. The labels are Chinese, so a CJK face has to
# come first; the rest are fallbacks for machines that lack the YaHei collections
# (Windows Server installs, N editions). Nothing is bundled: these faces are not
# ours to redistribute.
_FONTS_BOLD = ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
               "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/Deng.ttf",
               "C:/Windows/Fonts/arialbd.ttf")
_FONTS_REG = ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/arial.ttf")


def _font(candidates: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        # Index 1 of the YaHei collections is the "UI" face; older copies lack it.
        for index in (1, 0):
            try:
                return ImageFont.truetype(path, size, index=index)
            except (OSError, ValueError):
                continue
    return ImageFont.load_default(size)


class Fonts:
    def __init__(self):
        self.hero = _font(_FONTS_BOLD, 52)
        self.hero_sm = _font(_FONTS_BOLD, 40)
        self.value = _font(_FONTS_BOLD, 32)
        self.value_sm = _font(_FONTS_BOLD, 26)
        self.value_xs = _font(_FONTS_BOLD, 21)
        self.label = _font(_FONTS_BOLD, 15)
        self.row = _font(_FONTS_BOLD, 13)
        self.sub = _font(_FONTS_REG, 13)
        self.meta = _font(_FONTS_REG, 12)
        self.tiny = _font(_FONTS_REG, 11)


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


NO_LINE_START = "，。、；：？！）》」』】…%°"


def wrap(draw, text: str, font, max_w: int, max_lines: int = 99) -> list[str]:
    """Break a paragraph to width, one character at a time.

    Character-wise rather than word-wise because the text this exists for is
    Chinese, which has no spaces to break at; an ASCII run that would be split
    mid-word is moved down to the next line instead, which keeps the occasional
    embedded process name readable. The last line is ellipsized rather than
    dropped, so a truncated paragraph never looks like a complete one.
    """
    lines: list[str] = []
    line = ""
    for ch in text.replace("\r", ""):
        if ch == "\n":
            lines.append(line)
            line = ""
            continue
        if not line or draw.textlength(line + ch, font=font) <= max_w:
            line += ch
            continue
        cut = len(line)
        if ch.isascii() and not ch.isspace():
            # Walk back to the start of the ASCII run, unless that is the whole
            # line — a single long token has to be broken somewhere.
            while cut > 0 and line[cut - 1].isascii() and not line[cut - 1].isspace():
                cut -= 1
            if cut == 0:
                cut = len(line)
        # Chinese punctuation never opens a line: pull the preceding character
        # down with it rather than leaving a comma hanging at the left margin.
        first = line[cut] if cut < len(line) else ch
        if first in NO_LINE_START and cut > 1:
            cut -= 1
        lines.append(line[:cut].rstrip())
        line = line[cut:] + ch
    if line:
        lines.append(line)

    if len(lines) > max_lines:
        tail = "".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [ellipsize(draw, tail, font, max_w)]
    return lines


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
    # A cell fits its clock in 24px: an 11px label, a 9px bar and the gap. Below
    # that the clocks go rather than shrink, since the bars are what you read.
    show_mhz = bool(mhz) and cw >= 30 and ch >= 24
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
    d.text((ix0, y), f"{c['percent']:.0f}%", font=f.value_sm, fill=INK)
    vw = d.textlength(f"{c['percent']:.0f}%", font=f.value_sm)

    # Temperature and package power sit beside the headline number: they are CPU
    # state, not a series of their own, so they take status ink and no hue.
    tx = ix0 + vw + 12
    if temp is not None:
        dot(d, tx, y + 9, _temp_color(temp), 4)
        d.text((tx + 13, y + 3), f"{temp:.0f}°C", font=f.value_xs, fill=INK2)
        tx += 13 + d.textlength(f"{temp:.0f}°C", font=f.value_xs) + 12
    if power is not None:
        d.text((tx, y + 3), f"{power:.0f} W", font=f.value_xs, fill=INK2)
    elif temp is None:
        d.text((tx, y + 7), "温度需 Afterburner", font=f.tiny, fill=MUTED)

    grid_top = y + 34
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

    # The big hero only earns its extra 12px when there is still room for the
    # trend line underneath it; otherwise the smaller one keeps both on screen.
    roomy = (y1 - y0) >= 96
    hero = f.hero if roomy else f.hero_sm
    y = y0 + TITLE_H - 4

    if v is None:
        d.text((ix0, y), "—", font=hero, fill=MUTED)
        if not fps["rtss"]:
            hint = ("请启动 MSI Afterburner / RTSS" if ix1 - ix0 >= 220
                    else "需 Afterburner / RTSS")
        else:
            hint = "前台没有游戏画面"
        d.text((ix0, y + (54 if roomy else 42)), ellipsize(d, hint, f.sub, ix1 - ix0),
               font=f.sub, fill=INK2)
        return

    d.text((ix0, y), f"{v:.0f}", font=hero, fill=state_color)
    hw = d.textlength(f"{v:.0f}", font=hero)
    d.text((ix0 + 5 + hw, y + (30 if roomy else 22)), "FPS", font=f.sub, fill=MUTED)
    # In a narrow tile the process name would ellipsize down to two letters, so
    # it yields to the frame time, which is short and always means something.
    room = ix1 - ix0 - hw - 42
    label = f"{fps['frametime_ms']:.1f} ms"
    if room >= 110 and fps["process"]:
        label += f" · {fps['process']}"
    d.text((ix0 + 5 + hw + 32, y + (30 if roomy else 22)),
           ellipsize(d, label, f.tiny, room), font=f.tiny, fill=MUTED)

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

    frac = g["mem_used_gb"] / g["mem_total_gb"] if g["mem_total_gb"] else 0.0
    label = f"显存 {g['mem_used_gb']:.1f} / {g['mem_total_gb']:.1f} GB"
    if y1 - y >= 26:
        d.text((ix0, y), label, font=f.tiny, fill=MUTED)
        meter(d, (ix0, y + 15, ix1, y + 22), frac, C_GPU)
    elif y1 - y >= 12:
        # Short tile: the VRAM label and its bar share one line rather than the
        # bar being dropped, because "how full is the card" is the reading this
        # tile exists for on a machine that is running a game.
        d.text((ix0, y), label, font=f.tiny, fill=MUTED)
        bx0 = ix0 + d.textlength(label, font=f.tiny) + 8
        if ix1 - bx0 >= 24:
            meter(d, (bx0, y + 3, ix1, y + 9), frac, C_GPU)


# --- AI quota -------------------------------------------------------------

def _until(ts: float | None) -> str:
    """How long until a quota window resets, at the precision that reads well."""
    if not ts:
        return ""
    rem = ts - time.time()
    if rem <= 0:
        return "即将"
    if rem >= 86400:
        return f"{rem / 86400:.0f}天后"
    return f"{int(rem // 3600)}:{int(rem % 3600 // 60):02d}后"


def _money(amount: float, currency: str) -> str:
    sym = {"USD": "$", "CNY": "¥", "RMB": "¥"}.get((currency or "").upper(), "")
    return f"{sym}{amount:.0f}" if abs(amount) >= 100 else f"{sym}{amount:.2f}"


def _quota_cell(label: str, window) -> tuple:
    """A percentage gauge, or a placeholder when the plan has no such window."""
    if not window or window.get("pct") is None:
        return (label, "—", None, MUTED)
    pct = window["pct"]
    # The bar keeps AI's own hue; only the number turns, the way the FPS hero
    # does, so a mark's colour never depends on its value.
    ink = S_CRIT if pct >= 100 else (S_WARN if pct >= 85 else INK)
    return (label, f"{pct:.0f}%", pct / 100.0, ink)


def _ai_cells(ai: dict) -> list[tuple]:
    """Five gauges in a fixed order — position is how they are found at a glance.

    Providers that are not configured keep their slot and show a dash rather
    than collapsing the row, so the layout never moves under the eye.

    The Opus and extra-usage windows are deliberately absent: neither is a
    quota you pace yourself against on this dashboard, and dropping them buys
    the remaining five gauges enough width to carry their reset countdowns.
    """
    claude = ai.get("claude") or {}
    cells = [_quota_cell("C 5h", claude.get("five_hour")),
             _quota_cell("C 7d", claude.get("seven_day"))]

    ds = ai.get("deepseek") or {}
    if not ds.get("ok") or ds.get("balance") is None:
        cells.append(("DS", "—", None, MUTED))
    else:
        on = bool(ds.get("available"))
        cells.append(("DS", _money(ds["balance"], ds.get("currency")),
                      1.0 if on else None, INK if on else S_CRIT))

    mm = ai.get("minimax") or {}
    for key, label in (("five_hour", "M 5h"), ("weekly", "M 周")):
        value = mm.get(key)
        cells.append(_quota_cell(
            label, None if value is None else
            {"pct": value, "resets_at": mm.get(key + "_reset")}))
    return cells


def _reset_ink(ts: float | None):
    """Reset countdowns are the one thing on this line you act on."""
    if not ts:
        return MUTED
    return S_WARN if ts - time.time() < 3600 else INK


def _ai_runs(ai: dict) -> list[list[tuple[str, tuple]]]:
    """The note beside the gauges, as chunks of coloured runs.

    Chunks are joined with a muted separator; within a chunk the label stays
    muted and the countdown takes ink, because at a uniform grey the reset time
    read as part of the label next to it and was effectively invisible.
    """
    claude = ai.get("claude")
    if not claude:
        return [[("读取中…", MUTED)]]
    if not claude.get("five_hour"):
        err = claude.get("err")
        text = {"no-creds": "未登录 Claude Code",
                "no-token": "凭据里没有令牌",
                "rate-limited": "接口限流，稍后重试",
                "offline": "连不上 Anthropic"}.get(err, f"Claude 出错：{err}")
        return [[(text, MUTED)]]

    plan = f"Claude {(claude.get('plan') or '').capitalize()}".strip()
    chunks: list[list[tuple[str, tuple]]] = [[(plan, MUTED)]]
    for key, name in (("five_hour", "5小时"), ("seven_day", "7天")):
        window = claude.get(key)
        if window and window.get("resets_at"):
            ts = window["resets_at"]
            chunks.append([(name + " ", MUTED),
                           (_until(ts) + "重置", _reset_ink(ts))])
    if not claude.get("ok"):
        chunks.append([("数据可能过期", S_WARN)])
    return chunks


def _draw_runs(d, x, y, chunks, font, limit: float) -> None:
    """Coloured runs on one line, separated by a muted dot, truncated to fit."""
    for i, chunk in enumerate(chunks):
        parts = ([(" · ", MUTED)] if i else []) + list(chunk)
        width = sum(d.textlength(t, font=font) for t, _ in parts)
        if x + width > limit:
            return
        for text, ink in parts:
            d.text((x, y), text, font=font, fill=ink)
            x += d.textlength(text, font=font)


# A window's own length, so how far into it we are can be worked out from the
# reset time alone — the API reports when it ends, never when it began.
_WINDOW_S = {"five_hour": 5 * 3600.0, "seven_day": 7 * 86400.0}


def _pace(window, length: float):
    """(burn rate, elapsed fraction, percent used) for one quota window.

    Burn rate is usage divided by how much of the window has gone by, so 1.0 is
    exactly even pacing, 2.0 is twice as fast as the window can sustain. Returns
    None in the first few percent of a window, where the ratio is dominated by
    whatever happened in the last minute.
    """
    if not window:
        return None
    pct, resets_at = window.get("pct"), window.get("resets_at")
    if pct is None or not resets_at:
        return None
    frac = 1.0 - (resets_at - time.time()) / length
    if not 0.08 <= frac <= 1.0:
        return None
    return pct / 100.0 / frac, frac, pct


def _ai_hint(ai: dict):
    """Whether to ease off or spend freely, as (text, ink) — or None if neither.

    The judgement is about pace, not level: 60% used is alarming three hours
    into a five-hour window and reassuring six days into a seven-day one. Only
    the two windows that actually gate work are considered, and the more urgent
    of them wins, because it is the one that will stop you first.
    """
    claude = ai.get("claude") or {}
    rows = [r for r in (_pace(claude.get(key), length)
                        for key, length in _WINDOW_S.items()) if r]
    if not rows:
        return None

    burn, _frac, pct = max(rows, key=lambda r: r[0])
    # A high ratio on a barely-touched window is arithmetic, not a warning.
    if burn >= 1.25 and pct >= 20:
        return ("用得偏快，建议节制", S_CRIT if burn >= 1.6 else S_WARN)

    burn, frac, _pct = min(rows, key=lambda r: r[0])
    # Unused quota expires at the reset, so late in a window an untouched one is
    # something to spend, not something to protect.
    if burn <= 0.6 and frac >= 0.35:
        return ("额度富余，可尽快使用", S_GOOD)
    return None


def _ai_tile(img, d, f, s, box) -> None:
    """Every quota this machine can see, as a row of small gauges.

    Two shapes from one renderer: given enough height the tile takes a title and
    puts the reset countdowns on a line of their own; in the landscape strip it
    drops the title bar and folds the title into that same line, so the gauges
    keep their full height either way.
    """
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    ai = s.get("ai") or {}
    cells = _ai_cells(ai)
    chunks = _ai_runs(ai)
    hint = _ai_hint(ai)
    roomy = (y1 - y0) >= 56

    if roomy:
        tile(d, box, f, "AI 额度")
        top, bottom = y0 + TITLE_H, y1 - 18
    else:
        d.rounded_rectangle(box, radius=7, fill=SURFACE, outline=BORDER, width=1)
        top, bottom = y0 + 4, y1 - 16
        chunks = [[("AI 额度", INK2)]] + chunks

    cw = (ix1 - ix0) / len(cells)
    row_h = bottom - top

    for i, (label, value, frac, ink) in enumerate(cells):
        cx = ix0 + i * cw
        cell_w = cw - 10
        vw = d.textlength(value, font=f.row)
        d.text((cx, top + 1), ellipsize(d, label, f.tiny, cell_w - vw - 5),
               font=f.tiny, fill=MUTED)
        d.text((cx + cell_w, top), value, font=f.row, fill=ink, anchor="ra")
        # Bottom-anchored so the bar survives the shortest row either layout
        # asks for; below that there is no room for it at all.
        by = top + row_h - 6
        if row_h >= 20:
            if frac is None:
                d.rounded_rectangle((cx, by, cx + cell_w, by + 6), radius=3,
                                    fill=blend(C_AI, SURFACE, 0.16))
            else:
                meter(d, (cx, by, cx + cell_w, by + 6), frac, C_AI)

    # The hint is the only coloured text on the line, and it is anchored right so
    # it holds still while the countdowns beside it change width every second.
    note_y = y1 - 15
    limit = ix1
    if hint:
        text, ink = hint
        d.text((ix1, note_y), text, font=f.tiny, fill=ink, anchor="ra")
        limit = ix1 - d.textlength(text, font=f.tiny) - 12
    _draw_runs(d, ix0, note_y, chunks, f.tiny, limit)


AI_ROW_H = 20


def _ai_detail_rows(ai: dict) -> list[tuple]:
    """Every quota window in full, as (label, value, fraction, ink, reset).

    This is where the windows page 1 has no room for end up — Opus, extra usage,
    and MiniMax's per-model groups. Page 1 answers "am I about to run out";
    this answers "out of what, exactly, and when does it come back".
    """
    rows: list[tuple] = []
    claude = ai.get("claude") or {}
    for key, name in (("five_hour", "Claude 5 小时"),
                      ("seven_day", "Claude 7 天"),
                      ("seven_day_opus", "Claude 7 天 Opus")):
        window = claude.get(key) or {}
        label, value, frac, ink = _quota_cell(name, claude.get(key))
        rows.append((label, value, frac, ink, window.get("resets_at")))

    extra = claude.get("extra") or {}
    if extra.get("used") is not None:
        on = bool(extra.get("enabled"))
        rows.append(("Claude 额外用量",
                     _money(extra["used"], extra.get("currency")),
                     1.0 if on else None, INK if on else INK2, None))

    ds = ai.get("deepseek") or {}
    if ds.get("balance") is not None:
        on = bool(ds.get("available"))
        rows.append(("DeepSeek 余额", _money(ds["balance"], ds.get("currency")),
                     1.0 if on else None, INK if on else S_CRIT, None))

    mm = ai.get("minimax") or {}
    groups = mm.get("models") or ([{"name": mm.get("model") or "general",
                                    "five_hour": mm.get("five_hour"),
                                    "weekly": mm.get("weekly"),
                                    "five_hour_reset": mm.get("five_hour_reset"),
                                    "weekly_reset": mm.get("weekly_reset")}]
                                  if mm.get("ok") else [])
    for group in groups:
        for key, suffix in (("five_hour", "5 小时"), ("weekly", "周")):
            value = group.get(key)
            if value is None:
                continue
            label, text, frac, ink = _quota_cell(
                f"MiniMax {group.get('name') or '?'} {suffix}", {"pct": value})
            rows.append((label, text, frac, ink, group.get(key + "_reset")))
    return rows


def _ai_detail_tile(img, d, f, s, box) -> None:
    """The full quota table, one window per row with its own reset countdown."""
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    rows = _ai_detail_rows(s.get("ai") or {})
    tile(d, box, f, "AI 额度明细")

    top = y0 + TITLE_H
    fits = max(0, int((y1 - 6 - top) // AI_ROW_H))
    rows = rows[:fits]
    # One shared left edge for every bar, and one shared right edge: a track
    # whose length depended on how long its label happened to be would make the
    # rows impossible to compare, which is the only reason to draw bars at all.
    bx0 = ix0 + max([d.textlength(r[0], font=f.tiny) for r in rows] or [0]) + 12
    bx1 = ix1 - max([d.textlength(r[1], font=f.row) for r in rows] or [0]) - 62

    for i, (label, value, frac, ink, reset) in enumerate(rows):
        ry = top + i * AI_ROW_H
        d.text((ix0, ry + 3), label, font=f.tiny, fill=INK2)
        d.text((ix1, ry + 1), value, font=f.row, fill=ink, anchor="ra")

        until = _until(reset)
        if until:
            d.text((bx1 + 8, ry + 3), until, font=f.tiny,
                   fill=_reset_ink(reset))

        if bx1 - bx0 >= 30:
            if frac is None:
                d.rounded_rectangle((bx0, ry + 6, bx1, ry + 12), radius=3,
                                    fill=blend(C_AI, SURFACE, 0.16))
            else:
                meter(d, (bx0, ry + 6, bx1, ry + 12), frac, C_AI)


def _ai_detail_height(s: dict, avail: int) -> int:
    rows = len(_ai_detail_rows(s.get("ai") or {}))
    return max(0, min(avail, TITLE_H + AI_ROW_H * rows + 8))


# --- weather --------------------------------------------------------------

def _weather_slim(img, d, f, s, box) -> None:
    """Now, +3h, +6h and the next two days on one 24px line.

    No icons: there is no colour emoji face we can rely on being installed, and
    a two-character word is both narrower and unambiguous at this size.
    """
    x0, y0, x1, y1 = box
    w = s.get("weather") or {}
    d.rounded_rectangle(box, radius=7, fill=SURFACE, outline=BORDER, width=1)
    cy = (y0 + y1) // 2

    if not w.get("ok"):
        d.text((x0 + PADI, cy), f"天气 {w.get('err') or '—'}", font=f.meta,
               fill=MUTED, anchor="lm")
        return

    def temp(v) -> str:
        return "—" if v is None else f"{v:.0f}°"

    now = w.get("now") or {}
    segments = [(f"{now.get('text') or ''} {temp(now.get('temp'))}", INK)]
    for key, name in (("h3", "3h"), ("h6", "6h")):
        block = w.get(key)
        if block:
            segments.append((f"{name} {temp(block.get('temp'))}", INK2))
    for key, name in (("d1", "明"), ("d2", "后")):
        block = w.get(key)
        if block:
            segments.append((f"{name} {temp(block.get('tmin'))[:-1]}~"
                             f"{temp(block.get('tmax'))}", INK2))

    # Draw what fits and stop: the tail of this line is the least urgent part of
    # the whole dashboard, so it yields rather than pushing anything else out.
    cx = x0 + PADI
    limit = x1 - PADI
    for text, ink in segments:
        width = d.textlength(text, font=f.tiny)
        if cx + width > limit:
            break
        d.text((cx, cy), text, font=f.tiny, fill=ink, anchor="lm")
        cx += width + 9


# --- AI advice ------------------------------------------------------------

def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    rem = time.time() - ts
    if rem < 90:
        return "刚刚"
    if rem < 5400:
        return f"{int(rem // 60)} 分钟前"
    return f"{int(rem // 3600)} 小时前"


ADVICE_LINE_H = 18


def _advice_height(d, f, s, width: int, avail: int) -> int:
    """As tall as the paragraph needs, never taller.

    The advice is one line on a healthy machine and a short paragraph on a sick
    one, so a box sized for the worst case would usually be a large empty panel.
    Ending the tile early leaves plain background instead, which reads as
    "nothing more to show" rather than "something failed to load".
    """
    text = (s.get("advice") or {}).get("text") or ""
    lines = len(wrap(d, text, f.sub, width - 2 * PADI - 16)) if text else 1
    return max(56, min(avail, TITLE_H + ADVICE_LINE_H * lines + 10))


def _advice_tile(img, d, f, s, box) -> None:
    """The last thing the advisor said, wrapped to the tile.

    Silence is the normal state: when nothing is wrong the advisor is asked to
    say so in one word, and this shows that rather than inventing filler, so a
    paragraph on screen always means something actually wanted attention.
    """
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    a = s.get("advice") or {}

    meta = " · ".join(p for p in (a.get("provider") or "", _ago(a.get("at"))) if p)
    tile(d, box, f, "AI 建议", meta)

    body = y0 + TITLE_H
    if not a.get("enabled"):
        d.text((ix0, body), "未开启（在设置页打开）", font=f.sub, fill=MUTED)
        return
    text = a.get("text") or ""
    if not text:
        d.text((ix0, body), a.get("err") or "还没有分析结果", font=f.sub, fill=MUTED)
        return

    warn = a.get("level") == "warn"
    dot(d, ix0, body + 4, S_WARN if warn else S_GOOD, 4)
    lines = wrap(d, text, f.sub, ix1 - ix0 - 16,
                 max_lines=max(1, int((y1 - 6 - body) // ADVICE_LINE_H)))
    for i, line in enumerate(lines):
        d.text((ix0 + 16, body + i * ADVICE_LINE_H), line, font=f.sub,
               fill=INK if warn else INK2)


# --- process tables -------------------------------------------------------

def _as_pct(value: float) -> str:
    return f"{value:.0f}%" if value >= 10 else f"{value:.1f}%"


def _as_mb(value: float) -> str:
    """Process memory: gigabytes once it gets there, so the column stays short."""
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


def _proc_tile(img, d, f, box, title, rows, color, empty: str, fmt=_as_pct,
               meta: str = "") -> None:
    """Top consumers as a small ranked table: mark, name, share."""
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    tile(d, box, f, title, meta)

    if not rows:
        d.text((ix0, y0 + TITLE_H + 2), empty, font=f.tiny, fill=MUTED)
        return

    top = y0 + TITLE_H
    avail = y1 - 6 - top
    n = min(len(rows), 3)
    row_h = min(24, avail / max(1, n))
    for i, (name, value) in enumerate(rows[:n]):
        ry = top + i * row_h
        dot(d, ix0, ry + row_h / 2 - 3, color, 3)
        value_text = fmt(value)
        pw = d.textlength(value_text, font=f.row)
        d.text((ix1, ry + row_h / 2), value_text, font=f.row, fill=INK, anchor="rm")
        d.text((ix0 + 12, ry + row_h / 2),
               ellipsize(d, name, f.meta, ix1 - ix0 - pw - 22),
               font=f.meta, fill=INK2, anchor="lm")


# --- energy ---------------------------------------------------------------

def _power_strip(img, d, f, s, box) -> None:
    """Draw right now, and what it has added up to over three windows.

    Laid out along a line rather than as a table: the windows nest — today is
    inside the week is inside the month — so the only relationship between them
    is order, and reading left to right in increasing span says exactly that.

    Where the line does not fit, the windows drop to a second row rather than
    being truncated. The 30-day figure is the one this tile exists for, and a
    strip that quietly stopped after "近 7 天" would be worse than useless.
    """
    x0, y0, x1, y1 = box
    p = s.get("power") or {}
    d.rounded_rectangle(box, radius=7, fill=SURFACE, outline=BORDER, width=1)
    limit = x1 - PADI

    days = int(p.get("days") or 0)
    meta = "估算" if p.get("estimated") else ""
    if days < 30:
        meta = (meta + " · " if meta else "") + f"已记录 {days} 天"

    windows = [("今日", f"{float(p.get('d1') or 0.0):.2f}"),
               ("近 7 天", f"{float(p.get('d7') or 0.0):.2f}"),
               ("近 30 天", f"{float(p.get('d30') or 0.0):.2f} kWh")]
    cost = p.get("cost30")
    if cost:
        windows.append(("电费", _money(cost, "CNY")))

    def window_w(row) -> float:
        return sum(d.textlength(n, font=f.tiny) + 5
                   + d.textlength(v, font=f.row) + 16 for n, v in row)

    watts = f"{float(p.get('watts') or 0.0):.0f}"
    head_w = (d.textlength("耗电量", font=f.label) + 12 + 13
              + d.textlength(watts, font=f.value_xs) + 25)
    meta_w = (d.textlength(meta, font=f.tiny) + 14) if meta else 0.0

    # Two rows only when one will not do and the box is tall enough for them.
    stacked = (head_w + window_w(windows) + meta_w > limit - x0 - PADI
               and y1 - y0 >= 40)
    head_y = y0 + 15 if stacked else (y0 + y1) // 2
    row_y = y1 - 14 if stacked else head_y

    if meta:
        d.text((limit, head_y), meta, font=f.tiny, fill=MUTED, anchor="rm")
        if not stacked:
            limit -= d.textlength(meta, font=f.tiny) + 14

    cx = x0 + PADI
    d.text((cx, head_y), "耗电量", font=f.label, fill=INK2, anchor="lm")
    cx += d.textlength("耗电量", font=f.label) + 12

    dot(d, cx, head_y - 4, C_PWR, 4)
    cx += 13
    d.text((cx, head_y), watts, font=f.value_xs, fill=INK, anchor="lm")
    cx += d.textlength(watts, font=f.value_xs) + 3
    d.text((cx, head_y + 3), "W", font=f.tiny, fill=MUTED, anchor="lm")
    cx += 22

    if stacked:
        cx = x0 + PADI
    for name, value in windows:
        d.text((cx, row_y + 1), name, font=f.tiny, fill=MUTED, anchor="lm")
        cx += d.textlength(name, font=f.tiny) + 5
        d.text((cx, row_y), value, font=f.row, fill=INK, anchor="lm")
        cx += d.textlength(value, font=f.row) + 16


# --- disk -----------------------------------------------------------------

def _disk_temp_color(dk: dict):
    """Status hue for a drive temperature, against thresholds it can survive.

    A drive's own declared warning is the point at which it starts throttling —
    82°C on this machine — so honouring it literally would leave the dot green
    at 75°C, which is not a calm number for an NVMe. The declared values are
    used as a ceiling and capped at temperatures that mean something to a person.
    """
    temp = dk.get("temp_c")
    if temp is None:
        return MUTED
    crit = min(dk.get("temp_crit") or 85.0, 75.0)
    warn = min(dk.get("temp_warn") or 80.0, 65.0)
    if temp >= crit:
        return S_CRIT
    if temp >= warn:
        return S_WARN
    return S_GOOD


def _disk_tile(img, d, f, s, box) -> None:
    """System drive: how hot it is, how hard it is working, how full it is."""
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    dk = s.get("disk") or {}
    letter = dk.get("letter") or "C"

    used_pct = dk.get("used_pct")
    meta = f"{used_pct:.0f}% 已用" if used_pct is not None else ""
    tile(d, box, f, f"磁盘 {letter}:", meta)

    if not dk.get("ok"):
        d.text((ix0, y0 + TITLE_H + 4), dk.get("err") or "—", font=f.sub,
               fill=MUTED)
        return

    y = y0 + TITLE_H
    temp = dk.get("temp_c")
    dot(d, ix0, y + 8, _disk_temp_color(dk), 4)
    if temp is None:
        # Not every drive has a sensor, and a volume spanning two disks has no
        # single temperature; say so rather than showing a hopeful dash.
        d.text((ix0 + 14, y + 4), "无温度读数", font=f.tiny, fill=MUTED)
    else:
        d.text((ix0 + 14, y), f"{temp:.0f}°C", font=f.value_xs, fill=INK)
    if dk.get("total_gb"):
        d.text((ix1, y + 5), f"{dk['used_gb']:.0f} / {dk['total_gb']:.0f} GB",
               font=f.tiny, fill=MUTED, anchor="ra")

    y += 28
    # Read and write share the drive's one hue: they are the same device, told
    # apart by the arrow, and a second hue here would collide with the network
    # tile's up/down pair sitting a few rows away.
    read = f"↓ {fmt_rate(float(dk.get('read_bps') or 0.0))}"
    write = f"↑ {fmt_rate(float(dk.get('write_bps') or 0.0))}"
    d.text((ix0, y), read, font=f.row, fill=INK)
    if ix1 - ix0 - d.textlength(read, font=f.row) > d.textlength(write, font=f.row) + 8:
        d.text((ix1, y), write, font=f.row, fill=INK, anchor="ra")
    else:
        y += 16
        d.text((ix0, y), write, font=f.row, fill=INK)

    y += 18
    if used_pct is not None and y1 - y >= 6:
        meter(d, (ix0, y, ix1, y + min(7, y1 - y)), used_pct / 100.0, C_DISK)


# --- docker ---------------------------------------------------------------

def _docker_state(state: str):
    """Container state as a status hue — running, paused/created, or stopped."""
    if state == "running":
        return S_GOOD
    if state in ("exited", "dead"):
        return MUTED
    return S_WARN


def _docker_tile(img, d, f, s, box) -> None:
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + PADI, x1 - PADI
    dk = s.get("docker") or {}
    rows = dk.get("containers") or []

    meta = f"{dk.get('running', 0)}/{dk.get('total', 0)} 运行中" if dk.get("ok") else ""
    tile(d, box, f, "Docker 容器", meta)

    if not dk.get("ok"):
        d.text((ix0, y0 + TITLE_H + 4), dk.get("err") or "—", font=f.sub,
               fill=MUTED)
        return
    if not rows:
        d.text((ix0, y0 + TITLE_H + 4), "没有容器", font=f.sub, fill=MUTED)
        return

    top = y0 + TITLE_H
    row_h = 22
    fits = max(1, int((y1 - 6 - top) // row_h))
    for i, c in enumerate(rows[:fits]):
        ry = top + i * row_h
        # The mark is the container's state, which is exactly what status hues
        # are for; the numbers beside it stay in ink.
        dot(d, ix0, ry + row_h / 2 - 3, _docker_state(c.get("state") or ""), 3)

        cpu, mem = c.get("cpu"), c.get("mem_mb")
        right = ""
        if cpu is not None:
            right = _as_pct(cpu)
        if mem is not None:
            right = (right + "  " if right else "") + _as_mb(mem)
        if not right:
            right = ellipsize(d, c.get("status") or "", f.tiny, (x1 - x0) * 0.4)
        rw = d.textlength(right, font=f.row if cpu is not None else f.tiny)
        d.text((ix1, ry + row_h / 2), right,
               font=f.row if cpu is not None else f.tiny,
               fill=INK if cpu is not None else MUTED, anchor="rm")
        d.text((ix0 + 12, ry + row_h / 2),
               ellipsize(d, c.get("name") or "?", f.meta, ix1 - ix0 - rw - 24),
               font=f.meta, fill=INK2, anchor="lm")

    hidden = len(rows) - fits
    if hidden > 0:
        d.text((ix1, y1 - 14), f"还有 {hidden} 个", font=f.tiny, fill=MUTED,
               anchor="ra")


def _docker_slim(img, d, f, s, box) -> None:
    """One line for the machines that have no Docker — which is most of them."""
    x0, y0, x1, y1 = box
    dk = s.get("docker") or {}
    d.rounded_rectangle(box, radius=7, fill=SURFACE, outline=BORDER, width=1)
    cy = (y0 + y1) // 2
    d.text((x0 + PADI, cy), f"Docker {dk.get('err') or '—'}", font=f.meta,
           fill=MUTED, anchor="lm")


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


def _weather_chip(d, f, s, right, y, room: float) -> float:
    """Condition and temperature now, beside the clock. Returns the width used.

    Page 1 has no spare row for the forecast strip any more, and the header does
    have spare width — but only while the device list is short, so this yields
    the moment the chips need the space. The full forecast is on page 2.
    """
    w = s.get("weather") or {}
    now = w.get("now") or {}
    if not w.get("ok") or now.get("temp") is None:
        return 0.0
    text = f"{now.get('text') or ''} {now['temp']:.0f}°".strip()
    width = d.textlength(text, font=f.meta)
    if width > room:
        return 0.0
    d.text((right, y), text, font=f.meta, fill=INK2, anchor="ra")
    return width


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
    # The chips get first claim on what is left: the device you are looking at
    # has to stay legible, and the weather never earns a truncated name.
    ww = _weather_chip(d, f, s, right, 5, max(0.0, right - left - 150))
    if ww:
        right -= ww + 12

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

def _mem_meta(s: dict) -> str:
    """Whole-machine memory, carried by the top-three tile's own title row.

    It used to have a bar of its own at the foot of the page. Two readings about
    memory a screen apart was the worse arrangement, and folding the total into
    the header of the table that breaks it down freed a whole row.
    """
    m = s.get("mem") or {}
    if not m.get("total_gb"):
        return ""
    return f"{m['used_gb']:.1f} / {m['total_gb']:.0f} GB · {m['percent']:.0f}%"


def _draw_landscape(img, d, f, s, devices=(), dev_idx=0, battery=None) -> None:
    w = LANDSCAPE[0]
    x0, x1 = PAD, w - PAD
    third = (x1 - x0 - 2 * GAP) // 3
    ax, bx = x0 + third + GAP, x0 + 2 * (third + GAP)
    _header(d, f, s, w, devices, dev_idx, battery)

    _cpu_tile(img, d, f, s, (x0, 28, x1, 140))
    # FPS and GPU gave up a row's worth of height between them to make room for
    # the three tiles below. FPS loses its trend line at this size, which is the
    # right thing to lose: it is blank whenever no game is running, which is most
    # of the time, while every tile it paid for is always showing something.
    _fps_tile(img, d, f, s, (x0, 146, x0 + third, 222))
    _gpu_tile(img, d, f, s, (ax, 146, ax + third, 222))
    _proc_tile(img, d, f, (bx, 146, x1, 222), "内存前三",
               s.get("mem_top", []), C_MEM, "暂无", _as_mb, _mem_meta(s))
    _proc_tile(img, d, f, (x0, 228, x0 + third, 304), "CPU 前三",
               s.get("top", []), C_CPU, "暂无")
    _proc_tile(img, d, f, (ax, 228, ax + third, 304), "GPU 前三",
               s.get("gpu_top", []), C_GPU, "暂无")
    _disk_tile(img, d, f, s, (bx, 228, x1, 304))
    _net_tile(img, d, f, s, (x0, 310, x1, 378))
    _ai_tile(img, d, f, s, (x0, 384, x1, 428))
    _power_strip(img, d, f, s, (x0, 434, x1, 472))


def _draw_portrait(img, d, f, s, devices=(), dev_idx=0, battery=None) -> None:
    w = PORTRAIT[0]
    x0, x1 = PAD, w - PAD
    half = (x1 - x0 - GAP) // 2
    mx = x0 + half + GAP
    _header(d, f, s, w, devices, dev_idx, battery)

    _cpu_tile(img, d, f, s, (x0, 28, x1, 140))
    # Standing these two side by side is what pays for the disk and memory
    # tiles below; at this height FPS still keeps its trend line and GPU still
    # keeps its VRAM bar, so nothing is actually lost to the move.
    _fps_tile(img, d, f, s, (x0, 146, x0 + half, 234))
    _gpu_tile(img, d, f, s, (mx, 146, x1, 234))
    _proc_tile(img, d, f, (x0, 240, x0 + half, 328), "CPU 前三",
               s.get("top", []), C_CPU, "暂无")
    _proc_tile(img, d, f, (mx, 240, x1, 328), "GPU 前三",
               s.get("gpu_top", []), C_GPU, "暂无")
    _proc_tile(img, d, f, (x0, 334, x0 + half, 422), "内存前三",
               s.get("mem_top", []), C_MEM, "暂无", _as_mb, _mem_meta(s))
    _disk_tile(img, d, f, s, (mx, 334, x1, 422))
    _net_tile(img, d, f, s, (x0, 428, x1, 504))
    # Portrait has the height landscape does not, so the quota tile takes a
    # title bar here and puts the reset countdowns on a line of their own.
    _ai_tile(img, d, f, s, (x0, 510, x1, 580))
    _power_strip(img, d, f, s, (x0, 586, x1, 632))


# --- page 2: detail -------------------------------------------------------
# Docker is the reason this page exists but most machines have none, and a
# half-screen panel saying "未安装 Docker" would be the worst use of the space on
# the whole dashboard. So there are two grids rather than one: the container list
# takes the top when there is something to list, and shrinks to a single line at
# the bottom when there is not, with the height going to the advice paragraph —
# which is the tile that can always use more.

SLIM_H = 24
DOCK_ROW_H = 22


def _has_docker(s: dict) -> bool:
    return bool((s.get("docker") or {}).get("ok"))


def _docker_height(s: dict, cap: int) -> int:
    """Just enough rows for the containers there are, up to what the page has.

    Page 1's grid is fixed because every tile on it always has something to show.
    Here the container count is whatever this machine happens to run, and a tile
    sized for ten when there are three would take the space out of the advice
    paragraph for nothing.
    """
    rows = len((s.get("docker") or {}).get("containers") or [])
    return max(64, min(cap, TITLE_H + DOCK_ROW_H * max(rows, 1) + 8))


def _draw_detail(img, d, f, s, size, dock_cap: int, devices, dev_idx,
                 battery) -> None:
    """Page 2 in either orientation — the same stack, only the width differs.

    Unlike page 1 there is no grid here, because every tile on this page varies:
    the container list is however many containers this machine runs, and the
    advice is one line or a paragraph. Both are sized to their content and the
    leftover is simply left blank, which reads as "nothing more to show" rather
    than as a panel that failed to load.
    """
    w, h = size
    x0, x1 = PAD, w - PAD
    bottom = h - PAD
    _header(d, f, s, w, devices, dev_idx, battery)

    # The forecast strip lives here now that page 1 only has room for the
    # current conditions in its header.
    _weather_slim(img, d, f, s, (x0, bottom - SLIM_H, x1, bottom))
    bottom -= SLIM_H + GAP

    y = 28
    if _has_docker(s):
        h = _docker_height(s, dock_cap)
        _docker_tile(img, d, f, s, (x0, y, x1, y + h))
        y += h + GAP
    else:
        # No containers to list, so the answer is one line at the foot of the
        # page and the height goes to the tile that can always use more.
        _docker_slim(img, d, f, s, (x0, bottom - SLIM_H, x1, bottom))
        bottom -= SLIM_H + GAP

    advice_h = _advice_height(d, f, s, x1 - x0, bottom - y)
    _advice_tile(img, d, f, s, (x0, y, x1, y + advice_h))
    y += advice_h + GAP

    # The windows page 1 has no room for land here, but only if there is room
    # left after the two tiles this page is actually about. A machine running
    # twenty containers gets the container list; a machine running none gets
    # the quota breakdown instead of half a screen of background.
    detail_h = _ai_detail_height(s, bottom - y)
    if detail_h >= TITLE_H + 3 * AI_ROW_H:
        _ai_detail_tile(img, d, f, s, (x0, y, x1, y + detail_h))


def _draw_landscape_detail(img, d, f, s, devices=(), dev_idx=0,
                           battery=None) -> None:
    _draw_detail(img, d, f, s, LANDSCAPE, 300, devices, dev_idx, battery)


def _draw_portrait_detail(img, d, f, s, devices=(), dev_idx=0,
                          battery=None) -> None:
    _draw_detail(img, d, f, s, PORTRAIT, 420, devices, dev_idx, battery)


# Page order is the order UP and DOWN walk through on the handheld.
_PAGES = ((_draw_landscape, _draw_portrait),
          (_draw_landscape_detail, _draw_portrait_detail))
PAGE_COUNT = len(_PAGES)

# orient counts quarter-turns clockwise as the user rotates the handheld, so the
# content is turned the opposite way to stay upright in their hands.
_CONTENT_ROTATION = {0: 0, 1: 270, 2: 180, 3: 90}


def draw_layout(snapshot: dict, fonts: Fonts, portrait: bool = False,
                devices=(), dev_idx: int = 0, battery=None,
                page: int = 0) -> Image.Image:
    """The layout at its natural size and upright — for humans looking at a screen."""
    img = Image.new("RGB", PORTRAIT if portrait else LANDSCAPE, PLANE)
    d = ImageDraw.Draw(img)
    draw = _PAGES[page % PAGE_COUNT][1 if portrait else 0]
    draw(img, d, fonts, snapshot, devices, dev_idx, battery)
    return img


def render(snapshot: dict, fonts: Fonts, orient: int = 0,
           panel_flip: bool = True, devices=(), dev_idx: int = 0,
           battery=None, page: int = 0) -> Image.Image:
    """The frame as the handheld should receive it, mapped onto the panel."""
    orient %= 4
    img = draw_layout(snapshot, fonts, portrait=orient in (1, 3),
                      devices=devices, dev_idx=dev_idx, battery=battery,
                      page=page)

    rotation = _CONTENT_ROTATION[orient]
    if rotation:
        img = img.rotate(rotation, expand=True)
    if panel_flip:
        img = img.transpose(Image.ROTATE_180)
    return img
