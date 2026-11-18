"""What the page can ask for. Each public method is callable from JavaScript
as `pywebview.api.<name>(...)` and returns plain JSON data.

whisrs itself is driven through its CLI (`whisrs status`, `whisrs restart`,
`whisrs transcribe-recovery`, ...), so the UI never needs a protocol of its
own and every action is one the user could type.
"""

from __future__ import annotations

import functools
import json
import os
import pathlib
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request

from . import archive, config

HOME = pathlib.Path.home()
WISPR_SOUNDS = pathlib.Path("/opt/wispr-flow-appimage/usr/lib/wispr-flow/resources/assets/sounds")
CUES = {
    "start": "Ao começar a gravar",
    "stop": "Ao parar a gravação",
    "done": "Quando o texto é colado",
    "nothing": "Quando nada foi dito",
}


def whisrs_bin() -> str:
    here = pathlib.Path(os.environ.get("WHISRS_BIN", "")) if os.environ.get("WHISRS_BIN") else None
    for candidate in (here, HOME / ".local" / "bin" / "whisrs"):
        if candidate and candidate.is_file():
            return str(candidate)
    return shutil.which("whisrs") or "whisrs"


def run(args: list[str], timeout: float = 30, stdin: str | None = None) -> dict:
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin,
        )
        return {"ok": p.returncode == 0, "code": p.returncode, "out": p.stdout, "err": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "code": None, "out": e.stdout or "", "err": f"tempo esgotado ({timeout:.0f} s)"}
    except FileNotFoundError as e:
        return {"ok": False, "code": None, "out": "", "err": str(e)}


def whisrs(*args: str, timeout: float = 30) -> dict:
    return run([whisrs_bin(), *args], timeout=timeout)


