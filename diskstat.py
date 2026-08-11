"""Temperature and throughput for the system drive.

Windows hands out drive temperature grudgingly. ``Get-StorageReliabilityCounter``
and the ``root\\wmi`` SMART classes both need elevation, and this program runs as
an ordinary user, so neither is an option. What *does* work is the storage stack's
own temperature query: ``IOCTL_STORAGE_QUERY_PROPERTY`` with
``StorageDeviceTemperatureProperty`` answers on a handle opened with **no** access
rights at all, which any user may obtain. That is the whole trick — the handle is
only a way to name the device, so the security check for reading data never
applies.

Throughput comes from PDH rather than ``psutil.disk_io_counters``, because the
question is about a drive letter and psutil counts per physical disk: a partition
sharing a disk with another would be reported as busy when its neighbour is.

Nothing here fails hard. A drive with no temperature sensor, a volume spanning
several disks, a missing counter — each just leaves that field ``None``.
"""

from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes

import psutil

import perfcounters

EVERY_S = 1.0        # throughput is a rate; it wants the same cadence as the frame
SLOW_EVERY_S = 20.0  # temperature and free space move far more slowly than that

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
_k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
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


class DiskPoller(threading.Thread):
    """Read/write rates, temperature and free space for one drive letter.

    Same shape as the other pollers: a daemon thread that owns its cadence and
    publishes to a plain attribute, so the frame loop never waits on a device
    handle or a counter collection.
    """

    def __init__(self, letter: str = "C"):
        super().__init__(daemon=True)
        self.letter = (letter or "C").strip().rstrip(":").upper()[:1] or "C"
        self.data: dict = {"ok": False, "letter": self.letter, "err": "读取中"}
        self._stop = threading.Event()
        self._drive: int | None = None
        self._slow_at = 0.0
        self._slow: dict = {}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
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
        if self._drive is None:
            self._drive = physical_drive(self.letter)
        if self._drive is not None:
            temp, warn, crit = temperature(self._drive)
            out["temp_c"], out["temp_warn"], out["temp_crit"] = temp, warn, crit

        try:
            usage = psutil.disk_usage(f"{self.letter}:\\")
        except OSError:
            return out
        out["used_gb"] = usage.used / 1024 ** 3
        out["total_gb"] = usage.total / 1024 ** 3
        out["used_pct"] = usage.percent
        return out


if __name__ == "__main__":
    import json

    print("PhysicalDrive:", physical_drive("C"))
    poller = DiskPoller("C")
    poller.start()
    for _ in range(4):
        time.sleep(1.2)
        print(json.dumps(poller.data, ensure_ascii=False, default=lambda o: round(o, 1)))
    poller.stop()
