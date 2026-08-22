"""MJPEG dashboard server for the Miyoo Mini handheld.

One producer thread samples telemetry and encodes a JPEG at a fixed rate; every
connected client is then fed the latest frame. Rendering cost is therefore
independent of how many clients are watching.

Settings are live-editable from /settings and persisted to config.json. Changing
the refresh rate bumps a generation counter, which drops streaming clients so the
handheld's retry loop reconnects and picks the new rate up — that is why the
handheld never needs its own copy of the frame rate.

Endpoints
    /              the settings page
    /settings      GET the page, POST a form to apply
    /stream.mjpg   raw concatenated JPEGs — what ffplay on the handheld reads.
                   ?orient=0..3 picks the layout, ?page=0..1 the page,
                   ?theme=dark|term the palette, and ?devs=a,b&i=0 draws the
                   handheld's own device switcher
    /preview.mjpg  multipart/x-mixed-replace — what a browser <img> reads
    /preview       live preview of the dashboard
    /hd            the same dashboard as a resolution-independent web page,
                   driven from the keyboard — for handhelds that run Windows
    /frame.jpg     a single current frame
    /config.json   effective settings, read by the handheld at launch
    /stats.json    the raw snapshot, for building other clients
    /hosts.json    the other PC Monitors on the LAN, swept for here because a
                   browser cannot probe a port itself
    /battery       the handheld reports its own charge level here
    /advice.json   the latest AI read on how the machine is running
    /alert.json    the latest AI 5-hour quota warning
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import html
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import metrics
import paths
import render
import theme
import webui

CONFIG_PATH = os.path.join(paths.state_dir(), "config.json")

IS_WINDOWS = os.name == "nt"

DEFAULTS = {
    "port": 8765,
    "fps": 8,
    "jpeg_quality": 72,
    "rotate180": True,
    "weather_city": "",
    "weather_lat": None,
    "weather_lon": None,
    "deepseek_key": "",
    "minimax_key": "",
    "minimax_region": "cn",
    # Nothing can read the wall socket, so the energy estimate needs to be told
    # what the sensors cannot see: the rest of the machine, the supply's losses,
    # and a TDP to fall back on when Afterburner is not running.
    "power_base_w": 45,
    "power_psu_pct": 90,
    "cpu_tdp_w": 65,
    "power_price": None,
    # Off by default: it spends someone's API quota, and a monitor that starts
    # talking to you unasked is not a good first impression.
    "advice_enabled": False,
    "advice_every_min": 30,
    # Which volume the disk tile watches. Almost always the system drive: a
    # drive letter on Windows, a mount point on Linux.
    "disk_letter": "C" if IS_WINDOWS else "/",
    # The palette everything is drawn in unless a client asks for another one.
    "theme": theme.DEFAULT,
    # Running out of the 5-hour window mid-task is the failure this warns about,
    # so unlike the advisor it is on by default: it costs nothing until the day
    # you are actually about to hit the wall.
    "ai_alert_enabled": True,
    "ai_alert_pct": 80,
    # --- the web dashboard at /hd ---
    # How often the page asks for /stats.json. It is a number rather than the
    # stream's fps because nothing is being encoded for it: the cost of a tick is
    # one small JSON response, so a handheld on battery can slow it right down
    # without the picture getting worse, which is not true of the MJPEG stream.
    "web_refresh_ms": 1000,
    # The page can watch another PC on the LAN, so it needs to know which ones
    # there are. The sweep happens here rather than in the browser, which has no
    # way to probe a port. Off for anyone who would rather this program not
    # touch addresses nobody asked it about.
    "web_scan": True,
    # Hosts the sweep cannot reach — another subnet, a VPN — as "ip" or
    # "ip:port", comma separated. Merged into the scan's answer.
    "web_hosts": "",
    # The key map printed at the foot of each client. On by default and easy to
    # turn off: it is for the first week, not the hundredth.
    "device_hints": True,
    "web_hints": True,
    # The row of buttons along the foot of the web page. On by default because
    # the screens most likely to open it — a phone, a Windows handheld — have no
    # key to press; off for anyone driving it from a real keyboard who would
    # rather have the height back for the tiles.
    "web_buttons": True,
}

# Editable from the settings page: name -> (kind, low, high). For "str" the high
# is a length cap; for "num" an empty field means None, which is how the weather
# widget is told to geolocate instead.
EDITABLE = {
    "fps": ("int", 1, 30),
    "jpeg_quality": ("int", 40, 95),
    "rotate180": ("bool", 0, 1),
    "weather_city": ("str", 0, 24),
    "weather_lat": ("num", -90, 90),
    "weather_lon": ("num", -180, 180),
    "deepseek_key": ("str", 0, 200),
    "minimax_key": ("str", 0, 400),
    "minimax_region": ("choice", 0, 0),
    "power_base_w": ("int", 0, 400),
    "power_psu_pct": ("int", 50, 100),
    "cpu_tdp_w": ("int", 0, 400),
    "power_price": ("num", 0, 100),
    "advice_enabled": ("bool", 0, 1),
    "advice_every_min": ("int", 5, 720),
    # One character is enough for a drive letter, but a Linux mount point is a
    # path; the Windows poller takes the first character of whatever it is given.
    "disk_letter": ("str", 0, 1 if IS_WINDOWS else 60),
    "theme": ("choice", 0, 0),
    "ai_alert_enabled": ("bool", 0, 1),
    "ai_alert_pct": ("int", 10, 100),
    "web_refresh_ms": ("int", 250, 10000),
    "web_scan": ("bool", 0, 1),
    "web_hosts": ("str", 0, 200),
    "device_hints": ("bool", 0, 1),
    "web_hints": ("bool", 0, 1),
    "web_buttons": ("bool", 0, 1),
}

# Checkboxes are absent from a POST body when unticked, so they have to be
# listed rather than inferred from what arrived.
CHECKBOXES = ("rotate180", "advice_enabled", "ai_alert_enabled",
              "web_scan", "device_hints", "web_hints", "web_buttons")

CHOICES = {"minimax_region": ("cn", "global"), "theme": theme.NAMES}

# API keys are write-only in the form: the page never renders a stored key back,
# so an empty field means "leave it alone" and a key can only be removed by
# ticking its clear box. Echoing a masked value back instead would make the key
# depend on the mask surviving a round trip through the browser's encoding —
# and anything that mangled it would silently overwrite the real key.
SECRET_KEYS = ("deepseek_key", "minimax_key")

# Only this one changes stream timing, so only it forces clients to reconnect.
RECONNECT_KEYS = {"fps"}

_START = time.time()


class Settings:
    """Config with validation, atomic-ish persistence and a change generation."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self.generation = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"[warn] config.json ignored: {exc}", flush=True)
            return
        for key, value in raw.items():
            if key in DEFAULTS:
                self._data[key] = value

    def get(self, key: str):
        with self._lock:
            return self._data[key]

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def coerce(self, key: str, raw) -> object:
        kind, low, high = EDITABLE[key]
        if kind == "bool":
            return str(raw).lower() in ("1", "true", "on", "yes")
        if kind == "str":
            text = str(raw).strip()
            if len(text) > high:
                raise ValueError(f"{key} 太长（最多 {high} 个字符）")
            return text
        if kind == "choice":
            text = str(raw).strip().lower()
            if text not in CHOICES[key]:
                raise ValueError(f"{key} must be one of {CHOICES[key]}")
            return text
        if kind == "num":
            text = str(raw).strip()
            if not text:
                return None
            number = float(text)
            if not low <= number <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            return round(number, 5)
        value = int(float(raw))
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        return value

    def update(self, changes: dict) -> list[str]:
        """Apply validated changes. Returns the keys that actually changed."""
        parsed = {k: self.coerce(k, v) for k, v in changes.items() if k in EDITABLE}
        with self._lock:
            changed = [k for k, v in parsed.items() if self._data[k] != v]
            self._data.update(parsed)
            if any(k in RECONNECT_KEYS for k in changed):
                self.generation += 1
            data = dict(self._data)
        if changed:
            self._save(data)
        return changed

    def _save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[warn] could not save config.json: {exc}", flush=True)


