"""Read per-application framerate out of RivaTuner Statistics Server.

RTSS publishes a shared-memory block named ``RTSSSharedMemoryV2`` containing an
array of application entries. Each entry carries a rolling frame counter and the
tick window it was measured over, which is all we need for an instantaneous FPS.

RTSS is a Windows program and there is no equivalent anywhere else, so on Linux
every function here reports "nothing to see": the FPS tile then shows a dash,
which is the right answer for a headless server that is not rendering anything.

Nothing here fails hard: if RTSS is not running the mapping either does not exist
or carries no signature, and :func:`read_fps` returns ``None``.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import struct
from dataclasses import dataclass

SIGNATURE = 0x52545353  # the four bytes 'SSTR' as a little-endian DWORD
MAP_NAME = "RTSSSharedMemoryV2"

_HEADER = struct.Struct("<8I")  # signature, version, appEntrySize, appArrOffset,
#                                 appArrSize, osdEntrySize, osdArrOffset, osdArrSize

# Offsets inside RTSS_SHARED_MEMORY_APP_ENTRY.
_OFF_PROCESS_ID = 0x000
_OFF_NAME = 0x004
_NAME_LEN = 260
_OFF_TIME0 = 0x10C
_OFF_TIME1 = 0x110
_OFF_FRAMES = 0x114
_OFF_FRAMETIME = 0x118
_OFF_STAT_FRAMERATE_MIN = 0x130
_OFF_STAT_FRAMERATE_AVG = 0x134

# An entry whose measurement window ended longer ago than this is a game that has
# already exited or is not currently presenting.
STALE_MS = 3000

IS_WINDOWS = os.name == "nt"

# What the FPS tile says when there is no framerate. On Windows that is a missing
# program and the text is an instruction; on Linux it is a fact about the platform
# and pointing at RTSS would only send someone looking for software that does not
# exist there.
STATE_MISSING = "RTSS 未运行" if IS_WINDOWS else "无 FPS 源"
HINT_LONG = "请启动 MSI Afterburner / RTSS" if IS_WINDOWS else "Linux 上没有 FPS 统计"
HINT_SHORT = "需 Afterburner / RTSS" if IS_WINDOWS else "Linux 无 FPS"

if IS_WINDOWS:
    _GetTickCount = ctypes.windll.kernel32.GetTickCount
    _GetTickCount.restype = ctypes.c_uint32

    _user32 = ctypes.windll.user32


def foreground_pid() -> int:
    """PID owning the foreground window, or 0."""
    if not IS_WINDOWS:
        return 0
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = ctypes.c_uint32(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


@dataclass
class FpsSample:
    fps: float
    frametime_ms: float
    process: str
    pid: int


def _tick() -> int:
    return int(_GetTickCount())


def _open(size: int) -> mmap.mmap:
    return mmap.mmap(-1, size, tagname=MAP_NAME, access=mmap.ACCESS_READ)


def _read_entry(buf: memoryview, base: int) -> FpsSample | None:
    (pid,) = struct.unpack_from("<I", buf, base + _OFF_PROCESS_ID)
    if not pid:
        return None

    t0, t1, frames = struct.unpack_from("<3I", buf, base + _OFF_TIME0)
    if t1 <= t0 or not frames:
        return None
    if _tick() - t1 > STALE_MS:
        return None

    fps = frames * 1000.0 / (t1 - t0)
    if not (0 < fps < 10000):
        return None

    (frametime_us,) = struct.unpack_from("<I", buf, base + _OFF_FRAMETIME)
    frametime_ms = frametime_us / 1000.0 if frametime_us else (1000.0 / fps)

    raw = bytes(buf[base + _OFF_NAME : base + _OFF_NAME + _NAME_LEN])
    name = raw.split(b"\0", 1)[0].decode("mbcs", "replace")

    return FpsSample(fps=fps, frametime_ms=frametime_ms,
                     process=os.path.basename(name) or "?", pid=pid)


def read_fps() -> FpsSample | None:
    """FPS of the app you are actually looking at, or None.

    RTSS hooks every Direct3D process, including desktop widgets and chat clients,
    so simply taking the freshest entry reports nonsense like "TrafficMonitor.exe
    9 FPS". Restricting it to the process that owns the foreground window is what
    makes the number mean "the game".
    """
    fg = foreground_pid()
    if not fg:
        return None
    try:
        head = _open(_HEADER.size)
    except OSError:
        return None
    try:
        sig, _ver, entry_size, arr_offset, arr_size = _HEADER.unpack(
            head.read(_HEADER.size))[:5]
    finally:
        head.close()

    if sig != SIGNATURE or not entry_size or not arr_size:
        return None

    total = arr_offset + entry_size * arr_size
    try:
        mm = _open(total)
    except OSError:
        return None

    try:
        buf = memoryview(mm)
        found: FpsSample | None = None
        for i in range(arr_size):
            base = arr_offset + i * entry_size
            (pid,) = struct.unpack_from("<I", buf, base + _OFF_PROCESS_ID)
            if pid != fg:
                continue
            found = _read_entry(buf, base)
            if found is not None:
                break
        buf.release()
        return found
    finally:
        mm.close()


def is_running() -> bool:
    """True when the RTSS shared memory block exists and is signed."""
    if not IS_WINDOWS:
        return False
    try:
        head = _open(_HEADER.size)
    except OSError:
        return False
    try:
        return _HEADER.unpack(head.read(_HEADER.size))[0] == SIGNATURE
    finally:
        head.close()


if __name__ == "__main__":
    print("RTSS running:", is_running())
    print("foreground pid:", foreground_pid())
    print("sample:", read_fps())
