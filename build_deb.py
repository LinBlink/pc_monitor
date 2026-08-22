"""Build a .deb that installs PC Monitor as a service on Ubuntu / Debian.

Run it on the Linux box (or in WSL) — it needs ``dpkg-deb``, and nothing else:

    python3 build_deb.py            ->  dist/pcmon_1.0.0_all.deb
    sudo apt install ./dist/pcmon_1.0.0_all.deb

Why a .deb rather than a PyInstaller binary like the Windows side gets: what is
left to depend on is already in the distribution, so the honest thing is to
declare it and let apt do its job — Pillow arrives with the distribution's own
security updates, and the CJK font the dashboard needs is a package too. That
turns "install 40 MB of vendored copies" into "install 70 kB of Python".

The package is architecture-independent because there is nothing compiled in it.

Layout, which is plain Debian convention:

    /usr/lib/pcmon/*.py                  the program (read-only)
    /usr/bin/pcmon                       run it by hand
    /usr/lib/systemd/system/pcmon.service   the service, enabled on install
    /var/lib/pcmon/                      config.json and the counters

That split is the whole reason ``paths.state_dir()`` exists: on Windows the state
sits beside the exe, but here the code is root-owned and shared, so the service is
pointed at /var/lib/pcmon through ``PCMON_DATA``.
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

VERSION = "1.0.4"
PACKAGE = "pcmon"
MAINTAINER = "linblink <wang.berlin.mail@gmail.com>"

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything the server imports, and nothing else: the handheld deployer needs
# paramiko, and the exe builder, icon maker and preview renderer are development
# tools. None of them belong on a monitored server.
MODULES = ("advice.py", "aiquota.py", "alerts.py", "diskstat.py", "dockerstat.py",
           "metrics.py", "paths.py", "perfcounters.py", "power.py", "render.py",
           "rtss.py", "sensors.py", "server.py", "sysinfo.py", "theme.py",
           "weather.py", "webjson.py", "webui.py")

# Two real dependencies, and both are load-bearing:
#
#   python3-pil     the frames are JPEG, and there is no encoding one of those in
#                   pure Python at eight frames a second.
#   a CJK font      every label on the dashboard is Chinese; without one Pillow
#                   falls back to a bitmap face and draws a screen of squares.
#                   wqy-zenhei is offered first because it is a fifth the size of
#                   Noto CJK, and either satisfies the alternative.
#
# Everything else this needs is in the standard library — see sysinfo.py, which
# is what replaced psutil on Linux. 3.10 is Ubuntu 22.04 / Pop!_OS 22.04, and the
# code is kept inside what that parser accepts.
CONTROL = f"""Package: {PACKAGE}
Version: {VERSION}
Section: admin
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pil, fonts-wqy-zenhei | fonts-noto-cjk, adduser
Recommends: systemd
Suggests: docker.io, nvidia-utils-550
Maintainer: {MAINTAINER}
Description: Telemetry dashboard streamed to a handheld over WiFi
 Samples CPU, memory, network, disk, GPU and Docker on this machine, draws them
 as a dashboard and serves it as an MJPEG stream for a handheld console, plus a
 resolution-independent web page for anything with a browser.
 .
 The service listens on port 8765 and is discovered automatically by handhelds
 and by other PC Monitor instances sweeping the local subnet.
"""

SERVICE = """[Unit]
Description=PC Monitor telemetry server
Documentation=https://github.com/linblink/pcmon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pcmon
Group=pcmon
# The code is root-owned and read-only; everything this writes goes here.
Environment=PCMON_DATA=/var/lib/pcmon
StateDirectory=pcmon
WorkingDirectory=/var/lib/pcmon
ExecStart=/usr/bin/python3 /usr/lib/pcmon/server.py
Restart=always
RestartSec=5

# It is an HTTP server on the LAN, so it gets no more of the machine than it
# needs to read. Every one of these is compatible with reading /proc and /sys,
# which is all the collector does.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectControlGroups=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
# AF_UNIX is the Docker socket; AF_NETLINK is how the network interfaces are
# enumerated. Neither is optional, the rest are refused.
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target
"""

LAUNCHER = """#!/bin/sh
# Run PC Monitor in this terminal. The packaged service runs the same program as
# the pcmon user with PCMON_DATA=/var/lib/pcmon; started this way it keeps its
# settings in ~/.local/share/pcmon instead, so the two do not fight over a file.
exec /usr/bin/python3 /usr/lib/pcmon/server.py "$@"
"""

POSTINST = """#!/bin/sh
set -e

