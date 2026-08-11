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
                   ?orient=0..3 picks the layout, ?devs=a,b&i=0 draws the
                   handheld's own device switcher in the header
    /preview.mjpg  multipart/x-mixed-replace — what a browser <img> reads
    /preview       live preview of the dashboard
    /frame.jpg     a single current frame
    /config.json   effective settings, read by the handheld at launch
    /stats.json    the raw snapshot, for building other clients
    /battery       the handheld reports its own charge level here
"""

from __future__ import annotations

import argparse
import collections
import html
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import metrics
import paths
import render

CONFIG_PATH = os.path.join(paths.base_dir(), "config.json")

DEFAULTS = {
    "port": 8765,
    "fps": 8,
    "jpeg_quality": 72,
    "rotate180": True,
}

# Editable from the settings page: name -> (kind, low, high).
EDITABLE = {
    "fps": ("int", 1, 30),
    "jpeg_quality": ("int", 40, 95),
    "rotate180": ("bool", 0, 1),
}

# Only this one changes stream timing, so only it forces clients to reconnect.
RECONNECT_KEYS = {"fps"}


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

    A variant key is ``("panel", orient, chrome, client)`` for frames mapped onto
    the handheld's panel, or ``("upright", portrait)`` for the browser, which wants
    the layout the right way up. ``chrome`` is the handheld's own device list as
    ``(names, index)`` and ``client`` its address: both are part of the key because
    two handhelds need different headers — different device lists, and each one's
    own battery level.
    """

    def __init__(self, settings: Settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.collector = metrics.Collector()
        self.fonts = render.Fonts()

        self._cv = threading.Condition()
        self._variants: dict[tuple, bytes] = {}
        self._seq = 0
        self._snapshot: dict = {}
        self._wanted: collections.Counter = collections.Counter()
        self._stop = threading.Event()
        self.last_orient = 0
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

    def preview_key(self) -> tuple:
        """Upright variant matching however the handheld is currently held."""
        return ("upright", 1 if self.last_orient in (1, 3) else 0)

    def acquire(self, key: tuple) -> None:
        with self._cv:
            self._wanted[key] += 1
            if key[0] == "panel":
                self.last_orient = key[1]
                self.last_chrome = key[2]

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

            with self._cv:
                keys = set(self._wanted) | {self.preview_key()}

            variants = {}
            for key in keys:
                if key[0] == "panel":
                    _, orient, chrome, client = key
                    names, dev_idx = chrome or ((), 0)
                    img = render.render(snap, self.fonts, orient=orient,
                                        panel_flip=flip, devices=names,
                                        dev_idx=dev_idx,
                                        battery=self.battery(client))
                else:
                    img = render.draw_layout(snap, self.fonts,
                                             portrait=bool(key[1]))
                variants[key] = self._encode(img, quality)

            panel = [v for k, v in variants.items() if k[0] == "panel"]

            with self._cv:
                self._variants = variants
                self._snapshot = snap
                self._seq += 1
                # Report the handheld's frame size when there is one; that is the
                # number the bandwidth estimate on the settings page is about.
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


def settings_page(settings: Settings, source: FrameSource, message: str = "",
                  warn: str = "") -> bytes:
    cfg = settings.snapshot()
    fps = int(cfg["fps"])
    quality = int(cfg["jpeg_quality"])
    rotate = bool(cfg["rotate180"])
    frame_kb = max(1, source.last_frame_bytes // 1024)
    orient_label = ("横向", "竖向 ⟳", "横向 ⤒倒置", "竖向 ⟲")[source.last_orient % 4]

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

    <div class="actions">
      <button class="save" type="submit">保存</button>
      <span class="hint" style="margin:0">保存后掌机会自动重连</span>
    </div>
  </div>
</form>

<div class="card">
  <table>
    <tr><td>掌机朝向</td><td>{orient_label}（掌机上按 <b>Y</b> 切换）</td></tr>
    <tr><td>掌机扫到的设备</td><td>{devs}</td></tr>
    <tr><td>掌机电量</td><td>{batt}</td></tr>
    <tr><td>切换设备</td><td>掌机上按 <b>左 / 右</b>，退出按 <b>MENU</b></td></tr>
    <tr><td>实时预览</td><td><a href="/preview">/preview</a></td></tr>
    <tr><td>原始数据</td><td><a href="/stats.json">/stats.json</a> ·
      <a href="/config.json">/config.json</a></td></tr>
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
  document.getElementById('fpsOut').textContent = f + " fps";
  document.getElementById('qOut').textContent = q;
  // Frame size scales roughly linearly with quality in this range.
  const kb = {frame_kb} * (q / {quality});
  const bps = kb * 1024 * 8 * f;
  document.getElementById('bw').textContent = fmt(bps);
  document.getElementById('bwnote').textContent =
    bps > 12e6 ? " — 超出掌机 WiFi 可能承受的范围" : "";
}}
function setFps(v){{ document.getElementById('fps').value = v; sync(); }}
sync();
</script>
</html>""".encode("utf-8")


def preview_page() -> bytes:
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC Monitor 预览</title><style>{PAGE_CSS}</style>
<div class="wrap">
<h1>实时预览</h1>
<p class="sub">掌机上看到的画面，已转正。<a href="/settings">← 设置</a></p>
<img class="prev" src="/preview.mjpg" alt="dashboard">
</div></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "pcmon"
    source: FrameSource  # injected below
    settings: Settings

    QUIET_PATHS = ("/stats.json", "/config.json", "/battery")

    def log_message(self, fmt, *args):
        if not self.path.startswith(self.QUIET_PATHS):
            print(f"[http] {self.client_address[0]} {self.command} {self.path}",
                  flush=True)

    def _send(self, ctype: str, body: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/settings"):
            msg = query.get("saved", [""])[0]
            self._send("text/html; charset=utf-8",
                       settings_page(self.settings, self.source, message=msg))
        elif path == "/preview":
            self._send("text/html; charset=utf-8", preview_page())
        elif path == "/stream.mjpg":
            self._stream(("panel", self._orient(query), self._chrome(query),
                          self.client_address[0]), multipart=False)
        elif path == "/battery":
            self._battery(query)
        elif path == "/preview.mjpg":
            self._stream(self.source.preview_key(), multipart=True)
        elif path == "/frame.jpg":
            frame, _ = self.source.wait_frame(self.source.preview_key(), -1)
            if frame is None:
                self._send("text/plain", b"no frame yet", 503)
            else:
                self._send("image/jpeg", frame)
        elif path == "/config.json":
            body = json.dumps(dict(self.settings.snapshot(),
                                   name=socket.gethostname())).encode("utf-8")
            self._send("application/json", body)
        elif path == "/stats.json":
            body = json.dumps(self.source.snapshot, ensure_ascii=False,
                              default=str).encode("utf-8")
            self._send("application/json; charset=utf-8", body)
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
        if "rotate180" not in changes:
            changes["rotate180"] = "0"

        try:
            changed = self.settings.update(changes)
        except (ValueError, TypeError) as exc:
            self._send("text/html; charset=utf-8",
                       settings_page(self.settings, self.source,
                                     warn=f"设置未保存：{exc}"), 400)
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
    import psutil

    out = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
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
                        "Programs", "Startup", "PC Monitor.cmd")


def install_autostart(remove: bool = False) -> int:
    """Add or remove a per-user startup entry. No admin rights involved.

    A .cmd that launches the program and exits is used rather than a .lnk: making
    a shortcut needs COM, and this has to work from a frozen exe with no extra
    dependencies. `start` hands off and returns, so nothing lingers.
    """
    link = startup_shortcut()
    if not os.environ.get("APPDATA"):
        print("APPDATA is not set — cannot find the Startup folder.")
        return 1

    if remove:
        if os.path.exists(link):
            os.remove(link)
            print(f"removed {link}")
        else:
            print("nothing to remove.")
        return 0

    if paths.frozen():
        target = f'"{os.path.abspath(sys.executable)}"'
    else:
        target = (f'"{os.path.abspath(sys.executable)}" '
                  f'"{os.path.abspath(__file__)}"')
    try:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        with open(link, "w", encoding="mbcs") as fh:
            fh.write("@echo off\r\n")
            fh.write(f'cd /d "{paths.base_dir()}"\r\n')
            fh.write(f"start \"PC Monitor\" {target}\r\n")
    except OSError as exc:
        print(f"could not write {link}: {exc}")
        return 1
    print(f"installed {link}\n  runs: {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PC telemetry MJPEG server")
    ap.add_argument("--port", type=int)
    ap.add_argument("--save", metavar="PNG",
                    help="render one frame to a file and exit")
    ap.add_argument("--install-autostart", action="store_true",
                    help="run automatically when this user logs in")
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
        render.draw_layout(snap, source.fonts).save(args.save)
        source.collector.close()
        print("wrote", args.save)
        return 0

    source.start()
    Handler.source = source
    Handler.settings = settings

    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # in use, and requests then land on whichever socket the OS picks — a second
    # copy of this server would silently answer some of them. Refusing to reuse
    # the address turns that into an error at startup instead.
    ThreadingHTTPServer.allow_reuse_address = False
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
