//! The dictation archive (`[general] archive_dir`): every batch dictation's
//! audio, what the transcription backend and the LLM answered, the texts
//! and the cost, kept as material to compare backends with later.
//!
//! The folder is usually a network share, and a share that is down must not
//! hold a dictation up (an NFS mount that stopped answering blocks every
//! call into it). So nothing on the dictation's path touches it:
//!
//! - [`archive`] returns at once. A thread of its own writes the dictation
//!   to the **spool**, a folder on the local disk, and then runs a sync.
//! - A **sync** moves every spooled dictation into the archive folder. One
//!   runs at a time ([`SYNCING`]): while one is stuck in a dead mount, later
//!   dictations only add to the spool, and nothing piles up behind it. A
//!   sync that fails leaves the spool as it is, for the next one (the next
//!   dictation, or the next start of the daemon).
//! - The archive folder itself is never created: a share that is not
//!   mounted leaves an empty mount point, and writing there would fill the
//!   local disk under it. A folder that is missing counts as unreachable.
//!
//! A dictation is two files, `<UTC time>_<id>.wav` and `.json`, in a folder
//! per month, plus one line in `costs.jsonl` at the top. The `.json` is
//! written last in the spool, so its presence means the dictation is whole.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use tracing::{debug, info, warn};

use whisrs::capture::{Captured, Response};

/// A sync is running (or stuck in a mount that stopped answering).
static SYNCING: AtomicBool = AtomicBool::new(false);

/// The archive folder was reported unreachable; said once per outage.
static WARNED_UNREACHABLE: AtomicBool = AtomicBool::new(false);

/// Tells apart dictations finished in the same millisecond.
static SEQUENCE: AtomicU32 = AtomicU32::new(0);

/// One finished dictation, as the archive keeps it.
pub(crate) struct Dictation {
    /// The audio that was transcribed: 16 kHz, mono.
    pub(crate) samples: Vec<i16>,
    pub(crate) language: String,
    /// `[general] backend`.
    pub(crate) backend: String,
    /// The transcription model the backend was asked for.
    pub(crate) asr_model: String,
    /// The LLM that post-processed it, when `llm_post_process` is on.
    pub(crate) llm_model: Option<String>,
    /// What the backends answered.
    pub(crate) captured: Captured,
    /// The transcript after whisrs's own filters (fillers, `[replacements]`):
    /// what the LLM was given.
    pub(crate) transcript: String,
    /// The text handed to the injection.
    pub(crate) text: String,
    /// The LLM's reply is that text (not a fallback to the transcript).
    pub(crate) post_processed: bool,
}

/// The local folder dictations wait in until the archive folder takes them.
fn spool_dir() -> PathBuf {
    dirs::data_dir()
        .unwrap_or_else(|| PathBuf::from("~/.local/share"))
        .join("whisrs")
        .join("archive-spool")
}

/// `[general] archive_dir` as a path, with a leading `~/` expanded. `None`
/// when it is unset or blank.
pub(crate) fn archive_dir(configured: Option<&str>) -> Option<PathBuf> {
    let dir = configured?.trim();
    if dir.is_empty() {
        return None;
    }
    if let Some(rest) = dir.strip_prefix("~/") {
        if let Some(home) = dirs::home_dir() {
            return Some(home.join(rest));
        }
    }
    Some(PathBuf::from(dir))
}

/// Keep `dictation` in the archive folder `dir`. Returns at once: the
/// writing happens on a thread of its own, first to the local spool.
pub(crate) fn archive(dir: PathBuf, dictation: Dictation) {
    let spawned = std::thread::Builder::new()
        .name("whisrs-archive".into())
        .spawn(move || {
            let spool = spool_dir();
            if let Err(e) = write_to_spool(&spool, &dictation, Utc::now()) {
                warn!(
                    "dictation archive: cannot write to {}: {e:#}",
                    spool.display()
                );
                return;
            }
            sync(&spool, &dir);
        });
    if let Err(e) = spawned {
        warn!("dictation archive: cannot start its thread: {e}");
    }
}

/// Move what an earlier run left in the spool, on a thread of its own.
/// Called once when the daemon starts.
pub(crate) fn flush_in_background(dir: PathBuf) {
    let _ = std::thread::Builder::new()
        .name("whisrs-archive".into())
        .spawn(move || sync(&spool_dir(), &dir));
}

