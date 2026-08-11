"""Read CPU temperature, package power and per-core clocks from MSI Afterburner.

Windows exposes no CPU temperature to unprivileged code — ``psutil`` returns
nothing and WMI's thermal zones are motherboard sensors at best. Afterburner's
hardware monitor already reads the CPU's own registers and republishes everything
in a shared-memory block named ``MAHMSharedMemory``, so when it is running we can
read the real numbers without a kernel driver of our own.

The block is a flat array of named entries. Names are stable strings like
``CPU temperature``, ``CPU power``, ``CPU3 clock``, so this module just indexes by
name rather than assuming any particular sensor order.

Nothing here fails hard: with Afterburner closed the mapping does not exist and
:func:`read` returns ``None``.
"""

from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass, field

SIGNATURE = 0x4D41484D  # the four bytes 'MHAM' as a little-endian DWORD
MAP_NAME = "MAHMSharedMemory"

# signature, version, header size, entry count, entry size
_HEADER = struct.Struct("<5I")

# MAHM_SHARED_MEMORY_ENTRY: five 260-byte strings (source name, units, localised
# name, localised units, recommended format) and then the value triplet.
_STR = 260
_OFF_DATA = 5 * _STR  # 1300

# Afterburner marks a sensor it cannot read with +/-FLT_MAX rather than omitting
# it, which is how an idle "Framerate" entry shows up as 3.4e38.
_ABSENT = 1e30


@dataclass
class Sensors:
    cpu_temp_c: float | None = None
    cpu_power_w: float | None = None
    cpu_clock_mhz: float | None = None
    core_temps_c: list[float] = field(default_factory=list)
    core_clocks_mhz: list[float] = field(default_factory=list)


def _open(size: int) -> mmap.mmap:
    return mmap.mmap(-1, size, tagname=MAP_NAME, access=mmap.ACCESS_READ)


def _value(raw: float) -> float | None:
    return None if abs(raw) >= _ABSENT else raw


def _entries() -> dict[str, float] | None:
    """Every sensor as ``name -> value``, or None when Afterburner is not up."""
    try:
        head = _open(_HEADER.size)
    except OSError:
        return None
    try:
        sig, _ver, hdr_size, count, entry_size = _HEADER.unpack(
            head.read(_HEADER.size))
    finally:
        head.close()

    if sig != SIGNATURE or not count or entry_size < _OFF_DATA + 4:
        return None

    try:
        mm = _open(hdr_size + count * entry_size)
    except OSError:
        return None
    try:
        buf = memoryview(mm)
        out: dict[str, float] = {}
        for i in range(count):
            base = hdr_size + i * entry_size
            name = bytes(buf[base:base + _STR]).split(b"\0", 1)[0].decode(
                "latin-1")
            if not name:
                continue
            (raw,) = struct.unpack_from("<f", buf, base + _OFF_DATA)
            value = _value(raw)
            if value is not None:
                out[name] = value
        buf.release()
        return out
    finally:
        mm.close()


def _series(entries: dict[str, float], suffix: str) -> list[float]:
    """``CPU1 x``..``CPUn x`` as a dense list, stopping at the first gap."""
    out: list[float] = []
    i = 1
    while f"CPU{i} {suffix}" in entries:
        out.append(entries[f"CPU{i} {suffix}"])
        i += 1
    return out


def read() -> Sensors | None:
    entries = _entries()
    if entries is None:
        return None
    return Sensors(
        cpu_temp_c=entries.get("CPU temperature"),
        cpu_power_w=entries.get("CPU power"),
        cpu_clock_mhz=entries.get("CPU clock"),
        core_temps_c=_series(entries, "temperature"),
        core_clocks_mhz=_series(entries, "clock"),
    )


def is_running() -> bool:
    """True when the Afterburner shared memory block exists and is signed."""
    try:
        head = _open(_HEADER.size)
    except OSError:
        return False
    try:
        return _HEADER.unpack(head.read(_HEADER.size))[0] == SIGNATURE
    finally:
        head.close()


if __name__ == "__main__":
    print("Afterburner running:", is_running())
    s = read()
    if s:
        print(f"CPU {s.cpu_temp_c}°C  {s.cpu_power_w} W  {s.cpu_clock_mhz} MHz")
        print("core temps :", s.core_temps_c)
        print("core clocks:", s.core_clocks_mhz)
