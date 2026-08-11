"""One tiny JSON-over-HTTP helper for the outbound API pollers.

Everything else in this program either reads local counters or answers requests;
these are the only calls that leave the machine. They run on background threads
that must never die, so nothing here raises: a failure is a status code of 0 and
a payload of None, and the caller decides whether to back off or keep the last
reading. HTTP error codes are returned rather than raised, because 401 and 429
are both answers we act on rather than faults.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8.0


def get_json(url: str, headers: dict | None = None, data: dict | None = None,
             timeout: float = TIMEOUT) -> tuple[int, object]:
    """Fetch JSON. Returns ``(status, payload)``; ``(0, None)`` if it never landed.

    ``data`` makes it a POST with a JSON body. ``payload`` is None when the body
    was not JSON, which is what a captive portal or an HTML error page looks like.
    """
    body = None
    hdrs = dict(headers or {})
    hdrs.setdefault("Accept", "application/json")
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        # The body of a 4xx often carries the reason, and the status is the part
        # the caller branches on, so this is a result rather than a failure.
        try:
            payload = _decode(exc.read())
        except OSError:
            payload = None
        return exc.code, payload
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def _decode(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
