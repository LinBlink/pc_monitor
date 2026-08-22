"""Note when an AI 5-hour quota window is nearly spent.

The 5-hour window is the one that actually stops you working, and it is easy to
walk past 80% without looking at the dashboard — so this watches it and records
the crossing.

It used to say so out loud and put a banner across the web dashboard. Both were
taken out, so what is left is the record: a line in the log, the last one on the
settings page, and /alert.json for anything that wants to build its own
notification on top of it.

*Once per window, not once per sample.* The quota is checked at the frame rate,
but a warning that repeats every eighth of a second is noise. A window is
identified by its reset time, so each one can only fire once; the next window is
a different reset time and arms itself. When a provider reports no reset time,
the fallback is a hysteresis band — it re-arms only after dropping well back
below the level, so a reading hovering on the threshold cannot chatter.
"""

from __future__ import annotations

import threading
import time

# Which 5-hour windows exist to watch, and what to call them in the message.
WATCHED = (("claude", "Claude"), ("minimax", "MiniMax"))

# Below this the alert re-arms when the provider gives no reset time to key on.
REARM_MARGIN = 10.0


def _pct_of(provider: str, block: dict | None) -> tuple[float | None, float | None]:
    """``(percent used, window reset)`` for one provider's 5-hour window.

    The two providers shape it differently — Claude nests a ``{pct, resets_at}``
    dict, MiniMax reports a bare number beside a separate reset field — so the
    difference is absorbed here rather than in the watcher.
    """
    if not isinstance(block, dict) or not block.get("ok"):
        return None, None
    window = block.get("five_hour")
    if isinstance(window, dict):
        return window.get("pct"), window.get("resets_at")
    if isinstance(window, (int, float)):
        return float(window), block.get("five_hour_reset")
    return None, None


class QuotaAlert:
    """Watches every 5-hour AI quota window and records the first one to cross.

    ``cfg`` is a callable returning the live settings, so the threshold and the
    on/off switch take effect without a restart.
    """

    def __init__(self, out_dir: str, cfg=None):
        self.out_dir = out_dir
        self._cfg = cfg or (lambda: {})
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[float | None, float]] = {}
        # Published as-is in /stats.json and /alert.json.
        self.data: dict = {"id": 0, "at": None, "text": "", "provider": "",
                           "pct": None}

    # --- polling -----------------------------------------------------------

    def check(self, ai: dict) -> None:
        """Called once per sample with the quota poller's data. Cheap."""
        cfg = self._cfg() or {}
        if not cfg.get("ai_alert_enabled", True):
            return
        try:
            level = float(cfg.get("ai_alert_pct") or 80)
        except (TypeError, ValueError):
            level = 80.0

        for key, name in WATCHED:
            pct, resets_at = _pct_of(key, (ai or {}).get(key))
            if pct is None:
                continue
            if self._claim(key, pct, resets_at, level):
                self._fire(name, pct, resets_at, level)
                return  # one at a time; the next sample takes the rest

    def _claim(self, key: str, pct: float, resets_at: float | None,
               level: float) -> bool:
        """Whether this reading is a new crossing rather than the same one again."""
        with self._lock:
            fired_for, low_water = self._seen.get(key, (None, 0.0))
            if pct < level:
                # Remember how far back down it went: that is what re-arms a
                # provider whose window has no reset time to key on.
                self._seen[key] = (fired_for, min(low_water, pct))
                return False
            if resets_at is not None:
                # A different window is a different alert, however recently the
                # previous one fired.
                if fired_for is not None and abs(resets_at - fired_for) < 60:
                    return False
                self._seen[key] = (resets_at, pct)
            elif fired_for is not None and low_water > level - REARM_MARGIN:
                return False
            else:
                self._seen[key] = (time.time(), pct)
            return True

    def _fire(self, name: str, pct: float, resets_at: float | None,
              level: float) -> None:
        when = ""
        if resets_at:
            rem = resets_at - time.time()
            if rem > 0:
                when = f"，{int(rem // 3600)} 小时 {int(rem % 3600 // 60)} 分后重置"
        text = f"提醒：{name} 的 5 小时额度已经用掉 {pct:.0f}%{when}。"

        with self._lock:
            self.data = dict(self.data, id=self.data["id"] + 1, at=time.time(),
                             text=text, provider=name, pct=pct, level=level)
        print(f"[alert] {text}", flush=True)


if __name__ == "__main__":
    import paths

    watcher = QuotaAlert(paths.state_dir(), lambda: {"ai_alert_enabled": True})
    sample = {"claude": {"ok": True, "five_hour": {"pct": 84.0,
                                                   "resets_at": time.time() + 5400}}}
    watcher.check(sample)
    watcher.check(sample)  # same window: must not fire twice
    print(watcher.data)