/// Write `dictation` to `spool` as `<stem>.wav` then `<stem>.json`.
/// Returns the stem.
fn write_to_spool(
    spool: &Path,
    dictation: &Dictation,
    now: DateTime<Utc>,
) -> anyhow::Result<String> {
    std::fs::create_dir_all(spool)?;
    let stem = format!(
        "{}_{:06x}{:02x}",
        now.format("%Y%m%dT%H%M%SZ"),
        now.timestamp_subsec_micros() & 0xff_ffff,
        SEQUENCE.fetch_add(1, Ordering::Relaxed) & 0xff
    );
    let wav = whisrs::audio::wav::encode_wav(&dictation.samples)?;
    write_whole(&spool.join(format!("{stem}.wav")), &wav)?;
    let entry = entry_json(dictation, &stem, now);
    write_whole(
        &spool.join(format!("{stem}.json")),
        serde_json::to_string_pretty(&entry)?.as_bytes(),
    )?;
    Ok(stem)
}

/// The `.json` of one dictation.
fn entry_json(dictation: &Dictation, stem: &str, now: DateTime<Utc>) -> Value {
    let asr = dictation.captured.asr.as_ref();
    let llm = dictation.captured.llm.as_ref();
    json!({
        "id": stem,
        "timestamp": now.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        "audio_file": format!("{stem}.wav"),
        "duration_secs": dictation.samples.len() as f64 / 16_000.0,
        "language": dictation.language,
        "backend": dictation.backend,
        "asr": {
            "model": dictation.asr_model,
            "latency_ms": asr.map(|r| r.elapsed.as_millis() as u64),
            // What whisrs kept of it: after fillers and [replacements].
            "transcript": dictation.transcript,
            "response": asr.map(body_json),
        },
        "llm": dictation.llm_model.as_ref().map(|model| json!({
            "model": model,
            "latency_ms": llm.map(|r| r.elapsed.as_millis() as u64),
            // False when the LLM failed or answered nothing: the transcript
            // was used as it was.
            "used": dictation.post_processed,
            "response": llm.map(body_json),
        })),
        "text": dictation.text,
        "cost": cost_json(dictation),
    })
}

/// A response body as JSON, or as the text it was when it is not JSON.
fn body_json(response: &Response) -> Value {
    serde_json::from_str(&response.body).unwrap_or_else(|_| Value::String(response.body.clone()))
}

/// What the dictation cost, from each response's `usage`: the audio seconds
/// and the charge the transcription provider reports, and the LLM's token
/// counts (and its charge, when the endpoint reports one). `null` where a
/// response has no such field.
fn cost_json(dictation: &Dictation) -> Value {
    let usage = |response: Option<&Response>| {
        response
            .map(body_json)
            .and_then(|body| body.get("usage").cloned())
            .unwrap_or(Value::Null)
    };
    let asr = usage(dictation.captured.asr.as_ref());
    let llm = usage(dictation.captured.llm.as_ref());
    let number = |usage: &Value, key: &str| usage.get(key).cloned().unwrap_or(Value::Null);
    let asr_usd = number(&asr, "cost");
    let llm_usd = number(&llm, "cost");
    let total = match (asr_usd.as_f64(), llm_usd.as_f64()) {
        (None, None) => Value::Null,
        (a, l) => json!(a.unwrap_or(0.0) + l.unwrap_or(0.0)),
    };
    json!({
        "asr": {
            "model": dictation.asr_model,
            "usd": asr_usd,
            "audio_seconds": number(&asr, "seconds"),
        },
        "llm": dictation.llm_model.as_ref().map(|model| json!({
            "model": model,
            "usd": llm_usd,
            "prompt_tokens": number(&llm, "prompt_tokens"),
            "completion_tokens": number(&llm, "completion_tokens"),
            "total_tokens": number(&llm, "total_tokens"),
        })),
        // Only what the providers reported: an LLM billed by subscription
        // reports tokens and no charge.
        "reported_usd": total,
    })
}

/// Write `bytes` to `path` whole: to a temporary name, then renamed.
fn write_whole(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let tmp = path.with_extension(format!(
        "{}.tmp",
        path.extension().and_then(|e| e.to_str()).unwrap_or("part")
    ));
    std::fs::write(&tmp, bytes)?;
    std::fs::rename(&tmp, path)
}

/// Move every whole dictation in `spool` into `dir`, oldest first. Does
/// nothing while another sync runs.
fn sync(spool: &Path, dir: &Path) {
    if SYNCING.swap(true, Ordering::SeqCst) {
        debug!("dictation archive: a sync is already running");
        return;
    }
    let result = sync_spool(spool, dir);
    SYNCING.store(false, Ordering::SeqCst);
    match result {
        Ok(0) => {}
        Ok(moved) => {
            if WARNED_UNREACHABLE.swap(false, Ordering::SeqCst) {
                info!(
                    "dictation archive: {} answers again, {moved} dictation(s) moved there",
                    dir.display()
                );
            } else {
                debug!(
                    "dictation archive: {moved} dictation(s) moved to {}",
                    dir.display()
                );
            }
        }
        Err(e) => {
            if !WARNED_UNREACHABLE.swap(true, Ordering::SeqCst) {
                warn!(
                    "dictation archive: cannot write to {}: {e:#}. Dictations are kept in {} \
                     until it answers",
                    dir.display(),
                    spool.display()
                );
            }
        }
    }
}

