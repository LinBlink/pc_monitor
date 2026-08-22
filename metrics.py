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

import advice
import aiquota
import alerts
import diskstat
import dockerstat
import paths
import perfcounters
import power
import rtss
import sensors
import sysinfo
import weather

HISTORY = 72  # samples kept per sparkline

IS_WINDOWS = os.name == "nt"

_VIRTUAL_NIC_HINTS = ("loopback", "vethernet", "vmware", "virtualbox", "hyper-v",
                      "bluetooth", "teredo", "isatap", "tap-", "wsl", "docker",
                      "todesk", "tailscale", "zerotier")

# Linux interface names are short and mean nothing to a substring search, so they
# are matched as prefixes instead: "lo", "virbr0", "veth3f2a", "br-1a2b", "tun0".
_VIRTUAL_NIC_PREFIXES = ("lo", "veth", "virbr", "br-", "docker", "tun", "tap",
                         "tailscale", "zt", "wg")


def _looks_virtual(name: str) -> bool:
    low = name.lower()
    if any(h in low for h in _VIRTUAL_NIC_HINTS):
        return True
    return not IS_WINDOWS and low.startswith(_VIRTUAL_NIC_PREFIXES)


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


_PROC_PATHS = {
    "cpu": r"\Process(*)\% Processor Time",
    "gpu": r"\GPU Engine(*)\Utilization Percentage",
    # Private working set, not the plain one: shared pages (every process maps
    # the same system DLLs) would otherwise be counted once per process and the
    # column would add up to several times the RAM in the machine.
    "mem": r"\Process(*)\Working Set - Private",
}


