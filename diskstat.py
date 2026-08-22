"""Temperature and throughput for the system drive.

**Windows** hands out drive temperature grudgingly. ``Get-StorageReliabilityCounter``
and the ``root\\wmi`` SMART classes both need elevation, and this program runs as
an ordinary user, so neither is an option. What *does* work is the storage stack's
own temperature query: ``IOCTL_STORAGE_QUERY_PROPERTY`` with
``StorageDeviceTemperatureProperty`` answers on a handle opened with **no** access
rights at all, which any user may obtain. That is the whole trick — the handle is
only a way to name the device, so the security check for reading data never
applies. Throughput comes from PDH rather than per-disk counters, because the
question is about a drive letter and those count per physical disk: a partition
sharing a disk with another would be reported as busy when its neighbour is.

**Linux** asks for a mount point instead of a drive letter, and everything is a
file: the mount point's device number leads through ``/sys/dev/block`` to the
disk (a partition is climbed to its parent), the ``nvme`` or ``drivetemp`` hwmon
node hanging off that disk carries the temperature, and ``/proc/diskstats``
carries the sector counts that throughput is differenced from. Per-disk counters
are the right granularity here, because the mount point was resolved to a disk
rather than the other way round.

Nothing here fails hard. A drive with no temperature sensor, a volume spanning
several disks, a missing counter — each just leaves that field ``None``.
"""

from __future__ import annotations

import ctypes
import glob
import os
import struct
import threading
import time

import perfcounters
import sysinfo

EVERY_S = 1.0        # throughput is a rate; it wants the same cadence as the frame
SLOW_EVERY_S = 20.0  # temperature and free space move far more slowly than that

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _FILE_SHARE_RW = 3
    _OPEN_EXISTING = 3
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
    _IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    _STORAGE_DEVICE_TEMPERATURE_PROPERTY = 52

    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p]
    _k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD),
                                     ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]


def _ioctl(path: str, code: int, request: bytes | None, out_size: int):
    """One control code against a device, on a zero-access handle.

    Zero access is deliberate and load-bearing: ``GENERIC_READ`` on ``\\\\.\\C:``
    is refused to non-administrators, while the same open with no rights at all
    succeeds and is enough for both of the queries below.
    """
    handle = _k32.CreateFileW(path, 0, _FILE_SHARE_RW, None, _OPEN_EXISTING, 0,
                              None)
    if handle == _INVALID_HANDLE:
        return None
    try:
        out = ctypes.create_string_buffer(out_size)
        written = wintypes.DWORD()
        ok = _k32.DeviceIoControl(handle, code, request,
                                  len(request) if request else 0,
                                  out, out_size, ctypes.byref(written), None)
        return out.raw[:written.value] if ok else None
    finally:
        _k32.CloseHandle(handle)


def physical_drive(letter: str) -> int | None:
    """The physical disk a drive letter lives on, or None if it spans several."""
    raw = _ioctl(rf"\\.\{letter}:", _IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 1024)
    if not raw or len(raw) < 12:
        return None
    (count,) = struct.unpack_from("<I", raw, 0)
    # A striped or spanned volume has no single temperature to report, so rather
    # than picking one of its disks arbitrarily this reports nothing.
    if count != 1:
        return None
    return struct.unpack_from("<I", raw, 8)[0]


def temperature(drive: int) -> tuple[float | None, float | None, float | None]:
    """(current, warning threshold, critical threshold) in °C for a physical disk.

    The descriptor carries one entry per sensor, and drives routinely declare
    more sensors than they populate: this machine's NVMe reports three, of which
    only the first (the composite) holds a real reading and the others come back
    as ``-32768``. So entries outside a plausible range are skipped rather than
    trusted, and the first survivor wins — index 0 is the composite by spec.
    """
    query = struct.pack("<II", _STORAGE_DEVICE_TEMPERATURE_PROPERTY, 0) + b"\0" * 8
    raw = _ioctl(rf"\\.\PhysicalDrive{drive}", _IOCTL_STORAGE_QUERY_PROPERTY,
                 query, 512)
    if not raw or len(raw) < 24:
        return None, None, None

    _ver, _size, crit, warn, count = struct.unpack_from("<IIhhH", raw, 0)
    for i in range(min(count, 16)):
        offset = 24 + i * 10
        if offset + 10 > len(raw):
            break
        _index, temp = struct.unpack_from("<hh", raw, offset)
        if -20 <= temp <= 150:
            return (float(temp),
                    float(warn) if 0 < warn <= 150 else None,
                    float(crit) if 0 < crit <= 150 else None)
    return None, None, None


# --- Linux ----------------------------------------------------------------

def _text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def block_device(mount: str) -> str | None:
    """The disk backing a mount point — ``nvme0n1``, ``sda`` — or None.

    The mount point's ``st_dev`` names the block device directly, which is what
    makes this work for LVM and bind mounts where the path says nothing useful.
    A partition is climbed to its parent disk, because that is the level both the
    temperature sensor and ``/proc/diskstats``' interesting numbers live at.
    """
    try:
        st = os.stat(mount)
    except OSError:
        return None
    sysfs = f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    real = os.path.realpath(sysfs)
    if not os.path.exists(real):
        # tmpfs, overlayfs, ZFS: an anonymous device number with no block device
        # behind it. There is no disk to report and no sensible guess to make.
        return None
    if os.path.exists(os.path.join(real, "partition")):
        real = os.path.dirname(real)
    return os.path.basename(real)


