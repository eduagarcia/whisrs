#!/usr/bin/env python3
"""whisrs-history: one searchable index of every dictation, whisrs's and the
older Wispr Flow history, and the commands to search it.

The archive folders stay the source of truth (one JSON and one audio file per
whisrs dictation; Wispr Flow's export as JSONL). This builds a SQLite file
from them, with full-text search that ignores accents, and can always be
rebuilt from scratch.

SQLite must not be written over NFS, so one machine writes: `update` builds
the index on the local disk, then publishes a copy into the archive folder
by replacing the file whole. Every other machine opens that copy read-only.

Standard library only, so any machine runs it as it is:
    uv run --no-project python history.py search "openrouter" --since 7d
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys
import textwrap
import time

HOME = pathlib.Path.home()
DEFAULT_ARCHIVE = pathlib.Path("/mnt/workspace/personal/whisrs-archive")
SCHEMA_VERSION = 1
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------


def _config_archive_dir() -> pathlib.Path | None:
    base = os.environ.get("XDG_CONFIG_HOME") or str(HOME / ".config")
    path = pathlib.Path(base) / "whisrs" / "config.toml"
    try:
        import tomllib

        raw = (tomllib.loads(path.read_text(encoding="utf-8")).get("general") or {}).get("archive_dir")
    except (OSError, ValueError):
        return None
    if not raw or not str(raw).strip():
        return None
    raw = str(raw).strip()
    return HOME / raw[2:] if raw.startswith("~/") else pathlib.Path(raw)


def archive_dir(arg: str | None = None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg).expanduser()
    if os.environ.get("WHISRS_ARCHIVE"):
        return pathlib.Path(os.environ["WHISRS_ARCHIVE"]).expanduser()
    return _config_archive_dir() or DEFAULT_ARCHIVE


def wispr_dir(archive: pathlib.Path, arg: str | None = None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg).expanduser()
    return archive.parent / "wispr-flow-archive"


def local_index() -> pathlib.Path:
    base = os.environ.get("XDG_DATA_HOME") or str(HOME / ".local" / "share")
    return pathlib.Path(base) / "whisrs" / "dictations.sqlite"


def published_index(archive: pathlib.Path) -> pathlib.Path:
    return archive / "index" / "dictations.sqlite"


def spool_dir() -> pathlib.Path:
    base = os.environ.get("XDG_DATA_HOME") or str(HOME / ".local" / "share")
    return pathlib.Path(base) / "whisrs" / "archive-spool"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS dictations (
    id TEXT PRIMARY KEY,          -- whisrs: the file stem; Wispr Flow: wispr:<entity id>
    source TEXT NOT NULL,         -- whisrs | wispr
    ts TEXT NOT NULL,             -- UTC, ISO 8601
    local_ts TEXT,                -- local time, YYYY-MM-DD HH:MM:SS
    duration REAL,                -- seconds of audio
    language TEXT,
    app TEXT,                     -- window class (whisrs) or app (Wispr Flow)
    window_title TEXT,
    url TEXT,
    status TEXT,                  -- ok | failed (whisrs); Wispr Flow's own status
    error TEXT,
    asr_model TEXT,               -- the model that answered
    configured_model TEXT,
    llm_model TEXT,
    llm_used INTEGER,
    asr_ms INTEGER,
    llm_ms INTEGER,
    asr_failures INTEGER DEFAULT 0,
    llm_failures INTEGER DEFAULT 0,
    usd REAL,
    llm_tokens INTEGER,
    raw TEXT,                     -- the transcript before the cleanup
    text TEXT,                    -- what was pasted
    audio TEXT,                   -- path of the audio file
    record TEXT,                  -- path of the source record
    mtime REAL                    -- the source record's mtime, for updates
);
CREATE INDEX IF NOT EXISTS dictations_ts ON dictations (ts);
CREATE INDEX IF NOT EXISTS dictations_app ON dictations (app);
CREATE TABLE IF NOT EXISTS failures (
    dictation_id TEXT NOT NULL,
    stage TEXT,                   -- asr | llm
    model TEXT,
    status INTEGER,               -- HTTP status; NULL when there was no answer
    latency_ms INTEGER,
    message TEXT
);
CREATE INDEX IF NOT EXISTS failures_dictation ON failures (dictation_id);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5 (
    id UNINDEXED, text, raw, app, window_title,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect_rw(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    return con


def connect_ro(path: pathlib.Path) -> sqlite3.Connection:
    # The published copy is replaced whole, never written in place:
    # immutable spares NFS any locking.
    uri = f"file:{path}?mode=ro" + ("&immutable=1" if not str(path).startswith(str(HOME)) else "")
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def open_for_reading(args) -> tuple[sqlite3.Connection, pathlib.Path]:
    """The freshest index this machine can read: the local one (the writer
    keeps it current), else the published copy."""
    archive = archive_dir(getattr(args, "archive", None))
    candidates = [local_index(), published_index(archive)]
    if getattr(args, "index", None):
        candidates = [pathlib.Path(args.index)]
    for path in candidates:
        if path.is_file():
            return connect_ro(path), path
    sys.exit(
        "No dictation index yet. On the machine that dictates run: whisrs-history update"
        f"\n(looked in {', '.join(map(str, candidates))})"
    )


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _to_local(ts_utc: dt.datetime) -> str:
    return ts_utc.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    s = raw.strip().replace("Z", "+00:00")
    # Wispr Flow: "2026-09-22 18:22:53.494 +00:00"
    s = re.sub(r" ([+-]\d\d:\d\d)$", r"\1", s)
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def _message(response) -> str:
    if isinstance(response, dict):
        err = response.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return str(err)
        return json.dumps(response, ensure_ascii=False)[:500]
    return str(response or "")[:500]


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def whisrs_row(entry: dict, record: pathlib.Path, mtime: float) -> tuple[dict, list[tuple]]:
    asr = entry.get("asr") or {}
    llm = entry.get("llm") or {}
    cost = entry.get("cost") or {}
    window = entry.get("window") or {}
    ts = _parse_ts(entry.get("timestamp", "")) or dt.datetime.fromtimestamp(mtime, dt.timezone.utc)
    audio_name = entry.get("audio_file") or ""
    audio = record.with_name(audio_name) if audio_name else None
    if audio is None or not audio.is_file():
        for ext in (".flac", ".wav"):
            if record.with_suffix(ext).is_file():
                audio = record.with_suffix(ext)
                break
    stem = entry.get("id") or record.stem
    row = {
        "id": stem,
        "source": "whisrs",
        "ts": ts.isoformat(),
        "local_ts": _to_local(ts),
        "duration": _num(entry.get("duration_secs")),
        "language": entry.get("language"),
        "app": window.get("class"),
        "window_title": window.get("title"),
        "url": None,
        "status": entry.get("status") or "ok",
        "error": entry.get("error"),
        "asr_model": asr.get("model"),
        "configured_model": asr.get("configured_model") or asr.get("model"),
        "llm_model": llm.get("model"),
        "llm_used": 1 if llm.get("used") else 0,
        "asr_ms": asr.get("latency_ms"),
        "llm_ms": llm.get("latency_ms"),
        "asr_failures": len(asr.get("failures") or []),
        "llm_failures": len(llm.get("failures") or []),
        "usd": _num(cost.get("reported_usd")),
        "llm_tokens": (cost.get("llm") or {}).get("total_tokens"),
        "raw": asr.get("transcript") or "",
        "text": entry.get("text") or "",
        "audio": str(audio) if audio and audio.is_file() else None,
        "record": str(record),
        "mtime": mtime,
    }
    fails = []
    for stage, part in (("asr", asr), ("llm", llm)):
        for f in part.get("failures") or []:
            fails.append((stem, stage, f.get("model"), f.get("status"), f.get("latency_ms"), _message(f.get("response"))))
    return row, fails


def wispr_rows(path: pathlib.Path) -> list[dict]:
    rows = []
    base = path.parent
    mtime = path.stat().st_mtime
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = _parse_ts(d.get("timestamp", ""))
            if ts is None:
                continue
            audio = d.get("audio_file")
            audio_path = base / audio if audio else None
            if audio_path is not None and not audio_path.is_file():
                alt = audio_path.with_suffix(".flac")
                audio_path = alt if alt.is_file() else None
            text = d.get("pastedText") or d.get("editedText") or d.get("formattedText") or d.get("asrText") or ""
            rows.append({
                "id": "wispr:" + str(d.get("transcriptEntityId")),
                "source": "wispr",
                "ts": ts.isoformat(),
                "local_ts": _to_local(ts),
                "duration": _num(d.get("duration")),
                "language": d.get("detectedLanguage"),
                "app": d.get("app") or None,
                "window_title": None,
                "url": d.get("url") or None,
                "status": d.get("status"),
                "error": None,
                "asr_model": "wispr-flow",
                "configured_model": "wispr-flow",
                "llm_model": None,
                "llm_used": 0,
                "asr_ms": int(float(d["e2eLatency"])) if _num(d.get("e2eLatency")) else None,
                "llm_ms": None,
                "asr_failures": 0,
                "llm_failures": 0,
                "usd": None,
                "llm_tokens": None,
                "raw": d.get("asrText") or "",
                "text": text,
                "audio": str(audio_path) if audio_path else None,
                "record": str(path),
                "mtime": mtime,
            })
    return rows


COLUMNS = ["id", "source", "ts", "local_ts", "duration", "language", "app", "window_title", "url",
           "status", "error", "asr_model", "configured_model", "llm_model", "llm_used", "asr_ms",
           "llm_ms", "asr_failures", "llm_failures", "usd", "llm_tokens", "raw", "text", "audio",
           "record", "mtime"]


def _put(con: sqlite3.Connection, row: dict, fails: list[tuple]) -> None:
    con.execute("DELETE FROM search WHERE id = ?", (row["id"],))
    con.execute("DELETE FROM failures WHERE dictation_id = ?", (row["id"],))
    con.execute(
        f"INSERT OR REPLACE INTO dictations ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
        [row.get(c) for c in COLUMNS],
    )
    con.execute(
        "INSERT INTO search (id, text, raw, app, window_title) VALUES (?, ?, ?, ?, ?)",
        (row["id"], row["text"], row["raw"], " ".join(filter(None, [row["app"], row["url"]])), row["window_title"]),
    )
    con.executemany("INSERT INTO failures VALUES (?, ?, ?, ?, ?, ?)", fails)


def update(archive: pathlib.Path, wispr: pathlib.Path, index: pathlib.Path, publish: bool = True,
           quiet: bool = False) -> dict:
    """Bring `index` in line with the archive folders, then publish it.
    Returns what changed."""
    started = time.time()
    con = connect_rw(index)
    known = dict(con.execute("SELECT record, mtime FROM dictations WHERE source = 'whisrs'"))
    added = removed = 0
    seen: set[str] = set()

    records = []
    if archive.is_dir():
        for month in sorted(p for p in archive.iterdir() if p.is_dir() and re.match(r"\d{4}-\d\d$", p.name)):
            records.extend(month.glob("*.json"))
    spool = spool_dir()
    if spool.is_dir():
        records.extend(spool.glob("*.json"))
    for record in records:
        key = str(record)
        seen.add(key)
        try:
            mtime = record.stat().st_mtime
        except OSError:
            continue
        if known.get(key) == mtime:
            continue
        try:
            entry = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        row, fails = whisrs_row(entry, record, mtime)
        # A dictation moved from the spool to the archive keeps its id.
        con.execute("DELETE FROM dictations WHERE id = ? AND record != ?", (row["id"], key))
        _put(con, row, fails)
        added += 1
    for key in set(known) - seen:
        ids = [r[0] for r in con.execute("SELECT id FROM dictations WHERE record = ?", (key,))]
        for i in ids:
            if not con.execute("SELECT 1 FROM dictations WHERE id = ? AND record != ?", (i, key)).fetchone():
                con.execute("DELETE FROM search WHERE id = ?", (i,))
                con.execute("DELETE FROM failures WHERE dictation_id = ?", (i,))
        con.execute("DELETE FROM dictations WHERE record = ?", (key,))
        removed += 1

    jsonl = wispr / "transcripts.jsonl"
    wispr_count = 0
    if jsonl.is_file():
        mtime = jsonl.stat().st_mtime
        stored = con.execute("SELECT value FROM meta WHERE key = 'wispr_mtime'").fetchone()
        if not stored or float(stored[0]) != mtime:
            con.execute("DELETE FROM search WHERE id IN (SELECT id FROM dictations WHERE source = 'wispr')")
            con.execute("DELETE FROM dictations WHERE source = 'wispr'")
            for row in wispr_rows(jsonl):
                _put(con, row, [])
                wispr_count += 1
            con.execute("INSERT OR REPLACE INTO meta VALUES ('wispr_mtime', ?)", (str(mtime),))
    changed = bool(added or removed or wispr_count)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('updated_at', ?)", (dt.datetime.now(dt.timezone.utc).isoformat(),))
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM dictations").fetchone()[0]

    published = None
    if publish and archive.is_dir():
        target = published_index(archive)
        if changed or not target.is_file():
            published = _publish(con, target)
    con.close()
    result = {"whisrs_updated": added, "removed": removed, "wispr_loaded": wispr_count, "total": total,
              "published": str(published) if published else None, "seconds": round(time.time() - started, 2)}
    if not quiet:
        print(json.dumps(result, ensure_ascii=False))
    return result


def _publish(con: sqlite3.Connection, target: pathlib.Path) -> pathlib.Path:
    """Copy the index next to `target` and rename it over `target`, so a
    reader on another machine sees the old file or the new one, never half."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    con.backup(out)
    out.execute("PRAGMA journal_mode=DELETE")
    out.execute("VACUUM")
    out.close()
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------