class FrameSource(threading.Thread):
    """Renders and encodes frames at the configured rate; publishes the latest.

    The handheld can be held in four orientations, and portrait is a different
    layout rather than a rotation, so a frame exists in several *variants*. Only
    the variants some client is actually watching get rendered, which keeps the
    cost at one or two renders per cycle no matter how many orientations exist.

    A variant key is ``("panel", orient, page, chrome, client, theme)`` for frames
    mapped onto the handheld's panel, or ``("upright", portrait, page, theme)``
    for the browser, which wants the layout the right way up. ``chrome`` is the
    handheld's own device list as ``(names, index)`` and ``client`` its address:
    both are part of the key because two handhelds need different headers —
    different device lists, and each one's own battery level. ``page`` and
    ``theme`` are there for the same reason: two handhelds can be looking at
    different pages of the same machine, in different palettes.
    """

    def __init__(self, settings: Settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.collector = metrics.Collector(settings)
        self.fonts = render.Fonts()

        self._cv = threading.Condition()
        self._variants: dict[tuple, bytes] = {}
        self._seq = 0
        self._snapshot: dict = {}
        self._wanted: collections.Counter = collections.Counter()
        self._stop = threading.Event()
        self.last_orient = 0
        self.last_page = 0
        self.last_theme = theme.resolve(settings.get("theme"))
        self.last_chrome: tuple | None = None
        self.last_frame_bytes = 0
        # client address -> (percent, charging, monotonic time reported)
        self._battery: dict[str, tuple[float, bool, float]] = {}

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    @property
    def snapshot(self) -> dict:
        return self._snapshot

    def wake_clients(self) -> None:
        with self._cv:
            self._cv.notify_all()

    # A handheld that has gone away should not leave a stale charge level on
    # screen, so a report expires rather than persisting until it is replaced.
    BATTERY_TTL_S = 120.0

    def report_battery(self, client: str, percent: float, charging: bool) -> None:
        with self._cv:
            self._battery[client] = (percent, charging, time.monotonic())

    def battery(self, client: str) -> dict | None:
        with self._cv:
            entry = self._battery.get(client)
        if not entry:
            return None
        percent, charging, at = entry
        if time.monotonic() - at > self.BATTERY_TTL_S:
            return None
        return {"percent": percent, "charging": charging}

    def batteries(self) -> dict[str, dict]:
        """Every live report, for the settings page."""
        with self._cv:
            items = list(self._battery.items())
        now = time.monotonic()
        return {ip: {"percent": p, "charging": c}
                for ip, (p, c, at) in items if now - at <= self.BATTERY_TTL_S}

    def preview_key(self, page: int | None = None,
                    theme_name: str | None = None) -> tuple:
        """Upright variant matching however the handheld is currently held.

        The page and the palette follow the handheld too unless the browser
        asked for its own, so opening /preview shows what is actually on the
        device right now.
        """
        return ("upright", 1 if self.last_orient in (1, 3) else 0,
                self.last_page if page is None else page % render.PAGE_COUNT,
                self.last_theme if theme_name is None
                else theme.resolve(theme_name))

    def acquire(self, key: tuple) -> None:
        with self._cv:
            self._wanted[key] += 1
            if key[0] == "panel":
                self.last_orient = key[1]
                self.last_page = key[2]
                self.last_chrome = key[3]
                self.last_theme = key[5]

    def release(self, key: tuple) -> None:
        with self._cv:
            self._wanted[key] -= 1
            if self._wanted[key] <= 0:
                del self._wanted[key]

    def wait_frame(self, key: tuple, last_seq: int, timeout: float = 5.0):
        """Block until a frame newer than last_seq exists. Returns (bytes, seq)."""
        with self._cv:
            if self._seq == last_seq:
                self._cv.wait(timeout)
            return self._variants.get(key), self._seq

    def one_frame(self, key: tuple, timeout: float = 4.0):
        """A single frame of any variant, rendering it if nobody is watching it.

        Only wanted variants get drawn, so a one-shot request for a page or
        orientation no client is streaming would otherwise wait for a frame that
        is never produced. Registering interest for the duration of the request
        is what makes the next cycle include it.
        """
        self.acquire(key)
        try:
            deadline = time.monotonic() + timeout
            seq = -1
            while time.monotonic() < deadline:
                frame, seq = self.wait_frame(key, seq, timeout=1.0)
                if frame is not None:
                    return frame
            return None
        finally:
            self.release(key)

    def _encode(self, img, quality: int) -> bytes:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=False)
        return buf.getvalue()

    def run(self) -> None:
        interval = 1.0 / self.settings.get("fps")
        next_at = time.monotonic()
        while not self._stop.is_set():
            want = 1.0 / self.settings.get("fps")
            if want != interval:  # rate changed under us — restart the cadence
                interval = want
                next_at = time.monotonic()

            snap = self.collector.sample()
            flip = bool(self.settings.get("rotate180"))
            quality = int(self.settings.get("jpeg_quality"))

            # Only what someone is actually watching. Every consumer — the MJPEG
            # stream, /frame.jpg, /preview — registers its variant through
            # acquire() first, so an idle server has nothing to draw and should
            # draw nothing: rendering and JPEG-encoding a dashboard eight times a
            # second for nobody costs a whole core on a low-power machine, which
            # is exactly the kind of machine this is meant to sit on.
            with self._cv:
                keys = set(self._wanted)

            variants = {}
            for key in keys:
                if key[0] == "panel":
                    _, orient, page, chrome, client, name = key
                    names, dev_idx = chrome or ((), 0)
                    img = render.render(snap, self.fonts, orient=orient,
                                        panel_flip=flip, devices=names,
                                        dev_idx=dev_idx,
                                        battery=self.battery(client), page=page,
                                        theme=name)
                else:
                    img = render.draw_layout(snap, self.fonts,
                                             portrait=bool(key[1]), page=key[2],
                                             theme=key[3])
                variants[key] = self._encode(img, quality)

            panel = [v for k, v in variants.items() if k[0] == "panel"]

            with self._cv:
                self._variants = variants
                self._snapshot = snap
                self._seq += 1
                # Report the handheld's frame size when there is one; that is the
                # number the bandwidth estimate on the settings page is about.
                # With nobody watching there is no new size to report, and the
                # last one is a better answer than zero.
                if variants:
                    self.last_frame_bytes = len(
                        panel[0] if panel else next(iter(variants.values())))
                self._cv.notify_all()

            # Even spacing matters more than frame count: ffplay is started with a
            # fixed -framerate and has no timestamps to resynchronise from, so a
            # burst of catch-up frames after a slow cycle reads as a stutter. Skip
            # the slots that were missed instead of firing them back to back.
            next_at += interval
            now2 = time.monotonic()
            if next_at < now2:
                missed = int((now2 - next_at) / interval) + 1
                next_at += missed * interval
            self._stop.wait(max(0.0, next_at - time.monotonic()))
        self.collector.close()


