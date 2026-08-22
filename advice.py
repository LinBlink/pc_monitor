"""Periodic snapshots of how the machine is running, and an AI read on them.

Two halves that only share a buffer:

:class:`History` keeps a compact record of every sample interval — a few numbers
per entry, not the whole telemetry dict — in memory and appended to a file, so
there is something to look back at after a restart and something to summarise.

:class:`Advisor` takes the last half hour of that, reduces it to a digest small
enough to be worth sending, and asks one of the configured AI providers whether
anything in it deserves attention. The prompt asks for silence when nothing
does: a monitor that produces a paragraph of advice every half hour trains you to
stop reading it, so "正常" has to be an acceptable answer and the usual one.

Which provider gets asked is whichever has the most quota left, which is the
point of already tracking quota. Claude is deliberately not among them: the
credential this program can see is a Claude Code subscription token, which is not
an API key and is not ours to spend on background jobs. DeepSeek and MiniMax are
keys the user entered here for exactly this kind of use.
"""

from __future__ import annotations

import json
import os
import threading
import time

import webjson

SAMPLE_EVERY_S = 30.0
KEEP_SAMPLES = 3000  # ~25 hours at the interval above
WINDOW_S = 1800.0  # what one round of advice looks at

DEEPSEEK_CHAT = "https://api.deepseek.com/chat/completions"
MINIMAX_CHAT = {
    "cn": "https://api.minimaxi.com/v1/text/chatcompletion_v2",
    "global": "https://api.minimax.io/v1/text/chatcompletion_v2",
}

SYSTEM_PROMPT = (
    "你是一台 Windows 台式机的运维助手。用户会给你这台机器最近一段时间的运行统计。\n"
    "判断这段时间里有没有值得用户注意的异常，例如：温度过高、风扇可能积灰、"
    "某个进程长时间占满 CPU 或显卡、内存快满、网络流量异常、容器退出等。\n"
    "如果一切正常，只回复两个字：正常。不要解释，不要加标点。\n"
    "如果确实有问题，用不超过 70 个汉字说清楚「哪里不对」和「建议怎么做」，"
    "一段话，不要分点，不要寒暄，不要复述数据。\n"
    "拿不准的时候倾向于回复正常——误报比漏报更让人不想看。"
)

