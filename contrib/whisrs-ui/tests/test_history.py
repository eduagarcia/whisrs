import json
import types

from whisrs_ui import history


def whisrs_entry(stem, ts, text, raw=None, **over):
    e = {
        "id": stem, "timestamp": ts, "duration_secs": 5.0, "language": "pt", "status": "ok", "error": None,
        "audio_file": f"{stem}.flac",
        "window": {"class": "com.t3tools.T3Code", "title": "Ditado no celular"},
        "asr": {"model": "microsoft/mai-transcribe-2", "configured_model": "microsoft/mai-transcribe-2",
                "latency_ms": 900, "transcript": raw or text, "failures": []},
        "llm": {"model": "gpt-6-luna", "used": True, "latency_ms": 1500, "failures": []},
        "text": text,
        "cost": {"asr": {"usd": 0.0001}, "llm": {"total_tokens": 80}, "reported_usd": 0.0001},
    }
    e.update(over)
    return e


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    archive = tmp_path / "whisrs-archive"
    month = archive / "2026-11"
    month.mkdir(parents=True)
    wispr = tmp_path / "wispr-flow-archive"
    (wispr / "audio" / "2026-10").mkdir(parents=True)
    rows = [
        {"transcriptEntityId": "aaa", "timestamp": "2026-10-01 12:00:00.000 +00:00", "asrText": "configura o roteador",
         "formattedText": "Configura o roteador.", "app": "Slack", "url": "", "duration": "3.5", "status": "formatted",
         "audio_file": "audio/2026-10/x.wav"},
        {"transcriptEntityId": "bbb", "timestamp": "2026-10-02 12:00:00.000 +00:00", "asrText": "outra coisa",
         "pastedText": "Outra coisa.", "app": "firefox", "url": "https://github.com", "duration": "2"},
    ]
    (wispr / "transcripts.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    (wispr / "audio" / "2026-10" / "x.flac").write_bytes(b"fLaC")
    return archive, month, wispr


def write(month, entry):
    (month / f"{entry['id']}.json").write_text(json.dumps(entry, ensure_ascii=False))
    (month / f"{entry['id']}.flac").write_bytes(b"fLaC")


def args(**kw):
    base = dict(since=None, until=None, source=None, app=None, failed=False, limit=20, sort="recent", query="")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_update_search_and_publish(tmp_path, monkeypatch):
    archive, month, wispr = setup(tmp_path, monkeypatch)
    write(month, whisrs_entry("20261118T100000Z_a", "2026-11-18T10:00:00.000Z", "Revisa a configuração do OpenRouter, por favor."))
    write(month, whisrs_entry("20261118T110000Z_b", "2026-11-18T11:00:00.000Z", "", status="failed",
                              error="ASR sidecar error (429)",
                              asr={"model": "microsoft/mai-transcribe-2", "failures": [
                                  {"model": "microsoft/mai-transcribe-2", "status": 429, "latency_ms": 400,
                                   "response": {"error": {"message": "Provider returned 429"}}}]}))
    index = history.local_index()
    r = history.update(archive, wispr, index, quiet=True)
    assert (r["whisrs_updated"], r["wispr_loaded"], r["total"]) == (2, 2, 4)
    published = history.published_index(archive)
    assert published.is_file() and r["published"] == str(published)

    con = history.connect_ro(published)
    # Accents and case do not matter, words are prefixes.
    hits = history.search(con, args(query="configuracao openrout"))
    assert [h["id"] for h in hits] == ["20261118T100000Z_a"]
    assert "[configuração]" in hits[0]["snip"]
    # The Wispr Flow history is in the same search.
    hits = history.search(con, args(query="roteador"))
    assert [h["id"] for h in hits] == ["wispr:aaa"]
    assert hits[0]["app"] == "Slack" and hits[0]["audio"].endswith("x.flac")
    # Filters: source, app (class, title or url), failures, dates.
    assert {h["id"] for h in history.search(con, args(source="wispr"))} == {"wispr:aaa", "wispr:bbb"}
    assert [h["id"] for h in history.search(con, args(app="github"))] == ["wispr:bbb"]
    assert [h["id"] for h in history.search(con, args(app="celular"))][0].startswith("20261118")
    assert [h["id"] for h in history.search(con, args(failed=True))] == ["20261118T110000Z_b"]
    assert len(history.search(con, args(since="2026-11-01"))) == 2
    fail = con.execute("SELECT * FROM failures").fetchone()
    assert (fail["stage"], fail["status"], fail["message"]) == ("asr", 429, "Provider returned 429")

    # A second update with nothing new changes nothing and publishes nothing.
    r = history.update(archive, wispr, index, quiet=True)
    assert (r["whisrs_updated"], r["wispr_loaded"], r["published"]) == (0, 0, None)


def test_spool_to_archive_and_deletions(tmp_path, monkeypatch):
    archive, month, wispr = setup(tmp_path, monkeypatch)
    spool = history.spool_dir()
    spool.mkdir(parents=True)
    e = whisrs_entry("20261118T120000Z_c", "2026-11-18T12:00:00.000Z", "Texto do spool")
    (spool / f"{e['id']}.json").write_text(json.dumps(e))
    index = history.local_index()
    history.update(archive, wispr, index, publish=False, quiet=True)
    con = history.connect_ro(index)
    assert con.execute("SELECT record FROM dictations WHERE id = ?", (e["id"],)).fetchone()[0].startswith(str(spool))

    # The sync moves it to the archive: same id, one row, still searchable.
    (spool / f"{e['id']}.json").unlink()
    write(month, e)
    history.update(archive, wispr, index, publish=False, quiet=True)
    con = history.connect_ro(index)
    rows = con.execute("SELECT record FROM dictations WHERE id = ?", (e["id"],)).fetchall()
    assert len(rows) == 1 and rows[0][0].startswith(str(month))
    assert [h["id"] for h in history.search(con, args(query="spool"))] == [e["id"]]

    # Deleted from the archive: gone from the index and the search.
    (month / f"{e['id']}.json").unlink()
    history.update(archive, wispr, index, publish=False, quiet=True)
    con = history.connect_ro(index)
    assert con.execute("SELECT COUNT(*) FROM dictations WHERE source = 'whisrs'").fetchone()[0] == 0
    assert history.search(con, args(query="spool")) == []


def test_parse_when_and_fts_query():
    assert history.parse_when("2026-11-18") == "2026-11-18 00:00:00"
    assert history.parse_when("2026-11-18", end=True) == "2026-11-18 23:59:59"
    assert history.parse_when("7d") < history.parse_when("hoje", end=True)
    assert history.fts_query('openrouter "frase exata" OR teste') == '"openrouter"* "frase exata" OR "teste"*'
    assert history.fts_query("vírgula, ponto!") == '"vírgula"* "ponto"*'
