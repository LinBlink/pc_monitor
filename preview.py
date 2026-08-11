"""Render sample frames to PNG so both layouts can be eyeballed without a device."""

import math
import sys
import time

import render

HISTORY = 72


def wave(n, lo, hi, k=1.0, phase=0.0):
    return [lo + (hi - lo) * (0.5 + 0.5 * math.sin(phase + i * k / 6)) for i in range(n)]


def snapshot(with_fps: bool) -> dict:
    n = HISTORY
    return {
        "host": "LINPRO",
        "time": "16:24:07",
        "cpu": {"name": "Intel", "percent": 63.4, "ghz": 4.72, "cores": 16,
                "hist": wave(n, 8, 88, 1.3), "peak": 91.0,
                "core_pct": [round(v) for v in wave(16, 5, 99, 5.0)],
                "core_mhz": [4300, 4300, 4100, 4100, 4300, 4300, 4300, 4300,
                             4100, 4300, 4300, 4300, 4000, 4100, 4100, 4300],
                "core_temps": [62, 62, 64, 64, 67, 67, 67, 67,
                               64, 64, 65, 65, 64, 64, 65, 65],
                "temp_c": 68.0, "power_w": 95.4, "sensors": True},
        "mem": {"percent": 45.8, "used_gb": 21.9, "total_gb": 47.8,
                "hist": wave(n, 40, 52, 0.5)},
        "net": {"nic": "Ethernet 2", "down_bps": 11.4 * 1024**2,
                "up_bps": 812 * 1024,
                "down_hist": [v * 1024**2 for v in wave(n, 0.2, 23.5, 2.1)],
                "up_hist": [v * 1024 for v in wave(n, 20, 1400, 1.7, 2.0)],
                "down_peak": 23.5 * 1024**2, "up_peak": 1400 * 1024,
                "day_down": 8.24 * 1024**3, "day_up": 1.12 * 1024**3},
        "gpu": {"ok": True, "name": "GeForce RTX 3080", "percent": 97.0,
                "temp_c": 71.0, "power_w": 318.0, "mem_used_gb": 8.4,
                "mem_total_gb": 10.0, "hist": wave(n, 60, 99, 1.1)},
        "fps": ({"rtss": True, "value": 142.0, "frametime_ms": 7.0,
                 "process": "Cyberpunk2077.exe", "hist": wave(n, 96, 165, 1.9)}
                if with_fps else
                {"rtss": False, "value": None, "frametime_ms": None,
                 "process": None, "hist": [0.0] * n}),
        "top": [("chrome.exe", 14.2), ("Code.exe", 6.1), ("python.exe", 3.4)],
        "gpu_top": [("Cyberpunk2077.exe", 92.0), ("chrome.exe", 4.3),
                    ("dwm.exe", 1.2)],
        # Deliberately awkward: a maxed-out window, a plan without Opus, extra
        # usage switched off and MiniMax unconfigured — the states most likely
        # to break the layout all at once.
        "ai": {
            "claude": {
                "ok": True, "err": None, "plan": "pro",
                "five_hour": {"pct": 100.0, "resets_at": time.time() + 4 * 3600 + 720},
                "seven_day": {"pct": 33.0, "resets_at": time.time() + 3 * 86400},
                "seven_day_opus": None,
                "extra": {"enabled": False, "pct": None, "used": 60.13,
                          "currency": "USD", "reason": "out_of_credits"},
            },
            "deepseek": {"ok": True, "err": None, "available": True,
                         "balance": 42.7, "currency": "CNY"},
            "minimax": None,
            "at": time.time(),
        },
        "weather": {
            "ok": True, "err": None, "city": "深圳", "at": time.time(),
            "now": {"code": 0, "text": "晴", "temp": 28.4},
            "h3": {"code": 2, "text": "少云", "temp": 29.1},
            "h6": {"code": 61, "text": "雨", "temp": 26.0},
            "d1": {"code": 3, "text": "阴", "tmin": 24.0, "tmax": 31.0},
            "d2": {"code": 95, "text": "雷雨", "tmin": 23.0, "tmax": 30.0},
        },
    }


DEVICES = ("LINPRO", "STUDIO-PC", "NAS-01")
BATTERY = {"percent": 78, "charging": False}

if __name__ == "__main__":
    fonts = render.Fonts()
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    for tag, flag in (("game", True), ("idle", False)):
        for portrait, oname in ((False, "landscape"), (True, "portrait")):
            img = render.draw_layout(snapshot(flag), fonts, portrait=portrait,
                                     devices=DEVICES, dev_idx=0,
                                     battery=BATTERY)
            path = f"{out}/preview_{oname}_{tag}.png"
            img.save(path)
            print("wrote", path, img.size)
