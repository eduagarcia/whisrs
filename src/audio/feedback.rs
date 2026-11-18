//! Audio feedback — play subtle tones on recording start, stop, and completion.
//!
//! Generates simple tones programmatically using `cpal` for playback on the
//! ALSA host's default output device. Each play function spawns a thread so it never
//! blocks the caller.
//!
//! A cue has a sound of the user's own when `sounds/<cue>.wav` exists beside
//! config.toml (`start.wav`, `stop.wav`, `done.wav`): the file is played in
//! place of the tone ([`custom_cue`]). An empty file (0 bytes) silences that
//! cue. A file that cannot be read is warned about and the tone plays.
//! `nothing.wav` is a cue with no tone of its own: with the file, a
//! dictation that ends with nothing to type (no speech heard, an empty
//! transcript) plays it in place of the done cue ([`play_nothing`]).

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, StreamConfig};
use std::path::{Path, PathBuf};

use tracing::warn;

/// Sample rate for generated tones.
const TONE_SAMPLE_RATE: u32 = 44_100;

/// Silence played before every cue. An output that was idle (nothing else
/// playing) drops the first stretch of a new stream while it starts: about
/// 60 to 110 ms measured on PipeWire, which is most of a short cue. The
/// silence is what gets dropped instead.
const OUTPUT_WAKE_LEAD_IN: std::time::Duration = std::time::Duration::from_millis(150);

/// How long a stream stays open after its samples were all handed over, for
/// the last of them to be heard.
const OUTPUT_DRAIN: std::time::Duration = std::time::Duration::from_millis(250);

/// The longest a custom cue plays; the rest of a longer file is cut.
const CUSTOM_CUE_MAX_SECS: f32 = 1.5;

/// Where the user's own cue for `name` lives: `sounds/<name>.wav` beside
/// config.toml.
fn custom_cue_path(name: &str) -> PathBuf {
    crate::config_path()
        .with_file_name("sounds")
        .join(format!("{name}.wav"))
}

/// The user's own sound for the cue `name` (`start`, `stop` or `done`), ready
/// to play, or `None` when there is no such file or it cannot be read. No
/// samples at all means the cue is silenced.
fn custom_cue(name: &str, volume: f32) -> Option<Vec<f32>> {
    custom_cue_at(&custom_cue_path(name), volume)
}

/// [`custom_cue`] for the file at `path`.
fn custom_cue_at(path: &Path, volume: f32) -> Option<Vec<f32>> {
    let size = std::fs::metadata(path).ok().filter(|m| m.is_file())?.len();
    if size == 0 {
        // An empty file: this cue stays silent.
        return Some(Vec::new());
    }
    match read_cue(path, volume) {
        Ok(samples) => Some(samples),
        Err(e) => {
            warn!(
                "cannot play {}: {e}; playing the built-in tone",
                path.display()
            );
            None
        }
    }
}

/// Read a WAV file as mono samples at [`TONE_SAMPLE_RATE`]. Any channel count
/// and sample rate, integer or float samples. `volume` 0.5, the default of
/// `audio_feedback_volume`, plays the file as recorded; 1.0 doubles it.
fn read_cue(path: &Path, volume: f32) -> anyhow::Result<Vec<f32>> {
    let mut reader = hound::WavReader::open(path)?;
    let spec = reader.spec();
    let channels = usize::from(spec.channels.max(1));
    let interleaved: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => reader.samples::<f32>().collect::<Result<_, _>>()?,
        hound::SampleFormat::Int => {
            let scale = (1i64 << (spec.bits_per_sample.clamp(1, 32) - 1)) as f32;
            reader
                .samples::<i32>()
                .map(|sample| sample.map(|v| v as f32 / scale))
                .collect::<Result<_, _>>()?
        }
    };
    let mono: Vec<f32> = interleaved
        .chunks(channels)
        .map(|frame| frame.iter().sum::<f32>() / frame.len() as f32)
        .collect();
    let gain = volume * 2.0;
    let mut samples = resample_linear(&mono, spec.sample_rate, TONE_SAMPLE_RATE);
    samples.truncate((TONE_SAMPLE_RATE as f32 * CUSTOM_CUE_MAX_SECS) as usize);
    for sample in &mut samples {
        *sample = (*sample * gain).clamp(-1.0, 1.0);
    }
    Ok(samples)
}