def parse_when(value: str | None, end: bool = False) -> str | None:
    """`hoje`, `ontem`, `7d`, `3h`, `2026-11-18` or `2026-11-18 14:00`, as a
    local timestamp string comparable with `local_ts`."""
    if not value:
        return None
    v = value.strip().lower()
    now = dt.datetime.now()
    if v in ("hoje", "today"):
        day = now.date()
    elif v in ("ontem", "yesterday"):
        day = now.date() - dt.timedelta(days=1)
    elif m := re.fullmatch(r"(\d+)\s*([dhm])", v):
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": dt.timedelta(days=n), "h": dt.timedelta(hours=n), "m": dt.timedelta(minutes=n)}[unit]
        return (now - delta).strftime("%Y-%m-%d %H:%M:%S")
    else:
        try:
            t = dt.datetime.fromisoformat(value.strip())
            if len(value.strip()) > 10:
                return t.strftime("%Y-%m-%d %H:%M:%S")
            day = t.date()
        except ValueError:
            sys.exit(f"Data não entendida: {value} (use hoje, ontem, 7d, 3h, 2026-11-18)")
    t = dt.datetime.combine(day, dt.time.max if end else dt.time.min)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def fts_query(text: str) -> str:
    """User words as an FTS5 query: each word a prefix term, all required;
    "quoted phrases" kept as phrases; OR between words kept."""
    parts = re.findall(r'"[^"]+"|\S+', text)
    terms = []
    for p in parts:
        if p.startswith('"'):
            terms.append(p)
        elif p.upper() in ("OR", "AND", "NOT"):
            terms.append(p.upper())
        else:
            word = re.sub(r"[^\w\-]", "", p, flags=re.UNICODE)
            if word:
                terms.append(f'"{word}"*')
    return " ".join(terms)


