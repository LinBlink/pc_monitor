"""Current weather and a short forecast, polled in the background.

Open-Meteo is used because it needs no API key and no account, which matters
here: this program has no secret store, and a widget that stops working when a
free tier expires is worse than no widget. Location comes from the public IP
unless config.json pins it, so a fresh install shows the right city with nothing
to fill in.

One request carries everything the tile draws — now, +3h, +6h, tomorrow and the
day after — and the source data itself only moves hourly, so a quarter-hour
interval is plenty (96 calls a day against a 10k limit).
"""

from __future__ import annotations

import json
import threading
import time

import webjson

GEO_URL = "http://ip-api.com/json/?fields=status,city,lat,lon"
FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat:.4f}&longitude={lon:.4f}"
    "&current=temperature_2m,weather_code"
    "&hourly=temperature_2m,weather_code"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min"
    "&forecast_days=3&timezone=auto"
)

EVERY_S = 900.0
RETRY_S = 60.0
GEO_RETRY_S = 1800.0

# WMO weather codes, collapsed to two characters: the tile is a 24px strip on a
# 3.5" panel, so a precise phrase would only be ellipsized into a wrong one.
_CODES = (
    ((0,), "晴"), ((1, 2), "少云"), ((3,), "阴"), ((45, 48), "雾"),
    ((51, 53, 55, 56, 57), "毛雨"), ((61, 63, 65, 66, 67), "雨"),
    ((71, 73, 75, 77), "雪"), ((80, 81, 82), "阵雨"), ((85, 86), "阵雪"),
    ((95, 96, 99), "雷雨"),
)


def label(code) -> str:
    for codes, name in _CODES:
        if code in codes:
            return name
    return "—"


class WeatherPoller(threading.Thread):
    """Latest forecast in ``data``; never raises, never blocks the frame loop."""

    def __init__(self, cfg=None):
        super().__init__(daemon=True)
        self._cfg = cfg or (lambda: {})
        self.data: dict = {"ok": False, "city": "", "err": "starting"}
        self._stop = threading.Event()
        self._geo: tuple[float, float, str] | None = None
        self._geo_due = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        due = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= due:
                ok = self._poll(self._cfg() or {})
                due = time.monotonic() + (EVERY_S if ok else RETRY_S)
            self._stop.wait(5.0)

    def _locate(self, cfg: dict):
        """Configured coordinates win; otherwise geolocate the public IP once."""
        lat, lon = cfg.get("weather_lat"), cfg.get("weather_lon")
        city = (cfg.get("weather_city") or "").strip()
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon), city

        if self._geo and time.monotonic() < self._geo_due:
            return self._geo[0], self._geo[1], city or self._geo[2]

        status, payload = webjson.get_json(GEO_URL, timeout=5.0)
        if status == 200 and isinstance(payload, dict) and \
                payload.get("status") == "success":
            self._geo = (float(payload["lat"]), float(payload["lon"]),
                         payload.get("city") or "")
            self._geo_due = time.monotonic() + GEO_RETRY_S
            return self._geo[0], self._geo[1], city or self._geo[2]
        return None

    def _poll(self, cfg: dict) -> bool:
        where = self._locate(cfg)
        if where is None:
            if not self.data.get("ok"):
                self.data = {"ok": False, "city": "", "err": "定位中"}
            return False
        lat, lon, city = where

        status, payload = webjson.get_json(
            FORECAST_URL.format(lat=lat, lon=lon), timeout=10.0)
        if status != 200 or not isinstance(payload, dict):
            if not self.data.get("ok"):
                self.data = {"ok": False, "city": city, "err": "无法获取"}
            return False

        try:
            self.data = _shape(payload, city)
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        return True


def _shape(payload: dict, city: str) -> dict:
    cur = payload.get("current") or {}
    hourly = payload.get("hourly") or {}
    daily = payload.get("daily") or {}
    times = hourly.get("time") or []

    # The hourly series starts at local midnight of the first forecast day, not
    # at "now", so the offsets have to be measured from where the current hour
    # actually sits in the array.
    now_hour = (cur.get("time") or "")[:13]
    try:
        base = times.index(next(t for t in times if t[:13] == now_hour))
    except (StopIteration, ValueError):
        base = 0

    def at(offset: int) -> dict | None:
        i = base + offset
        temps, codes = hourly.get("temperature_2m") or [], \
            hourly.get("weather_code") or []
        if i >= len(temps) or i >= len(codes):
            return None
        return {"code": codes[i], "text": label(codes[i]), "temp": temps[i]}

    def day(i: int) -> dict | None:
        codes = daily.get("weather_code") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        if i >= len(codes) or i >= len(highs) or i >= len(lows):
            return None
        return {"code": codes[i], "text": label(codes[i]),
                "tmax": highs[i], "tmin": lows[i]}

    return {
        "ok": True,
        "err": None,
        "city": city,
        "at": time.time(),
        "now": {"code": cur.get("weather_code"),
                "text": label(cur.get("weather_code")),
                "temp": cur.get("temperature_2m")},
        "h3": at(3),
        "h6": at(6),
        # Index 0 is today, which the current reading already covers.
        "d1": day(1),
        "d2": day(2),
    }


if __name__ == "__main__":
    poller = WeatherPoller(lambda: {})
    poller.start()
    for _ in range(20):
        time.sleep(1.0)
        if poller.data.get("ok"):
            break
    print(json.dumps(poller.data, ensure_ascii=False, indent=2))
    poller.stop()