/// `samples` at rate `from`, as samples at rate `to`, by linear interpolation:
/// plenty for a short cue.
fn resample_linear(samples: &[f32], from: u32, to: u32) -> Vec<f32> {
    if from == to || from == 0 || samples.is_empty() {
        return samples.to_vec();
    }
    let len = (samples.len() as u64 * u64::from(to) / u64::from(from)) as usize;
    let step = f64::from(from) / f64::from(to);
    (0..len)
        .map(|i| {
            let pos = i as f64 * step;
            let at = pos as usize;
            let next = (at + 1).min(samples.len() - 1);
            let frac = (pos - at as f64) as f32;
            samples[at.min(samples.len() - 1)] * (1.0 - frac) + samples[next] * frac
        })
        .collect()
}

/// Play the "start recording" sound: a short rising tone (800Hz -> 1200Hz, ~150ms).
pub fn play_start(volume: f32) {
    std::thread::Builder::new()
        .name("whisrs-feedback".into())
        .spawn(move || {
            let samples = custom_cue("start", volume)
                .unwrap_or_else(|| generate_sweep(800.0, 1200.0, 0.15, volume));
            play_samples(&samples);
        })
        .ok();
}

/// Play the "stop recording" sound: a short falling tone (1200Hz -> 800Hz, ~150ms).
pub fn play_stop(volume: f32) {
    std::thread::Builder::new()
        .name("whisrs-feedback".into())
        .spawn(move || {
            let samples = custom_cue("stop", volume)
                .unwrap_or_else(|| generate_sweep(1200.0, 800.0, 0.15, volume));
            play_samples(&samples);
        })
        .ok();
}

/// Play the "done" sound: a soft double beep (~200ms total).
pub fn play_done(volume: f32) {
    std::thread::Builder::new()
        .name("whisrs-feedback".into())
        .spawn(move || play_done_blocking(volume))
        .ok();
}

/// Play the sound of a dictation that ended with nothing to type:
/// `sounds/nothing.wav` when it exists, else the done cue, as before that
/// file was a cue.
pub fn play_nothing(volume: f32) {
    std::thread::Builder::new()
        .name("whisrs-feedback".into())
        .spawn(move || match custom_cue("nothing", volume) {
            Some(samples) => play_samples(&samples),
            None => play_done_blocking(volume),
        })
        .ok();
}

/// The done cue, on the calling thread.
fn play_done_blocking(volume: f32) {
    if let Some(samples) = custom_cue("done", volume) {
        play_samples(&samples);
        return;
    }
    let beep1 = generate_tone(1000.0, 0.07, volume);
    let silence = vec![0.0f32; (TONE_SAMPLE_RATE as f32 * 0.06) as usize];
    let beep2 = generate_tone(1200.0, 0.07, volume);

    let mut samples = Vec::with_capacity(beep1.len() + silence.len() + beep2.len());
    samples.extend_from_slice(&beep1);
    samples.extend_from_slice(&silence);
    samples.extend_from_slice(&beep2);
    play_samples(&samples);
}

/// Generate a frequency sweep from `start_hz` to `end_hz` over `duration_secs`.
fn generate_sweep(start_hz: f32, end_hz: f32, duration_secs: f32, volume: f32) -> Vec<f32> {
    let num_samples = (TONE_SAMPLE_RATE as f32 * duration_secs) as usize;
    let mut samples = Vec::with_capacity(num_samples);
    let fade_samples = (TONE_SAMPLE_RATE as f32 * 0.02) as usize; // 20ms fade

    for i in 0..num_samples {
        let t = i as f32 / TONE_SAMPLE_RATE as f32;
        let progress = i as f32 / num_samples as f32;
        let freq = start_hz + (end_hz - start_hz) * progress;
        let mut sample = (2.0 * std::f32::consts::PI * freq * t).sin() * volume;

        // Apply fade-in/fade-out envelope to avoid clicks.
        if i < fade_samples {
            sample *= i as f32 / fade_samples as f32;
        } else if i > num_samples - fade_samples {
            sample *= (num_samples - i) as f32 / fade_samples as f32;
        }

        samples.push(sample);
    }

    samples
}