def _filters(args) -> tuple[list[str], list]:
    where, params = [], []
    since, until = parse_when(getattr(args, "since", None)), parse_when(getattr(args, "until", None), end=True)
    if since:
        where.append("d.local_ts >= ?"); params.append(since)
    if until:
        where.append("d.local_ts <= ?"); params.append(until)
    if getattr(args, "source", None):
        where.append("d.source = ?"); params.append(args.source)
    if getattr(args, "app", None):
        where.append("(d.app LIKE ? OR d.window_title LIKE ? OR d.url LIKE ?)")
        params += [f"%{args.app}%"] * 3
    if getattr(args, "failed", False):
        where.append("(d.status = 'failed' OR d.asr_failures > 0 OR d.llm_failures > 0)")
    return where, params


def search(con, args) -> list[sqlite3.Row]:
    where, params = _filters(args)
    if args.query:
        sql = (
            "SELECT d.*, snippet(search, 1, '[', ']', '…', 18) AS snip "
            "FROM search JOIN dictations d ON d.id = search.id "
            "WHERE search MATCH ? " + "".join(f" AND {w}" for w in where) +
            " ORDER BY " + ("bm25(search)" if args.sort == "relevance" else "d.ts DESC") + " LIMIT ?"
        )
        return con.execute(sql, [fts_query(args.query), *params, args.limit]).fetchall()
    sql = "SELECT d.*, substr(d.text, 1, 160) AS snip FROM dictations d"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.ts DESC LIMIT ?"
    return con.execute(sql, [*params, args.limit]).fetchall()


