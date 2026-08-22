"""Render sample frames to PNG so both layouts can be eyeballed without a device."""

import math
import sys
import time

import render

HISTORY = 72


def wave(n, lo, hi, k=1.0, phase=0.0):
    return [lo + (hi - lo) * (0.5 + 0.5 * math.sin(phase + i * k / 6)) for i in range(n)]


def snapshot(with_fps: bool, docker: bool = True) -> dict:
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
            "minimax": {"ok": True, "err": None, "model": "general",
                        "five_hour": 8.0, "weekly": 41.0,
                        "five_hour_reset": time.time() + 1800,
                        "weekly_reset": time.time() + 5 * 86400,
                        "models": []},
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
        "mem_top": [("idea64", 3061.0), ("Memory Compression", 3027.4),
                    ("Code", 981.2)],
        # A young log: the 30-day figure has to admit it is not a month yet.
        "power": {"watts": 431.7, "estimated": False, "d1": 2.14, "d7": 9.83,
                  "d30": 9.83, "cost30": 5.9, "days": 4},
        # Under load the drive is hot enough to colour its dot; idle exercises
        # the other branch, where the drive reports no temperature at all.
        "disk": ({"ok": True, "err": None, "letter": "C",
                  "temp_c": 68.0, "temp_warn": 82.0, "temp_crit": 85.0,
                  "read_bps": 412.0 * 1024**2, "write_bps": 96.4 * 1024**2,
                  "used_gb": 338.8, "total_gb": 464.8, "used_pct": 72.9}
                 if with_fps else
                 {"ok": True, "err": None, "letter": "C",
                  "temp_c": None, "temp_warn": None, "temp_crit": None,
                  "read_bps": 0.0, "write_bps": 18.2 * 1024,
                  "used_gb": 338.8, "total_gb": 464.8, "used_pct": 72.9}),
        "docker": ({
            "ok": True, "err": None, "running": 3, "total": 5,
            "containers": [
                {"name": "immich-server", "state": "running", "status": "Up 3 days",
                 "image": "immich", "cpu": 12.4, "mem_mb": 812.0, "mem_pct": 10.4},
                {"name": "postgres", "state": "running", "status": "Up 3 days",
                 "image": "pg", "cpu": 2.1, "mem_mb": 240.0, "mem_pct": 3.1},
                {"name": "a-very-long-container-name-here", "state": "running",
                 "status": "Up 1 hour", "image": "x", "cpu": 0.3,
                 "mem_mb": 1536.0, "mem_pct": 19.5},
                {"name": "redis", "state": "exited", "status": "Exited (0) 2 days ago",
                 "image": "redis", "cpu": None, "mem_mb": None, "mem_pct": None},
                {"name": "watchtower", "state": "paused", "status": "Paused",
                 "image": "wt", "cpu": None, "mem_mb": None, "mem_pct": None},
            ]} if docker else
            {"ok": False, "err": "未安装", "containers": []}),
        "advice": {
            "enabled": True, "ok": True, "err": None, "level": "warn",
            "provider": "MiniMax", "at": time.time() - 240, "id": 7,
            "text": "GPU 已连续 20 分钟满载且温度接近 80°C，同时 idea64 占用了 3 GB "
                    "内存；如果不是在跑渲染任务，建议检查后台是否有挖矿或失控进程，"
                    "并确认机箱进风口没有被挡住。",
        },
    }


DEVICES = ("LINPRO", "STUDIO-PC", "NAS-01")
BATTERY = {"percent": 78, "charging": False}

if __name__ == "__main__":
    fonts = render.Fonts()
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    # Page 1 in both orientations for the two states that stress it, then page 2
    # with and without Docker — the two grids it switches between. The last two
    # are the same page 1 in the other theme: what changes between themes is
    # colour only, so one state is enough to see all of it.
    shots = [(f"{tag}", snapshot(flag), 0, "dark") for tag, flag in
             (("game", True), ("idle", False))]
    shots += [("docker", snapshot(True, docker=True), 1, "dark"),
              ("nodocker", snapshot(True, docker=False), 1, "dark"),
              ("term", snapshot(True), 0, "term")]
    for tag, snap, page, theme in shots:
        for portrait, oname in ((False, "landscape"), (True, "portrait")):
            img = render.draw_layout(snap, fonts, portrait=portrait,
                                     devices=DEVICES, dev_idx=0,
                                     battery=BATTERY, page=page, theme=theme)
            path = f"{out}/preview_{oname}_{tag}.png"
            img.save(path)
            print("wrote", path, img.size)
