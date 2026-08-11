"""AI subscription quota, polled in the background.

Three providers, one thread. Claude needs no configuration — it reads the OAuth
token Claude Code already keeps on this machine — while DeepSeek and MiniMax are
only polled once an API key is set in config.json.

The intervals are deliberately unequal: Anthropic's usage endpoint is rate
limited hard enough that aimon (the project this is ported from) had to keep the
previous snapshot on a 429, so it is polled once a minute and backs off on any
failure, while the two balance endpoints move slowly enough for five minutes.

Both a normalised view (``data``, what the dashboard tile draws) and the raw
payloads (``raw``, what /api/usage serves) are kept, so the aimon-compatible API
costs no extra requests.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time

import webjson

CRED_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Claude Code's own public OAuth client; the refresh grant is bound to it.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-code/2.1.72"

DEEPSEEK_URL = "https://api.deepseek.com/user/balance"
MINIMAX_URLS = {
    "cn": "https://api.minimaxi.com/v1/token_plan/remains",
    "global": "https://api.minimax.io/v1/token_plan/remains",
}

CLAUDE_EVERY_S = 60.0
BALANCE_EVERY_S = 300.0
UNCONFIGURED_S = 15.0
MAX_BACKOFF_S = 600.0
# Refresh this far ahead of expiry rather than waiting for the 401.
REFRESH_MARGIN_S = 300.0


def _epoch(iso: str | None) -> float | None:
    """ISO-8601 with an offset -> epoch seconds. None for anything unparseable."""
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _pct(value) -> float | None:
    """Anthropic reports utilization as 0-100 already; only guard the range.

    aimon multiplied anything <= 1 by 100 to cover both conventions, which turns
    a genuine 1% into 100% — exactly the reading you least want to be wrong.
    """
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _window(block) -> dict | None:
    """One `{utilization, resets_at, ...}` bucket, or None when the plan lacks it."""
    if not isinstance(block, dict):
        return None
    pct = _pct(block.get("utilization"))
    if pct is None:
        return None
    return {"pct": pct, "resets_at": _epoch(block.get("resets_at"))}


class AiPoller(threading.Thread):
    """Keeps the latest quota for every configured provider.

    ``cfg`` is a callable returning the current settings dict, so a key typed
    into the settings page takes effect on the next cycle without a restart.
    """

    def __init__(self, cfg=None):
        super().__init__(daemon=True)
        self._cfg = cfg or (lambda: {})
        self.data: dict = {"claude": None, "deepseek": None, "minimax": None,
                           "at": None}
        self.raw: dict = {}
        self._stop = threading.Event()
        self._due = {"claude": 0.0, "deepseek": 0.0, "minimax": 0.0}
        self._backoff = {"claude": 0.0, "deepseek": 0.0, "minimax": 0.0}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            cfg = self._cfg() or {}
            now = time.monotonic()
            if now >= self._due["claude"]:
                self._cycle("claude", CLAUDE_EVERY_S, self._claude, cfg)
            if now >= self._due["deepseek"]:
                self._cycle("deepseek", BALANCE_EVERY_S, self._deepseek, cfg)
            if now >= self._due["minimax"]:
                self._cycle("minimax", BALANCE_EVERY_S, self._minimax, cfg)
            self.data = dict(self.data, at=time.time())
            self._stop.wait(5.0)

    def _cycle(self, name: str, every: float, fetch, cfg: dict) -> None:
        """Run one provider's fetch, and schedule the next one from the outcome.

        A failed fetch never clears what is on screen: the previous reading stays
        until a newer one replaces it, so a flaky minute does not blank the tile.
        """
        try:
            result, ok = fetch(cfg)
        except Exception as exc:  # a poller thread that dies stops the whole tile
            result, ok = {"ok": False, "err": str(exc)}, False

        if result is None:  # no key configured — nothing to show, nothing to call
            self.data = dict(self.data, **{name: None})
            self.raw.pop(name, None)
            self._backoff[name] = 0.0
            # Come back soon rather than in five minutes: this costs no request,
            # and it is what makes a freshly pasted key light up right away.
            self._due[name] = time.monotonic() + UNCONFIGURED_S
            return

        if ok or self.data.get(name) is None:
            self.data = dict(self.data, **{name: result})
        else:
            # Keep the last good numbers, but let the tile show it is stale.
            self.data = dict(self.data,
                             **{name: dict(self.data[name], ok=False,
                                           err=result.get("err"))})

        if ok:
            self._backoff[name] = 0.0
            self._due[name] = time.monotonic() + every
        else:
            self._backoff[name] = min(MAX_BACKOFF_S,
                                      max(every, self._backoff[name] * 2))
            self._due[name] = time.monotonic() + self._backoff[name]

    # --- Claude ------------------------------------------------------------

    def _claude(self, cfg: dict):
        creds = self._read_creds()
        if creds is None:
            return {"ok": False, "err": "no-creds"}, True  # nothing to retry for
        token = creds.get("accessToken")
        if not token:
            return {"ok": False, "err": "no-token"}, True

        expires_at = creds.get("expiresAt")
        if isinstance(expires_at, (int, float)):
            # expiresAt is in milliseconds.
            if expires_at / 1000.0 - time.time() < REFRESH_MARGIN_S:
                token = self._refresh(creds) or token

        status, payload = self._usage(token)
        if status == 401:
            # Either the proactive refresh was skipped or the token was revoked
            # under us; one retry with a fresh token before giving up.
            fresh = self._refresh(creds)
            if fresh:
                status, payload = self._usage(fresh)

        if status != 200 or not isinstance(payload, dict):
            err = "rate-limited" if status == 429 else f"http {status}" if status \
                else "offline"
            return {"ok": False, "err": err}, False

        self.raw["claude"] = payload
        return self._shape(payload, creds), True

    @staticmethod
    def _usage(token: str):
        # All three headers are load-bearing: without the beta header or the
        # Claude Code user agent the endpoint refuses the OAuth token.
        return webjson.get_json(USAGE_URL, {
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
        })

    @staticmethod
    def _read_creds() -> dict | None:
        try:
            with open(CRED_PATH, encoding="utf-8") as fh:
                return json.load(fh).get("claudeAiOauth") or {}
        except (OSError, ValueError):
            return None

    def _refresh(self, creds: dict) -> str | None:
        """Trade the refresh token for a new access token and write it back.

        Claude Code refreshes the same file, so the whole document is re-read
        immediately before the write and replaced atomically: the worst case is
        that one side's fresh token replaces the other's equally fresh one, and
        both remain valid.
        """
        refresh = creds.get("refreshToken")
        if not refresh:
            return None
        status, payload = webjson.get_json(
            TOKEN_URL, {"User-Agent": USER_AGENT},
            data={"grant_type": "refresh_token", "refresh_token": refresh,
                  "client_id": CLIENT_ID})
        if status != 200 or not isinstance(payload, dict):
            return None
        token = payload.get("access_token")
        if not token:
            return None

        creds["accessToken"] = token
        creds["refreshToken"] = payload.get("refresh_token") or refresh
        if payload.get("expires_in"):
            creds["expiresAt"] = int((time.time() + payload["expires_in"]) * 1000)
        self._write_creds(creds)
        return token

    @staticmethod
    def _write_creds(oauth: dict) -> None:
        try:
            with open(CRED_PATH, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return
        doc["claudeAiOauth"] = oauth
        tmp = CRED_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, CRED_PATH)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    @staticmethod
    def _shape(payload: dict, creds: dict) -> dict:
        extra = payload.get("extra_usage") or {}
        used = extra.get("used_credits")
        places = extra.get("decimal_places")
        if isinstance(used, (int, float)) and isinstance(places, int):
            used_amount = used / (10 ** places)
        else:
            used_amount = None

        return {
            "ok": True,
            "err": None,
            "plan": creds.get("subscriptionType") or "",
            "five_hour": _window(payload.get("five_hour")),
            "seven_day": _window(payload.get("seven_day")),
            "seven_day_opus": _window(payload.get("seven_day_opus")),
            "extra": {
                "enabled": bool(extra.get("is_enabled")),
                "pct": _pct(extra.get("utilization")),
                "used": used_amount,
                "currency": extra.get("currency") or "USD",
                "reason": extra.get("disabled_reason") or "",
            },
        }

    # --- balances ----------------------------------------------------------

    def _deepseek(self, cfg: dict):
        key = (cfg.get("deepseek_key") or "").strip()
        if not key:
            return None, True
        status, payload = webjson.get_json(
            DEEPSEEK_URL, {"Authorization": "Bearer " + key})
        if status != 200 or not isinstance(payload, dict):
            return {"ok": False, "err": f"http {status}" if status else "offline"}, False

        self.raw["deepseek"] = payload
        infos = payload.get("balance_infos") or [{}]
        first = infos[0] if isinstance(infos[0], dict) else {}
        try:
            balance = float(first.get("total_balance"))
        except (TypeError, ValueError):
            balance = None
        return {"ok": True, "err": None,
                "available": payload.get("is_available") is True,
                "balance": balance,
                "currency": first.get("currency") or ""}, True

    def _minimax(self, cfg: dict):
        key = (cfg.get("minimax_key") or "").strip()
        if not key:
            return None, True
        url = MINIMAX_URLS.get(cfg.get("minimax_region") or "cn", MINIMAX_URLS["cn"])
        status, payload = webjson.get_json(url, {"Authorization": "Bearer " + key})
        if status != 200 or not isinstance(payload, dict):
            return {"ok": False, "err": f"http {status}" if status else "offline"}, False

        self.raw["minimax"] = payload
        models = [m for m in (payload.get("model_remains") or [])
                  if isinstance(m, dict)]
        if not models:
            return {"ok": False, "err": "no-models"}, False

        # Quotas are per model group ("general" for text, "video", ...). The
        # tile has room for one, so it takes the text quota — the one that runs
        # out while you are working; /ai lists every group in full.
        primary = next((m for m in models if m.get("model_name") == "general"),
                       models[0])
        return {"ok": True, "err": None,
                "model": primary.get("model_name") or "",
                "five_hour": _used(primary.get("current_interval_remaining_percent")),
                "weekly": _used(primary.get("current_weekly_remaining_percent")),
                "five_hour_reset": _ms(primary.get("end_time")),
                "weekly_reset": _ms(primary.get("weekly_end_time")),
                "models": [{"name": m.get("model_name") or "?",
                            "five_hour": _used(m.get("current_interval_remaining_percent")),
                            "weekly": _used(m.get("current_weekly_remaining_percent")),
                            "five_hour_reset": _ms(m.get("end_time")),
                            "weekly_reset": _ms(m.get("weekly_end_time"))}
                           for m in models]}, True


def _used(remaining) -> float | None:
    """MiniMax reports what is *left*; every bar here is drawn from what is used."""
    pct = _pct(remaining)
    return None if pct is None else 100.0 - pct


def _ms(value) -> float | None:
    """MiniMax timestamps are epoch milliseconds."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return value / 1000.0


if __name__ == "__main__":
    poller = AiPoller(lambda: {})
    poller.start()
    for _ in range(12):
        time.sleep(1.0)
        if poller.data.get("claude"):
            break
    print(json.dumps(poller.data, ensure_ascii=False, indent=2))
    print("--- raw ---")
    print(json.dumps(poller.raw, ensure_ascii=False, indent=2)[:2000])
    poller.stop()