def _where_label(r) -> str:
    bits = [b for b in (r["app"], r["window_title"], r["url"]) if b]
    return " | ".join(bits)[:90]


def print_rows(rows, as_json: bool) -> None:
    if as_json:
        print(json.dumps([{k: r[k] for k in r.keys()} for r in rows], ensure_ascii=False, indent=1))
        return
    if not rows:
        print("Nada encontrado.")
        return
    for r in rows:
        head = f"{r['local_ts'][:16]}  {r['id']}  {r['source']}"
        if r["duration"]:
            head += f"  {r['duration']:.0f} s"
        place = _where_label(r)
        if place:
            head += f"  [{place}]"
        if r["status"] == "failed":
            head += "  FALHOU"
        print(head)
        snip = (r["snip"] or "").replace("\n", " ")
        print(textwrap.indent(textwrap.fill(snip, 110), "    "))


def show(con, ident: str, as_json: bool) -> None:
    r = con.execute("SELECT * FROM dictations WHERE id = ? OR id = ?", (ident, "wispr:" + ident)).fetchone()
    if r is None:
        r = con.execute("SELECT * FROM dictations WHERE id LIKE ? ORDER BY ts DESC LIMIT 1", (ident + "%",)).fetchone()
    if r is None:
        sys.exit(f"Nenhum ditado {ident}")
    fails = con.execute("SELECT * FROM failures WHERE dictation_id = ?", (r["id"],)).fetchall()
    if as_json:
        out = {k: r[k] for k in r.keys()}
        out["failures"] = [{k: f[k] for k in f.keys()} for f in fails]
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    print(f"{r['id']}  ({r['source']})")
    print(f"quando: {r['local_ts']}   duração: {r['duration'] or 0:.1f} s   idioma: {r['language'] or '?'}")
    if _where_label(r):
        print(f"onde: {_where_label(r)}")
    models = f"transcrição: {r['asr_model'] or '?'}"
    if r["configured_model"] and r["configured_model"] != r["asr_model"]:
        models += f" (reserva; configurado {r['configured_model']})"
    if r["asr_ms"]:
        models += f", {r['asr_ms']} ms"
    if r["llm_model"]:
        models += f"   limpeza: {r['llm_model']}" + (f", {r['llm_ms']} ms" if r["llm_used"] else ", não usada")
    print(models)
    if r["usd"] is not None:
        print(f"custo: US$ {r['usd']:.5f}")
    if r["status"] == "failed":
        print(f"FALHOU: {r['error']}")
    for f in fails:
        print(f"  pedido falho ({f['stage']}, {f['model']}): {f['status'] or 'sem resposta'} em {f['latency_ms']} ms: {f['message']}")
    print("\ntexto colado:")
    print(textwrap.indent(r["text"] or "(nenhum)", "  "))
    if r["raw"] and r["raw"] != r["text"]:
        print("\ntranscrição antes da limpeza:")
        print(textwrap.indent(r["raw"], "  "))
    if r["audio"]:
        print(f"\náudio: {r['audio']}")
    if r["source"] == "whisrs":
        print(f"registro: {r['record']}")


