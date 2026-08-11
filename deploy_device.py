"""Push the handheld half of PC Monitor to the Miyoo over SSH.

Shell scripts must land with LF endings and the exec bit set, which is why this
uploads through SFTP with explicit newline handling instead of a plain copy.
"""

from __future__ import annotations

import os
import posixpath
import sys

import paramiko

HOST = os.environ.get("MIYOO_HOST", "192.168.2.5")
USER = os.environ.get("MIYOO_USER", "onion")
PWD = os.environ.get("MIYOO_PASS", "onion")
REMOTE_DIR = "/mnt/SDCARD/App/PCMonitor"

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE = os.path.join(HERE, "device")

TEXT_FILES = ["launch.sh", "settings.cfg", "config.json"]
BINARY_FILES = [("icon.png", os.path.join(HERE, "icon.png"))]
EXECUTABLE = {"launch.sh"}


def main() -> int:
    keep_settings = "--keep-settings" in sys.argv

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PWD, timeout=15,
                   allow_agent=False, look_for_keys=False)
    sftp = client.open_sftp()

    try:
        sftp.stat(REMOTE_DIR)
    except IOError:
        sftp.mkdir(REMOTE_DIR)

    existing = set(sftp.listdir(REMOTE_DIR))

    for name in TEXT_FILES:
        if name == "settings.cfg" and keep_settings and name in existing:
            print(f"  keep    {name}")
            continue
        with open(os.path.join(DEVICE, name), encoding="utf-8") as fh:
            body = fh.read().replace("\r\n", "\n")
        remote = posixpath.join(REMOTE_DIR, name)
        with sftp.open(remote, "w") as out:
            out.write(body)
        sftp.chmod(remote, 0o755 if name in EXECUTABLE else 0o644)
        print(f"  wrote   {name} ({len(body)} bytes)")

    for name, local in BINARY_FILES:
        remote = posixpath.join(REMOTE_DIR, name)
        sftp.put(local, remote)
        sftp.chmod(remote, 0o644)
        print(f"  wrote   {name}")

    sftp.close()

    _, out, _ = client.exec_command(f"ls -l {REMOTE_DIR}")
    print(out.read().decode("utf-8", "replace"))
    client.close()
    print(f"Deployed to {HOST}:{REMOTE_DIR}")
    print("Open Onion's Apps menu and launch \"PC Monitor\". MENU exits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
