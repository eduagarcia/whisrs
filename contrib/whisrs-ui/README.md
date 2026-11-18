# whisrs-ui

A desktop window for whisrs, on Linux with GTK and WebKit: what the dictation
pipeline is doing, its settings, every archived dictation, and the commands.

- **Painel**: the chain from the microphone to the cursor (transcription
  model, cleanup LLM, delivery), live with the daemon's state, the latest
  failures and today's dictations and cost.
- **Modelos**, **Vocabulário**, **Prompt do Luna**: the transcription model
  (with OpenRouter's speech-to-text list and prices), retries and fallback
  model, the cleanup LLM and its parameters, the vocabulary and the fixed
  replacements, and the instruction the LLM gets, shown as it is sent.
- **Histórico**: each dictation in `[general] archive_dir` with its audio,
  waveform, both texts, latencies, cost and every failed request. Any of
  them can be transcribed again.
- **Recuperação**: the audio of failed dictations
  (`whisrs transcribe-recovery`).
- **Comandos**, **Sons**, **Todas as configurações**, **Arquivo TOML**: the
  CLI commands, the cue sounds, every key of the
  [config reference](../../docs/config-reference.md), and the file itself.

Saving writes `config.toml` with its comments kept (through a symlink, if it
is one), keeps the previous file in `~/.local/state/whisrs-ui/backups/`, and
restarts the daemon once it is idle. A daemon that does not come back puts the
old file back. Everything else goes through the `whisrs` CLI.

## Install

Needs `uv`, `python-gobject` and `webkit2gtk-4.1` (Arch package names).

```sh
ln -s "$PWD/whisrs-ui" ~/.local/bin/whisrs-ui
install -Dm644 whisrs-ui.desktop ~/.local/share/applications/whisrs-ui.desktop
install -Dm644 whisrs-ui.svg ~/.local/share/icons/hicolor/scalable/apps/whisrs-ui.svg
```

With `[general] tray_click = "window"` a left click on the tray icon opens the
window; the tray menu has an "Open whisrs window" item either way. A second
launch brings the open window forward.

## Develop

```sh
uv venv --python /usr/bin/python3 --system-site-packages .venv && uv sync
uv run pytest
uv run python -m whisrs_ui.devserver   # the page in a browser, on 127.0.0.1:8765
WHISRS_UI_DEBUG=1 ./whisrs-ui          # the window with the web inspector
```