def stats(con, args) -> None:
    group = {"day": "substr(d.local_ts, 1, 10)", "month": "substr(d.local_ts, 1, 7)",
             "model": "coalesce(d.asr_model, '?')", "app": "coalesce(d.app, '?')",
             "source": "d.source", "llm": "coalesce(d.llm_model, '-')"}[args.by]
    where, params = _filters(args)
    sql = (f"SELECT {group} AS k, COUNT(*) n, ROUND(SUM(coalesce(d.duration,0))/60.0, 1) minutes, "
           "ROUND(SUM(coalesce(d.usd,0)), 4) usd, SUM(d.status = 'failed') failed, "
           "SUM(d.asr_failures) asr_retries, SUM(d.llm_failures > 0 AND d.llm_used = 0) llm_skipped, "
           "CAST(AVG(d.asr_ms) AS INT) asr_ms, CAST(AVG(CASE WHEN d.llm_used THEN d.llm_ms END) AS INT) llm_ms "
           "FROM dictations d" + (" WHERE " + " AND ".join(where) if where else "") +
           f" GROUP BY k ORDER BY {'k DESC' if args.by in ('day', 'month') else 'n DESC'} LIMIT ?")
    rows = con.execute(sql, [*params, args.limit]).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
        return
    print(f"{args.by:<28} {'ditados':>8} {'min':>7} {'US$':>8} {'falhas':>6} {'retries':>7} {'s/limp':>6} {'asr ms':>7} {'llm ms':>7}")
    for r in rows:
        print(f"{str(r['k'])[:28]:<28} {r['n']:>8} {r['minutes']:>7} {r['usd']:>8} {r['failed'] or 0:>6} "
              f"{r['asr_retries'] or 0:>7} {r['llm_skipped'] or 0:>6} {r['asr_ms'] or '':>7} {r['llm_ms'] or '':>7}")


