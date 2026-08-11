"""Push the handheld half of PC Monitor to a handheld over SSH.

Three targets, because three firmwares put things in different places and want
different files:

    --miyoo    Onion OS   -> /mnt/SDCARD/App/PCMonitor   (default)
    --rocknix  ROCKNIX    -> /storage/roms/ports
    --muos     muOS       -> /mnt/mmc/MUOS/application/PC Monitor

Shell scripts must land with LF endings and the exec bit set, which is why this
uploads through SFTP with explicit newline handling instead of a plain copy.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import shlex
import sys
import xml.etree.ElementTree as ET

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE = os.path.join(HERE, "device")

MIYOO = {
    "host": os.environ.get("MIYOO_HOST", "192.168.2.5"),
    "user": os.environ.get("MIYOO_USER", "onion"),
    "password": os.environ.get("MIYOO_PASS", "onion"),
    "dir": "/mnt/SDCARD/App/PCMonitor",
    # (local name under device/, remote name, mode)
    "files": [("launch.sh", "launch.sh", 0o755),
              ("settings.cfg", "settings.cfg", 0o644),
              ("config.json", "config.json", 0o644)],
    "assets": [(os.path.join(HERE, "icon.png"), "icon.png", 0o644)],
    "settings": "settings.cfg",
    "open_with": 'Onion 的 Apps 菜单里选 "PC Monitor"。SELECT 退出。',
}

# ROCKNIX's EmulationStation lists every .sh directly under roms/ports, so the
# launcher has to sit in that directory itself. Its state, log and settings go
# in a subdirectory that gamelist.xml marks hidden - otherwise the Ports menu
# grows a folder next to the entry that launches it.
ROCKNIX = {
    "host": os.environ.get("ROCKNIX_HOST", "192.168.2.81"),
    "user": os.environ.get("ROCKNIX_USER", "root"),
    "password": os.environ.get("ROCKNIX_PASS", "rocknix"),
    "dir": "/storage/roms/ports",
    "data": "/storage/roms/ports/pcmonitor",
    "files": [("launch_rocknix.sh", "PCMonitor.sh", 0o755)],
    "data_files": [("settings_rocknix.cfg", "settings.cfg", 0o644)],
    "assets": [],
    "settings": "settings.cfg",
    "gamelist": "/storage/roms/ports/gamelist.xml",
    # The earlier port of this app dropped its working files straight into the
    # ports directory; clear them out so a redeploy is not shadowed by stale
    # settings sitting one level up.
    "stale": ["settings.cfg", "hosts.txt", "state.cfg", "pcmonitor.log", ".pid"],
    "open_with": 'EmulationStation 的 Ports 菜单里选 "PC Monitor"。SELECT 退出。',
}

# muOS wants one directory per application, with mux_launch.sh as the entry
# point; everything else in the directory is the app's own business, so state
# and settings just live next to the launcher.
MUOS = {
    "host": os.environ.get("MUOS_HOST", "192.168.2.105"),
    "user": os.environ.get("MUOS_USER", "root"),
    "password": os.environ.get("MUOS_PASS", "root"),
    "dir": "/mnt/mmc/MUOS/application/PC Monitor",
    "files": [("mux_launch.sh", "mux_launch.sh", 0o755),
              ("launch_muos.sh", "pcmonitor.sh", 0o755),
              ("settings_muos.cfg", "settings.cfg", 0o644)],
    # No icon: muOS takes application icons from the active theme
    # (image/grid/muxapp/<name>.png), not from the app's own directory, so a
    # file dropped here would do nothing. The entry falls back to the theme's
    # default glyph.
    "assets": [],
    "settings": "settings.cfg",
    "open_with": 'muOS 的 Applications 菜单里选 "PC Monitor"。SELECT 退出。',
}


def connect(target: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(target["host"], username=target["user"],
                   password=target["password"], timeout=15,
                   allow_agent=False, look_for_keys=False)
    return client


def ensure_dir(sftp, path: str) -> None:
    try:
        sftp.stat(path)
    except IOError:
        sftp.mkdir(path)


def put_text(sftp, local: str, remote: str, mode: int) -> None:
    with open(local, encoding="utf-8") as fh:
        body = fh.read().replace("\r\n", "\n")
    with sftp.open(remote, "w") as out:
        out.write(body)
    sftp.chmod(remote, mode)
    print(f"  wrote   {remote} ({len(body)} bytes)")


def update_gamelist(sftp, path: str) -> None:
    """Give the entry a real name and hide the working directory next to it.

    ES writes this file back itself, so the existing content (PortMaster's own
    hidden-folder entry, scraped metadata) is merged into rather than replaced.
    """
    try:
        with sftp.open(path) as fh:
            root = ET.fromstring(fh.read().decode("utf-8"))
    except (IOError, ET.ParseError):
        root = ET.Element("gameList")

    wanted = [("game", "./PCMonitor.sh", "PC Monitor",
               "通过 WiFi 全屏显示 PC 的 CPU / GPU / 内存 / 网络 / 游戏 FPS。"
               "左右键切换设备，Y 转屏，SELECT 退出。", False),
              ("folder", "./pcmonitor", "PC Monitor data", None, True)]

    for tag, rel, name, desc, hidden in wanted:
        node = next((n for n in root.findall(tag)
                     if (n.findtext("path") or "").strip() == rel), None)
        if node is None:
            node = ET.SubElement(root, tag)
            ET.SubElement(node, "path").text = rel
        for field, value in (("name", name), ("desc", desc),
                             ("hidden", "true" if hidden else None)):
            if value is None:
                continue
            child = node.find(field)
            if child is None:
                child = ET.SubElement(node, field)
            child.text = value

    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="unicode")
    with sftp.open(path, "w") as out:
        out.write(body)
    print(f"  wrote   {path} (Ports 菜单条目)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rocknix", action="store_true", help="deploy to ROCKNIX")
    ap.add_argument("--muos", action="store_true", help="deploy to muOS")
    ap.add_argument("--miyoo", action="store_true", help="deploy to Onion (default)")
    ap.add_argument("--keep-settings", action="store_true",
                    help="leave the handheld's own settings.cfg alone")
    args = ap.parse_args()

    target = ROCKNIX if args.rocknix else MUOS if args.muos else MIYOO
    print(f"Deploying to {target['user']}@{target['host']}:{target['dir']}")

    client = connect(target)
    sftp = client.open_sftp()

    ensure_dir(sftp, target["dir"])
    data_dir = target.get("data")
    if data_dir:
        ensure_dir(sftp, data_dir)

    groups = [(target["dir"], target["files"]),
              (data_dir, target.get("data_files", []))]
    for base, files in groups:
        if not base:
            continue
        existing = set(sftp.listdir(base))
        for local_name, remote_name, mode in files:
            if (remote_name == target["settings"] and args.keep_settings
                    and remote_name in existing):
                print(f"  keep    {remote_name}")
                continue
            put_text(sftp, os.path.join(DEVICE, local_name),
                     posixpath.join(base, remote_name), mode)

    for local, remote_name, mode in target["assets"]:
        remote = posixpath.join(target["dir"], remote_name)
        sftp.put(local, remote)
        sftp.chmod(remote, mode)
        print(f"  wrote   {remote_name}")

    for name in target.get("stale", []):
        stale = posixpath.join(target["dir"], name)
        try:
            sftp.remove(stale)
            print(f"  removed {stale} (前一版留下的)")
        except IOError:
            pass

    if target.get("gamelist"):
        update_gamelist(sftp, target["gamelist"])

    sftp.close()

    _, out, _ = client.exec_command(f"ls -l {shlex.quote(target['dir'])}")
    print(out.read().decode("utf-8", "replace"))
    client.close()
    print(f"Deployed to {target['host']}:{target['dir']}")
    print(target["open_with"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