class LanScan(threading.Thread):
    """The other PC Monitors on the LAN, found for the web page's host switcher.

    The handheld sweeps the subnet itself, but the browser cannot: it has no way
    to open a TCP connection to an arbitrary address, and probing 254 of them
    with fetch() would take a minute of failed requests and fill the console with
    errors. So the sweep happens here — the same TCP connect sweep the launcher
    does, with the same confirmation step, because an open port is not proof of a
    PC Monitor — and the page just asks for the answer.

    A scan is never run on the request's own thread. ``/hosts.json`` answers from
    the last result immediately and asks for a fresh sweep in the background, so
    a page that polls this never waits on 254 sockets; the list it draws is at
    worst one sweep out of date, and a machine that has just come up appears a
    few seconds later without anybody reloading anything.
    """

    # Long enough that idling on the page is not a port scan every minute, short
    # enough that a PC you just switched on shows up while you are still looking.
    REFRESH_S = 120.0
    CONNECT_TIMEOUT_S = 0.35
    CONFIRM_TIMEOUT_S = 2.0
    FANOUT = 64

    def __init__(self, settings: Settings):
        super().__init__(daemon=True)
        self.settings = settings
        self._lock = threading.Lock()
        self._hosts: list[dict] = []
        self._at = 0.0
        self._scanning = False
        self._wake = threading.Event()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def result(self, force: bool = False) -> dict:
        """The last list, plus a background refresh when it has gone stale."""
        with self._lock:
            hosts, at, busy = list(self._hosts), self._at, self._scanning
        stale = force or not at or time.time() - at > self.REFRESH_S
        if stale and not busy:
            self._wake.set()
        return {"hosts": self.merged(hosts), "at": at, "scanning": busy or stale,
                "enabled": bool(self.settings.get("web_scan"))}

    def merged(self, hosts: list[dict]) -> list[dict]:
        """Scan results behind this machine and whatever was typed in by hand.

        This PC is always first and always present, so the page has something to
        show before the first sweep finishes — and so that "switch back to the
        machine I am sitting at" is one keypress from anywhere in the list.
        """
        port = int(self.settings.get("port") or 8765)
        ips = lan_ips()
        out = [{"ip": ips[0] if ips else "127.0.0.1", "port": port,
                "name": socket.gethostname(), "self": True}]
        seen = {(out[0]["ip"], port)} | {(ip, port) for ip in ips}
        for host in hosts + self._manual(port):
            key = (host["ip"], host["port"])
            if key in seen:
                continue
            seen.add(key)
            out.append(host)
        return out

    def _manual(self, port: int) -> list[dict]:
        out = []
        for item in str(self.settings.get("web_hosts") or "").split(","):
            item = item.strip()
            if not item:
                continue
            ip, _, raw = item.partition(":")
            ip = ip.strip()
            if not ip:
                continue
            try:
                p = int(raw) if raw.strip() else port
            except ValueError:
                p = port
            out.append({"ip": ip, "port": p, "name": ip, "self": False,
                        "manual": True})
        return out

    def run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.REFRESH_S)
            self._wake.clear()
            if self._stop.is_set():
                break
            if not self.settings.get("web_scan"):
                # Still stamp the time, or every request would ask again.
                with self._lock:
                    self._hosts, self._at = [], time.time()
                continue
            with self._lock:
                self._scanning = True
            try:
                found = self._sweep()
            except Exception as exc:  # a failed sweep must not kill the thread
                print(f"[warn] LAN 扫描失败：{exc}", flush=True)
                found = []
            with self._lock:
                self._hosts, self._at, self._scanning = found, time.time(), False

    def _sweep(self) -> list[dict]:
        port = int(self.settings.get("port") or 8765)
        mine = lan_ips()
        targets = []
        for ip in mine:
            net = ip.rsplit(".", 1)[0]
            targets += [f"{net}.{i}" for i in range(1, 255)]
        # Two adapters on the same subnet would otherwise probe it twice.
        targets = list(dict.fromkeys(targets))

        with concurrent.futures.ThreadPoolExecutor(self.FANOUT) as pool:
            open_ips = [ip for ip, ok in
                        zip(targets, pool.map(lambda t: self._open(t, port), targets))
                        if ok]
            names = list(pool.map(lambda t: self._identify(t, port), open_ips))
        return [{"ip": ip, "port": port, "name": name, "self": ip in mine}
                for ip, name in zip(open_ips, names) if name]

    def _open(self, ip: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.CONNECT_TIMEOUT_S)
            try:
                return sock.connect_ex((ip, port)) == 0
            except OSError:
                return False

    def _identify(self, ip: str, port: int) -> str:
        """The machine's name, or "" for anything that is not a PC Monitor."""
        try:
            with urllib.request.urlopen(
                    f"http://{ip}:{port}/config.json",
                    timeout=self.CONFIRM_TIMEOUT_S) as resp:
                data = json.loads(resp.read(65536).decode("utf-8", "replace"))
        except Exception:
            return ""
        # "name" alone would also match some other program's config endpoint;
        # "pages" is this dashboard's own field.
        if not isinstance(data, dict) or "pages" not in data:
            return ""
        return str(data.get("name") or ip)[:40]


