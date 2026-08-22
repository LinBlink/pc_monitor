"""The machine's own counters: psutil on Windows, ``/proc`` on Linux.

psutil is the right answer on Windows — the numbers live behind Win32 calls that
would mean a pile of ctypes here. On Linux it is a thin reader over ``/proc`` and
``/sys``, and this module reads those files directly instead, which is what lets
the .deb install with no Python dependency but Pillow. Same files, same numbers,
one less package to keep current on a server.

The names below are psutil's, with psutil's shapes and psutil's quirks kept
deliberately:

* ``cpu_percent`` is a delta since *its own* last call, so the first call returns
  zero and total and per-CPU keep separate state;
* a process's ``cpu_percent`` is a share of one core and may exceed 100;
* ``disk_usage(...).percent`` counts the filesystem's reserved blocks as used,
  so it matches ``df``;
* ``process_iter`` caches its Process objects between calls, which is the only
  reason per-process CPU deltas mean anything.

Keeping the signatures identical is what allows the Windows side to be a plain
re-export, with no adapter layer and no second code path to test.
"""

from __future__ import annotations

import collections
import os
import time

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    # Every one of these is used exactly as psutil defines it.
    from psutil import (Error, NoSuchProcess, Process, cpu_count,  # noqa: F401
                        cpu_freq, cpu_percent, disk_io_counters, disk_usage,
                        net_if_addrs, net_if_stats, net_io_counters,
                        process_iter, virtual_memory)