case "$1" in
  configure)
    # Before adduser, not after: adduser checks the home directory it is told
    # about and warns when it is missing, which looks like a failure and is not.
    # StateDirectory= in the unit would create it too, but only at first start.
    mkdir -p /var/lib/pcmon

    if ! getent passwd pcmon >/dev/null; then
      adduser --system --group --no-create-home --home /var/lib/pcmon \\
              --gecos "PC Monitor" pcmon
    fi
    chown pcmon:pcmon /var/lib/pcmon
    chmod 0750 /var/lib/pcmon

    # Byte-compile with the interpreter that will actually run this. It caches
    # the .pyc so startup is quick from a read-only directory, and it is the one
    # check that would catch this Python being too old for the source. postrm
    # deletes the cache: dpkg only knows about files it installed itself.
    if ! python3 -m compileall -q /usr/lib/pcmon >/dev/null; then
      echo "[error] /usr/lib/pcmon failed to compile under $(python3 -V 2>&1)." >&2
      echo "        PC Monitor needs Python 3.10 or newer." >&2
    fi

    if [ -d /run/systemd/system ]; then
      systemctl daemon-reload || true
      systemctl enable --now pcmon.service || true
      echo ""
      echo "PC Monitor is running on port 8765:"
      for ip in $(hostname -I 2>/dev/null || true); do
        echo "  settings  http://$ip:8765/settings"
        echo "  dashboard http://$ip:8765/hd"
      done
      echo ""
      echo "  logs      journalctl -u pcmon -f"
      echo ""
      echo "  Docker tile, rootful daemon:"
      echo "      sudo adduser pcmon docker && sudo systemctl restart pcmon"
      echo "      (the restart is not optional - group membership is read at"
      echo "      start; and that account then has root-equivalent access)"
      echo "  Docker tile, ROOTLESS daemon (dockerd running as you, not root):"
      echo "      its socket lives in /run/user/YOUR_UID and no other account can"
      echo "      reach it. Run PC Monitor as yourself instead:"
      echo "      systemctl --user disable --now pcmon 2>/dev/null; sudo systemctl"
      echo "      disable --now pcmon && pcmon --install-autostart"
      echo "  CPU wattage needs:  read access to /sys/class/powercap/*/energy_uj"
      echo "                      otherwise it is estimated from the configured TDP"
    fi
    ;;
esac

exit 0
"""

PRERM = """#!/bin/sh
set -e

if [ "$1" = remove ] || [ "$1" = deconfigure ]; then
  if [ -d /run/systemd/system ]; then
    systemctl stop pcmon.service || true
  fi
fi

exit 0
"""

POSTRM = """#!/bin/sh
set -e

case "$1" in
  remove)
    if [ -d /run/systemd/system ]; then
      systemctl disable pcmon.service || true
      systemctl daemon-reload || true
    fi
    # Written by postinst, so dpkg does not know to remove it and would leave
    # an empty /usr/lib/pcmon behind.
    rm -rf /usr/lib/pcmon/__pycache__
    rmdir /usr/lib/pcmon 2>/dev/null || true
    ;;
  purge)
    # Settings, the daily counters and the snapshot history. Only on purge:
    # "apt remove" is reversible and taking a month of energy readings with it
    # would not be.
    rm -rf /var/lib/pcmon
    if getent passwd pcmon >/dev/null; then
      deluser --system pcmon >/dev/null 2>&1 || true
    fi
    ;;
esac

