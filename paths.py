"""Where this program keeps its files, running from source or as a frozen exe.

PyInstaller's one-file build unpacks the code into a temporary directory that is
deleted on exit, so ``__file__`` is the wrong anchor for anything the user is
meant to keep: config.json and traffic.json have to sit next to the exe instead.
"""

from __future__ import annotations

import os
import sys


def frozen() -> bool:
    return getattr(sys, "frozen", False)


def base_dir() -> str:
    """The directory holding user-visible files — beside the exe, or the source."""
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_path(*parts: str) -> str:
    """A read-only file shipped with the program (bundled into the exe)."""
    root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)
