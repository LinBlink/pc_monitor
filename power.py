"""Whole-machine energy use, integrated from the power the sensors do report.

There is no wall-socket reading available to software, so this is an estimate and
says so on screen. What it adds up is:

    (CPU package + GPU board + a fixed rest-of-machine figure) / PSU efficiency

CPU package power comes from Afterburner and GPU board power from nvidia-smi;
when either is missing it falls back to that part's TDP scaled by utilisation,
which is much closer to the truth than counting it as zero. The rest of the
machine — board, RAM, drives, fans, USB — barely moves with load, so a single
configurable number covers it better than a model would. All of it is then
divided by the supply's efficiency, because that is the part the meter sees.

Energy is accumulated the same way the daily traffic counter works: watts times
the interval since the last sample, added into a per-day bucket and persisted, so
a restart costs only the seconds the server was down. Only time the server was
running is counted, which is the honest thing for it to report.
"""

from __future__ import annotations

import datetime
import json
import os
import time

# A sample gap longer than this is sleep, hibernation or a stalled frame loop
# rather than elapsed running time, so it contributes nothing. Counting it would
# silently bill a night of standby at the machine's last known load.
MAX_GAP_S = 60.0

KEEP_DAYS = 40  # a little past the longest window, so 30 days is always whole
SAVE_EVERY_S = 30.0


class PowerLog:
    """Running estimate in watts, plus per-day totals in kWh."""

    def __init__(self, path: str, cfg=None):
        self.path = path
        self._cfg = cfg or (lambda: {})
        self.days: dict[str, float] = {}
        self.watts = 0.0
        self.estimated = True
        self._last_t: float | None = None
        self._last_save = 0.0
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.date.today().isoformat()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        days = data.get("days")
        if isinstance(days, dict):
            for day, kwh in days.items():
                try:
                    self.days[str(day)] = float(kwh)
                except (TypeError, ValueError):
                    continue

    def _save(self) -> None:
        # Trim here rather than on read: the file is the only thing that grows,
        # and a day that has fallen out of every window will never be shown again.
        for day in sorted(self.days)[:-KEEP_DAYS]:
            del self.days[day]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"days": {d: round(v, 5)
                                    for d, v in sorted(self.days.items())}}, fh,
                          indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def estimate(self, snap: dict) -> tuple[float, bool]:
        """Wall-socket watts for this sample, and whether anything was guessed."""
        cfg = self._cfg() or {}
        base = float(cfg.get("power_base_w") or 0)
        eff = max(10.0, float(cfg.get("power_psu_pct") or 90)) / 100.0

        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        guessed = False

        cpu_w = cpu.get("power_w")
        if cpu_w is None:
            # Idle silicon is not free, so the curve starts a quarter of the way
            # up rather than at the origin.
            tdp = float(cfg.get("cpu_tdp_w") or 0)
            cpu_w = tdp * (0.25 + 0.75 * float(cpu.get("percent") or 0) / 100.0)
            guessed = True

        if gpu.get("ok"):
            gpu_w = float(gpu.get("power_w") or 0.0)
            if gpu_w <= 0:  # some laptop GPUs report no power at all
                guessed = True
        else:
            gpu_w = 0.0  # no discrete GPU visible; the iGPU is inside the package

        return (float(cpu_w) + gpu_w + base) / eff, guessed

    def add(self, snap: dict) -> None:
        """Fold one telemetry sample into the running totals."""
        watts, guessed = self.estimate(snap)
        self.watts, self.estimated = watts, guessed

        now = time.monotonic()
        last, self._last_t = self._last_t, now
        if last is None:
            return  # first sample: an interval needs two of them
        dt = now - last
        if dt <= 0 or dt > MAX_GAP_S:
            return

        today = self._today()
        self.days[today] = self.days.get(today, 0.0) + watts * dt / 3_600_000.0
        if now - self._last_save >= SAVE_EVERY_S:
            self._last_save = now
            self._save()

    def kwh(self, days: int) -> float:
        """Total over the last ``days`` calendar days, today included."""
        first = (datetime.date.today() -
                 datetime.timedelta(days=days - 1)).isoformat()
        return sum(v for d, v in self.days.items() if d >= first)

    def data(self) -> dict:
        cfg = self._cfg() or {}
        price = cfg.get("power_price")
        d1, d7, d30 = self.kwh(1), self.kwh(7), self.kwh(30)
        return {
            "watts": self.watts,
            "estimated": self.estimated,
            "d1": d1,
            "d7": d7,
            "d30": d30,
            # The cost line is off unless a price is configured: a made-up
            # tariff would look like a measurement.
            "cost30": (d30 * float(price)) if price else None,
            "days": len(self.days),
        }


if __name__ == "__main__":
    import metrics

    log = PowerLog(os.path.join(os.path.dirname(__file__), "power.json"),
                   lambda: {"power_base_w": 45, "power_psu_pct": 90,
                            "cpu_tdp_w": 65})
    collector = metrics.Collector()
    for _ in range(3):
        time.sleep(1.0)
        log.add(collector.sample())
        print(json.dumps(log.data(), indent=2))
    collector.close()