class ProcPoller(threading.Thread):
    """Top CPU, GPU and memory consumers, sampled through PDH directly.

    The previous version spawned a PowerShell child that called Get-Counter
    twice per cycle. Get-Counter wraps every one of ~500 GPU-engine instances
    in a rich .NET object, which cost ~2.5 seconds of CPU per sample — enough
    to push our own helper to the top of the CPU list. PDH returns the same
    data in a few milliseconds, so the heavy work happens in C and this
    thread only does aggregation.

    CPU comes from ``\\Process(*)\\% Processor Time``: instances are named
    after the process with a ``#N`` suffix for duplicates. Summing them and
    dividing by the core count gives a share of the whole machine.

    GPU comes from ``\\GPU Engine(*)\\Utilization Percentage``: instances are
    named ``pid_PID_luid_..._engtype_TYPE``, so several engines per process
    have to be summed by PID. The result matches Task Manager's GPU column
    and, unlike ``nvidia-smi pmon``, counts graphics work, not just compute.

    Memory comes from ``\\Process(*)\\Working Set - Private`` and is aggregated
    the same way as CPU, so a browser's dozen renderers are reported as one
    entry rather than filling the table with copies of the same name.

    English counter paths work on every locale via ``PdhAddEnglishCounterW``,
    so no registry name translation is needed.

    On Linux none of that applies: ``/proc`` is a handful of small text files, so
    the plain process walk that was too slow on Windows costs a few milliseconds
    and is what :meth:`_run_linux` does. Only the GPU still needs a subprocess.
    """

    def __init__(self, interval_s: float = 2.0, count: int = 3):
        super().__init__(daemon=True)
        self.interval_s = max(0.5, float(interval_s))
        self.count = count
        self.cpu_top: list[tuple[str, float]] = []
        self.gpu_top: list[tuple[str, float]] = []
        self.mem_top: list[tuple[str, float]] = []  # (name, MB)
        self.ok = False
        self._stop = threading.Event()
        self._ncpu = max(1, sysinfo.cpu_count(logical=True) or 1)
        self._pmon_ok = True
        self._pmon_retry = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not IS_WINDOWS:
            self._run_linux()
            return

        try:
            query = perfcounters.CounterQuery(_PROC_PATHS)
        except perfcounters.PdhError:
            return

        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                data = query.collect()
                self._publish(data)
                wait = self.interval_s - (time.monotonic() - t0)
                if wait > 0:
                    self._stop.wait(wait)
        finally:
            query.close()

    def _run_linux(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._sample_linux()
            wait = self.interval_s - (time.monotonic() - t0)
            if wait > 0:
                self._stop.wait(wait)

    def _sample_linux(self) -> None:
        """Top CPU and memory from ``/proc``, top GPU from ``nvidia-smi pmon``.

        ``process_iter`` keeps its own cache of Process objects between calls,
        which is what makes ``cpu_percent()`` a delta over our interval rather
        than an average since boot. The first pass therefore reports zeros.

        Memory is RSS: the private working set (USS) would mean reading ``smaps``
        for every process, which is a hundred times dearer and needs privileges
        we do not ask for.
        """
        cpu_by_name: dict[str, float] = {}
        mem_by_name: dict[str, float] = {}
        # ad_value keeps a process we may not inspect — another user's, or one
        # in a container — from aborting the whole walk; it just contributes
        # nothing.
        for proc in sysinfo.process_iter(["name", "cpu_percent", "memory_info"],
                                        ad_value=None):
            info = proc.info
            name = info.get("name") or ""
            if not name:
                continue  # a kernel thread, or it exited mid-walk
            cpu_by_name[name] = cpu_by_name.get(name, 0.0) + (
                info.get("cpu_percent") or 0.0)
            mem = info.get("memory_info")
            if mem:
                mem_by_name[name] = mem_by_name.get(name, 0.0) + mem.rss

        self.cpu_top = [(name, min(value / self._ncpu, 100.0))
                        for name, value in
                        sorted(cpu_by_name.items(),
                               key=lambda r: r[1], reverse=True)[:self.count]]
        self.mem_top = [(name, value / 1024.0 ** 2)
                        for name, value in
                        sorted(mem_by_name.items(),
                               key=lambda r: r[1], reverse=True)[:self.count]]
        self.gpu_top = self._gpu_top_linux()
        self.ok = True

    def _gpu_top_linux(self) -> list[tuple[str, float]]:
        """Per-process GPU utilisation from ``nvidia-smi pmon``.

        There is no ``/proc`` answer to this question, and the compute-apps query
        only reports memory, so a subprocess it is. ``pmon -c 1`` samples for a
        second before printing, which is why this is the only part of the Linux
        walk that costs anything — and why a machine without an NVIDIA GPU stops
        being asked after the first failure, bar an occasional re-check.
        """
        now = time.monotonic()
        if not self._pmon_ok and now < self._pmon_retry:
            return []
        try:
            proc = subprocess.run(
                ["nvidia-smi", "pmon", "-c", "1", "-s", "u"],
                capture_output=True, text=True, timeout=15.0)
            out = proc.stdout if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            out = ""
        if not out.strip():
            self._pmon_ok = False
            self._pmon_retry = now + 300.0
            return []
        self._pmon_ok = True

        by_name: dict[str, float] = {}
        for line in out.splitlines():
            if line.startswith("#"):
                continue
            fields = line.split()
            # gpu, pid, type, sm, mem, ... , command — newer drivers add columns
            # in the middle, so the ends are what can be indexed safely.
            if len(fields) < 5 or not fields[1].isdigit():
                continue
            try:
                sm = float(fields[3])
            except ValueError:
                continue  # "-" for a process using memory but no engine
            name = fields[-1]
            by_name[name] = by_name.get(name, 0.0) + sm
        return [(name, min(value, 100.0))
                for name, value in sorted(by_name.items(),
                                          key=lambda r: r[1],
                                          reverse=True)[:self.count]]

    @staticmethod
    def _by_name(rows) -> dict[str, float]:
        """Sum ``\\Process(*)`` instances back into one entry per executable.

        Instances are named after the process with a ``#N`` suffix for
        duplicates, so a browser's renderers arrive as ``chrome``, ``chrome#1``,
        ``chrome#2`` and are only meaningful added together.
        """
        totals: dict[str, float] = {}
        for instance, value in rows:
            if instance in ("_Total", "_total", "Idle", "idle"):
                continue
            base = instance.rsplit("#", 1)[0] if "#" in instance else instance
            totals[base] = totals.get(base, 0.0) + value
        return totals

    def _publish(self, data: dict[str, list[tuple[str, float]]]) -> None:
        cpu_by_name = self._by_name(data.get("cpu", []))
        self.cpu_top = [(name, min(value / self._ncpu, 100.0))
                        for name, value in
                        sorted(cpu_by_name.items(),
                               key=lambda r: r[1], reverse=True)[:self.count]]

        mem_by_name = self._by_name(data.get("mem", []))
        self.mem_top = [(name, value / 1024.0 ** 2)
                        for name, value in
                        sorted(mem_by_name.items(),
                               key=lambda r: r[1], reverse=True)[:self.count]]

        gpu_by_pid: dict[int, float] = {}
        for instance, value in data.get("gpu", []):
            parts = instance.split("_", 2)
            if len(parts) < 2 or parts[0] != "pid":
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            gpu_by_pid[pid] = gpu_by_pid.get(pid, 0.0) + value

        # Cap the per-PID lookups so a batch of exited processes can't drag us
        # through every PID in the list.
        rows: list[tuple[str, float]] = []
        candidates = sorted(gpu_by_pid.items(),
                            key=lambda r: r[1], reverse=True)
        for pid, pct in candidates[:self.count * 4]:
            if len(rows) >= self.count:
                break
            try:
                name = sysinfo.Process(pid).name()
            except (sysinfo.Error, ValueError):
                continue  # exited between the counter read and now
            if name.lower().endswith(".exe"):
                name = name[:-4]
            rows.append((name, min(pct, 100.0)))
        self.gpu_top = rows
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


TRAFFIC_PATH = os.path.join(paths.state_dir(), "traffic.json")
POWER_PATH = os.path.join(paths.state_dir(), "power.json")
HISTORY_PATH = os.path.join(paths.state_dir(), "history.jsonl")


class Collector:
    """Samples everything the renderer needs.

    ``settings`` is the live config, read by the two web pollers for their API
    keys and weather location; it is optional so the module self-test and
    ``--save`` can build a collector without one.
    """

    def __init__(self, settings=None):
        self.host = socket.gethostname()
        self.cpu_name = self._cpu_name()
        self.cpu_cores = sysinfo.cpu_count(logical=True) or 1

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

        sysinfo.cpu_percent(None)  # prime the delta
        sysinfo.cpu_percent(None, percpu=True)  # percpu keeps its own last-values

        self.gpu = GpuPoller()
        self.gpu.start()
        self.procs = ProcPoller()
        self.procs.start()
        # Shells out to the docker CLI, which is far too slow to do inline.
        self.docker = dockerstat.DockerPoller()
        self.docker.start()

        # Both talk to the internet, so like the GPU and process pollers they
        # own a thread and the frame loop only ever reads their last result.
        cfg = settings.snapshot if settings else (lambda: {})
        # The volume is fixed for the life of the process: the counter path and
        # the device handle are both opened once, so changing it takes a restart
        # — which the settings page says. A drive letter on Windows, a mount
        # point on Linux; the default differs accordingly.
        self.disk = diskstat.DiskPoller(
            (cfg() or {}).get("disk_letter") or ("C" if IS_WINDOWS else "/"))
        self.disk.start()
        # Reads the same sample the renderer gets, so it is fed at the end of
        # sample() rather than owning a thread of its own.
        self.power = power.PowerLog(POWER_PATH, cfg)
        self.ai = aiquota.AiPoller(cfg)
        self.ai.start()
        # Reads the quota the poller above just fetched, so like the energy log
        # it is driven from sample() rather than owning a thread.
        self.ai_alert = alerts.QuotaAlert(paths.state_dir(), cfg)
        self.weather = weather.WeatherPoller(cfg)
        self.weather.start()

        # The advisor reads back the snapshot this collector just produced, so
        # it is handed a getter rather than being wired into sample() itself.
        self._cfg = cfg
        self._last: dict = {}
        self.history = advice.History(HISTORY_PATH)
        self.advisor = advice.Advisor(self.history, cfg, lambda: self._last)
        self.advisor.start()

        self._rtss_checked = 0.0
        self._rtss_up = False
        # Afterburner is the only CPU temperature source here, and its shared
        # memory has to be re-walked to notice it appearing or going away.
        self._sensors_checked = 0.0
        self._sensors: sensors.Sensors | None = None

    def close(self) -> None:
        self.gpu.stop()
        self.procs.stop()
        self.docker.stop()
        self.disk.stop()
        self.ai.stop()
        self.weather.stop()
        self.advisor.stop()

    @staticmethod
    def _cpu_name() -> str:
        """The marketing name of the CPU — "Ryzen 7 5800X", not "x86_64".

        ``platform.processor()`` is the fallback and a poor one: it returns the
        architecture on Linux and a family/model string on Windows. Both systems
        keep the real name somewhere better — the registry, or ``/proc/cpuinfo``.
        """
        name = platform.processor() or ""
        if IS_WINDOWS:
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                    name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
            except OSError:
                pass
        else:
            fields: dict[str, str] = {}
            try:
                with open("/proc/cpuinfo", encoding="utf-8",
                          errors="replace") as fh:
                    for line in fh:
                        key, sep, value = line.partition(":")
                        if sep and value.strip():
                            fields.setdefault(key.strip().lower(), value.strip())
            except OSError:
                pass
            # x86 says "model name". ARM boards have no such field and say
            # "Hardware" or "Model" instead — but on x86 "model" is the model
            # *number*, so a bare number there is not a name and is skipped
            # rather than shown as the CPU's name.
            for key in ("model name", "hardware", "model"):
                value = fields.get(key, "")
                if value and not value.isdigit():
                    name = value
                    break
        return " ".join(name.replace("(R)", "").replace("(TM)", "").split())

    @staticmethod
    def _pick_nic() -> str | None:
        stats = sysinfo.net_if_stats()
        counters = sysinfo.net_io_counters(pernic=True)
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
            c = sysinfo.net_io_counters(pernic=True).get(self.nic)
            if c:
                return c.bytes_recv, c.bytes_sent
        c = sysinfo.net_io_counters()
        return c.bytes_recv, c.bytes_sent

    def sample(self) -> dict:
        now = time.monotonic()

        cpu = sysinfo.cpu_percent(None)
        self.cpu_hist.push(cpu)
        cores = sysinfo.cpu_percent(None, percpu=True) or []

        # Afterburner reads the CPU's own registers, so prefer its clock over
        # cpu_freq(), which reports the nominal base frequency on Windows.
        if now - self._sensors_checked > 2.0:
            self._sensors = sensors.read()
            self._sensors_checked = now
        sens = self._sensors

        freq = sysinfo.cpu_freq()
        cpu_ghz = (freq.current / 1000.0) if freq and freq.current else 0.0
        if sens and sens.cpu_clock_mhz:
            cpu_ghz = sens.cpu_clock_mhz / 1000.0

        vm = sysinfo.virtual_memory()
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

        cfg = self._cfg() or {}
        out = {
            "host": self.host,
            "time": time.strftime("%H:%M:%S"),
            # Which OS produced this sample. The web page can be watching another
            # machine on the LAN, so "what can this host measure" has to travel
            # with the numbers rather than be read from the browser's own host.
            "platform": "windows" if IS_WINDOWS else "linux",
            # Carried in the snapshot rather than passed to the renderer: both
            # clients draw their own key map from it, and the web page already
            # receives the snapshot and nothing else.
            "hints": bool(cfg.get("device_hints", True)),
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
            "top": list(self.procs.cpu_top),
            "gpu_top": list(self.procs.gpu_top),
            "mem_top": list(self.procs.mem_top),
            "ai": self.ai.data,
            "weather": self.weather.data,
            "docker": self.docker.data,
            "disk": self.disk.data,
        }
        # Energy is integrated from the reading that was just taken, so it can
        # only be folded in once the rest of the snapshot exists.
        self.power.add(out)
        out["power"] = self.power.data()
        out["advice"] = self.advisor.data
        self.ai_alert.check(out["ai"])
        out["ai_alert"] = self.ai_alert.data
        self._last = out
        self.history.maybe_add(out)
        return out


if __name__ == "__main__":
    import json

    c = Collector()
    time.sleep(1.5)
    for _ in range(2):
        print(json.dumps(c.sample(), ensure_ascii=False, indent=2,
                         default=lambda o: round(o, 2)))
        time.sleep(1)
    c.close()