/// Generate a pure tone at `freq_hz` for `duration_secs`.
fn generate_tone(freq_hz: f32, duration_secs: f32, volume: f32) -> Vec<f32> {
    let num_samples = (TONE_SAMPLE_RATE as f32 * duration_secs) as usize;
    let mut samples = Vec::with_capacity(num_samples);
    let fade_samples = (TONE_SAMPLE_RATE as f32 * 0.01) as usize; // 10ms fade

    for i in 0..num_samples {
        let t = i as f32 / TONE_SAMPLE_RATE as f32;
        let mut sample = (2.0 * std::f32::consts::PI * freq_hz * t).sin() * volume;

        // Apply fade envelope.
        if i < fade_samples {
            sample *= i as f32 / fade_samples as f32;
        } else if i > num_samples - fade_samples {
            sample *= (num_samples - i) as f32 / fade_samples as f32;
        }

        samples.push(sample);
    }

    samples
}

/// `samples` after `lead_in` samples of silence.
fn with_lead_in(samples: &[f32], lead_in: usize) -> Vec<f32> {
    let mut out = vec![0.0; lead_in];
    out.extend_from_slice(samples);
    out
}

/// Play f32 samples on the default output device (blocking until complete).
/// Nothing to play (a silenced cue) opens no stream.
fn play_samples(samples: &[f32]) {
    if samples.is_empty() {
        return;
    }
    let host = match super::device::alsa_host() {
        Ok(host) => host,
        Err(e) => {
            warn!("{e}");
            return;
        }
    };
    let device = match host.default_output_device() {
        Some(d) => d,
        None => {
            warn!("no default audio output device for feedback");
            return;
        }
    };

    let config = StreamConfig {
        channels: 1,
        sample_rate: TONE_SAMPLE_RATE,
        buffer_size: cpal::BufferSize::Default,
    };

    // Check if the device supports f32 output at our sample rate.
    let supports_f32 = device
        .supported_output_configs()
        .ok()
        .map(|configs| {
            configs.into_iter().any(|c| {
                c.channels() >= 1
                    && c.min_sample_rate() <= TONE_SAMPLE_RATE
                    && c.max_sample_rate() >= TONE_SAMPLE_RATE
                    && c.sample_format() == SampleFormat::F32
            })
        })
        .unwrap_or(false);

    if !supports_f32 {
        // Try anyway — cpal may do conversion.
    }

    let lead_in = (OUTPUT_WAKE_LEAD_IN.as_secs_f32() * TONE_SAMPLE_RATE as f32) as usize;
    let samples = with_lead_in(samples, lead_in);
    let sample_idx = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let sample_idx_clone = std::sync::Arc::clone(&sample_idx);
    let samples_len = samples.len();
    let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let done_clone = std::sync::Arc::clone(&done);

    let stream = match device.build_output_stream(
        config,
        move |data: &mut [f32], _info: &cpal::OutputCallbackInfo| {
            for sample in data.iter_mut() {
                let idx = sample_idx_clone.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                if idx < samples_len {
                    *sample = samples[idx];
                } else {
                    *sample = 0.0;
                    done_clone.store(true, std::sync::atomic::Ordering::Release);
                }
            }
        },
        |err| {
            warn!("audio feedback stream error: {err}");
        },
        None,
    ) {
        Ok(s) => s,
        Err(e) => {
            warn!("failed to build audio feedback stream: {e}");
            return;
        }
    };

    if let Err(e) = stream.play() {
        warn!("failed to play audio feedback: {e}");
        return;
    }

    // Wait until the callback has taken every sample (with a timeout).
    let length = std::time::Duration::from_secs_f32(samples_len as f32 / TONE_SAMPLE_RATE as f32);
    let timeout = length + std::time::Duration::from_secs(2);
    let start = std::time::Instant::now();
    while !done.load(std::sync::atomic::Ordering::Acquire) {
        if start.elapsed() > timeout {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }

    // Taken is not heard yet: let the device's buffer drain.
    std::thread::sleep(OUTPUT_DRAIN);
    drop(stream);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A stereo 48 kHz 16-bit file comes back mono at the playback rate, at
    /// its own level with the default volume, and cut at the cue limit.
    #[test]
    fn a_custom_cue_is_read_as_mono_at_the_playback_rate() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("start.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 48_000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(&path, spec).unwrap();
        // 0.5 s: left at half scale, right silent, so mono is a quarter.
        for _ in 0..24_000 {
            writer.write_sample(16_384i16).unwrap();
            writer.write_sample(0i16).unwrap();
        }
        writer.finalize().unwrap();

        let samples = read_cue(&path, 0.5).unwrap();
        assert_eq!(samples.len(), 22_050);
        assert!((samples[100] - 0.25).abs() < 1e-3, "{}", samples[100]);
        // Volume 1.0 doubles it.
        assert!((read_cue(&path, 1.0).unwrap()[100] - 0.5).abs() < 1e-3);

        // A long file is cut.
        let mut writer = hound::WavWriter::create(&path, spec).unwrap();
        for _ in 0..(48_000 * 3 * 2) {
            writer.write_sample(0i16).unwrap();
        }
        writer.finalize().unwrap();
        assert_eq!(
            read_cue(&path, 0.5).unwrap().len(),
            (TONE_SAMPLE_RATE as f32 * CUSTOM_CUE_MAX_SECS) as usize
        );

        // Not a WAV file: an error, so the tone plays.
        std::fs::write(&path, b"not audio").unwrap();
        assert!(read_cue(&path, 0.5).is_err());
        assert!(custom_cue_at(&path, 0.5).is_none());

        // An empty file silences the cue; no file leaves the tone.
        std::fs::write(&path, b"").unwrap();
        assert_eq!(custom_cue_at(&path, 0.5), Some(Vec::new()));
        assert!(custom_cue_at(&dir.path().join("stop.wav"), 0.5).is_none());
    }

    #[test]
    fn a_cue_is_played_after_the_lead_in_silence() {
        let out = with_lead_in(&[0.5, -0.5], 3);
        assert_eq!(out, vec![0.0, 0.0, 0.0, 0.5, -0.5]);
    }

    #[test]
    fn resample_linear_keeps_the_same_rate_untouched() {
        let samples = vec![0.0, 0.5, 1.0];
        assert_eq!(resample_linear(&samples, 44_100, 44_100), samples);
        assert_eq!(resample_linear(&samples, 22_050, 44_100).len(), 6);
        assert!(resample_linear(&[], 48_000, 44_100).is_empty());
    }

    #[test]
    fn generate_sweep_correct_length() {
        let samples = generate_sweep(800.0, 1200.0, 0.15, 0.5);
        let expected = (TONE_SAMPLE_RATE as f32 * 0.15) as usize;
        assert_eq!(samples.len(), expected);
    }

    #[test]
    fn generate_tone_correct_length() {
        let samples = generate_tone(1000.0, 0.1, 0.5);
        let expected = (TONE_SAMPLE_RATE as f32 * 0.1) as usize;
        assert_eq!(samples.len(), expected);
    }

    #[test]
    fn volume_scaling() {
        let loud = generate_tone(1000.0, 0.1, 1.0);
        let quiet = generate_tone(1000.0, 0.1, 0.25);
        // The peak of the quiet signal should be roughly 1/4 of the loud signal.
        let loud_peak = loud.iter().cloned().fold(0.0f32, |a, b| a.max(b.abs()));
        let quiet_peak = quiet.iter().cloned().fold(0.0f32, |a, b| a.max(b.abs()));
        assert!(quiet_peak < loud_peak);
        assert!((quiet_peak / loud_peak - 0.25).abs() < 0.05);
    }

    #[test]
    fn fade_envelope_no_click() {
        let samples = generate_tone(1000.0, 0.1, 0.5);
        // First and last samples should be near zero (fade in/out).
        assert!(samples[0].abs() < 0.01);
        assert!(samples[samples.len() - 1].abs() < 0.01);
    }

    #[test]
    fn sweep_fade_envelope() {
        let samples = generate_sweep(800.0, 1200.0, 0.15, 0.5);
        assert!(samples[0].abs() < 0.01);
        assert!(samples[samples.len() - 1].abs() < 0.01);
    }
}