/// The body of [`sync`]: how many dictations were moved, or the first error.
fn sync_spool(spool: &Path, dir: &Path) -> anyhow::Result<usize> {
    let mut stems: Vec<String> = match std::fs::read_dir(spool) {
        Ok(entries) => entries
            .filter_map(|entry| entry.ok())
            .filter_map(|entry| {
                let path = entry.path();
                (path.extension().and_then(|e| e.to_str()) == Some("json"))
                    .then(|| path.file_stem()?.to_str().map(str::to_string))
                    .flatten()
            })
            .collect(),
        // No spool yet: nothing to move.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(e.into()),
    };
    if stems.is_empty() {
        return Ok(0);
    }
    stems.sort();
    if !dir.is_dir() {
        anyhow::bail!("the folder does not exist (a share that is not mounted?)");
    }
    let mut moved = 0;
    for stem in stems {
        let json_path = spool.join(format!("{stem}.json"));
        let wav_path = spool.join(format!("{stem}.wav"));
        let entry: Value = serde_json::from_slice(&std::fs::read(&json_path)?)?;
        // 20261117T220115Z_... -> 2026-11
        let month = match (stem.get(0..4), stem.get(4..6)) {
            (Some(year), Some(month)) => format!("{year}-{month}"),
            _ => "unsorted".to_string(),
        };
        let month_dir = dir.join(month);
        std::fs::create_dir_all(&month_dir)?;
        if wav_path.is_file() {
            write_whole(
                &month_dir.join(format!("{stem}.wav")),
                &std::fs::read(&wav_path)?,
            )?;
        }
        write_whole(
            &month_dir.join(format!("{stem}.json")),
            &std::fs::read(&json_path)?,
        )?;
        append_cost_line(dir, &entry)?;
        // Only now is it safe to forget the local copy. The .json goes
        // first: without it the pair no longer counts as a dictation.
        std::fs::remove_file(&json_path)?;
        let _ = std::fs::remove_file(&wav_path);
        moved += 1;
    }
    Ok(moved)
}

