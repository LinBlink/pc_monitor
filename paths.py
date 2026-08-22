"""Where this program keeps its files: running from source, as a frozen exe, or
installed from a package.

Three questions, three answers:

* **Where is the code?** ``data_path`` — read-only, and under PyInstaller it is a
  temporary directory that is deleted on exit.
* **Where is the program's own directory?** ``base_dir`` — beside the exe, or the
  source tree.
* **Where do config.json, traffic.json, power.json and history.jsonl go?**
  ``state_dir``, and that is the interesting one. Beside the exe on Windows, so
  moving the exe moves its settings with it. But a .deb installs the code into
  ``/usr/lib/pcmon``, which no ordinary user may write to and which several users
  might share, so on Linux the state goes to the user's own
  ``~/.local/share/pcmon`` — or wherever ``PCMON_DATA`` points, which is how the
  packaged systemd unit sends it to ``/var/lib/pcmon`` instead.
"""

from __future__ import annotations

import os
import sys


def frozen() -> bool:
    return getattr(sys, "frozen", False)


def base_dir() -> str:
    """The directory the program itself lives in."""
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _writable(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK)


def state_dir() -> str:
    """Where the files this program writes belong. Created if it is missing.

    ``PCMON_DATA`` wins when it is set: that is the packaged service's way of
    saying "your state lives in /var/lib/pcmon", and it is equally the escape
    hatch for anyone who wants two copies with separate settings.
    """
    override = os.environ.get("PCMON_DATA")
    if override:
        return _ensure(override)

    home = base_dir()
    # Windows, and any source checkout: keep everything in one directory, which
    # is what makes an exe or a git clone self-contained.
    if os.name == "nt" or _writable(home):
        return home
    return _ensure(os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "pcmon"))


def _ensure(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # Nothing here may fail hard: a read-only state directory costs the
        # saved settings and the daily counters, not the dashboard.
        pass
    return path


def data_path(*parts: str) -> str:
    """A read-only file shipped with the program (bundled into the exe)."""
    root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)