exit 0
"""

COPYRIGHT = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: pcmon
Source: https://github.com/linblink/pcmon

Files: *
Copyright: 2026 linblink
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy of
 this software and associated documentation files (the "Software"), to deal in
 the Software without restriction, including without limitation the rights to
 use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
 the Software, and to permit persons to whom the Software is furnished to do so,
 subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all
 copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
 FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
 COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
 IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


def _write(path: str, text: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # newline="\n" matters: this script also runs on Windows for a dry build, and
    # a maintainer script with CRLF endings fails with a baffling "not found".
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.chmod(path, mode)


def build(out_dir: str) -> str:
    root = os.path.join(out_dir, f"{PACKAGE}_{VERSION}_all")
    if os.path.exists(root):
        shutil.rmtree(root)

    lib = os.path.join(root, "usr", "lib", PACKAGE)
    os.makedirs(lib)
    for name in MODULES:
        src = os.path.join(HERE, name)
        if not os.path.exists(src):
            raise SystemExit(f"missing source file: {name}")
        shutil.copy2(src, os.path.join(lib, name))
        os.chmod(os.path.join(lib, name), 0o644)

    _write(os.path.join(root, "usr", "bin", PACKAGE), LAUNCHER, 0o755)
    _write(os.path.join(root, "usr", "lib", "systemd", "system",
                        f"{PACKAGE}.service"), SERVICE)

    doc = os.path.join(root, "usr", "share", "doc", PACKAGE)
    _write(os.path.join(doc, "copyright"), COPYRIGHT)
    changelog = (f"{PACKAGE} (1.0.2) stable; urgency=medium\n\n"
                 f"  * Do not render frames when nobody is watching. An idle\n"
                 f"    server drew 8 dashboards a second for no one, which cost a\n"
                 f"    whole core on a low-power machine.\n"
                 f"  * Create /var/lib/pcmon before adduser, so it stops warning.\n\n"
                 f" -- {MAINTAINER}  "
                 f"{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}\n\n"
                 f"{PACKAGE} (1.0.1) stable; urgency=medium\n\n"
                 f"  * Drop the psutil dependency: sysinfo.py reads /proc directly.\n"
                 f"  * Require Python 3.10 rather than 3.12, so 22.04 can install it.\n"
                 f"  * Offer fonts-wqy-zenhei as the smaller CJK font alternative.\n\n"
                 f" -- {MAINTAINER}  "
                 f"{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}\n\n"
                 f"{PACKAGE} (1.0.0) stable; urgency=medium\n\n"
                 f"  * Linux support: hwmon/cpufreq/RAPL sensors, /proc process\n"
                 f"    tables, mount-point disk stats, systemd service.\n\n"
                 f" -- {MAINTAINER}  "
                 f"{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}\n")
    os.makedirs(doc, exist_ok=True)
    with gzip.GzipFile(os.path.join(doc, "changelog.gz"), "wb", mtime=0) as fh:
        fh.write(changelog.encode("utf-8"))
    os.chmod(os.path.join(doc, "changelog.gz"), 0o644)

    _write(os.path.join(root, "DEBIAN", "control"), CONTROL)
    _write(os.path.join(root, "DEBIAN", "postinst"), POSTINST, 0o755)
    _write(os.path.join(root, "DEBIAN", "prerm"), PRERM, 0o755)
    _write(os.path.join(root, "DEBIAN", "postrm"), POSTRM, 0o755)

    for dirpath, dirnames, _files in os.walk(root):
        for name in [dirpath] + [os.path.join(dirpath, d) for d in dirnames]:
            os.chmod(name, 0o755)
    return root


def _modes_stick(where: str) -> bool:
    """Does chmod survive in this directory?

    On a Windows drive mounted into WSL it does not: every file reads back as
    0777, and dpkg-deb refuses a control directory with those permissions. That
    is the normal case when this repo lives on C: and the build runs under WSL,
    so it is detected rather than diagnosed by the error message.
    """
    probe = os.path.join(where, ".modeprobe")
    try:
        os.makedirs(probe, exist_ok=True)
        os.chmod(probe, 0o755)
        return stat.S_IMODE(os.stat(probe).st_mode) == 0o755
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="build the PC Monitor .deb")
    ap.add_argument("--out", default=os.path.join(HERE, "dist"),
                    help="where to put the .deb (default: dist/)")
    ap.add_argument("--tree-only", action="store_true",
                    help="assemble the package tree but do not call dpkg-deb")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    staging = args.out if _modes_stick(args.out) else tempfile.mkdtemp(
        prefix="pcmon-deb-")
    root = build(staging)
    print(f"tree: {root}")
    if args.tree_only:
        return 0

    if not shutil.which("dpkg-deb"):
        print("dpkg-deb not found — run this on Debian/Ubuntu (or under WSL).\n"
              "The package tree above is complete; only the final step is missing.")
        return 1

    name = f"{PACKAGE}_{VERSION}_all.deb"
    built = os.path.join(staging, name)
    # --root-owner-group: files must be owned by root inside the archive, and the
    # user building it is not root.
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", root, built],
                   check=True)

    deb = os.path.join(args.out, name)
    if os.path.abspath(built) != os.path.abspath(deb):
        shutil.move(built, deb)
        shutil.rmtree(staging, ignore_errors=True)

    subprocess.run(["dpkg-deb", "--info", deb], check=False)
    size = os.path.getsize(deb)
    print(f"\nwrote {deb} ({size / 1024:.0f} kB)\n"
          f"install with:  sudo apt install {deb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