def disk_temperature(device: str) -> tuple[float | None, float | None, float | None]:
    """(current, warning, critical) in °C for a Linux block device.

    NVMe controllers expose a hwmon node unconditionally; SATA drives only do so
    when the ``drivetemp`` module is loaded, which is why this can come back empty
    on a perfectly healthy machine.
    """
    for path in sorted(glob.glob(f"/sys/block/{device}/device/hwmon*/temp1_input")):
        raw = _text(path)
        try:
            temp = float(raw) / 1000.0 if raw is not None else None
        except ValueError:
            continue
        if temp is None or not -20 <= temp <= 150:
            continue

        def limit(suffix: str) -> float | None:
            value = _text(path.replace("temp1_input", f"temp1_{suffix}"))
            try:
                celsius = float(value) / 1000.0 if value is not None else None
            except ValueError:
                return None
            return celsius if celsius and 0 < celsius <= 150 else None

        return temp, limit("max"), limit("crit")
    return None, None, None


class DiskPoller(threading.Thread):
    """Read/write rates, temperature and free space for one volume.

    ``target`` is a drive letter on Windows and a mount point on Linux; either
    way it is fixed for the life of the process, because the counter query and
    the device handle are opened once — which the settings page says.

    Same shape as the other pollers: a daemon thread that owns its cadence and
    publishes to a plain attribute, so the frame loop never waits on a device
    handle or a counter collection.
    """

    def __init__(self, target: str = ""):
        super().__init__(daemon=True)
        if IS_WINDOWS:
            self.letter = (target or "C").strip().rstrip(":").upper()[:1] or "C"
            self.mount = f"{self.letter}:\\"
        else:
            self.mount = (target or "/").strip() or "/"
            # The tile has room for a short name, and "/" is what a server's
            # system volume is actually called.
            self.letter = self.mount
        self.data: dict = {"ok": False, "letter": self.letter, "err": "读取中"}
        self._stop = threading.Event()
        self._drive: int | None = None
        self._device: str | None = None
        self._last_io: tuple[float, float, float] | None = None
        self._slow_at = 0.0
        self._slow: dict = {}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not IS_WINDOWS:
            self._run_linux()
            return

        paths = {"read": rf"\LogicalDisk({self.letter}:)\Disk Read Bytes/sec",
                 "write": rf"\LogicalDisk({self.letter}:)\Disk Write Bytes/sec"}
        try:
            query = perfcounters.CounterQuery(paths)
        except perfcounters.PdhError as exc:
            self.data = {"ok": False, "letter": self.letter, "err": str(exc)}
            return

        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self._publish(query.collect())
                wait = EVERY_S - (time.monotonic() - started)
                if wait > 0:
                    self._stop.wait(wait)
        finally:
            query.close()

    def _run_linux(self) -> None:
        self._device = block_device(self.mount)
        while not self._stop.is_set():
            started = time.monotonic()
            self._publish_linux()
            wait = EVERY_S - (time.monotonic() - started)
            if wait > 0:
                self._stop.wait(wait)

    def _rates_linux(self) -> tuple[float, float]:
        """Bytes/sec since the previous call, differenced from the raw counters."""
        if not self._device:
            return 0.0, 0.0
        try:
            counters = sysinfo.disk_io_counters(perdisk=True).get(self._device)
        except (OSError, RuntimeError):
            counters = None
        if counters is None:
            return 0.0, 0.0

        now = time.monotonic()
        last, self._last_io = self._last_io, (float(counters.read_bytes),
                                              float(counters.write_bytes), now)
        if last is None:
            return 0.0, 0.0  # first sample: a rate needs two of them
        dt = now - last[2]
        if dt <= 0:
            return 0.0, 0.0
        return (max(0.0, counters.read_bytes - last[0]) / dt,
                max(0.0, counters.write_bytes - last[1]) / dt)

    def _publish_linux(self) -> None:
        read_bps, write_bps = self._rates_linux()
        now = time.monotonic()
        if now - self._slow_at >= SLOW_EVERY_S or not self._slow:
            self._slow_at = now
            self._slow = self._read_slow()
        self.data = dict(self._slow, ok=True, letter=self.letter, err=None,
                         read_bps=read_bps, write_bps=write_bps)

    def _publish(self, counters: dict) -> None:
        now = time.monotonic()
        if now - self._slow_at >= SLOW_EVERY_S or not self._slow:
            self._slow_at = now
            self._slow = self._read_slow()

        def rate(key: str) -> float:
            rows = counters.get(key) or []
            return float(rows[0][1]) if rows else 0.0

        self.data = dict(self._slow, ok=True, letter=self.letter, err=None,
                         read_bps=rate("read"), write_bps=rate("write"))

    def _read_slow(self) -> dict:
        out: dict = {"temp_c": None, "temp_warn": None, "temp_crit": None,
                     "used_gb": None, "total_gb": None, "used_pct": None}
        if IS_WINDOWS:
            if self._drive is None:
                self._drive = physical_drive(self.letter)
            if self._drive is not None:
                out["temp_c"], out["temp_warn"], out["temp_crit"] = temperature(
                    self._drive)
        elif self._device:
            out["temp_c"], out["temp_warn"], out["temp_crit"] = disk_temperature(
                self._device)

        try:
            usage = sysinfo.disk_usage(self.mount)
        except OSError:
            return out
        out["used_gb"] = usage.used / 1024 ** 3
        out["total_gb"] = usage.total / 1024 ** 3
        out["used_pct"] = usage.percent
        return out


if __name__ == "__main__":
    import json

    target = "C" if IS_WINDOWS else "/"
    print("device:", physical_drive("C") if IS_WINDOWS else block_device("/"))
    poller = DiskPoller(target)
    poller.start()
    for _ in range(4):
        time.sleep(1.2)
        print(json.dumps(poller.data, ensure_ascii=False, default=lambda o: round(o, 1)))
    poller.stop()