/// Append the dictation's cost to `costs.jsonl` in `dir`: one line per
/// dictation, so the spending can be summed without opening each `.json`.
fn append_cost_line(dir: &Path, entry: &Value) -> anyhow::Result<()> {
    let cost = &entry["cost"];
    let line = json!({
        "timestamp": entry["timestamp"],
        "id": entry["id"],
        "duration_secs": entry["duration_secs"],
        "asr_model": cost["asr"]["model"],
        "asr_audio_seconds": cost["asr"]["audio_seconds"],
        "asr_usd": cost["asr"]["usd"],
        "llm_model": cost["llm"]["model"],
        "llm_prompt_tokens": cost["llm"]["prompt_tokens"],
        "llm_completion_tokens": cost["llm"]["completion_tokens"],
        "llm_total_tokens": cost["llm"]["total_tokens"],
        "llm_usd": cost["llm"]["usd"],
        "reported_usd": cost["reported_usd"],
    });
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("costs.jsonl"))?;
    writeln!(file, "{line}")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn dictation() -> Dictation {
        Dictation {
            samples: vec![0i16; 16_000],
            language: "pt".into(),
            backend: "asr-sidecar".into(),
            asr_model: "microsoft/mai-transcribe-2".into(),
            llm_model: Some("gpt-6-luna".into()),
            captured: Captured {
                asr: Some(Response {
                    body: r#"{"text":"oi tudo bem","usage":{"seconds":1,"cost":0.0000278}}"#.into(),
                    elapsed: Duration::from_millis(900),
                }),
                llm: Some(Response {
                    body: r#"{"choices":[{"message":{"content":"Oi, tudo bem?"}}],"usage":{"prompt_tokens":80,"completion_tokens":6,"total_tokens":86}}"#.into(),
                    elapsed: Duration::from_millis(1300),
                }),
            },
            transcript: "oi tudo bem".into(),
            text: "Oi, tudo bem?".into(),
            post_processed: true,
        }
    }

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-11-17T22:01:15.862Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    /// A dictation lands in the month's folder as a WAV and a JSON holding
    /// both responses whole, with one cost line, and leaves the spool.
    #[test]
    fn a_dictation_is_spooled_then_moved_with_its_cost() {
        let tmp = tempfile::tempdir().unwrap();
        let (spool, dir) = (tmp.path().join("spool"), tmp.path().join("archive"));
        std::fs::create_dir(&dir).unwrap();

        let stem = write_to_spool(&spool, &dictation(), now()).unwrap();
        assert!(stem.starts_with("20261117T220115Z_"), "{stem}");
        assert_eq!(sync_spool(&spool, &dir).unwrap(), 1);

        let month = dir.join("2026-11");
        let wav = std::fs::read(month.join(format!("{stem}.wav"))).unwrap();
        assert_eq!(&wav[..4], b"RIFF");
        let entry: Value =
            serde_json::from_slice(&std::fs::read(month.join(format!("{stem}.json"))).unwrap())
                .unwrap();
        assert_eq!(entry["asr"]["response"]["usage"]["cost"], 0.0000278);
        assert_eq!(entry["asr"]["latency_ms"], 900);
        assert_eq!(
            entry["llm"]["response"]["choices"][0]["message"]["content"],
            "Oi, tudo bem?"
        );
        assert_eq!(entry["text"], "Oi, tudo bem?");
        assert_eq!(entry["duration_secs"], 1.0);
        assert_eq!(entry["cost"]["asr"]["usd"], 0.0000278);
        assert_eq!(entry["cost"]["llm"]["total_tokens"], 86);
        // The LLM reported no charge: only the transcription's counts.
        assert!(entry["cost"]["llm"]["usd"].is_null());
        assert_eq!(entry["cost"]["reported_usd"], 0.0000278);

        let costs = std::fs::read_to_string(dir.join("costs.jsonl")).unwrap();
        let line: Value = serde_json::from_str(costs.lines().next().unwrap()).unwrap();
        assert_eq!(costs.lines().count(), 1);
        assert_eq!(line["id"], stem.as_str());
        assert_eq!(line["asr_usd"], 0.0000278);
        assert_eq!(line["llm_prompt_tokens"], 80);

        // The spool is empty, and a second sync moves nothing.
        assert_eq!(std::fs::read_dir(&spool).unwrap().count(), 0);
        assert_eq!(sync_spool(&spool, &dir).unwrap(), 0);
    }

    /// While the archive folder is missing (the share is down) dictations
    /// stay in the spool, and they all go once it is back, oldest first.
    #[test]
    fn dictations_wait_in_the_spool_while_the_folder_is_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let (spool, dir) = (tmp.path().join("spool"), tmp.path().join("archive"));

        let first = write_to_spool(&spool, &dictation(), now()).unwrap();
        let second =
            write_to_spool(&spool, &dictation(), now() + chrono::Duration::seconds(5)).unwrap();
        assert!(sync_spool(&spool, &dir).is_err());
        // Never created: it may be an empty mount point.
        assert!(!dir.exists());
        assert_eq!(std::fs::read_dir(&spool).unwrap().count(), 4);

        std::fs::create_dir(&dir).unwrap();
        assert_eq!(sync_spool(&spool, &dir).unwrap(), 2);
        let costs = std::fs::read_to_string(dir.join("costs.jsonl")).unwrap();
        let ids: Vec<String> = costs
            .lines()
            .map(|l| {
                serde_json::from_str::<Value>(l).unwrap()["id"]
                    .as_str()
                    .unwrap()
                    .to_string()
            })
            .collect();
        assert_eq!(ids, vec![first, second]);
    }

    /// Without an LLM pass the entry says so, and a body that is not JSON is
    /// kept as text.
    #[test]
    fn an_entry_without_an_llm_and_with_a_plain_body() {
        let mut d = dictation();
        d.llm_model = None;
        d.captured.llm = None;
        d.captured.asr = Some(Response {
            body: "plain text".into(),
            elapsed: Duration::ZERO,
        });
        let entry = entry_json(&d, "x", now());
        assert!(entry["llm"].is_null());
        assert_eq!(entry["asr"]["response"], "plain text");
        assert!(entry["cost"]["asr"]["usd"].is_null());
        assert!(entry["cost"]["reported_usd"].is_null());
    }

    #[test]
    fn archive_dir_is_unset_when_blank_and_expands_home() {
        assert!(archive_dir(None).is_none());
        assert!(archive_dir(Some("  ")).is_none());
        assert_eq!(archive_dir(Some("/mnt/x")), Some(PathBuf::from("/mnt/x")));
        let home = dirs::home_dir().unwrap();
        assert_eq!(archive_dir(Some("~/a")), Some(home.join("a")));
    }
}
