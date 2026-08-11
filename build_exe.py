"""Build a single-file PCMonitor.exe with PyInstaller.

    python -m pip install pyinstaller
    python build_exe.py

The result is ``dist/PCMonitor.exe`` and needs nothing installed on the machine it
runs on — no Python, no psutil, no Pillow. It still uses the system's own tools
where they exist (``nvidia-smi`` for GPU, PowerShell for the GPU-per-process
counters, Windows' fonts), and reads config.json / traffic.json from the folder
the exe sits in.

Deliberately excluded: paramiko and the device/ folder. Deploying to the handheld
is a one-time job from the development machine, and the handheld finds new PCs by
scanning the subnet, so a fresh PC only needs the exe running.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "PCMonitor"
ENTRY = "server.py"
ICON = "app.ico"

VERSION = (1, 0, 0, 0)
VERSION_FILE = "version_info.txt"

# Pulled in by nothing we use, but present in many environments; leaving them out
# keeps the exe from doubling in size.
EXCLUDES = ("tkinter", "paramiko", "cryptography", "numpy", "matplotlib",
            "pytest", "setuptools", "pip", "PIL.ImageQt", "PyQt5", "PySide2",
            "unittest", "pydoc_data")


def version_resource() -> str:
    """A Windows version resource, so the exe is not anonymous in Properties."""
    v = ", ".join(str(n) for n in VERSION)
    dotted = ".".join(str(n) for n in VERSION[:3])
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}), prodvers=({v}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'PC Monitor'),
      StringStruct('FileDescription', 'PC Monitor - 掌机 WiFi 实时看电脑状态'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', '{NAME}'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
      StringStruct('ProductName', 'PC Monitor'),
      StringStruct('ProductVersion', '{dotted}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
  ]
)
"""


def main() -> int:
    os.chdir(HERE)

    if not os.path.exists(ICON):
        print("app.ico missing — generating it")
        subprocess.run([sys.executable, "make_icon.py"], check=True)

    with open(VERSION_FILE, "w", encoding="utf-8") as fh:
        fh.write(version_resource())

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--console", "--name", NAME, "--icon", ICON,
           "--version-file", VERSION_FILE, "--noupx"]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(ENTRY)

    print("$", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode:
        return result.returncode

    exe = os.path.join("dist", NAME + ".exe")
    size_mb = os.path.getsize(exe) / 1024 ** 2
    print(f"\nbuilt {exe}  ({size_mb:.1f} MB)")

    # A frozen build has its own import graph; rendering one frame is the cheapest
    # proof that Pillow, psutil and the fonts all survived the bundling.
    print("smoke test: rendering one frame...")
    probe = os.path.join("dist", "_smoke.png")
    check = subprocess.run([exe, "--save", probe], capture_output=True, text=True,
                           timeout=180)
    ok = check.returncode == 0 and os.path.exists(probe)
    print((check.stdout or "").strip() or (check.stderr or "").strip())
    if ok:
        os.remove(probe)
        print("smoke test: OK")
    else:
        print("smoke test: FAILED")
        return 1

    for junk in ("build", VERSION_FILE, NAME + ".spec"):
        shutil.rmtree(junk, ignore_errors=True) if os.path.isdir(junk) else (
            os.path.exists(junk) and os.remove(junk))
    print("\nCopy dist/PCMonitor.exe anywhere and run it. config.json and "
          "traffic.json appear next to it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