OK_REPLY = "正常"


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class History:
    """A rolling record of the machine's state, in memory and on disk."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.samples: list[dict] = []
        self._last = 0.0
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()[-KEEP_SAMPLES:]
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                self.samples.append(row)

    def maybe_add(self, snap: dict) -> None:
        """Record this sample if the interval has elapsed. Cheap to call often."""
        now = time.monotonic()
        if self._last and now - self._last < SAMPLE_EVERY_S:
            return
        self._last = now
        row = _compact(snap)
        with self._lock:
            self.samples.append(row)
            trimmed = len(self.samples) > KEEP_SAMPLES + 200
            if trimmed:
                self.samples = self.samples[-KEEP_SAMPLES:]
                rows = list(self.samples)
        # Appending is the common path; the file is only rewritten when the
        # window has drifted far enough past its cap to be worth the write.
        try:
            if trimmed:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    for entry in rows:
                        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                os.replace(tmp, self.path)
            else:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def window(self, seconds: float) -> list[dict]:
        cutoff = time.time() - seconds
        with self._lock:
            return [r for r in self.samples if _num(r.get("t")) >= cutoff]


def _compact(snap: dict) -> dict:
    """One telemetry sample reduced to what a half-hour summary can use."""
    cpu = snap.get("cpu") or {}
    gpu = snap.get("gpu") or {}
    mem = snap.get("mem") or {}
    net = snap.get("net") or {}
    fps = snap.get("fps") or {}
    pwr = snap.get("power") or {}
    return {
        "t": round(time.time()),
        "cpu": round(_num(cpu.get("percent")), 1),
        "cpu_t": cpu.get("temp_c"),
        "cpu_w": cpu.get("power_w"),
        "gpu": round(_num(gpu.get("percent")), 1) if gpu.get("ok") else None,
        "gpu_t": gpu.get("temp_c") if gpu.get("ok") else None,
        "vram": round(_num(gpu.get("mem_used_gb")), 1) if gpu.get("ok") else None,
        "mem": round(_num(mem.get("percent")), 1),
        "down": round(_num(net.get("down_bps")) / 1024 ** 2, 2),
        "up": round(_num(net.get("up_bps")) / 1024 ** 2, 2),
        "fps": fps.get("value"),
        "game": fps.get("process"),
        "w": round(_num(pwr.get("watts"))),
        "top": [n for n, _v in (snap.get("top") or [])[:3]],
        "memtop": [[n, round(_num(v))] for n, v in (snap.get("mem_top") or [])[:3]],
    }


def _stat(rows, key):
    values = [_num(r.get(key)) for r in rows if r.get(key) is not None]
    if not values:
        return None, None
    return sum(values) / len(values), max(values)


def digest(rows: list[dict], snap: dict) -> str:
    """The window as a short block of text — this is what gets sent upstream.

    Averages and peaks rather than the series itself: a half hour of samples is
    thousands of numbers, and the questions worth asking ("did it sit at 100%",
    "how hot did it get") are all answered by two of them.
    """
    if not rows:
        return ""
    minutes = max(1, round((_num(rows[-1].get("t")) - _num(rows[0].get("t"))) / 60))
    cpu_avg, cpu_max = _stat(rows, "cpu")
    cput_avg, cput_max = _stat(rows, "cpu_t")
    gpu_avg, gpu_max = _stat(rows, "gpu")
    gput_avg, gput_max = _stat(rows, "gpu_t")
    mem_avg, mem_max = _stat(rows, "mem")
    _d_avg, d_max = _stat(rows, "down")
    _u_avg, u_max = _stat(rows, "up")
    _w_avg, _w_max = _stat(rows, "w")

    def line(name, avg, peak, unit=""):
        if avg is None:
            return f"{name}：无数据"
        return f"{name}：平均 {avg:.0f}{unit}，峰值 {peak:.0f}{unit}"

    gpu = snap.get("gpu") or {}
    mem = snap.get("mem") or {}
    parts = [
        f"统计窗口：最近 {minutes} 分钟，共 {len(rows)} 个采样点",
        line("CPU 占用", cpu_avg, cpu_max, "%"),
        line("CPU 温度", cput_avg, cput_max, "℃"),
    ]
    if gpu.get("ok"):
        parts.append(f"显卡：{gpu.get('name') or '未知'}，"
                     f"显存 {_num(gpu.get('mem_total_gb')):.0f} GB")
        parts.append(line("显卡占用", gpu_avg, gpu_max, "%"))
        parts.append(line("显卡温度", gput_avg, gput_max, "℃"))
    parts.append(f"内存：共 {_num(mem.get('total_gb')):.0f} GB，" +
                 line("占用", mem_avg, mem_max, "%").split("：", 1)[1])
    if d_max is not None:
        parts.append(f"网络峰值：下行 {d_max:.1f} MB/s，上行 {u_max:.1f} MB/s")

    games = {r.get("game") for r in rows if r.get("game")}
    if games:
        fps_avg, _fps_max = _stat(rows, "fps")
        parts.append(f"运行中的游戏：{'、'.join(sorted(games))}，"
                     f"平均 {_num(fps_avg):.0f} FPS")

    # The names, not the percentages: which process was consistently at the top
    # is the useful signal, and it survives being counted rather than averaged.
    counts: dict[str, int] = {}
    for row in rows:
        for name in row.get("top") or []:
            counts[name] = counts.get(name, 0) + 1
    if counts:
        hot = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        parts.append("最常占用 CPU 的进程：" +
                     "、".join(f"{n}（{c * 100 // len(rows)}% 的时间在前三）"
                               for n, c in hot))
    last_mem = (rows[-1].get("memtop") or [])
    if last_mem:
        parts.append("当前内存占用前三：" +
                     "、".join(f"{n} {v} MB" for n, v in last_mem))

    pwr = snap.get("power") or {}
    parts.append(f"整机功耗估算：当前 {_num(pwr.get('watts')):.0f} W，"
                 f"今日累计 {_num(pwr.get('d1')):.2f} 度")

    dk = snap.get("docker") or {}
    if dk.get("ok"):
        stopped = [c.get("name") for c in dk.get("containers") or []
                   if c.get("state") != "running"]
        parts.append(f"Docker：{dk.get('running')}/{dk.get('total')} 个容器在运行" +
                     (f"，未运行：{'、'.join(stopped)}" if stopped else ""))
    return "\n".join(parts)


def pick_provider(cfg: dict, ai: dict):
    """The configured provider with the most quota left, as ``(name, caller)``.

    "Most left" has to compare a percentage against a prepaid balance, which are
    not the same unit. A balance that is spendable is treated as a full tank —
    it has no window to run out of — so DeepSeek wins unless it is empty, and
    MiniMax is ranked by what its weekly window has left.
    """
    options = []
    ds = ai.get("deepseek") or {}
    if (cfg.get("deepseek_key") or "").strip() and ds.get("ok"):
        options.append((100.0 if ds.get("available") else -1.0, "DeepSeek",
                        _ask_deepseek))
    mm = ai.get("minimax") or {}
    if (cfg.get("minimax_key") or "").strip() and mm.get("ok"):
        used = mm.get("weekly")
        options.append((100.0 - _num(used, 0.0), "MiniMax", _ask_minimax))

    options = [o for o in options if o[0] > 0]
    if not options:
        return None, None
    _score, name, fn = max(options, key=lambda o: o[0])
    return name, fn


def _chat_reply(payload) -> str | None:
    """The assistant's text out of an OpenAI-shaped response."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = (choices[0] or {}).get("message") or {}
    text = message.get("content")
    return text.strip() if isinstance(text, str) else None


def _messages(text: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}]


