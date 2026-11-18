import json
import wave

from whisrs_ui import archive


def entry(i, **over):
    e = {
        "id": f"20261118T1{i}0000Z_abc",
        "timestamp": f"2026-11-18T1{i}:00:00.000Z",
        "duration_secs": 4.0,
        "language": "pt",
        "status": "ok",
        "error": None,
        "asr": {"model": "microsoft/mai-transcribe-2", "configured_model": "microsoft/mai-transcribe-2",
                "latency_ms": 900, "transcript": "oi", "failures": []},
        "llm": {"model": "gpt-6-luna", "used": True, "latency_ms": 1500, "failures": []},
        "text": "Oi.",
        "cost": {"asr": {"usd": 0.0001}, "llm": {"total_tokens": 80}, "reported_usd": 0.0001},
    }
    e.update(over)
    return e


def write_wav(path, seconds=1.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = bytearray()
        for i in range(int(16000 * seconds)):
            v = 8000 if (i // 800) % 2 else -8000
            frames += int(v).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    nas = tmp_path / "nas" / "2026-11"
    nas.mkdir(parents=True)
    return {"general": {"archive_dir": str(tmp_path / "nas")}}, nas


def test_list_summarizes_sorts_and_totals(tmp_path, monkeypatch):
    cfg, nas = setup(tmp_path, monkeypatch)
    rows = [
        entry(1),
        entry(2, status="failed", error="ASR sidecar error (429)", text="",
              asr={"model": "microsoft/mai-transcribe-2", "failures": [{"status": 429}, {"status": 429}]},
              cost={"reported_usd": None}),
        entry(3, asr={"model": "elevenlabs/scribe-v2", "configured_model": "microsoft/mai-transcribe-2",
                      "failures": [{"status": 429}] * 3}),
        entry(4, llm={"model": "gpt-6-luna", "used": False, "failures": [{"status": 503}]}),
    ]
    for r in rows:
        (nas / f"{r['id']}.json").write_text(json.dumps(r))
    spool = tmp_path / "data" / "whisrs" / "archive-spool"
    spool.mkdir(parents=True)
    late = entry(5)
    (spool / f"{late['id']}.json").write_text(json.dumps(late))

    listing = archive.list_entries(cfg)
    ids = [r["id"] for r in listing["entries"]]
    assert ids == [late["id"], rows[3]["id"], rows[2]["id"], rows[1]["id"], rows[0]["id"]]
    by_id = {r["id"]: r for r in listing["entries"]}
    assert by_id[late["id"]]["where"] == "spool"
    assert by_id[rows[1]["id"]]["status"] == "failed" and by_id[rows[1]["id"]]["asr_failures"] == 2
    assert by_id[rows[2]["id"]]["fallback"] is True
    assert by_id[rows[3]["id"]]["llm_used"] is False and by_id[rows[3]["id"]]["llm_failures"] == 1
    t = listing["totals"]["all"]
    assert (t["count"], t["failed"], t["fallbacks"], t["llm_failures"]) == (5, 1, 1, 1)
    assert listing["problem"] is None


def test_an_unreachable_archive_is_reported(tmp_path, monkeypatch):
    cfg, nas = setup(tmp_path, monkeypatch)
    cfg["general"]["archive_dir"] = str(tmp_path / "not-mounted")
    listing = archive.list_entries(cfg)
    assert listing["entries"] == [] and "not-mounted" in listing["problem"]


def test_detail_with_audio_and_copy_to_recovery(tmp_path, monkeypatch):
    cfg, nas = setup(tmp_path, monkeypatch)
    e = entry(1)
    (nas / f"{e['id']}.json").write_text(json.dumps(e))
    write_wav(nas / f"{e['id']}.wav", 1.0)

    d = archive.entry_detail(cfg, e["id"])
    assert d["audio"].startswith("data:audio/wav;base64,")
    assert 0 < len(d["peaks"]) <= 480 and max(d["peaks"]) == 1.0

    name = archive.copy_to_recovery(cfg, e["id"])
    assert name.startswith("recovery_") and name.endswith(".wav")
    files = archive.recovery_files()
    assert [f["name"] for f in files] == [name]
    assert abs(files[0]["seconds"] - 1.0) < 0.01


def test_recovery_names_are_checked():
    import pytest

    for bad in ("../config.toml", "x.wav", "recovery_a/../../b.wav"):
        with pytest.raises(ValueError):
            archive.recovery_file(bad)