def run_sql(con, query: str, as_json: bool) -> None:
    if not re.match(r"^\s*(select|with|pragma\s+table_info)\b", query, re.I):
        sys.exit("Só consultas de leitura (SELECT/WITH).")
    cur = con.execute(query)
    rows = cur.fetchall()
    cols = [c[0] for c in cur.description or []]
    if as_json:
        print(json.dumps([dict(zip(cols, r)) for r in rows], ensure_ascii=False, indent=1, default=str))
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if v is None else str(v).replace("\n", " ")[:200] for v in r))


def where_cmd(args) -> None:
    archive = archive_dir(args.archive)
    for label, path in (("local", local_index()), ("publicado", published_index(archive))):
        if path.is_file():
            con = connect_ro(path)
            n = con.execute("SELECT COUNT(*) FROM dictations").fetchone()[0]
            upd = (con.execute("SELECT value FROM meta WHERE key = 'updated_at'").fetchone() or ["?"])[0]
            last = con.execute("SELECT MAX(local_ts) FROM dictations").fetchone()[0]
            print(f"{label}: {path}  {n} ditados, último {last}, atualizado {upd}")
        else:
            print(f"{label}: {path}  (não existe)")
    print(f"arquivo: {archive}   wispr: {wispr_dir(archive, args.wispr)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="whisrs-history", description=__doc__.split("\n\n")[0])
    p.add_argument("--archive", help="pasta do arquivo do whisrs (padrão: [general] archive_dir)")
    p.add_argument("--wispr", help="pasta do arquivo do Wispr Flow (padrão: ao lado do arquivo)")
    p.add_argument("--index", help="arquivo do índice a ler")
    sub = p.add_subparsers(dest="cmd", required=True)

    def filters(sp, limit=20):
        sp.add_argument("--since", help="hoje, ontem, 7d, 3h, 2026-11-18")
        sp.add_argument("--until")
        sp.add_argument("--source", choices=["whisrs", "wispr"])
        sp.add_argument("--app", help="parte do app, título da janela ou URL")
        sp.add_argument("--failed", action="store_true", help="só ditados com falha ou retry")
        sp.add_argument("--limit", type=int, default=limit)
        sp.add_argument("--json", action="store_true")

    u = sub.add_parser("update", help="atualizar o índice e publicar a cópia no arquivo")
    u.add_argument("--no-publish", action="store_true")
    u.add_argument("--quiet", action="store_true")
    s = sub.add_parser("search", help="buscar no texto (sem acento, prefixos, \"frase exata\", OR)")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--sort", choices=["recent", "relevance"], default="recent")
    filters(s)
    r = sub.add_parser("recent", help="os últimos ditados")
    filters(r, 15)
    sh = sub.add_parser("show", help="um ditado inteiro")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true")
    st = sub.add_parser("stats", help="contagem, minutos, custo, falhas e tempos")
    st.add_argument("--by", choices=["day", "month", "model", "app", "source", "llm"], default="day")
    filters(st, 30)
    q = sub.add_parser("sql", help="uma consulta SELECT no índice")
    q.add_argument("query")
    q.add_argument("--json", action="store_true")
    sub.add_parser("where", help="onde estão o índice e os arquivos")

    args = p.parse_args(argv)
    if args.cmd == "update":
        archive = archive_dir(args.archive)
        update(archive, wispr_dir(archive, args.wispr), local_index(), publish=not args.no_publish, quiet=args.quiet)
        return 0
    if args.cmd == "where":
        where_cmd(args)
        return 0
    con, _ = open_for_reading(args)
    if args.cmd == "search":
        print_rows(search(con, args), args.json)
    elif args.cmd == "recent":
        args.query, args.sort = "", "recent"
        print_rows(search(con, args), args.json)
    elif args.cmd == "show":
        show(con, args.id, args.json)
    elif args.cmd == "stats":
        stats(con, args)
    elif args.cmd == "sql":
        run_sql(con, args.query, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