else:
    import fcntl
    import socket
    import struct

    class Error(Exception):
        """Base class, so callers can except on it exactly as with psutil."""

    class NoSuchProcess(Error):
        pass

    _Usage = collections.namedtuple("sdiskusage", "total used free percent")
    _DiskIo = collections.namedtuple("sdiskio", "read_bytes write_bytes")
    _Vm = collections.namedtuple("svmem", "total available percent used free")
    _NetIo = collections.namedtuple("snetio", "bytes_sent bytes_recv")
    _NicStats = collections.namedtuple("snicstats", "isup duplex speed mtu")
    _NicAddr = collections.namedtuple("snicaddr", "family address netmask")
    _Freq = collections.namedtuple("scpufreq", "current min max")
    _MemInfo = collections.namedtuple("pmem", "rss vms")

    _SECTOR = 512  # /proc/diskstats counts 512-byte sectors regardless of the
    #                device's real block size; this is kernel ABI, not a guess.

    def _read(path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    # --- CPU --------------------------------------------------------------

    def cpu_count(logical: bool = True) -> int:
        return os.cpu_count() or 1

    def _cpu_times() -> list[tuple[str, float, float]]:
        """(name, busy+idle, idle) per line of /proc/stat, aggregate first."""
        out = []
        for line in _read("/proc/stat").splitlines():
            if not line.startswith("cpu"):
                break
            parts = line.split()
            values = [float(v) for v in parts[1:]]
            if len(values) < 5:
                continue
            # idle + iowait: a CPU waiting on a disk is not doing work, and
            # counting iowait as busy is the classic way to report 100% on an
            # idle machine with a slow disk.
            out.append((parts[0], sum(values), values[3] + values[4]))
        return out

    _cpu_last: dict[str, tuple[float, float]] = {}

    def cpu_percent(interval=None, percpu: bool = False):
        """Busy percentage since the previous call with the same ``percpu``.

        ``interval`` is accepted for signature compatibility and must be None or
        0 — this program never blocks a sampling thread on it.
        """
        key = "percpu" if percpu else "total"
        rows = _cpu_times()
        out = []
        for name, total, idle in rows:
            slot = f"{key}:{name}"
            prev = _cpu_last.get(slot)
            _cpu_last[slot] = (total, idle)
            if prev is None or total <= prev[0]:
                out.append(0.0)
                continue
            busy = 1.0 - (idle - prev[1]) / (total - prev[0])
            out.append(round(max(0.0, min(1.0, busy)) * 100.0, 1))
        if percpu:
            return out[1:]  # drop the "cpu" aggregate line
        return out[0] if out else 0.0

    def cpu_freq():
        """Current clock in MHz, from cpufreq or /proc/cpuinfo. None if neither."""
        khz = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").strip()
        if khz:
            try:
                return _Freq(float(khz) / 1000.0, 0.0, 0.0)
            except ValueError:
                pass
        for line in _read("/proc/cpuinfo").splitlines():
            if line.lower().startswith("cpu mhz"):
                try:
                    return _Freq(float(line.split(":", 1)[1]), 0.0, 0.0)
                except (IndexError, ValueError):
                    break
        return None

    # --- memory -----------------------------------------------------------

    def virtual_memory():
        info = {}
        for line in _read("/proc/meminfo").splitlines():
            key, _, value = line.partition(":")
            parts = value.split()
            if parts:
                try:
                    info[key.strip()] = float(parts[0]) * 1024.0
                except ValueError:
                    continue
        total = info.get("MemTotal", 0.0)
        # MemAvailable is the kernel's own estimate of what a new workload could
        # get; free + cache overstates it and MemFree alone understates it badly.
        available = info.get("MemAvailable")
        if available is None:
            available = (info.get("MemFree", 0.0) + info.get("Cached", 0.0)
                         + info.get("Buffers", 0.0))
        used = max(0.0, total - available)
        percent = round(used * 100.0 / total, 1) if total else 0.0
        return _Vm(total, available, percent, used, info.get("MemFree", 0.0))

    # --- network ----------------------------------------------------------

    def _net_dev() -> dict[str, tuple[int, int]]:
        out = {}
        for line in _read("/proc/net/dev").splitlines()[2:]:
            name, _, rest = line.partition(":")
            fields = rest.split()
            if len(fields) < 9:
                continue
            try:
                out[name.strip()] = (int(fields[0]), int(fields[8]))  # recv, sent
            except ValueError:
                continue
        return out

    def net_io_counters(pernic: bool = False):
        counters = _net_dev()
        if pernic:
            return {name: _NetIo(sent, recv)
                    for name, (recv, sent) in counters.items()}
        return _NetIo(sum(s for _r, s in counters.values()),
                      sum(r for r, _s in counters.values()))

    def net_if_stats():
        out = {}
        for name in sorted(os.listdir("/sys/class/net")) if os.path.isdir(
                "/sys/class/net") else []:
            flags = _read(f"/sys/class/net/{name}/flags").strip()
            try:
                # Bit 0 is IFF_UP, which is what psutil reports as isup: the
                # administrative state, not whether a cable is plugged in.
                isup = bool(int(flags, 16) & 1)
            except ValueError:
                isup = False
            mtu = _read(f"/sys/class/net/{name}/mtu").strip()
            out[name] = _NicStats(isup, 0, 0, int(mtu) if mtu.isdigit() else 0)
        return out

    _SIOCGIFADDR = 0x8915
    _SIOCGIFNETMASK = 0x891B

    def _ioctl_addr(name: str, request: int) -> str | None:
        """One IPv4 address off an interface, through the socket ioctl."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return None
        try:
            packed = fcntl.ioctl(sock.fileno(), request,
                                 struct.pack("256s", name.encode()[:15]))
            return socket.inet_ntoa(packed[20:24])
        except OSError:
            return None  # no IPv4 on this interface, which is not an error
        finally:
            sock.close()

    def net_if_addrs():
        """IPv4 only — the LAN sweep is the one caller and IPv4 is what it uses."""
        out = {}
        for name in net_if_stats():
            address = _ioctl_addr(name, _SIOCGIFADDR)
            if address is None:
                out[name] = []
                continue
            out[name] = [_NicAddr(socket.AF_INET, address,
                                  _ioctl_addr(name, _SIOCGIFNETMASK))]
        return out

    # --- disks ------------------------------------------------------------

    def disk_usage(path: str):
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize          # what an ordinary user may use
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        # df's arithmetic: the root-reserved blocks count as neither used nor
        # available, so the percentage is over used+free rather than over total.
        percent = round(used * 100.0 / (used + free), 1) if used + free else 0.0
        return _Usage(total, used, free, percent)

    def disk_io_counters(perdisk: bool = False):
        out = {}
        for line in _read("/proc/diskstats").splitlines():
            fields = line.split()
            if len(fields) < 10:
                continue
            try:
                out[fields[2]] = _DiskIo(int(fields[5]) * _SECTOR,
                                         int(fields[9]) * _SECTOR)
            except ValueError:
                continue
        if perdisk:
            return out
        return _DiskIo(sum(v.read_bytes for v in out.values()),
                       sum(v.write_bytes for v in out.values()))

    # --- processes --------------------------------------------------------

    _TICKS = os.sysconf("SC_CLK_TCK")
    _PAGE = os.sysconf("SC_PAGE_SIZE")

    class Process:
        """The handful of process facts the dashboard asks for."""

        def __init__(self, pid: int):
            self.pid = pid
            self.info: dict = {}
            self._last: tuple[float, float] | None = None

        def _stat(self) -> list[str]:
            raw = _read(f"/proc/{self.pid}/stat")
            if not raw:
                raise NoSuchProcess(self.pid)
            # The second field is the executable name in parentheses and may
            # itself contain spaces and parentheses, so the split has to start
            # after the *last* ')'.
            return raw.rsplit(")", 1)[1].split()

        def name(self) -> str:
            comm = _read(f"/proc/{self.pid}/comm").strip()
            if not comm:
                raise NoSuchProcess(self.pid)
            if len(comm) < 15:
                return comm
            # comm is capped at 15 characters, which turns "unattended-upgrade"
            # into "unattended-upgr". At the cap, the command line has the real
            # name — psutil does the same thing for the same reason.
            argv0 = _read(f"/proc/{self.pid}/cmdline").split("\0")[0]
            base = os.path.basename(argv0)
            return base if base.startswith(comm) else comm

        def cpu_percent(self) -> float:
            fields = self._stat()
            try:
                used = (int(fields[11]) + int(fields[12])) / _TICKS
            except (IndexError, ValueError):
                raise NoSuchProcess(self.pid) from None
            now = time.monotonic()
            last, self._last = self._last, (used, now)
            if last is None or now <= last[1]:
                return 0.0
            return max(0.0, (used - last[0]) * 100.0 / (now - last[1]))

        def memory_info(self):
            fields = _read(f"/proc/{self.pid}/statm").split()
            if len(fields) < 2:
                raise NoSuchProcess(self.pid)
            return _MemInfo(int(fields[1]) * _PAGE, int(fields[0]) * _PAGE)

        def as_dict(self, attrs, ad_value=None) -> dict:
            out = {}
            for attr in attrs:
                try:
                    out[attr] = getattr(self, attr)()
                except NoSuchProcess:
                    raise
                except (OSError, ValueError, IndexError):
                    out[attr] = ad_value
            return out

    _seen: dict[int, Process] = {}

    def process_iter(attrs=None, ad_value=None):
        """Every process, as cached Process objects so CPU deltas work.

        The cache is the whole point: a fresh Process each cycle would have no
        previous CPU time to subtract and every process would read as 0%.
        """
        alive = set()
        try:
            entries = os.listdir("/proc")
        except OSError:
            return
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            alive.add(pid)
            proc = _seen.get(pid)
            if proc is None:
                proc = _seen[pid] = Process(pid)
            if attrs:
                try:
                    proc.info = proc.as_dict(attrs, ad_value)
                except NoSuchProcess:
                    continue  # exited between listdir and now
            yield proc
        for pid in set(_seen) - alive:
            _seen.pop(pid, None)


if __name__ == "__main__":
    import json

    cpu_percent(None)          # prime: the first reading has no interval
    cpu_percent(None, percpu=True)
    time.sleep(1.0)
    print("cpu    :", cpu_percent(None), cpu_percent(None, percpu=True))
    print("freq   :", cpu_freq())
    print("memory :", virtual_memory().percent, "%")
    print("net    :", json.dumps({k: list(v) for k, v in
                                  net_io_counters(pernic=True).items()})[:200])
    print("addrs  :", {k: [a.address for a in v]
                       for k, v in net_if_addrs().items() if v})
    print("disk   :", disk_usage("C:\\" if IS_WINDOWS else "/"))
    top = sorted(((p.info.get("name"), p.info.get("memory_info"))
                  for p in process_iter(["name", "memory_info"], ad_value=None)),
                 key=lambda r: (r[1].rss if r[1] else 0), reverse=True)[:3]
    print("procs  :", [(n, round(m.rss / 1024 ** 2, 1)) for n, m in top if m])
