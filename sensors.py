"""CPU temperature, package power and per-core clocks, from whatever the OS has.

Two backends, because the two systems could hardly be less alike:

**Windows** exposes no CPU temperature to unprivileged code — ``psutil`` returns
nothing and WMI's thermal zones are motherboard sensors at best. MSI Afterburner's
hardware monitor already reads the CPU's own registers and republishes everything
in a shared-memory block named ``MAHMSharedMemory``, so when it is running we can
read the real numbers without a kernel driver of our own. The block is a flat
array of named entries with stable names like ``CPU temperature``, ``CPU power``,
``CPU3 clock``, so this module indexes by name rather than assuming a sensor order.

**Linux** hands the same numbers to any user through sysfs, with no helper app to
install: hwmon for temperatures (``coretemp`` on Intel, ``k10temp`` on AMD),
cpufreq for the real per-core clock, and RAPL for package power. RAPL is an energy
counter rather than a wattage, so power only appears from the second reading on —
and on kernels since 5.10 the counter is root-only, in which case power stays
``None`` and the estimate falls back to the configured TDP.

Nothing here fails hard. With Afterburner closed, or on a machine whose sensors
sysfs is empty, :func:`read` returns ``None`` and the dashboard hides those fields.
"""

from __future__ import annotations

import glob
import mmap
import os
import struct
import time
from dataclasses import dataclass, field

IS_WINDOWS = os.name == "nt"

# What to tell the user when :func:`read` comes back empty. Different advice per
# platform: on Windows it means "install Afterburner", on Linux "this kernel or
# this CPU is not reporting", which no app can fix.
HINT = "温度需 Afterburner" if IS_WINDOWS else "无 hwmon 温度"

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


def _read_windows() -> Sensors | None:
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


# --- Linux: hwmon, cpufreq and RAPL ---------------------------------------

# hwmon chips that are the CPU itself, best first. Anything else in hwmon is a
# drive, a NIC or the board's ambient probe, none of which is a CPU temperature.
_CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal",
              "soc_thermal")


def _text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:  # a sensor can disappear between the glob and the read
        return None


def _milli(path: str) -> float | None:
    """A sysfs millidegree / microwatt style reading, as a plain float."""
    raw = _text(path)
    try:
        return float(raw) / 1000.0 if raw is not None else None
    except ValueError:
        return None


def _cpu_hwmon() -> str | None:
    """The hwmon directory belonging to the CPU, or None if there is none."""
    found: dict[str, str] = {}
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        name = _text(os.path.join(path, "name"))
        if name and name in _CPU_CHIPS and name not in found:
            found[name] = path
    for name in _CPU_CHIPS:
        if name in found:
            return found[name]
    return None


def _hwmon_temps(chip: str) -> tuple[float | None, list[float]]:
    """(package temperature, per-core temperatures) from one hwmon directory.

    Labels are how the entries are told apart: Intel's coretemp names them
    ``Package id 0`` and ``Core 0``..``Core n``, AMD's k10temp reports only
    ``Tctl``/``Tdie`` with no per-core breakdown. Cores are ordered by the number
    in their label rather than by file name, because ``temp10_input`` sorts before
    ``temp2_input``.
    """
    package: float | None = None
    cores: list[tuple[int, float]] = []
    for path in glob.glob(os.path.join(chip, "temp*_input")):
        value = _milli(path)
        if value is None or not -20 <= value <= 150:
            continue
        label = (_text(path[:-len("_input")] + "_label") or "").strip()
        low = label.lower()
        if low.startswith("core "):
            try:
                cores.append((int(low[5:]), value))
            except ValueError:
                continue
        elif package is None or low.startswith(("package", "tctl", "tdie")):
            package = value
    cores.sort()
    if package is None and cores:
        package = max(v for _i, v in cores)
    return package, [v for _i, v in cores]


def _core_clocks() -> list[float]:
    """Per-core MHz from cpufreq, in logical-CPU order.

    ``scaling_cur_freq`` is the governor's current setting and is present on every
    machine with a cpufreq driver; ``/proc/cpuinfo``'s ``cpu MHz`` is the fallback
    for VMs and ARM boards that have none.
    """
    out: list[float] = []
    index = 0
    while True:
        khz = _text(f"/sys/devices/system/cpu/cpu{index}/cpufreq/scaling_cur_freq")
        if khz is None:
            break
        try:
            out.append(float(khz) / 1000.0)
        except ValueError:
            break
        index += 1
    if out:
        return out

    for line in (_text("/proc/cpuinfo") or "").splitlines():
        if line.lower().startswith("cpu mhz"):
            try:
                out.append(float(line.split(":", 1)[1]))
            except (IndexError, ValueError):
                continue
    return out


# RAPL counts joules since boot, so watts is a difference between two readings.
_rapl_last: tuple[float, float] | None = None  # (microjoules, monotonic time)


def _rapl_path() -> str | None:
    for path in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")):
        if (_text(os.path.join(path, "name")) or "") == "package-0":
            return os.path.join(path, "energy_uj")
    return None


def _package_power() -> float | None:
    """CPU package watts, or None until a second RAPL reading exists."""
    global _rapl_last

    path = _rapl_path()
    if not path:
        return None
    raw = _text(path)
    if raw is None:  # root-only on kernels >= 5.10, which is the common case
        return None
    try:
        energy = float(raw)
    except ValueError:
        return None

    now = time.monotonic()
    last, _rapl_last = _rapl_last, (energy, now)
    if last is None:
        return None
    dt = now - last[1]
    # The counter wraps at max_energy_range_uj; a negative delta is that wrap (or
    # a suspend), and one skipped sample is cheaper than pretending to know where
    # it wrapped.
    if dt <= 0 or dt > 60 or energy < last[0]:
        return None
    return (energy - last[0]) / 1e6 / dt


def _read_linux() -> Sensors | None:
    chip = _cpu_hwmon()
    package, cores = _hwmon_temps(chip) if chip else (None, [])
    clocks = _core_clocks()
    power = _package_power()

    if package is None and not clocks and power is None:
        return None
    return Sensors(
        cpu_temp_c=package,
        cpu_power_w=power,
        # The "current" clock of a whole CPU is a fiction; the busiest core is the
        # honest single number, and it is what the per-core row is topped by.
        cpu_clock_mhz=max(clocks) if clocks else None,
        core_temps_c=cores,
        core_clocks_mhz=clocks,
    )


def read() -> Sensors | None:
    return _read_windows() if IS_WINDOWS else _read_linux()


def is_running() -> bool:
    """True when a sensor source is present: Afterburner, or Linux sysfs."""
    if not IS_WINDOWS:
        return read() is not None
    try:
        head = _open(_HEADER.size)
    except OSError:
        return False
    try:
        return _HEADER.unpack(head.read(_HEADER.size))[0] == SIGNATURE
    finally:
        head.close()


if __name__ == "__main__":
    print("sensors available:", is_running())
    if not IS_WINDOWS:
        read()  # prime RAPL: watts needs two readings
        time.sleep(1.0)
    s = read()
    if s:
        print(f"CPU {s.cpu_temp_c}°C  {s.cpu_power_w} W  {s.cpu_clock_mhz} MHz")
        print("core temps :", s.core_temps_c)
        print("core clocks:", s.core_clocks_mhz)