PAGE_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 56px;background:#0d0d0d;color:#c3c2b7;
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:660px;margin:0 auto}
h1{margin:0 0 4px;font-size:20px;color:#fff;font-weight:600}
.sub{margin:0 0 28px;color:#898781;font-size:13px}
.card{background:#1a1a19;border:1px solid #313130;border-radius:10px;
  padding:20px;margin-bottom:16px}
.row{margin-bottom:24px}
.row:last-child{margin-bottom:0}
label{display:block;color:#fff;font-weight:600;margin-bottom:2px}
.hint{color:#898781;font-size:13px;margin-bottom:12px}
.ctl{display:flex;align-items:center;gap:14px}
input[type=range]{flex:1;min-width:0;accent-color:#3987e5}
input[type=text],input[type=password],select{flex:1;min-width:0;background:#242423;
  color:#fff;border:1px solid #313130;border-radius:6px;padding:8px 10px;
  font:inherit;font-size:14px}
input::placeholder{color:#6b6a66}
select{flex:0 0 auto}
output{min-width:74px;text-align:right;color:#fff;font-weight:600;
  font-variant-numeric:tabular-nums}
.presets{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.presets button{background:#242423;color:#c3c2b7;border:1px solid #313130;
  border-radius:6px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
.presets button:hover{border-color:#3987e5;color:#fff}
.check{display:flex;align-items:center;gap:10px}
.check input{width:17px;height:17px;accent-color:#3987e5}
.check label{margin:0}
.est{color:#898781;font-size:13px;margin-top:10px}
.est b{color:#c3c2b7;font-weight:600;font-variant-numeric:tabular-nums}
.actions{display:flex;align-items:center;gap:16px;margin-top:4px}
button.save{background:#2a78d6;color:#fff;border:0;border-radius:7px;
  padding:10px 22px;font:inherit;font-weight:600;cursor:pointer}
button.save:hover{background:#3987e5}
.note{color:#0ca30c;font-size:13px}
.warn{color:#fab219;font-size:13px}
a{color:#3987e5}
/* The preview stream is served upright at its layout's own aspect ratio, so no
   fixed height and no counter-rotation here. */
img.prev{max-width:100%;border:1px solid #313130;border-radius:8px;
  display:block;image-rendering:pixelated}
table{border-collapse:collapse;font-size:13px;width:100%}
td{padding:3px 0;vertical-align:top}
td:first-child{color:#898781;padding-right:16px;white-space:nowrap}
code{background:#242423;padding:1px 5px;border-radius:4px;font-size:12px}
"""


def _clear_box(key: str, stored: bool) -> str:
    if not stored:
        return ""
    return (f'<div class="check" style="margin-top:10px">'
            f'<input type="checkbox" id="c_{key}" name="clear_{key}" value="1">'
            f'<label for="c_{key}" style="font-weight:400;color:#898781">'
            f'删除已保存的 key</label></div>')


def settings_page(settings: Settings, source: FrameSource, message: str = "",
                  warn: str = "", scan: "LanScan | None" = None) -> bytes:
    cfg = settings.snapshot()
    fps = int(cfg["fps"])
    quality = int(cfg["jpeg_quality"])
    rotate = bool(cfg["rotate180"])
    region = str(cfg.get("minimax_region") or "cn")
    saved = "已保存一个 key，留空即保持不变。"
    ds_state = saved if cfg.get("deepseek_key") else "留空就不查。"
    mm_state = saved if cfg.get("minimax_key") else "留空就不查。"
    frame_kb = max(1, source.last_frame_bytes // 1024)
    orient_label = ("横向", "竖向 ⟳", "横向 ⤒倒置", "竖向 ⟲")[source.last_orient % 4]
    page_label = PAGE_NAMES[source.last_page % len(PAGE_NAMES)]
    theme_name = theme.resolve(cfg.get("theme"))
    theme_opts = "".join(
        f'<option value="{name}" {"selected" if name == theme_name else ""}>'
        f'{html.escape(theme.LABELS.get(name, name))}</option>'
        for name in theme.NAMES)
    alert_pct = int(cfg.get("ai_alert_pct") or 80)
    disk_hint = ("磁盘监控的盘符" if IS_WINDOWS
                 else "磁盘监控的挂载点，如 / 或 /data")
    alert = source.collector.ai_alert.data
    alert_state = ("还没有触发过。" if not alert.get("id")
                   else f'上次：{html.escape(alert.get("text") or "")}')

    if source.last_chrome:
        names, idx = source.last_chrome
        devs = "、".join(f"<b>{html.escape(n)}</b>" if i == idx else html.escape(n)
                        for i, n in enumerate(names))
        devs += f"（共 {len(names)} 台，粗体为当前）"
    else:
        devs = "掌机还没连上来"

    batts = source.batteries()
    if batts:
        batt = "、".join(
            f"{html.escape(ip)} <b>{v['percent']:.0f}%</b>"
            + ("（充电中 ⚡）" if v["charging"] else "")
            for ip, v in sorted(batts.items()))
    else:
        batt = "掌机没有上报（旧版 launch.sh 不会上报电量）"

    # The two "will this actually work" answers, computed rather than promised:
    # what the advisor last did, and whether anything on this machine can read
    # the language it writes in.
    adv = source.collector.advisor.data
    if not cfg.get("advice_enabled"):
        advice_state = ""
    elif adv.get("ok"):
        advice_state = (f'<br>上次由 <b>{html.escape(adv.get("provider") or "")}</b> '
                        f'给出，结论：{html.escape(adv.get("text") or "")}')
    else:
        advice_state = f'<br>当前状态：{html.escape(adv.get("err") or "还没跑过")}'

    web_ms = int(cfg.get("web_refresh_ms") or 1000)
    port_now = int(cfg.get("port") or 8765)
    # Reported rather than promised, like the advisor and the voice above: what
    # the last sweep actually found, so a machine that is missing from the list
    # is a fact on this page rather than something to guess at.
    found = (scan.result() if scan else {}).get("hosts") or []
    others = [h for h in found if not h.get("self")]
    if not cfg.get("web_scan"):
        scan_state = "没有开启，网页版只能看这一台。"
    elif others:
        scan_state = "扫到 " + "、".join(
            f'<b>{html.escape(h["name"])}</b>（{html.escape(h["ip"])}'
            + ("，手填" if h.get("manual") else "") + "）" for h in others[:8])
    elif (scan.result() if scan else {}).get("at"):
        scan_state = "扫过了，本网段里没有别的 PC Monitor。"
    else:
        scan_state = "还没扫完，过几秒刷新这个页面。"

    banner = ""
    if message:
        banner += f'<p class="note">{html.escape(message)}</p>'
    if warn:
        banner += f'<p class="warn">{html.escape(warn)}</p>'

    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC Monitor 设置</title><style>{PAGE_CSS}</style>
<div class="wrap">
<h1>PC Monitor 设置</h1>
<p class="sub">改动立即生效并写入 config.json。掌机会自动跟随，无需在掌机上改任何东西。</p>
{banner}
<form method="post" action="/settings">
  <div class="card">
    <div class="row">
      <label for="fps">刷新速率</label>
      <div class="hint">仪表盘每秒重绘并推送多少帧。掌机重连后生效（约 1–2 秒）。</div>
      <div class="ctl">
        <input type="range" id="fps" name="fps" min="1" max="30" step="1"
               value="{fps}" oninput="sync()">
        <output id="fpsOut"></output>
      </div>
      <div class="presets">
        <button type="button" onclick="setFps(2)">2 · 省电</button>
        <button type="button" onclick="setFps(5)">5 · 平稳</button>
        <button type="button" onclick="setFps(8)">8 · 默认</button>
        <button type="button" onclick="setFps(15)">15 · 流畅</button>
        <button type="button" onclick="setFps(24)">24 · 最高</button>
      </div>
      <div class="est">当前每帧约 <b>{frame_kb} KB</b>，估算占用带宽
        <b><span id="bw"></span></b><span id="bwnote"></span></div>
    </div>

    <div class="row">
      <label for="q">画质</label>
      <div class="hint">JPEG 质量。降低可以省带宽，文字会稍微发虚。</div>
      <div class="ctl">
        <input type="range" id="q" name="jpeg_quality" min="40" max="95" step="1"
               value="{quality}" oninput="sync()">
        <output id="qOut"></output>
      </div>
    </div>

    <div class="row">
      <div class="check">
        <input type="checkbox" id="rot" name="rotate180" value="1"
               {"checked" if rotate else ""}>
        <label for="rot">预旋转 180°</label>
      </div>
      <div class="hint" style="margin:8px 0 0">Miyoo 面板是倒装的，所以默认开启。
        掌机上画面上下颠倒就关掉它。</div>
    </div>

    <div class="row">
      <label for="th">主题</label>
      <div class="hint">配色方案，串流画面和网页版共用一套。掌机上按 <b>X</b>、
        网页版上按 <b>T</b> 都能临时换一个，这里选的是默认值。</div>
      <div class="ctl">
        <select id="th" name="theme">{theme_opts}</select>
        <span class="hint" style="margin:0">「终端绿」是黑底绿字的终端风格，
          各项指标用鲜艳的 ANSI 色区分</span>
      </div>
    </div>

    <div class="actions">
      <button class="save" type="submit">保存</button>
      <span class="hint" style="margin:0">保存后掌机会自动重连</span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <label for="city">天气位置</label>
      <div class="hint">填城市名就按城市查（中英文都行）；再填经纬度则以经纬度为准；
        全留空才按公网 IP 自动定位——走代理时 IP 会定位到别的国家，填一下城市即可。
        用的是免费的 Open-Meteo，不需要任何 key。</div>
      <div class="ctl">
        <input type="text" id="city" name="weather_city" placeholder="城市名，如 南京 / Nanjing"
               value="{html.escape(str(cfg.get("weather_city") or ""))}">
      </div>
      <div class="ctl" style="margin-top:10px">
        <input type="text" name="weather_lat" placeholder="纬度 例 22.5431"
               value="{"" if cfg.get("weather_lat") is None else cfg["weather_lat"]}">
        <input type="text" name="weather_lon" placeholder="经度 例 114.0579"
               value="{"" if cfg.get("weather_lon") is None else cfg["weather_lon"]}">
      </div>
    </div>

    <div class="row">
      <label for="ds">DeepSeek API key</label>
      <div class="hint">{ds_state}只保存在本机 config.json 里，不会发给掌机。</div>
      <div class="ctl">
        <input type="password" id="ds" name="deepseek_key" placeholder="sk-…">
      </div>
      {_clear_box("deepseek_key", bool(cfg.get("deepseek_key")))}
    </div>

    <div class="row">
      <label for="mm">MiniMax API key</label>
      <div class="hint">{mm_state}海外账号请把区域切成「海外」。</div>
      <div class="ctl">
        <input type="password" id="mm" name="minimax_key" placeholder="eyJ…">
        <select name="minimax_region">
          <option value="cn" {"selected" if region == "cn" else ""}>国内</option>
          <option value="global" {"selected" if region == "global" else ""}>海外</option>
        </select>
      </div>
      {_clear_box("minimax_key", bool(cfg.get("minimax_key")))}
    </div>

    <div class="actions">
      <button class="save" type="submit">保存</button>
      <span class="hint" style="margin:0">额度与天气会在下一轮轮询时更新</span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <label for="wr">网页版刷新间隔</label>
      <div class="hint">高清网页版 <a href="/hd">/hd</a> 每隔多久取一次数据。
        它不走视频流，一次只是一个几 KB 的 JSON，所以调慢了画面不会变糊，
        只是数字更新得慢一些——掌机用电池时可以放心调慢。
        网页上按 <b>[</b> <b>]</b> 也能临时改，这里设的是打开时的默认值。</div>
      <div class="ctl">
        <input type="range" id="wr" name="web_refresh_ms" min="250" max="10000"
               step="250" value="{web_ms}" oninput="sync()">
        <output id="wrOut"></output>
      </div>
      <div class="presets">
        <button type="button" onclick="setWeb(500)">0.5s · 最灵敏</button>
        <button type="button" onclick="setWeb(1000)">1s · 默认</button>
        <button type="button" onclick="setWeb(2000)">2s · 省电</button>
        <button type="button" onclick="setWeb(5000)">5s · 挂着看</button>
      </div>
    </div>

    <div class="row">
      <div class="check">
        <input type="checkbox" id="wsc" name="web_scan" value="1"
               {"checked" if cfg.get("web_scan") else ""}>
        <label for="wsc">网页版可以监视局域网里的其他主机</label>
      </div>
      <div class="hint" style="margin:8px 0 0">开启后这台机器会每隔两分钟扫一遍本网段
        （只连一下端口 {port_now}，确认对方也是 PC Monitor 才算数），
        网页上按 <b>N</b> 就能轮流查看扫到的每一台，按 <b>0</b> 回到本机、
        按 <b>R</b> 立刻重扫。浏览器自己没法探测端口，所以这一步只能由 PC 来做。
        <br>当前：{scan_state}</div>
      <div class="ctl" style="margin-top:12px">
        <input type="text" name="web_hosts" placeholder="扫不到的机器写这里，如 192.168.2.7 或 10.0.0.5:8765，逗号分隔"
               value="{html.escape(str(cfg.get("web_hosts") or ""))}">
      </div>
    </div>

    <div class="row">
      <div class="check">
        <input type="checkbox" id="dhint" name="device_hints" value="1"
               {"checked" if cfg.get("device_hints") else ""}>
        <label for="dhint">掌机上显示按键提示</label>
      </div>
      <div class="hint" style="margin:8px 0 0">画面最底下那一行灰色的小字
        （{html.escape(render.HINTS)}）。掌机的按键上没有字，
        所以默认开着；记熟了就可以关掉，页面会多出一行的空间。</div>
      <div class="check" style="margin-top:14px">
        <input type="checkbox" id="whint" name="web_hints" value="1"
               {"checked" if cfg.get("web_hints") else ""}>
        <label for="whint">网页版显示按键提示</label>
      </div>
      <div class="hint" style="margin:8px 0 0">网页顶栏右边那一行灰色小字
        （窗口太窄时它会自己让位）。无论开关，按 <b>H</b> 都能调出完整的按键表。</div>
      <div class="check" style="margin-top:14px">
        <input type="checkbox" id="wbtn" name="web_buttons" value="1"
               {"checked" if cfg.get("web_buttons") else ""}>
        <label for="wbtn">网页版显示底部按钮栏</label>
      </div>
      <div class="hint" style="margin:8px 0 0">翻页、主题、刷新快慢、换主机、全屏
        都各有一个按钮——手机和 Windows 掌机上没有键盘，只能这么点。
        手指左右滑动画面也能翻页。用键盘看的话可以关掉，
        省下的一行高度会还给下面的卡片。</div>
    </div>

    <div class="actions">
      <button class="save" type="submit">保存</button>
      <span class="hint" style="margin:0">网页版刷新一下就会用上新设置</span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <label>耗电量估算</label>
      <div class="hint">没有任何软件能读到插座上的实际功率，所以这是估算：
        CPU 封装功耗（来自 {"Afterburner" if IS_WINDOWS else "RAPL"}）加显卡功耗（来自 nvidia-smi），
        再加下面这个「其余部分」，最后除以电源效率。累计值只统计本程序运行的时间。</div>
      <div class="ctl">
        <input type="text" name="power_base_w" style="max-width:110px"
               value="{int(cfg.get("power_base_w") or 0)}">
        <span class="hint" style="margin:0">W 主板/内存/硬盘/风扇等其余部分</span>
      </div>
      <div class="ctl" style="margin-top:10px">
        <input type="text" name="power_psu_pct" style="max-width:110px"
               value="{int(cfg.get("power_psu_pct") or 90)}">
        <span class="hint" style="margin:0">% 电源效率（80Plus 金牌约 90）</span>
      </div>
      <div class="ctl" style="margin-top:10px">
        <input type="text" name="cpu_tdp_w" style="max-width:110px"
               value="{int(cfg.get("cpu_tdp_w") or 0)}">
        <span class="hint" style="margin:0">W CPU TDP，只在读不到功耗传感器时用来推算</span>
      </div>
      <div class="ctl" style="margin-top:10px">
        <input type="text" name="power_price" style="max-width:110px"
               placeholder="留空不显示"
               value="{"" if cfg.get("power_price") is None else cfg["power_price"]}">
        <span class="hint" style="margin:0">元/度，填了才显示电费</span>
      </div>
      <div class="ctl" style="margin-top:10px">
        <input type="text" name="disk_letter" style="max-width:110px"
               value="{html.escape(str(cfg.get("disk_letter") or DEFAULTS["disk_letter"]))}">
        <span class="hint" style="margin:0">{disk_hint}，改完要重启程序</span>
      </div>
    </div>

    <div class="actions">
      <button class="save" type="submit">保存</button>
      <span class="hint" style="margin:0">改动立即生效，不影响已累计的数据</span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <div class="check">
        <input type="checkbox" id="adv" name="advice_enabled" value="1"
               {"checked" if cfg.get("advice_enabled") else ""}>
        <label for="adv">AI 运行状况建议</label>
      </div>
      <div class="hint" style="margin:8px 0 0">每隔一段时间把这段时间的运行统计
        （占用、温度、进程、流量、容器）发给一家 AI，让它判断有没有值得注意的地方；
        一切正常时它只回「正常」，不会硬凑建议。用的是<b>余量最多</b>的那家——
        目前在 DeepSeek 和 MiniMax 里选。Claude 不参与：这里能读到的是 Claude Code
        的订阅令牌，不是 API key，不该拿来跑后台任务。{advice_state}</div>
      <div class="ctl" style="margin-top:12px">
        <input type="text" name="advice_every_min" style="max-width:110px"
               value="{int(cfg.get("advice_every_min") or 30)}">
        <span class="hint" style="margin:0">分钟一次（最少 5）</span>
      </div>
    </div>

    <div class="actions">
      <button class="save" type="submit">保存</button>
      <span class="hint" style="margin:0">打开后会在一分钟内做第一次分析</span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <div class="check">
        <input type="checkbox" id="alrt" name="ai_alert_enabled" value="1"
               {"checked" if cfg.get("ai_alert_enabled") else ""}>
        <label for="alrt">AI 额度告警</label>
      </div>
      <div class="hint" style="margin:8px 0 0">Claude / MiniMax 的
        <b>5 小时额度</b>用到下面这个比例时记一条告警，写进日志和
        <code>/alert.json</code>，不出声也不弹条。每个 5 小时窗口只记一次，
        下一个窗口重新计。<br>{alert_state}</div>
      <div class="ctl" style="margin-top:12px">
        <input type="text" name="ai_alert_pct" style="max-width:110px"
               value="{alert_pct}">
        <span class="hint" style="margin:0">% 触发阈值（默认 80）</span>
      </div>
    </div>

    <div class="actions">
      <button class="save" type="submit">保存</button>
      <span class="hint" style="margin:0">额度每分钟查一次，改完下一轮生效</span>
    </div>
  </div>
</form>

<div class="card">
  <table>
    <tr><td>掌机朝向</td><td>{orient_label}（掌机上按 <b>Y</b> 切换）</td></tr>
    <tr><td>当前页</td><td>{page_label}（掌机上按 <b>上 / 下</b> 翻页）</td></tr>
    <tr><td>掌机扫到的设备</td><td>{devs}</td></tr>
    <tr><td>掌机电量</td><td>{batt}</td></tr>
    <tr><td>切换设备</td><td>掌机上按 <b>左 / 右</b>，退出按 <b>MENU</b>，
      换主题按 <b>X</b></td></tr>
    <tr><td>实时预览</td><td><a href="/preview">/preview</a></td></tr>
    <tr><td>高清网页版</td><td><a href="/hd">/hd</a>
      （矢量重绘，任意分辨率都清晰；键盘 <b>←/→</b> 翻页、<b>T</b> 换主题、
      <b>N</b> 换主机、<b>[ ]</b> 调刷新、<b>F</b> 全屏、<b>H</b> 看全部按键，
      适合 Windows 掌机）</td></tr>
    <tr><td>局域网主机</td><td><a href="/hosts.json">/hosts.json</a>
      （网页版的主机列表就是这个）</td></tr>
    <tr><td>AI 额度</td><td><a href="/ai">/ai</a>（完整字段，不用再单独跑 aimon）</td></tr>
    <tr><td>AI 建议</td><td><a href="/advice.json">/advice.json</a>（掌机拉的就是它）</td></tr>
    <tr><td>AI 额度告警</td><td><a href="/alert.json">/alert.json</a></td></tr>
    <tr><td>原始数据</td><td><a href="/stats.json">/stats.json</a> ·
      <a href="/config.json">/config.json</a> ·
      <a href="/api/usage">/api/usage</a> · <a href="/api/info">/api/info</a></td></tr>
  </table>
</div>

<div class="card">
  <div class="hint" style="margin-bottom:12px">实时预览</div>
  <img class="prev" src="/preview.mjpg" alt="dashboard">
</div>
</div>
<script>
function fmt(bps){{
  return bps >= 1e6 ? (bps/1e6).toFixed(1)+" Mbps" : Math.round(bps/1e3)+" kbps";
}}
function sync(){{
  const f = +document.getElementById('fps').value;
  const q = +document.getElementById('q').value;
  const w = +document.getElementById('wr').value;
  document.getElementById('fpsOut').textContent = f + " fps";
  document.getElementById('qOut').textContent = q;
  document.getElementById('wrOut').textContent = (w / 1000).toFixed(2)
    .replace(/0$/, "") + " s";
  // Frame size scales roughly linearly with quality in this range.
  const kb = {frame_kb} * (q / {quality});
  const bps = kb * 1024 * 8 * f;
  document.getElementById('bw').textContent = fmt(bps);
  document.getElementById('bwnote').textContent =
    bps > 12e6 ? " — 超出掌机 WiFi 可能承受的范围" : "";
}}
function setFps(v){{ document.getElementById('fps').value = v; sync(); }}
function setWeb(v){{ document.getElementById('wr').value = v; sync(); }}
sync();
</script>
</html>""".encode("utf-8")


# --- aimon compatibility ---------------------------------------------------
# aimon was a second program doing the same quota lookups from its own server on
# port 9000. Its two endpoints are reproduced here so its handheld client and web
# page keep working against this process and nothing else has to be started.
# They live on this program's own port only — the second listener on 9000 is
# gone, so an old client has to be pointed at this port like everything else.
# Unlike aimon these answer from the poller's cache rather than calling upstream
# per request, which is also what keeps Anthropic from rate-limiting us.

def api_info(source: FrameSource, port: int) -> dict:
    import sysinfo

    snap = source.snapshot or {}
    mem = snap.get("mem") or {}
    total = mem.get("total_gb")
    return {
        "service": "aimon",
        "version": 1,
        "name": socket.gethostname(),
        "hostname": socket.gethostname(),
        "system": "Windows",
        "release": os.environ.get("OS", ""),
        "machine": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "os": f"PC Monitor on {socket.gethostname()}",
        "ips": lan_ips(),
        "port": port,
        "uptime": int(time.time() - _START),
        "time": int(time.time() * 1000),
        "cpuCount": sysinfo.cpu_count(logical=True),
        "cpuPercent": (snap.get("cpu") or {}).get("percent"),
        "memTotal": int(total * 1024 ** 3) if total else None,
        "memFree": int((total - mem["used_gb"]) * 1024 ** 3) if total else None,
    }


def api_usage(source: FrameSource) -> dict:
    poller = source.collector.ai
    out = dict(poller.raw)
    claude = (poller.data or {}).get("claude") or {}
    if "claude" not in out and claude.get("err"):
        out["claude"] = {"error": claude["err"]}
    out["ts"] = int(time.time() * 1000)
    return out


AI_CSS = """
.quota{margin-bottom:18px}
.quota:last-child{margin-bottom:0}
.qhead{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.qhead b{color:#fff;font-weight:600}
.qhead span{color:#898781;font-size:13px;font-variant-numeric:tabular-nums}
.bar{height:8px;border-radius:4px;background:#123;margin-top:7px;overflow:hidden}
.bar i{display:block;height:100%;background:#0096a3;border-radius:4px}
.bar.crit i{background:#d03b3b}
.bar.warn i{background:#fab219}
.big{font-size:26px;color:#fff;font-weight:600;font-variant-numeric:tabular-nums}
.muted{color:#898781}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}
"""


def _bar(pct: float | None) -> str:
    if pct is None:
        return '<div class="bar"><i style="width:0"></i></div>'
    cls = " crit" if pct >= 100 else (" warn" if pct >= 85 else "")
    return (f'<div class="bar{cls}"><i style="width:{min(100.0, pct):.1f}%">'
            f'</i></div>')


def _quota_row(name: str, window) -> str:
    if not window or window.get("pct") is None:
        return (f'<div class="quota"><div class="qhead"><b>{name}</b>'
                f'<span class="muted">该套餐没有这一档</span></div>'
                f'{_bar(None)}</div>')
    resets = window.get("resets_at")
    at = (f'<span data-reset="{resets:.0f}">…</span>' if resets else
          '<span class="muted">—</span>')
    return (f'<div class="quota"><div class="qhead"><b>{name}</b>'
            f'<span>{window["pct"]:.0f}% · {at}</span></div>'
            f'{_bar(window["pct"])}</div>')


def ai_page(source: FrameSource) -> bytes:
    """Everything the pollers know, in full — the tile only has room for gauges."""
    ai = source.collector.ai.data or {}
    claude = ai.get("claude") or {}
    weather = source.collector.weather.data or {}

    if claude.get("five_hour"):
        extra = claude.get("extra") or {}
        if extra.get("used") is None:
            extra_line = '<span class="muted">—</span>'
        else:
            state = "已开启" if extra.get("enabled") else \
                f"已关闭（{html.escape(extra.get('reason') or '未开启')}）"
            extra_line = (f'<span class="big">{extra["used"]:.2f}</span> '
                          f'{html.escape(extra.get("currency") or "")} · {state}')
        claude_body = (
            _quota_row("5 小时窗口", claude.get("five_hour")) +
            _quota_row("7 天窗口", claude.get("seven_day")) +
            _quota_row("7 天 Opus", claude.get("seven_day_opus")) +
            f'<div class="quota"><div class="qhead"><b>额外用量</b></div>'
            f'<div style="margin-top:6px">{extra_line}</div></div>')
        plan = html.escape((claude.get("plan") or "").capitalize())
        stale = "" if claude.get("ok") else \
            '<p class="warn">最近一次刷新失败，下面是上一次的数据。</p>'
    else:
        claude_body = (f'<p class="hint">拿不到额度：'
                       f'{html.escape(str(claude.get("err") or "读取中"))}<br>'
                       f'需要本机登录过 Claude Code（读取 ~/.claude/'
                       f'.credentials.json）。</p>')
        plan, stale = "", ""

    ds = ai.get("deepseek")
    if not ds:
        ds_body = '<p class="hint">没有配置 API key。</p>'
    elif not ds.get("ok") and ds.get("balance") is None:
        ds_body = f'<p class="hint">{html.escape(str(ds.get("err")))}</p>'
    else:
        ds_body = (f'<div class="big">{ds["balance"]:.2f} '
                   f'{html.escape(ds.get("currency") or "")}</div>'
                   f'<p class="hint" style="margin:6px 0 0">'
                   f'{"可用" if ds.get("available") else "不可用"}</p>')

    mm = ai.get("minimax")
    if not mm:
        mm_body = '<p class="hint">没有配置 API key。</p>'
    elif not mm.get("models"):
        mm_body = f'<p class="hint">{html.escape(str(mm.get("err")))}</p>'
    else:
        # Every model group, not just the one the tile has room for.
        mm_body = ""
        for model in mm["models"]:
            name = html.escape(model["name"])
            mm_body += (
                f'<div class="hint" style="margin:16px 0 8px">{name}</div>' +
                _quota_row("5 小时已用", None if model["five_hour"] is None else
                           {"pct": model["five_hour"],
                            "resets_at": model["five_hour_reset"]}) +
                _quota_row("本周已用", None if model["weekly"] is None else
                           {"pct": model["weekly"],
                            "resets_at": model["weekly_reset"]}))

    if weather.get("ok"):
        def cell(title, block, key_a, key_b=None):
            if not block:
                return ""
            if key_b:
                value = f'{block[key_a]:.0f}° ~ {block[key_b]:.0f}°'
            else:
                value = f'{block[key_a]:.0f}°'
            return (f'<div><div class="hint" style="margin:0">{title}</div>'
                    f'<div><b style="color:#fff">{html.escape(block.get("text") or "")}'
                    f'</b> {value}</div></div>')
        w_body = ('<div class="grid">' +
                  cell("现在", weather.get("now"), "temp") +
                  cell("3 小时后", weather.get("h3"), "temp") +
                  cell("6 小时后", weather.get("h6"), "temp") +
                  cell("明天", weather.get("d1"), "tmin", "tmax") +
                  cell("后天", weather.get("d2"), "tmin", "tmax") +
                  '</div>')
        w_title = f'天气 · {html.escape(weather.get("city") or "")}'
    else:
        w_body = f'<p class="hint">{html.escape(str(weather.get("err") or "—"))}</p>'
        w_title = "天气"

    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 额度</title><style>{PAGE_CSS}{AI_CSS}</style>
<div class="wrap">
<h1>AI 额度</h1>
<p class="sub">由 PC Monitor 在后台轮询，页面每分钟自动刷新。
  <a href="/settings">设置 →</a> · <a href="/api/usage">原始数据</a></p>
{stale}
<div class="card"><div class="hint" style="margin-bottom:14px">Claude {plan}</div>
{claude_body}</div>
<div class="card"><div class="hint" style="margin-bottom:14px">DeepSeek</div>
{ds_body}</div>
<div class="card"><div class="hint" style="margin-bottom:14px">MiniMax</div>
{mm_body}</div>
<div class="card"><div class="hint" style="margin-bottom:14px">{w_title}</div>
{w_body}</div>
</div>
<script>
function tick(){{
  const now = Date.now() / 1000;
  document.querySelectorAll('[data-reset]').forEach(el => {{
    let s = +el.dataset.reset - now;
    if (s <= 0) {{ el.textContent = '即将重置'; return; }}
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
    el.textContent = (d ? d + ' 天 ' : '') +
      h + ':' + String(m).padStart(2, '0') + ' 后重置';
  }});
}}
tick(); setInterval(tick, 1000); setTimeout(() => location.reload(), 60000);
</script>
</html>""".encode("utf-8")


def advice_json(source: FrameSource) -> dict:
    """The latest advice, as the handheld polls it."""
    return dict(source.collector.advisor.data)


PAGE_NAMES = ("总览", "详情")


def preview_page(page: int | None) -> bytes:
    # No page in the URL means "follow the handheld", which is its own state
    # rather than a synonym for page 0 — so it gets its own link.
    links = " · ".join(
        [("<b>跟随掌机</b>" if page is None else '<a href="/preview">跟随掌机</a>')] +
        [(f"<b>{name}</b>" if page == i else
          f'<a href="/preview?page={i}">{name}</a>')
         for i, name in enumerate(PAGE_NAMES)])
    src = "/preview.mjpg" if page is None else f"/preview.mjpg?page={page}"
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC Monitor 预览</title><style>{PAGE_CSS}</style>
<div class="wrap">
<h1>实时预览</h1>
<p class="sub">掌机上看到的画面，已转正。{links} ·
<a href="/hd">高清网页版</a> · <a href="/settings">← 设置</a></p>
<img class="prev" src="{src}" alt="dashboard">
</div></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "pcmon"
    source: FrameSource  # injected below
    settings: Settings
    scan: "LanScan | None" = None  # absent only in tests that build no scanner

    QUIET_PATHS = ("/stats.json", "/config.json", "/battery", "/api/",
                   "/advice.json", "/alert.json", "/hosts.json")

    # Read-only endpoints another machine's /hd page is allowed to fetch.
    CORS_PATHS = ("/api/", "/stats.json", "/config.json", "/hosts.json",
                  "/alert.json", "/advice.json")

    def log_message(self, fmt, *args):
        if not self.path.startswith(self.QUIET_PATHS):
            print(f"[http] {self.client_address[0]} {self.command} {self.path}",
                  flush=True)

    def _send(self, ctype: str, body: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # aimon's web page fetches these from another origin, so the API stays
        # open the way aimon's own server was. It is a LAN dashboard either way.
        # /hd needs the same of the read-only endpoints below: watching another
        # PC means fetching that PC's snapshot from this page's origin, and the
        # browser will not let it without this header on the *other* machine's
        # reply. Only what a page may read — the settings POST is not on the
        # list, so no other origin can change anything here.
        if self.path.startswith(self.CORS_PATHS):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, payload) -> None:
        self._send("application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False,
                              default=str).encode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/settings"):
            msg = query.get("saved", [""])[0]
            self._send("text/html; charset=utf-8",
                       settings_page(self.settings, self.source, message=msg,
                                     scan=self.scan))
        elif path == "/preview":
            self._send("text/html; charset=utf-8",
                       preview_page(self._page(query, None)))
        elif path == "/hd":
            self._send("text/html; charset=utf-8",
                       webui.page(self.settings.snapshot()))
        elif path == "/alert.json":
            self._json(self.source.collector.ai_alert.data)
        elif path == "/ai":
            self._send("text/html; charset=utf-8", ai_page(self.source))
        elif path == "/api/info":
            self._json(api_info(self.source, self.server.server_port))
        elif path == "/api/usage":
            self._json(api_usage(self.source))
        elif path == "/advice.json":
            self._json(advice_json(self.source))
        elif path == "/stream.mjpg":
            self._stream(("panel", self._orient(query), self._page(query),
                          self._chrome(query), self.client_address[0],
                          self._theme(query)),
                         multipart=False)
        elif path == "/battery":
            self._battery(query)
        elif path == "/preview.mjpg":
            self._stream(self.source.preview_key(self._page(query, None),
                                                 self._theme(query, None)),
                         multipart=True)
        elif path == "/frame.jpg":
            frame = self.source.one_frame(
                self.source.preview_key(self._page(query, None),
                                        self._theme(query, None)))
            if frame is None:
                self._send("text/plain", b"no frame yet", 503)
            else:
                self._send("image/jpeg", frame)
        elif path == "/config.json":
            # The page count and the theme list both come from here rather than
            # being hardcoded in the launcher, so adding either to the dashboard
            # does not mean pushing a new script to every handheld.
            body = json.dumps(dict(self.settings.snapshot(),
                                   name=socket.gethostname(),
                                   pages=render.PAGE_COUNT,
                                   themes=list(theme.NAMES))).encode("utf-8")
            self._send("application/json", body)
        elif path == "/stats.json":
            body = json.dumps(self.source.snapshot, ensure_ascii=False,
                              default=str).encode("utf-8")
            self._send("application/json; charset=utf-8", body)
        elif path == "/hosts.json":
            if not self.scan:
                self._json({"hosts": [], "at": 0, "scanning": False,
                            "enabled": False})
            else:
                self._json(self.scan.result(
                    force=query.get("rescan", ["0"])[0] not in ("", "0")))
        else:
            self._send("text/plain", b"not found", 404)

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path.rstrip("/") not in ("", "/settings"):
            self._send("text/plain", b"not found", 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(raw)
        changes = {k: v[-1] for k, v in form.items() if k in EDITABLE}
        # An unchecked checkbox is simply absent from the POST body.
        for key in CHECKBOXES:
            changes.setdefault(key, "0")
        # A blank key field means "keep what is stored"; clearing one is an
        # explicit tick, so an ordinary save can never wipe a key by omission.
        for key in SECRET_KEYS:
            if f"clear_{key}" in form:
                changes[key] = ""
            elif not changes.get(key, "").strip():
                changes.pop(key, None)

        try:
            changed = self.settings.update(changes)
        except (ValueError, TypeError) as exc:
            self._send("text/html; charset=utf-8",
                       settings_page(self.settings, self.source,
                                     warn=f"设置未保存：{exc}",
                                     scan=self.scan), 400)
            return

        if changed:
            print(f"[settings] changed {', '.join(sorted(changed))} -> "
                  f"{self.settings.snapshot()}", flush=True)
            self.source.wake_clients()
            msg = "已保存：" + "、".join(sorted(changed))
        else:
            msg = "没有改动"

        self.send_response(303)
        self.send_header("Location", "/settings?saved=" +
                         urllib.parse.quote(msg))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _battery(self, query: dict) -> None:
        """``/battery?pct=57&charging=0`` — the handheld's own charge level.

        It is a GET because busybox's curl is what has to call it, and it repeats
        every minute anyway, so nothing is lost by making it the simplest request
        the handheld can send. The level is attributed to the caller's address
        rather than anything in the query, so one handheld cannot overwrite
        another's reading.
        """
        try:
            pct = float(query.get("pct", [""])[0])
        except (TypeError, ValueError):
            self._send("text/plain", b"bad pct", 400)
            return
        if not 0 <= pct <= 100:
            self._send("text/plain", b"pct out of range", 400)
            return
        charging = query.get("charging", ["0"])[0] not in ("0", "", "false")
        self.source.report_battery(self.client_address[0], pct, charging)
        self._send("text/plain", b"ok")

    @staticmethod
    def _orient(query: dict) -> int:
        """Quarter-turns clockwise the handheld is being held at; 0 if unstated."""
        try:
            return int(query.get("orient", ["0"])[0]) % 4
        except (TypeError, ValueError):
            return 0

    def _theme(self, query: dict, default: str | None = "") -> str | None:
        """Which palette the client wants.

        An unstated theme means the configured one for a stream the handheld is
        driving, and "whatever the handheld is using" for the browser — the same
        split as ``_page``, and for the same reason: the preview is meant to show
        what is on the device, not a second opinion about it.
        """
        name = query.get("theme", [""])[0]
        if not name:
            return (theme.resolve(self.settings.get("theme"))
                    if default == "" else default)
        return theme.resolve(name)

    @staticmethod
    def _page(query: dict, default: int | None = 0) -> int | None:
        """Which page the client wants. The browser's default is "whichever the
        handheld is on", which is a different thing from page 0."""
        if "page" not in query:
            return default
        try:
            return int(query["page"][0]) % render.PAGE_COUNT
        except (TypeError, ValueError):
            return default

    # Guard rails, because this arrives from the network: the strip is drawn from
    # these strings, and an unbounded list would mean unbounded variants to render.
    MAX_DEVICES = 8
    MAX_NAME = 18

    @classmethod
    def _chrome(cls, query: dict):
        """The handheld's device list as ``(names, index)``, or None if unstated."""
        raw = query.get("devs", [""])[0]
        names = tuple(n.strip()[:cls.MAX_NAME] for n in raw.split(",")
                      if n.strip())[:cls.MAX_DEVICES]
        if not names:
            return None
        try:
            idx = int(query.get("i", ["0"])[0])
        except (TypeError, ValueError):
            idx = 0
        return names, max(0, min(idx, len(names) - 1))

    def _stream(self, key: tuple, multipart: bool) -> None:
        self.send_response(200)
        if multipart:
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=pcmonframe")
        else:
            # ffmpeg's raw mjpeg demuxer scans for JPEG markers, so a bare
            # concatenation of frames is the simplest thing it will accept.
            self.send_header("Content-Type", "video/x-motion-jpeg")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        gen = self.settings.generation
        seq = -1
        self.source.acquire(key)
        try:
            while self.settings.generation == gen:
                frame, seq = self.source.wait_frame(key, seq)
                if frame is None:
                    continue  # variant not rendered yet — wait for the next cycle
                if multipart:
                    self.wfile.write(b"--pcmonframe\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\n\r\n" % len(frame))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            self.source.release(key)


def lan_ips() -> list[str]:
    """Real LAN addresses only — VM and tunnel adapters would just mislead."""
    import sysinfo

    out = []
    stats = sysinfo.net_if_stats()
    for name, addrs in sysinfo.net_if_addrs().items():
        st = stats.get(name)
        if not st or not st.isup or metrics._looks_virtual(name):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(
                    ("127.", "169.254.")) and a.address not in out:
                out.append(a.address)
    return out


def startup_shortcut() -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", "PC Monitor.vbs")


def _legacy_startup_shortcut() -> str:
    """Where older versions wrote their (visible-console) launcher.

    Cleaned up alongside the current one so an upgrade doesn't leave a stray
    console window popping up at every boot in addition to the silent one.
    """
    return os.path.splitext(startup_shortcut())[0] + ".cmd"


def _run_command() -> str:
    """The command line that starts this program, quoted for a shell."""
    if paths.frozen():
        return f'"{os.path.abspath(sys.executable)}"'
    return (f'"{os.path.abspath(sys.executable)}" '
            f'"{os.path.abspath(__file__)}"')


def systemd_unit() -> str:
    """Where the per-user service file goes. XDG says this path, no root needed."""
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(home, "systemd", "user", "pcmonitor.service")


def install_autostart_linux(remove: bool = False) -> int:
    """Add or remove a systemd **user** service. No root involved.

    A user unit rather than a system one, to match the Windows side: it needs no
    privileges, it runs as whoever installed it, and that user's session is where
    the API keys in config.json belong. The cost is that it stops at logout unless
    ``loginctl enable-linger`` has been run, which the printed note says — on a
    server you almost certainly want lingering on.
    """
    unit = systemd_unit()

    def systemctl(*args: str) -> bool:
        """Run one systemctl call, reporting rather than swallowing a failure.

        Plenty of Linux machines have no user manager running — a container, an
        ssh session with no lingering — and there the unit file is written
        correctly but nothing picks it up. Saying so beats printing "installed"
        over a service that will never start.
        """
        try:
            proc = subprocess.run(["systemctl", "--user", *args], check=False,
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[warn] systemctl --user {' '.join(args)}: {exc}")
            return False
        if proc.returncode:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            print(f"[warn] systemctl --user {' '.join(args)} failed: "
                  f"{detail[0] if detail else proc.returncode}")
            return False
        return True

    if remove:
        if not os.path.exists(unit):
            print("nothing to remove.")
            return 0
        systemctl("disable", "--now", "pcmonitor.service")
        os.remove(unit)
        systemctl("daemon-reload")
        print(f"removed {unit}")
        return 0

    body = (
        "[Unit]\n"
        "Description=PC Monitor telemetry server\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={paths.state_dir()}\n"
        f"ExecStart={_run_command()}\n"
        # A monitor that gives up because the network was down for a moment is
        # worse than useless, so it always comes back.
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    try:
        os.makedirs(os.path.dirname(unit), exist_ok=True)
        with open(unit, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        print(f"could not write {unit}: {exc}")
        return 1

    systemctl("daemon-reload")
    started = systemctl("enable", "--now", "pcmonitor.service")
    print(f"{'installed' if started else 'wrote'} {unit}\n"
          f"  runs: {_run_command()}\n"
          f"  status: systemctl --user status pcmonitor\n"
          f"  logs:   journalctl --user -u pcmonitor -f\n"
          f"  survives logout only after: "
          f"sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
    return 0


def install_autostart(remove: bool = False) -> int:
    """Add or remove a per-user startup entry. No admin rights involved.

    A .vbs launcher is used rather than a .lnk or a .cmd: a shortcut needs COM,
    and this has to work from a frozen exe with no extra dependencies. A .cmd
    works too but always flashes a console window (and, since this is a console
    subsystem exe, leaves it open showing server logs) — WScript.Shell.Run with
    window style 0 launches the same process with its window hidden, so boot
    stays silent. `False` for waitOnReturn means it hands off and returns.
    """
    if not IS_WINDOWS:
        return install_autostart_linux(remove)

    link = startup_shortcut()
    legacy = _legacy_startup_shortcut()
    if not os.environ.get("APPDATA"):
        print("APPDATA is not set — cannot find the Startup folder.")
        return 1

    if remove:
        removed = False
        for path in (link, legacy):
            if os.path.exists(path):
                os.remove(path)
                print(f"removed {path}")
                removed = True
        if not removed:
            print("nothing to remove.")
        return 0

    target = _run_command()
    vbs_target = target.replace('"', '""')
    vbs_cwd = paths.base_dir().replace('"', '""')
    try:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        with open(link, "w", encoding="mbcs") as fh:
            fh.write('Set shell = CreateObject("WScript.Shell")\r\n')
            fh.write(f'shell.CurrentDirectory = "{vbs_cwd}"\r\n')
            fh.write(f'shell.Run "{vbs_target}", 0, False\r\n')
        if os.path.exists(legacy):
            os.remove(legacy)
    except OSError as exc:
        print(f"could not write {link}: {exc}")
        return 1
    print(f"installed {link}\n  runs (hidden): {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PC telemetry MJPEG server")
    ap.add_argument("--port", type=int)
    ap.add_argument("--save", metavar="PNG",
                    help="render one frame to a file and exit")
    ap.add_argument("--install-autostart", action="store_true",
                    help="run automatically when this user logs in "
                         "(Startup folder on Windows, systemd --user on Linux)")
    ap.add_argument("--remove-autostart", action="store_true",
                    help="undo --install-autostart")
    args = ap.parse_args()

    if args.install_autostart or args.remove_autostart:
        return install_autostart(remove=args.remove_autostart)

    settings = Settings(CONFIG_PATH)
    port = args.port or int(settings.get("port"))
    source = FrameSource(settings)

    if args.save:
        time.sleep(1.2)  # let the pollers produce a first reading
        snap = source.collector.sample()
        render.draw_layout(snap, source.fonts,
                           theme=settings.get("theme")).save(args.save)
        source.collector.close()
        print("wrote", args.save)
        return 0

    source.start()
    scan = LanScan(settings)
    scan.start()
    Handler.source = source
    Handler.settings = settings
    Handler.scan = scan

    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # in use, and requests then land on whichever socket the OS picks — a second
    # copy of this server would silently answer some of them. Refusing to reuse
    # the address turns that into an error at startup instead.
    #
    # On Linux the option means the opposite: a listening socket still blocks a
    # second bind, and all SO_REUSEADDR does is let us take back a port stuck in
    # TIME_WAIT. Without it a restart within the minute after a client's stream
    # was cut — exactly what "systemctl restart" is — fails to bind.
    ThreadingHTTPServer.allow_reuse_address = not IS_WINDOWS
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        print(f"cannot listen on port {port}: {exc}\n"
              f"Another PC Monitor is probably already running.", flush=True)
        source.stop()
        return 1
    httpd.daemon_threads = True

    cfg = settings.snapshot()
    # flush explicitly: redirected stdout is block-buffered, and this banner is
    # the only place the handheld's URL is shown.
    print(f"PC Monitor listening on port {port} "
          f"({cfg['fps']} fps, quality {cfg['jpeg_quality']}, "
          f"rotate180={cfg['rotate180']})")
    for ip in lan_ips():
        print(f"  settings     : http://{ip}:{port}/settings")
        print(f"  handheld URL : http://{ip}:{port}/stream.mjpg")
        print(f"  AI quota     : http://{ip}:{port}/ai")
    print(f"  config       : {CONFIG_PATH}")
    if paths.frozen() and not os.path.exists(startup_shortcut()):
        print("  tip          : --install-autostart 可开机自启")
    print("Ctrl+C to stop.", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        httpd.shutdown()
        source.stop()
        scan.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