def _guard(fn):
    """Errors come back as `{"error": ...}` instead of a rejected promise
    with no text."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - shown to the user as is
            traceback.print_exc()
            return {"error": str(e) or e.__class__.__name__}

    return wrapper


def _http_json(url: str, key: str | None = None, timeout: float = 15, body: dict | None = None) -> dict:
    headers = {"User-Agent": "whisrs-ui"}
    data = None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            message = json.loads(text).get("error", {})
            message = message.get("message") if isinstance(message, dict) else message
        except ValueError:
            message = text[:300]
        raise RuntimeError(f"HTTP {e.code}: {message}") from e


class Api:
    def __init__(self) -> None:
        self._window = None
        self._lock = threading.Lock()
        self._models_cache: dict[str, tuple[float, list]] = {}

    # -- status ---------------------------------------------------------------

    @_guard
    def status(self) -> dict:
        r = whisrs("status", timeout=5)
        text = (r["out"] or r["err"]).strip()
        first = text.splitlines()[0] if text else ""
        running = r["ok"]
        state = first.split()[0] if running and first else "parado"
        service = run(["systemctl", "--user", "is-active", "whisrs.service"], timeout=5)["out"].strip()
        return {"running": running, "state": state, "detail": text, "service": service}

    @_guard
    def overview(self) -> dict:
        cfg = config.load()
        listing = archive.list_entries(cfg)
        recent = listing["entries"][:40]
        last_llm_error = next(
            (r for r in recent if r["llm_failures"] and not r["llm_used"]),
            None,
        )
        last_asr_error = next((r for r in recent if r["status"] == "failed"), None)
        return {
            "totals": listing["totals"],
            "problem": listing["problem"],
            "recent": recent[:6],
            "last_llm_error": self._llm_error_detail(cfg, last_llm_error),
            "last_asr_error": last_asr_error,
            "recovery": archive.recovery_files(),
            "latency": self._latency(recent),
        }

    def _latency(self, rows: list[dict]) -> dict:
        def median(values):
            values = sorted(v for v in values if v)
            return values[len(values) // 2] if values else None

        return {
            "asr_ms": median(r["asr_latency_ms"] for r in rows),
            "llm_ms": median(r["llm_latency_ms"] for r in rows),
        }

    def _llm_error_detail(self, cfg: dict, row: dict | None):
        if not row:
            return None
        try:
            detail = archive.entry_detail(cfg, row["id"])
        except OSError:
            return row
        failures = ((detail["entry"].get("llm") or {}).get("failures")) or []
        last = failures[-1] if failures else {}
        response = last.get("response")
        if isinstance(response, dict):
            err = response.get("error")
            message = err.get("message") if isinstance(err, dict) else err
        else:
            message = response
        return {**row, "status_code": last.get("status"), "message": message}

    # -- config ---------------------------------------------------------------

    @_guard
    def get_config(self) -> dict:
        path = config.config_path()
        text = config.read_text(path)
        return {
            "path": str(path),
            "real_path": str(path.resolve()) if path.exists() else str(path),
            "text": text,
            "data": config.validate_text(text) if text else {},
            "reference": config.reference_as_data(),
        }

    @_guard
    def preview_ops(self, ops: list[dict]) -> dict:
        text = config.apply_ops(config.read_text(), ops)
        return {"text": text}

    @_guard
    def save_ops(self, ops: list[dict]) -> dict:
        with self._lock:
            new_text = config.apply_ops(config.read_text(), ops)
            return self._save_text(new_text)

    @_guard
    def save_text(self, text: str) -> dict:
        with self._lock:
            return self._save_text(text)

    def _save_text(self, text: str) -> dict:
        saved = config.write_text(text)
        if not saved.changed:
            return {"saved": False, "message": "Nada mudou no arquivo."}
        restart = self._restart_when_idle()
        if restart["ok"]:
            return {"saved": True, "restarted": True, "backup": str(saved.backup), "message": restart["message"]}
        # The daemon did not come back on the new file: put the old one back.
        if saved.backup:
            config.write_text(saved.backup.read_text(encoding="utf-8"))
            self._restart_when_idle()
        return {
            "saved": False,
            "error": f"Nada foi salvo: {restart['message']}",
        }

    def _restart_when_idle(self, wait: float = 120) -> dict:
        deadline = time.time() + wait
        while time.time() < deadline:
            st = self.status()
            if not st.get("running") or st.get("state") == "idle":
                break
            time.sleep(1)
        else:
            return {"ok": False, "message": "o whisrs ficou ocupado por 2 minutos. Salve de novo quando ele terminar o ditado."}
        since = time.time()
        r = whisrs("restart", timeout=40)
        if not r["ok"]:
            return {"ok": False, "message": (r["err"] or r["out"]).strip()}
        for _ in range(20):
            time.sleep(0.5)
            st = self.status()
            if st.get("running"):
                break
        else:
            return {"ok": False, "message": "\n".join(self._startup_lines(since, "ERROR")) or
                    "Ele não respondeu depois de reiniciar."}
        # The daemon starts on a config it does not accept too: it logs
        # "config: <error>" and goes on with a default (an unknown backend
        # becomes groq). That is a failed save.
        time.sleep(0.4)
        errors = [l for l in self._startup_lines(since, "ERROR") if "config:" in l]
        if errors:
            return {"ok": False, "message": "o whisrs recusou a configuração e voltou à anterior.\n" + "\n".join(errors)}
        warnings = [l for l in self._startup_lines(since, "WARN") if "config:" in l]
        message = "Salvo. O whisrs reiniciou com a nova configuração."
        if warnings:
            message += " Avisos dele:\n" + "\n".join(warnings)
        return {"ok": True, "message": message}

    def _startup_lines(self, since: float, level: str) -> list[str]:
        """The daemon's log lines at `level` since `since`, without the
        timestamp and colour codes, as `<target>: <message>`."""
        import re

        log = run(
            ["journalctl", "--user", "-u", "whisrs.service", "--since", f"@{int(since)}",
             "--no-pager", "-o", "cat"],
            timeout=10,
        )["out"]
        out = []
        for line in log.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            if f" {level} " not in line and not line.startswith(level):
                continue
            out.append(line.split(f"{level} ", 1)[1].strip())
        return out

    @_guard
    def backups(self) -> list:
        folder = config.backup_dir()
        if not folder.is_dir():
            return []
        return [
            {"name": p.name, "mtime": p.stat().st_mtime}
            for p in sorted(folder.glob("config-*.toml"), reverse=True)[:20]
        ]

    @_guard
    def backup_text(self, name: str) -> dict:
        if "/" in name:
            raise ValueError("nome inválido")
        return {"text": (config.backup_dir() / name).read_text(encoding="utf-8")}

    # -- models ---------------------------------------------------------------

    @_guard
    def stt_models(self) -> dict:
        """OpenRouter's speech-to-text models, with their price per hour."""
        hit = self._models_cache.get("stt")
        if hit and time.time() - hit[0] < 3600:
            return {"models": hit[1]}
        # The plain list leaves speech-to-text models out; this filter lists
        # them. Their `prompt` price is per second of audio for most, but per
        # hour for a few (MAI-Transcribe: "0.1"), and per token for the ones
        # that also price a completion: told apart by size.
        data = _http_json("https://openrouter.ai/api/v1/models?output_modalities=transcription", timeout=20)
        models = []
        for m in data.get("data", []):
            pricing = m.get("pricing") or {}
            try:
                prompt = float(pricing.get("prompt") or 0)
                completion = float(pricing.get("completion") or 0)
            except ValueError:
                prompt = completion = 0.0
            if completion or not prompt:
                per_hour = None
            elif prompt >= 0.01:
                per_hour = prompt
            else:
                per_hour = round(prompt * 3600, 4)
            models.append({"id": m["id"], "name": m.get("name") or m["id"], "per_hour": per_hour,
                           "by_token": bool(completion)})
        models.sort(key=lambda m: m["id"])
        self._models_cache["stt"] = (time.time(), models)
        return {"models": models}

    @_guard
    def llm_models(self) -> dict:
        cfg = config.load()
        llm = cfg.get("llm") or {}
        url = (llm.get("api_url") or "").rstrip("/")
        if not url:
            return {"models": [], "error": "Sem [llm] api_url na configuração."}
        base = url[: -len("/chat/completions")] if url.endswith("/chat/completions") else url
        data = _http_json(f"{base}/models", key=llm.get("api_key"), timeout=15)
        ids = sorted({m.get("id") for m in data.get("data", []) if m.get("id")})
        return {"models": ids}

    @_guard
    def openrouter_credit(self) -> dict:
        cfg = config.load()
        key = (cfg.get("asr-sidecar") or {}).get("api_key")
        if not key:
            return {"error": "Sem chave do OpenRouter em [asr-sidecar] api_key."}
        d = _http_json("https://openrouter.ai/api/v1/key", key=key).get("data", {})
        return {
            "remaining": d.get("limit_remaining"),
            "limit": d.get("limit"),
            "daily": d.get("usage_daily"),
            "monthly": d.get("usage_monthly"),
        }

    @_guard
    def test_llm(self) -> dict:
        """One short request to the cleanup LLM, with the configured model and
        parameters, to see whether it answers."""
        cfg = config.load()
        llm = cfg.get("llm") or {}
        body = {
            "model": llm.get("model"),
            "messages": [{"role": "user", "content": "Responda apenas: ok"}],
        }
        for key in ("reasoning_effort", "service_tier"):
            if llm.get(key):
                body[key] = llm[key]
        started = time.time()
        reply = _http_json(llm.get("api_url", ""), key=llm.get("api_key"), timeout=40, body=body)
        text = (((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return {"ok": True, "reply": text, "ms": round((time.time() - started) * 1000)}

    # -- history and recovery -------------------------------------------------

    @_guard
    def history(self) -> dict:
        cfg = config.load()
        listing = archive.list_entries(cfg)
        if not listing["entries"]:
            listing["log"] = archive.history(300)
        return listing

    @_guard
    def entry(self, entry_id: str) -> dict:
        return archive.entry_detail(config.load(), entry_id)

    @_guard
    def retranscribe(self, entry_id: str) -> dict:
        name = archive.copy_to_recovery(config.load(), entry_id)
        return self.transcribe_recovery(name)

    @_guard
    def recovery(self) -> list:
        return archive.recovery_files()

    @_guard
    def recovery_audio(self, name: str) -> dict:
        return archive.audio_payload(archive.recovery_file(name))

    @_guard
    def transcribe_recovery(self, name: str, language: str | None = None) -> dict:
        archive.recovery_file(name)  # refuses anything else
        args = ["transcribe-recovery", name]
        if language:
            args += ["--language", language]
        r = whisrs(*args, timeout=240)
        if not r["ok"]:
            return {"ok": False, "error": (r["err"] or r["out"]).strip()}
        return {"ok": True, "text": r["out"].strip()}

    @_guard
    def delete_recovery(self, name: str) -> dict:
        archive.recovery_file(name).unlink(missing_ok=True)
        return {"ok": True}

    # -- commands -------------------------------------------------------------

    _COMMANDS = {
        "toggle": ["toggle"],
        "cancel": ["cancel"],
        "status": ["status"],
        "restart": ["restart"],
        "log": ["log", "-n", "20"],
        "log-clear": ["log", "--clear"],
        "profile-list": ["profile", "list"],
        "profile-next": ["profile", "next"],
        "profile-base": ["profile", "base"],
        "recording-start": ["recording", "start"],
        "recording-stop": ["recording", "stop"],
        "recording-cancel": ["recording", "cancel"],
        "recording-status": ["recording", "status"],
        "recording-list": ["recording", "list"],
        "agent-list": ["agent"],
        "version": ["--version"],
    }

    @_guard
    def command(self, name: str, extra: list[str] | None = None) -> dict:
        """Run one of the whitelisted `whisrs` commands (or a recording or
        profile command with its argument)."""
        if name in self._COMMANDS:
            args = list(self._COMMANDS[name])
        elif name in ("profile", "llm-command", "recording-show", "recording-copy", "recording-retry",
                      "recording-summarize", "recording-delete"):
            if not extra:
                raise ValueError(f"{name} precisa de um argumento")
            head = name.split("-", 1)
            args = [head[0], *head[1:], *extra] if name.startswith("recording-") else [name, *extra]
        else:
            raise ValueError(f"comando desconhecido: {name}")
        timeout = 240 if name in ("recording-retry", "recording-summarize") else 40
        if name == "recording-delete":
            # It asks on a terminal only; the page asked already.
            r = run([whisrs_bin(), *args], timeout=timeout, stdin="y\n")
        else:
            r = whisrs(*args, timeout=timeout)
        return {"ok": r["ok"], "out": r["out"].strip(), "err": r["err"].strip(), "args": ["whisrs", *args]}

    @_guard
    def open_path(self, which: str) -> dict:
        cfg = config.load()
        paths = {
            "config": config.config_path().resolve().parent,
            "archive": archive.archive_dir(cfg),
            "recovery": archive.recovery_dir(),
            "sounds": config.config_dir() / "sounds",
            "backups": config.backup_dir(),
        }
        path = paths.get(which)
        if path is None:
            raise ValueError(f"pasta desconhecida: {which}")
        path.mkdir(parents=True, exist_ok=True) if which in ("recovery", "sounds", "backups") else None
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "path": str(path)}

    @_guard
    def open_terminal_config(self) -> dict:
        for term in ("kitty", "foot", "alacritty", "wezterm"):
            if shutil.which(term):
                subprocess.Popen([term, "-e", whisrs_bin(), "config"], start_new_session=True)
                return {"ok": True}
        raise RuntimeError("nenhum terminal encontrado (kitty, foot, alacritty, wezterm)")

    @_guard
    def journal(self, lines: int = 80) -> dict:
        r = run(
            ["journalctl", "--user", "-u", "whisrs.service", "-n", str(int(lines)), "--no-pager", "-o", "short-iso"],
            timeout=10,
        )
        return {"text": r["out"]}

    # -- sounds ---------------------------------------------------------------

    @_guard
    def sounds(self) -> dict:
        folder = config.config_dir() / "sounds"
        cues = []
        for cue, label in CUES.items():
            path = folder / f"{cue}.wav"
            if not path.exists():
                state = "tone"
            elif path.stat().st_size == 0:
                state = "silent"
            else:
                state = "file"
            cues.append({"cue": cue, "label": label, "state": state, "path": str(path)})
        library = []
        if WISPR_SOUNDS.is_dir():
            library = sorted(str(p.relative_to(WISPR_SOUNDS)) for p in WISPR_SOUNDS.rglob("*.wav"))
        return {"cues": cues, "library": library, "library_root": str(WISPR_SOUNDS)}

    def _cue_path(self, cue: str) -> pathlib.Path:
        if cue not in CUES:
            raise ValueError(f"som desconhecido: {cue}")
        return config.config_dir() / "sounds" / f"{cue}.wav"

    @_guard
    def play(self, source: str) -> dict:
        """Audio of a cue (`cue:start`) or of a library file (`lib:v6/x.wav`)."""
        if source.startswith("cue:"):
            return archive.audio_payload(self._cue_path(source[4:]))
        if source.startswith("lib:"):
            path = (WISPR_SOUNDS / source[4:]).resolve()
            if WISPR_SOUNDS.resolve() not in path.parents:
                raise ValueError("fora da biblioteca de sons")
            return archive.audio_payload(path)
        raise ValueError("origem desconhecida")

    @_guard
    def set_cue(self, cue: str, source: str) -> dict:
        """Make a cue play `lib:<file>`, `file:<path>`, or be `silent`. The
        file is written through a symlink, so a cue kept in a dotfiles
        repository stays linked there. whisrs reads it on each cue."""
        path = self._cue_path(cue)
        if source == "silent":
            data = b""
        elif source.startswith("lib:"):
            src = (WISPR_SOUNDS / source[4:]).resolve()
            if WISPR_SOUNDS.resolve() not in src.parents:
                raise ValueError("fora da biblioteca de sons")
            data = src.read_bytes()
        elif source.startswith("file:"):
            data = pathlib.Path(source[5:]).expanduser().read_bytes()
            if data[:4] != b"RIFF":
                raise ValueError("o arquivo não é um WAV")
        else:
            raise ValueError("origem desconhecida")
        target = path.resolve() if path.exists() else path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return {"ok": True}

    @_guard
    def pick_wav(self) -> dict:
        import webview

        if self._window is None:
            raise RuntimeError("sem janela")
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=("Áudio WAV (*.wav)",)
        )
        return {"path": picked[0] if picked else None}

    @_guard
    def copy_text(self, text: str) -> dict:
        # wl-copy stays behind to serve the clipboard: its output must not be
        # a pipe this process waits on.
        p = subprocess.Popen(
            ["wl-copy"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        p.communicate(text.encode(), timeout=5)
        return {"ok": p.returncode == 0}
