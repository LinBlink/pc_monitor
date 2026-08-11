"""Read Windows performance counters directly through PDH.

Used for the two per-process questions the dashboard asks — who is using the CPU
and who is using the GPU — because there is no cheap way to ask them otherwise:

* Walking ``psutil.process_iter`` opens a handle per process. With ~400 processes
  that takes one to three seconds and holds the GIL, which stalled the frame
  thread badly enough to be visible as stutter.
* ``Get-Counter`` in a PowerShell child does the same query natively but wraps
  every one of ~520 GPU-engine instances in a rich object: ~2.5 seconds of CPU per
  sample, enough to put our own helper at the top of the CPU list.

PDH answers both in a few milliseconds. ``PdhAddEnglishCounter`` takes the English
counter path on every locale, so no registry name translation is needed either.

Rate counters (both of these are) need two collections to produce a value, so the
first :meth:`CounterQuery.collect` after opening returns nothing.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_pdh = ctypes.WinDLL("pdh.dll")

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_INVALID_DATA = 0xC0000BC6
PDH_CALC_NEGATIVE_DENOMINATOR = 0x800007D8


class _CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD),
                ("doubleValue", ctypes.c_double)]


class _CounterItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR),
                ("FmtValue", _CounterValue)]


_pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
_pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]


class PdhError(OSError):
    pass


class CounterQuery:
    """A PDH query over one or more wildcard counter paths.

    Paths are given as ``{key: r"\\Process(*)\\% Processor Time"}``; :meth:`collect`
    returns ``{key: [(instance_name, value), ...]}``. Instances that report no
    valid data are skipped rather than reported as zero.
    """

    def __init__(self, paths: dict[str, str]):
        self._handle = ctypes.c_void_p()
        status = _pdh.PdhOpenQueryW(None, None, ctypes.byref(self._handle))
        if status:
            raise PdhError(f"PdhOpenQuery failed: {status & 0xFFFFFFFF:#x}")

        self._counters: dict[str, ctypes.c_void_p] = {}
        try:
            for key, path in paths.items():
                counter = ctypes.c_void_p()
                status = _pdh.PdhAddEnglishCounterW(self._handle, path, None,
                                                    ctypes.byref(counter))
                if status:
                    raise PdhError(f"cannot add {path!r}: "
                                   f"{status & 0xFFFFFFFF:#x}")
                self._counters[key] = counter
            # Prime the rate counters; the first collection has no interval to
            # divide by, so it cannot produce a value.
            _pdh.PdhCollectQueryData(self._handle)
        except PdhError:
            self.close()
            raise

    def close(self) -> None:
        if self._handle:
            _pdh.PdhCloseQuery(self._handle)
            self._handle = ctypes.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def collect(self) -> dict[str, list[tuple[str, float]]]:
        status = _pdh.PdhCollectQueryData(self._handle)
        if status:
            return {key: [] for key in self._counters}
        return {key: self._read(counter) for key, counter in self._counters.items()}

    def _read(self, counter) -> list[tuple[str, float]]:
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        status = _pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(size),
            ctypes.byref(count), None)
        if (status & 0xFFFFFFFF) != PDH_MORE_DATA or not count.value:
            return []

        buf = ctypes.create_string_buffer(size.value)
        status = _pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(size),
            ctypes.byref(count), buf)
        if status:
            return []

        items = ctypes.cast(buf, ctypes.POINTER(_CounterItem))
        out = []
        for i in range(count.value):
            item = items[i]
            if item.FmtValue.CStatus:  # per-instance error, e.g. it just exited
                continue
            if item.szName:
                out.append((item.szName, item.FmtValue.doubleValue))
        return out


if __name__ == "__main__":
    import time

    q = CounterQuery({"cpu": r"\Process(*)\% Processor Time",
                      "gpu": r"\GPU Engine(*)\Utilization Percentage"})
    for _ in range(2):
        time.sleep(1.0)
        t0 = time.perf_counter()
        data = q.collect()
        ms = (time.perf_counter() - t0) * 1000
        print(f"collect: {ms:.1f} ms  "
              f"cpu instances {len(data['cpu'])}  gpu instances {len(data['gpu'])}")
        for key in ("cpu", "gpu"):
            top = sorted(data[key], key=lambda r: -r[1])[:4]
            print(f"  {key}:", [(n, round(v, 2)) for n, v in top])
    q.close()
