"""Turn a line of advice into an audio file for the handheld to play.

Two backends, because neither one is always available:

* **MiniMax T2A**, when a MiniMax key is configured. The advice is Chinese and
  this speaks Chinese properly. It is the same key that wrote the sentence, so
  using it to read the sentence adds no new credential.
* **Windows SAPI**, offline, through ``System.Speech``. It needs no key and no
  network, but it can only read a language it has a voice for — and a stock
  Windows install outside China ships English voices only. Handing Chinese text
  to an English voice does not fail: it quietly skips every character it cannot
  pronounce and produces a file that says half a sentence. So the voice list is
  checked first and this backend declines rather than lying.

Both write into the program's own directory and return the path, or an
explanation of why there is no audio, which the settings page shows.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import webjson

MINIMAX_T2A = {
    "cn": "https://api.minimaxi.com/v1/t2a_v2",
    "global": "https://api.minimax.io/v1/t2a_v2",
}
MINIMAX_MODEL = "speech-02-turbo"
MINIMAX_VOICE = "female-shaonv"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Written once next to the program; PowerShell reads the text from a file rather
# than the command line so quoting and code pages cannot mangle it.
_PS1 = r"""param([string]$TextFile, [string]$OutFile, [string]$Lang)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($v in $s.GetInstalledVoices()) {
    if ($v.Enabled -and $v.VoiceInfo.Culture.Name.StartsWith($Lang)) {
        $s.SelectVoice($v.VoiceInfo.Name)
        break
    }
}
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    22050,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($OutFile, $fmt)
$s.Speak($text)
$s.Dispose()
"""

_VOICE_PS1 = (r"Add-Type -AssemblyName System.Speech; "
              r"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              r"$s.GetInstalledVoices() | Where-Object { $_.Enabled } | "
              r"ForEach-Object { $_.VoiceInfo.Culture.Name }; $s.Dispose()")

_voice_lock = threading.Lock()
_voices: tuple[list[str], float] | None = None
VOICE_TTL_S = 300.0


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def sapi_cultures() -> list[str]:
    """Culture names of the installed SAPI voices, e.g. ``["en-US"]``.

    Cached for a few minutes: it shells out to PowerShell, and a voice pack does
    not get installed twice a second — but it can get installed while this is
    running, which is why it expires at all.
    """
    global _voices
    with _voice_lock:
        if _voices and time.monotonic() - _voices[1] < VOICE_TTL_S:
            return list(_voices[0])
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _VOICE_PS1],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
        found = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        found = []
    with _voice_lock:
        _voices = (found, time.monotonic())
    return list(found)


def sapi_ready(text: str) -> tuple[bool, str]:
    """Whether SAPI can actually pronounce this text, and why not if it cannot."""
    cultures = sapi_cultures()
    if not cultures:
        return False, "系统里没有可用的语音引擎"
    if _has_cjk(text) and not any(c.lower().startswith("zh") for c in cultures):
        return False, ("系统里没有中文语音（设置 → 时间和语言 → 语音 → 添加语音，"
                       "装上「中文(简体)」即可）")
    return True, ""


def _sapi(text: str, out_dir: str) -> tuple[str | None, str]:
    script = os.path.join(out_dir, ".tts.ps1")
    txt = os.path.join(out_dir, ".tts.txt")
    out = os.path.join(out_dir, "advice.wav")
    ok, why = sapi_ready(text)
    if not ok:
        return None, why
    try:
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_PS1)
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write(text)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", script, "-TextFile", txt, "-OutFile", out,
             "-Lang", "zh" if _has_cjk(text) else "en"],
            capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"语音合成失败：{exc}"
    if proc.returncode != 0 or not os.path.exists(out):
        return None, "语音合成失败：" + ((proc.stderr or "").strip()[:120] or "未知错误")
    return out, ""


def _minimax(text: str, cfg: dict, out_dir: str) -> tuple[str | None, str]:
    key = (cfg.get("minimax_key") or "").strip()
    if not key:
        return None, "没有配置 MiniMax key"
    url = MINIMAX_T2A.get(cfg.get("minimax_region") or "cn", MINIMAX_T2A["cn"])
    status, payload = webjson.get_json(
        url, {"Authorization": "Bearer " + key},
        {"model": MINIMAX_MODEL, "text": text, "stream": False,
         "voice_setting": {"voice_id": MINIMAX_VOICE, "speed": 1.0,
                           "vol": 1.0, "pitch": 0},
         "audio_setting": {"sample_rate": 32000, "bitrate": 128000,
                           "format": "mp3", "channel": 1}},
        timeout=60.0)
    if status != 200 or not isinstance(payload, dict):
        return None, f"MiniMax 语音接口 HTTP {status}"
    base = payload.get("base_resp") or {}
    if base.get("status_code"):
        return None, f"MiniMax 语音：{base.get('status_msg')}"
    hexed = ((payload.get("data") or {}).get("audio")) or ""
    try:
        raw = bytes.fromhex(hexed)
    except ValueError:
        return None, "MiniMax 语音返回的不是音频"
    if not raw:
        return None, "MiniMax 语音返回了空音频"
    out = os.path.join(out_dir, "advice.mp3")
    try:
        with open(out, "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        return None, f"写入音频失败：{exc}"
    return out, ""


def synthesize(text: str, cfg: dict, out_dir: str) -> tuple[str | None, str]:
    """Speak ``text`` to a file. Returns ``(path, error)``; one of them is empty.

    MiniMax first when it is configured, since it is the one that can read the
    language the advice is written in; SAPI is the offline fallback and its own
    error is returned when neither works, because "install a Chinese voice" is
    the actionable half of the answer.
    """
    if (cfg.get("minimax_key") or "").strip():
        path, err = _minimax(text, cfg, out_dir)
        if path:
            return path, ""
        fallback, sapi_err = _sapi(text, out_dir)
        return (fallback, "") if fallback else (None, f"{err}；{sapi_err}")
    return _sapi(text, out_dir)


if __name__ == "__main__":
    import json
    import sys

    import paths

    base = paths.base_dir()
    try:
        with open(os.path.join(base, "config.json"), encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        config = {}
    print("installed voices:", sapi_cultures())
    sample = sys.argv[1] if len(sys.argv) > 1 else "显卡温度偏高，建议清一下灰。"
    print(synthesize(sample, config, base))
