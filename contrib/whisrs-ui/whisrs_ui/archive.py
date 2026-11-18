"""The dictations whisrs kept: the archive folder (`[general] archive_dir`),
its local spool, the history file (`whisrs log`) and the recovery folder.

Nothing here writes to the archive. Reading a network share that stopped
answering can block, so callers run these off the UI thread.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import pathlib
import shutil
import subprocess
import wave
from dataclasses import dataclass

HOME = pathlib.Path.home()


def data_dir() -> pathlib.Path:
    base = os.environ.get("XDG_DATA_HOME") or str(HOME / ".local" / "share")
    return pathlib.Path(base) / "whisrs"


def cache_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(HOME / ".cache")
    return pathlib.Path(base) / "whisrs"


def spool_dir() -> pathlib.Path:
    return data_dir() / "archive-spool"


def history_path() -> pathlib.Path:
    return data_dir() / "history.jsonl"


def recovery_dir() -> pathlib.Path:
    return cache_dir() / "recovery"


def archive_dir(config: dict) -> pathlib.Path | None:
    raw = ((config.get("general") or {}).get("archive_dir") or "").strip()
    if not raw:
        return None
    if raw.startswith("~/"):
        return HOME / raw[2:]
    return pathlib.Path(raw)


# ---------------------------------------------------------------------------
# Archive entries
# ---------------------------------------------------------------------------


@dataclass
class Located:
    json: pathlib.Path
    where: str  # "archive" or "spool"


_cache: dict[pathlib.Path, tuple[float, dict]] = {}


def _read_json(path: pathlib.Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _cache[path] = (mtime, data)
    return data


def locate_all(config: dict) -> tuple[list[Located], str | None]:
    """Every archived dictation's `.json`, and why the archive folder could
    not be read, when it could not."""
    found: list[Located] = []
    problem = None
    folder = archive_dir(config)
    if folder is not None:
        if folder.is_dir():
            for month in sorted(folder.iterdir()):
                if month.is_dir():
                    found.extend(Located(p, "archive") for p in month.glob("*.json"))
        else:
            problem = f"A pasta do arquivo não está acessível: {folder}"
    spool = spool_dir()
    if spool.is_dir():
        found.extend(Located(p, "spool") for p in spool.glob("*.json"))
    return found, problem


def _num(value):
    return value if isinstance(value, (int, float)) else None


def summarize(entry: dict, where: str) -> dict:
    asr = entry.get("asr") or {}
    llm = entry.get("llm") or {}
    cost = entry.get("cost") or {}
    asr_cost = cost.get("asr") or {}
    llm_cost = cost.get("llm") or {}
    model = asr.get("model") or asr_cost.get("model")
    configured = asr.get("configured_model") or model
    text = entry.get("text") or ""
    return {
        "id": entry.get("id"),
        "timestamp": entry.get("timestamp"),
        "where": where,
        "duration": _num(entry.get("duration_secs")),
        "language": entry.get("language"),
        "status": entry.get("status") or "ok",
        "error": entry.get("error"),
        "asr_model": model,
        "configured_model": configured,
        "fallback": bool(model and configured and model != configured),
        "asr_failures": len(asr.get("failures") or []),
        "asr_latency_ms": _num(asr.get("latency_ms")),
        "llm_model": llm.get("model"),
        "llm_used": bool(llm.get("used")),
        "llm_failures": len(llm.get("failures") or []),
        "llm_latency_ms": _num(llm.get("latency_ms")),
        "usd": _num(cost.get("reported_usd")),
        "asr_usd": _num(asr_cost.get("usd")),
        "llm_tokens": _num(llm_cost.get("total_tokens")),
        "chars": len(text),
        "preview": text[:220],
    }


def list_entries(config: dict) -> dict:
    located, problem = locate_all(config)
    rows = []
    for loc in located:
        data = _read_json(loc.json)
        if data:
            rows.append(summarize(data, loc.where))
    rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return {"entries": rows, "problem": problem, "totals": totals(rows)}


def totals(rows: list[dict]) -> dict:
    today = _dt.datetime.now(_dt.timezone.utc).astimezone().date()

    def is_today(r):
        try:
            ts = _dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            return ts.astimezone().date() == today
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def agg(items):
        return {
            "count": len(items),
            "seconds": sum(r["duration"] or 0 for r in items),
            "usd": sum(r["usd"] or 0 for r in items),
            "failed": sum(1 for r in items if r["status"] == "failed"),
            "asr_failures": sum(r["asr_failures"] for r in items),
            "llm_failures": sum(r["llm_failures"] for r in items),
            "fallbacks": sum(1 for r in items if r["fallback"]),
        }

    return {"all": agg(rows), "today": agg([r for r in rows if is_today(r)])}


def find(config: dict, entry_id: str) -> Located | None:
    for loc in locate_all(config)[0]:
        if loc.json.stem == entry_id:
            return loc
    return None


def entry_detail(config: dict, entry_id: str) -> dict:
    loc = find(config, entry_id)
    if loc is None:
        raise FileNotFoundError(f"ditado {entry_id} não encontrado no arquivo")
    data = _read_json(loc.json) or {}
    detail = {"entry": data, "summary": summarize(data, loc.where), "folder": str(loc.json.parent)}
    detail.update(audio_payload(audio_of(loc.json, data)))
    return detail


def audio_of(record: pathlib.Path, entry: dict | None = None) -> pathlib.Path:
    """The audio file of an archived dictation: the one its record names,
    else a `.flac` or `.wav` beside it."""
    name = (entry or {}).get("audio_file")
    if name and (record.parent / name).is_file():
        return record.parent / name
    for ext in (".flac", ".wav"):
        if record.with_suffix(ext).is_file():
            return record.with_suffix(ext)
    return record.with_suffix(".flac")


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def pcm16(path: pathlib.Path) -> bytes | None:
    """The audio of `path` as 16 kHz mono 16-bit PCM: read directly from a
    WAV in that format, decoded with ffmpeg otherwise (FLAC)."""
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                if (w.getsampwidth(), w.getnchannels(), w.getframerate()) == (2, 1, 16000):
                    return w.readframes(w.getnframes())
        except (OSError, wave.Error, EOFError):
            return None
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def peaks(path: pathlib.Path, bins: int = 480) -> list[float]:
    """The loudness outline of a dictation's audio: `bins` peaks from 0 to 1."""
    frames = pcm16(path)
    if not frames:
        return []
    channels = 1
    import array

    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % 2])
    if channels > 1:
        samples = samples[::channels]
    n = len(samples)
    if n == 0:
        return []
    step = max(1, n // bins)
    out = []
    for i in range(0, n, step):
        chunk = samples[i : i + step]
        out.append(max(abs(min(chunk)), abs(max(chunk))) / 32768.0)
    top = max(out) or 1.0
    return [round(v / top, 3) for v in out[:bins]]


def audio_payload(path: pathlib.Path) -> dict:
    if not path.is_file():
        return {"audio": None, "peaks": []}
    size = path.stat().st_size
    if size == 0:
        return {"audio": None, "peaks": [], "silent": True}
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    mime = {".wav": "audio/wav", ".flac": "audio/flac"}.get(path.suffix.lower(), "audio/ogg")
    return {"audio": f"data:{mime};base64,{data}", "peaks": peaks(path), "bytes": size}


# ---------------------------------------------------------------------------
# History file (whisrs log) and recovery folder
# ---------------------------------------------------------------------------


def history(limit: int = 300) -> list[dict]:
    path = history_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def recovery_files() -> list[dict]:
    folder = recovery_dir()
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("recovery_*.wav"), reverse=True):
        try:
            with wave.open(str(p), "rb") as w:
                seconds = w.getnframes() / float(w.getframerate() or 16000)
        except (OSError, wave.Error, EOFError):
            seconds = None
        out.append(
            {
                "name": p.name,
                "seconds": seconds,
                "bytes": p.stat().st_size,
                "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            }
        )
    return out


def recovery_file(name: str) -> pathlib.Path:
    """`name` in the recovery folder, refusing anything that is not one."""
    if "/" in name or not (name.startswith("recovery_") and name.endswith(".wav")):
        raise ValueError(f"não é um arquivo de recuperação: {name}")
    return recovery_dir() / name


def copy_to_recovery(config: dict, entry_id: str) -> str:
    """Put an archived dictation's audio in the recovery folder, so `whisrs
    transcribe-recovery` sends it again. Returns the file name."""
    loc = find(config, entry_id)
    audio = audio_of(loc.json, _read_json(loc.json)) if loc else None
    return copy_audio_to_recovery(audio)


def copy_audio_to_recovery(audio: pathlib.Path | None) -> str:
    """Put `audio` in the recovery folder as the WAV `whisrs
    transcribe-recovery` reads (16 kHz mono 16-bit), decoding a FLAC.
    Returns the file name."""
    if audio is None or not audio.is_file():
        raise FileNotFoundError("esse ditado não tem áudio guardado")
    frames = pcm16(audio)
    if not frames:
        raise RuntimeError(f"não consegui ler o áudio {audio}")
    folder = recovery_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    name = f"recovery_{stamp}_ui.wav"
    with wave.open(str(folder / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(frames)
    return name
