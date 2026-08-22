"""Docker container status, polled in the background.

The CLI is used rather than the engine API: the API lives on a named pipe on
Windows and a unix socket elsewhere, and talking to either from the standard
library means hand-rolling a transport for no gain. ``docker`` is on PATH
wherever the daemon is worth asking about.

Two commands per cycle, because neither answers on its own: ``ps -a`` lists
stopped containers, which ``stats`` never mentions, and ``stats`` carries the CPU
and memory numbers, which ``ps`` does not. ``stats --no-stream`` is the slow one —
it samples every running container over an interval — so this runs on its own
thread at a leisurely interval and the frame loop only ever reads the last result.

A machine with no Docker is the normal case, not an error: the poller notices,
says so once, and backs off to a slow re-check so the tile can appear if the
daemon is started later.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

EVERY_S = 10.0
IDLE_S = 60.0  # no daemon: still worth re-checking, just not every 10 seconds
TIMEOUT_S = 12.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _rootless_host() -> str | None:
    """``DOCKER_HOST`` for a rootless daemon owned by this user, if there is one.

    Rootless Docker puts its socket in the user's runtime directory rather than
    /var/run, and tells you to export DOCKER_HOST in your shell profile — which a
    systemd unit never reads. So the socket is looked for directly: if it is
    there and ours, the CLI is pointed at it.

    Nothing is guessed for *other* users' daemons. /run/user/<uid> is 0700, so a
    service account cannot reach them however hard it tries, and pretending
    otherwise would only produce a confusing error.
    """
    if os.name == "nt" or os.environ.get("DOCKER_HOST"):
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    sock = os.path.join(runtime, "docker.sock")
    return f"unix://{sock}" if os.path.exists(sock) else None


def _env() -> dict | None:
    host = _rootless_host()
    return dict(os.environ, DOCKER_HOST=host) if host else None


def _run(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=TIMEOUT_S, creationflags=_NO_WINDOW,
                              env=_env())
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip()
    return True, proc.stdout


def _lines(out: str):
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _pct(text) -> float | None:
    """``"12.34%"`` as a number; Docker prints ``--`` for what it cannot sample."""
    try:
        return float(str(text).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


# Longest first, because "b" is a suffix of every other unit here: matching it
# against "364MiB" left "364mi" to parse as a float, and every running container
# silently lost its memory column.
_UNITS = sorted({"b": 1 / 1024 ** 2, "kib": 1 / 1024, "kb": 1 / 1024, "mib": 1.0,
                 "mb": 1.0, "gib": 1024.0, "gb": 1024.0}.items(),
                key=lambda kv: -len(kv[0]))


def _mib(text) -> float | None:
    """The used half of ``"123.4MiB / 7.66GiB"``, in MiB."""
    part = str(text or "").split("/")[0].strip().lower()
    for unit, scale in _UNITS:
        if part.endswith(unit):
            try:
                return float(part[:-len(unit)]) * scale
            except ValueError:
                return None
    return None


def _why(stderr: str) -> str:
    """The docker CLI's complaint, as something short enough for a tile.

    Worth the mapping: "服务未运行" sent someone down the wrong path for an hour
    when the real answer was that the daemon was rootless and belonged to another
    account. The three cases below need three different actions from the reader,
    so they get three different sentences.
    """
    low = (stderr or "").lower()
    if not stderr:
        return "未安装"
    if "permission denied" in low:
        return "无权访问 docker.sock"
    if "cannot connect" in low or "is the docker daemon running" in low:
        return "服务未运行"
    # Anything else is rare enough to be worth showing verbatim; the tile
    # ellipsizes and the web page has room for the whole line.
    return stderr.splitlines()[0][:60]


class DockerPoller(threading.Thread):
    """Container list in ``data``; never raises, never blocks the frame loop."""

    def __init__(self):
        super().__init__(daemon=True)
        self.data: dict = {"ok": False, "err": "starting", "containers": []}
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            found = self._poll()
            self._stop.wait(EVERY_S if found else IDLE_S)

    def _poll(self) -> bool:
        ok, out = _run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"])
        if not ok:
            self.data = {"ok": False, "err": _why(out), "containers": []}
            return False

        rows = []
        for row in _lines(out):
            name = (row.get("Names") or row.get("Name") or "").split(",")[0]
            if not name:
                continue
            rows.append({
                "name": name,
                "state": (row.get("State") or "").lower(),
                "status": row.get("Status") or "",
                "image": row.get("Image") or "",
                "cpu": None,
                "mem_mb": None,
                "mem_pct": None,
            })

        by_name = {r["name"]: r for r in rows}
        if any(r["state"] == "running" for r in rows):
            ok, out = _run(["docker", "stats", "--no-stream",
                            "--format", "{{json .}}"])
            if ok:
                for row in _lines(out):
                    entry = by_name.get(row.get("Name") or "")
                    if entry is None:
                        continue
                    entry["cpu"] = _pct(row.get("CPUPerc"))
                    entry["mem_mb"] = _mib(row.get("MemUsage"))
                    entry["mem_pct"] = _pct(row.get("MemPerc"))

        # Running first, then by CPU: a short list on a small screen should lead
        # with the containers that are actually doing something.
        rows.sort(key=lambda r: (r["state"] != "running", -(r["cpu"] or 0.0),
                                 r["name"]))
        self.data = {
            "ok": True,
            "err": None,
            "at": time.time(),
            "running": sum(1 for r in rows if r["state"] == "running"),
            "total": len(rows),
            "containers": rows,
        }
        return True


if __name__ == "__main__":
    poller = DockerPoller()
    poller.start()
    for _ in range(30):
        time.sleep(1.0)
        if poller.data.get("err") != "starting":
            break
    print(json.dumps(poller.data, ensure_ascii=False, indent=2))
    poller.stop()