def _ask_deepseek(cfg: dict, text: str):
    key = (cfg.get("deepseek_key") or "").strip()
    status, payload = webjson.get_json(
        DEEPSEEK_CHAT, {"Authorization": "Bearer " + key},
        {"model": "deepseek-chat", "messages": _messages(text),
         "max_tokens": 300, "temperature": 0.3, "stream": False},
        timeout=60.0)
    return status, _chat_reply(payload)


def _ask_minimax(cfg: dict, text: str):
    key = (cfg.get("minimax_key") or "").strip()
    url = MINIMAX_CHAT.get(cfg.get("minimax_region") or "cn", MINIMAX_CHAT["cn"])
    status, payload = webjson.get_json(
        url, {"Authorization": "Bearer " + key},
        {"model": "MiniMax-Text-01", "messages": _messages(text),
         "max_tokens": 300, "temperature": 0.3},
        timeout=60.0)
    return status, _chat_reply(payload)


class Advisor(threading.Thread):
    """Asks for a read on the last window, on a timer. Never raises."""

    RETRY_S = 300.0
    # "No history yet" and "the quota pollers have not answered yet" are both
    # states that clear themselves in a minute or two after a restart, so they
    # get their own short retry rather than the five minutes a failed call earns.
    WARMUP_S = 60.0

    def __init__(self, history: History, cfg=None, snapshot=None):
        super().__init__(daemon=True)
        self.history = history
        self._cfg = cfg or (lambda: {})
        self._snapshot = snapshot or (lambda: {})
        self.data: dict = {"enabled": False, "ok": False, "err": None,
                           "text": "", "level": "ok", "provider": "",
                           "at": None, "id": 0}
        self._stop = threading.Event()
        self._ran_at: float | None = None  # last completed round
        self._retry_at = 0.0  # earliest next attempt after a failure

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            cfg = self._cfg() or {}
            enabled = bool(cfg.get("advice_enabled"))
            if not enabled:
                self.data = dict(self.data, enabled=False)
                # Turning it back on should analyse now, not a period from now.
                self._ran_at, self._retry_at = None, 0.0
                self._stop.wait(10.0)
                continue

            # The interval is compared against when the last round happened
            # rather than baked into a deadline, so shortening it on the
            # settings page applies to the wait already in progress.
            every = max(300.0, _num(cfg.get("advice_every_min"), 30) * 60.0)
            now = time.monotonic()
            if now >= self._retry_at and (self._ran_at is None
                                          or now - self._ran_at >= every):
                delay = self._round(cfg)
                # Only a round that produced something counts as "ran": a
                # warm-up failure has to come back in a minute, not wait out a
                # whole period it never used.
                if delay:
                    self._retry_at = time.monotonic() + delay
                else:
                    self._ran_at, self._retry_at = time.monotonic(), 0.0
            self._stop.wait(10.0)

    def _round(self, cfg: dict) -> float:
        """One round. Returns how long to wait before retrying, 0 for "on time"."""
        snap = self._snapshot() or {}
        rows = self.history.window(WINDOW_S)
        # Two samples is a minute of data; anything less says nothing about a
        # trend and would only spend quota to be told so.
        if len(rows) < 3:
            self.data = dict(self.data, enabled=True, ok=False, err="数据还不够")
            return self.WARMUP_S

        name, ask = pick_provider(cfg, snap.get("ai") or {})
        if not ask:
            # This is also what a fresh start looks like for the first minute:
            # the quota pollers have not answered yet, so nothing looks usable.
            self.data = dict(self.data, enabled=True, ok=False,
                             err="没有可用的 AI（需要 DeepSeek 或 MiniMax key）")
            return self.WARMUP_S

        status, reply = ask(cfg, digest(rows, snap))
        if not reply:
            self.data = dict(self.data, enabled=True, ok=False,
                             err=f"{name} 没有返回结果（HTTP {status}）")
            return self.RETRY_S

        normal = reply.strip().strip("。.！!") == OK_REPLY
        entry = {
            "enabled": True, "ok": True, "err": None,
            "provider": name,
            "level": "ok" if normal else "warn",
            "text": "一切正常" if normal else reply,
            "at": time.time(),
            "id": int(self.data.get("id") or 0) + 1,
        }
        self.data = entry
        return 0.0


if __name__ == "__main__":
    import metrics
    import paths

    settings = {"advice_enabled": True, "advice_every_min": 30}
    try:
        with open(os.path.join(paths.state_dir(), "config.json"),
                  encoding="utf-8") as fh:
            settings.update(json.load(fh))
    except (OSError, ValueError):
        pass

    log = History(os.path.join(paths.state_dir(), "history.jsonl"))
    collector = metrics.Collector()
    time.sleep(3.0)
    for _ in range(3):
        log.maybe_add(collector.sample())
        log._last = 0.0  # force a sample per loop for the self-test
        time.sleep(1.0)
    shot = collector.sample()
    print(digest(log.window(WINDOW_S), shot))
    print("-" * 60)
    provider, ask = pick_provider(settings, shot.get("ai") or {})
    print("provider:", provider)
    if ask:
        print(ask(settings, digest(log.window(WINDOW_S), shot)))
    collector.close()
