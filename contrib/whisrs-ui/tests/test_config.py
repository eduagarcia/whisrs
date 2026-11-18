import pathlib
import tomllib

import pytest

from whisrs_ui import config

SAMPLE = '''# whisrs: dictation config
[general]
backend = "asr-sidecar"
language = "pt"   # dictated in Portuguese
vocabulary = ["OpenRouter", "Hyprland"]
llm_instruction = \'\'\'
Clean this transcript.
Keep the meaning.\'\'\'

[replacements]
"pen router" = "OpenRouter"

[llm]
model = "gpt-6-luna"
'''


def test_set_keeps_comments_and_other_keys():
    out = config.apply_ops(SAMPLE, [
        {"op": "set", "path": ["general", "language"], "value": "en"},
        {"op": "set", "path": ["llm", "reasoning_effort"], "value": "low"},
    ])
    assert "# whisrs: dictation config" in out
    assert "# dictated in Portuguese" in out
    data = tomllib.loads(out)
    assert data["general"]["language"] == "en"
    assert data["llm"] == {"model": "gpt-6-luna", "reasoning_effort": "low"}
    assert data["general"]["llm_instruction"].startswith("Clean this transcript.")


def test_lists_multiline_strings_new_sections_and_unset():
    vocab = ["OpenRouter", "Hyprland", "MAI-Transcribe 2", "GPT-6 Luna", "T3 Code"]
    out = config.apply_ops(SAMPLE, [
        {"op": "set", "path": ["general", "vocabulary"], "value": vocab},
        {"op": "set", "path": ["general", "llm_instruction"], "value": "Line one.\nLine two with 'quotes'."},
        {"op": "set", "path": ["overlay", "colors", "accent"], "value": "#F2B544"},
        {"op": "unset", "path": ["replacements", "pen router"]},
        {"op": "set", "path": ["replacements", "hyper land"], "value": "Hyprland"},
    ])
    data = tomllib.loads(out)
    assert data["general"]["vocabulary"] == vocab
    assert data["general"]["llm_instruction"] == "Line one.\nLine two with 'quotes'."
    assert data["overlay"]["colors"]["accent"] == "#F2B544"
    assert data["replacements"] == {"hyper land": "Hyprland"}
    # A list this long is written one item per line.
    assert '\n    "MAI-Transcribe 2",' in out


def test_unset_of_the_last_key_removes_the_section():
    out = config.apply_ops(SAMPLE, [{"op": "unset", "path": ["llm", "model"]}])
    assert "llm" not in tomllib.loads(out)
    # Unsetting what is not there changes nothing.
    assert tomllib.loads(config.apply_ops(SAMPLE, [{"op": "unset", "path": ["tts", "voice"]}])) == tomllib.loads(SAMPLE)


def test_a_bad_edit_is_refused():
    with pytest.raises(config.ConfigError):
        config.apply_ops(SAMPLE, [{"op": "replace", "path": ["general", "x"]}])
    with pytest.raises(config.ConfigError):
        config.validate_text("[general\nbackend = 1")


def test_write_goes_through_a_symlink_and_keeps_a_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text(SAMPLE)
    link = tmp_path / "config" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(real)

    new = config.apply_ops(SAMPLE, [{"op": "set", "path": ["general", "language"], "value": "en"}])
    saved = config.write_text(new, link)
    assert saved.changed and link.is_symlink()
    assert tomllib.loads(real.read_text())["general"]["language"] == "en"
    assert saved.backup.read_text() == SAMPLE
    # Writing the same text again is not a change and keeps no backup.
    assert config.write_text(new, link).changed is False
    with pytest.raises(config.ConfigError):
        config.write_text("[broken", link)
    assert real.read_text() == new


def test_the_reference_of_the_fork_parses():
    sections = config.load_reference()
    names = [s.name for s in sections]
    assert "general" in names and "llm" in names and "asr-sidecar" in names
    general = next(s for s in sections if s.name == "general")
    keys = {k.name: k for k in general.keys}
    assert keys["notify"].kind == "bool"
    assert keys["transcription_retries"].kind == "int"
    assert (keys["transcription_retries"].minimum, keys["transcription_retries"].maximum) == (0, 5)
    assert keys["backend"].kind == "choice" and "asr-sidecar" in keys["backend"].choices
    assert keys["vocabulary"].kind == "list"
    assert keys["language"].kind == "text"  # "auto, or a language code" is open
    sidecar = next(s for s in sections if s.name == "asr-sidecar")
    assert next(k for k in sidecar.keys if k.name == "api_key").secret
    assert any(s.array for s in sections)  # [[agents]], [[llm_commands]]
