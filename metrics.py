"""Sampling of PC telemetry into a plain snapshot dict for the renderer.

A single :class:`Collector` runs in the server's frame loop. Cheap counters (CPU,
memory, network) are read inline; anything that needs a subprocess or a full
process walk runs on its own background thread so the frame rate never waits on
it.
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import platform
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

import psutil

import rtss
import sensors

HISTORY = 72  # samples kept per sparkline

_VIRTUAL_NIC_HINTS = ("loopback", "vethernet", "vmware", "virtualbox", "hyper-v",
                      "bluetooth", "teredo", "isatap", "tap-", "wsl", "docker",
                      "todesk", "tailscale", "zerotier")


def _looks_virtual(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in _VIRTUAL_NIC_HINTS)


@dataclass
class GpuInfo:
    name: str = ""
    util: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    temp_c: float = 0.0
    power_w: float = 0.0
    ok: bool = False


@dataclass
class Ring:
    """Fixed-length history that always reports a stable maximum."""
    values: collections.deque = field(
        default_factory=lambda: collections.deque([0.0] * HISTORY, maxlen=HISTORY))

    def push(self, v: float) -> None:
        self.values.append(float(v))

    def list(self) -> list[float]:
        return list(self.values)

    @property
    def peak(self) -> float:
        return max(self.values) if self.values else 0.0


class GpuPoller(threading.Thread):
    """Keeps one long-lived ``nvidia-smi`` running instead of spawning per frame."""

    QUERY = ("name,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw")

    def __init__(self, interval_s: float = 1.0):
        super().__init__(daemon=True)
        self.interval_s = interval_s
        self.info = GpuInfo()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            proc = None
            try:
                proc = subprocess.Popen(
                    ["nvidia-smi", f"--query-gpu={self.QUERY}",
                     "--format=csv,noheader,nounits",
                     f"--loop={max(1, int(self.interval_s))}"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    self._parse(line)
            except (OSError, ValueError):
                self.info = GpuInfo()
            finally:
                if proc and proc.poll() is None:
                    proc.kill()
            # nvidia-smi died (driver reload, sleep/resume) — back off and retry.
            if not self._stop.wait(5.0):
                continue

    def _parse(self, line: str) -> None:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            return

        def num(s: str) -> float:
            try:
                return float(s)
            except ValueError:
                return 0.0

        self.info = GpuInfo(name=parts[0], util=num(parts[1]),
                            mem_used_mb=num(parts[2]), mem_total_mb=num(parts[3]),
                            temp_c=num(parts[4]), power_w=num(parts[5]), ok=True)


class GpuProcPoller(threading.Thread):
    """Top GPU consumers, from the same counters Task Manager uses.

    ``\\GPU Engine(*)\\Utilization Percentage`` has one instance per process and
    engine, named ``pid_1234_luid_..._engtype_3D``. Summing a process's engines
    gives the figure people recognise from Task Manager's GPU column, and unlike
    ``nvidia-smi pmon`` it is vendor-neutral and lists graphics work, not just
    compute. Get-Counter re-enumerates instances every sample, so processes that
    start later still appear — which ``typeperf`` with a wildcard would not do.
    """

    SCRIPT = (
        "$ErrorActionPreference='SilentlyContinue';"
        "while($true){"
        " $s=(Get-Counter '\\GPU Engine(*)\\Utilization Percentage').CounterSamples;"
        " if($s){"
        "  $s | Group-Object {($_.InstanceName -split '_')[1]} | ForEach-Object {"
        "   $v=($_.Group | Measure-Object CookedValue -Sum).Sum;"
        "   if($v -gt 0.05){ \"$($_.Name) $([math]::Round($v,2))\" } } }"
        " '---'; [Console]::Out.Flush(); Start-Sleep -Seconds %d }"
    )

    def __init__(self, interval_s: float = 2.0, count: int = 3):
        super().__init__(daemon=True)
        self.interval_s = max(1, int(interval_s))
        self.count = count
        self.top: list[tuple[str, float]] = []
        self.ok = False
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            proc = None
            try:
                proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     self.SCRIPT % self.interval_s],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                batch: list[tuple[int, float]] = []
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if line == "---":
                        self._publish(batch)
                        batch = []
                        continue
                    pid, _, value = line.partition(" ")
                    try:
                        batch.append((int(pid), float(value)))
                    except ValueError:
                        continue
            except (OSError, ValueError):
                self.ok = False
            finally:
                if proc and proc.poll() is None:
                    proc.kill()
            if not self._stop.wait(5.0):
                continue

    def _publish(self, batch: list[tuple[int, float]]) -> None:
        rows = []
        for pid, pct in sorted(batch, key=lambda r: r[1], reverse=True):
            if len(rows) >= self.count:
                break
            try:
                rows.append((psutil.Process(pid).name(), min(pct, 100.0)))
            except (psutil.Error, ValueError):
                continue  # exited between the counter read and now
        self.top = rows
        self.ok = True


class DailyTraffic:
    """Bytes moved since local midnight, kept across restarts.

    The OS has no per-day total to ask for, so this accumulates the same deltas
    the rate display is built from and persists them; the file is what makes a
    server restart not lose the day. Only time the server was running is counted.
    """

    SAVE_EVERY_S = 30.0

    def __init__(self, path: str):
        self.path = path
        self.down = 0.0
        self.up = 0.0
        self.day = self._today()
        self._last_save = 0.0
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.date.today().isoformat()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("day") == self.day:
            self.down = float(data.get("down", 0.0))
            self.up = float(data.get("up", 0.0))

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"day": self.day, "down": round(self.down),
                           "up": round(self.up)}, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add(self, down_bytes: float, up_bytes: float) -> None:
        today = self._today()
        if today != self.day:
            self.day, self.down, self.up = today, 0.0, 0.0
            self._last_save = 0.0
        self.down += max(0.0, down_bytes)
        self.up += max(0.0, up_bytes)
        now = time.monotonic()
        if now - self._last_save >= self.SAVE_EVERY_S:
            self._last_save = now
            self._save()


class TopProcPoller(threading.Thread):
    """Top CPU consumers, refreshed slowly — a full process walk is not cheap."""

    def __init__(self, interval_s: float = 3.0, count: int = 3):
        super().__init__(daemon=True)
        self.interval_s = interval_s
        self.count = count
        self.top: list[tuple[str, float]] = []
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        ncpu = psutil.cpu_count() or 1
        for p in psutil.process_iter(["name"]):
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        while not self._stop.wait(self.interval_s):
            rows: list[tuple[str, float]] = []
            for p in psutil.process_iter(["name"]):
                if p.pid == 0:  # "System Idle Process" is not a consumer
                    continue
                try:
                    pct = p.cpu_percent(None) / ncpu
                except psutil.Error:
                    continue
                # pct is a share of the whole machine, so the floor has to be low
                # enough to still surface something on a mostly idle box.
                if pct > 0.1:
                    rows.append((p.info.get("name") or "?", pct))
            rows.sort(key=lambda r: r[1], reverse=True)
            self.top = rows[: self.count]


TRAFFIC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "traffic.json")


class Collector:
    def __init__(self):
        self.host = socket.gethostname()
        self.cpu_name = self._cpu_name()
        self.cpu_cores = psutil.cpu_count(logical=True) or 1

        self.cpu_hist = Ring()
        self.gpu_hist = Ring()
        self.mem_hist = Ring()
        self.down_hist = Ring()
        self.up_hist = Ring()
        self.fps_hist = Ring()

        self.nic = self._pick_nic()
        self._last_net = self._nic_counters()
        self._last_net_t = time.monotonic()

        self.traffic = DailyTraffic(TRAFFIC_PATH)

        psutil.cpu_percent(None)  # prime the delta
        psutil.cpu_percent(None, percpu=True)  # percpu keeps its own last-values

        self.gpu = GpuPoller()
        self.gpu.start()
        self.procs = TopProcPoller()
        self.procs.start()
        self.gpu_procs = GpuProcPoller()
        self.gpu_procs.start()

        self._rtss_checked = 0.0
        self._rtss_up = False
        # Afterburner is the only CPU temperature source here, and its shared
        # memory has to be re-walked to notice it appearing or going away.
        self._sensors_checked = 0.0
        self._sensors: sensors.Sensors | None = None

    def close(self) -> None:
        self.gpu.stop()
        self.procs.stop()
        self.gpu_procs.stop()

    @staticmethod
    def _cpu_name() -> str:
        name = platform.processor() or ""
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
        except OSError:
            pass
        return " ".join(name.replace("(R)", "").replace("(TM)", "").split())

    @staticmethod
    def _pick_nic() -> str | None:
        stats = psutil.net_if_stats()
        counters = psutil.net_io_counters(pernic=True)
        best, best_bytes = None, -1
        for name, st in stats.items():
            if not st.isup or _looks_virtual(name):
                continue
            c = counters.get(name)
            if not c:
                continue
            total = c.bytes_sent + c.bytes_recv
            if total > best_bytes:
                best, best_bytes = name, total
        return best

    def _nic_counters(self):
        if self.nic:
            c = psutil.net_io_counters(pernic=True).get(self.nic)
            if c:
                return c.bytes_recv, c.bytes_sent
        c = psutil.net_io_counters()
        return c.bytes_recv, c.bytes_sent

    def sample(self) -> dict:
        now = time.monotonic()

        cpu = psutil.cpu_percent(None)
        self.cpu_hist.push(cpu)
        cores = psutil.cpu_percent(None, percpu=True) or []

        # Afterburner reads the CPU's own registers, so prefer its clock over
        # psutil's, which reports the nominal base frequency on Windows.
        if now - self._sensors_checked > 2.0:
            self._sensors = sensors.read()
            self._sensors_checked = now
        sens = self._sensors

        freq = psutil.cpu_freq()
        cpu_ghz = (freq.current / 1000.0) if freq and freq.current else 0.0
        if sens and sens.cpu_clock_mhz:
            cpu_ghz = sens.cpu_clock_mhz / 1000.0

        vm = psutil.virtual_memory()
        self.mem_hist.push(vm.percent)

        recv, sent = self._nic_counters()
        dt = max(1e-3, now - self._last_net_t)
        d_bytes = max(0.0, recv - self._last_net[0])
        u_bytes = max(0.0, sent - self._last_net[1])
        down = d_bytes / dt
        up = u_bytes / dt
        self._last_net, self._last_net_t = (recv, sent), now
        self.traffic.add(d_bytes, u_bytes)
        self.down_hist.push(down)
        self.up_hist.push(up)

        gpu = self.gpu.info
        self.gpu_hist.push(gpu.util if gpu.ok else 0.0)

        # Re-probe RTSS presence occasionally so the hint text stays accurate.
        if now - self._rtss_checked > 4.0:
            self._rtss_up = rtss.is_running()
            self._rtss_checked = now
        fps = rtss.read_fps() if self._rtss_up else None
        self.fps_hist.push(fps.fps if fps else 0.0)

        return {
            "host": self.host,
            "time": time.strftime("%H:%M:%S"),
            "cpu": {
                "name": self.cpu_name,
                "percent": cpu,
                "ghz": cpu_ghz,
                "cores": self.cpu_cores,
                "hist": self.cpu_hist.list(),
                "peak": self.cpu_hist.peak,
                "core_pct": list(cores),
                "core_mhz": list(sens.core_clocks_mhz) if sens else [],
                "core_temps": list(sens.core_temps_c) if sens else [],
                "temp_c": sens.cpu_temp_c if sens else None,
                "power_w": sens.cpu_power_w if sens else None,
                "sensors": sens is not None,
            },
            "mem": {
                "percent": vm.percent,
                "used_gb": vm.used / 1024**3,
                "total_gb": vm.total / 1024**3,
                "hist": self.mem_hist.list(),
            },
            "net": {
                "nic": self.nic or "—",
                "down_bps": down,
                "up_bps": up,
                "down_hist": self.down_hist.list(),
                "up_hist": self.up_hist.list(),
                "down_peak": self.down_hist.peak,
                "up_peak": self.up_hist.peak,
                "day_down": self.traffic.down,
                "day_up": self.traffic.up,
            },
            "gpu": {
                "ok": gpu.ok,
                "name": gpu.name.replace("NVIDIA ", ""),
                "percent": gpu.util,
                "temp_c": gpu.temp_c,
                "power_w": gpu.power_w,
                "mem_used_gb": gpu.mem_used_mb / 1024.0,
                "mem_total_gb": gpu.mem_total_mb / 1024.0,
                "hist": self.gpu_hist.list(),
            },
            "fps": {
                "rtss": self._rtss_up,
                "value": fps.fps if fps else None,
                "frametime_ms": fps.frametime_ms if fps else None,
                "process": fps.process if fps else None,
                "hist": self.fps_hist.list(),
            },
            "top": list(self.procs.top),
            "gpu_top": list(self.gpu_procs.top),
        }


if __name__ == "__main__":
    import json

    c = Collector()
    time.sleep(1.5)
    for _ in range(2):
        print(json.dumps(c.sample(), ensure_ascii=False, indent=2,
                         default=lambda o: round(o, 2)))
        time.sleep(1)
    c.close()
