//! Clipboard implementations — Wayland (wl-copy/wl-paste), X11 (arboard), and noop.

use crate::{ClipboardBackend, SavedClipboard};
use anyhow::Context;
use std::process::Command;

// ---------------------------------------------------------------------------
// Wayland: shell out to wl-paste / wl-copy
// ---------------------------------------------------------------------------

/// MIME types treated as "this clipboard offer is text" — the standard
/// `text/plain` variants plus the legacy X11-selection type names wl-paste
/// also reports under Xwayland interop.
///
/// The order is the read preference: `run_wl_paste` reads the first of these
/// that the selection offers. `text/plain;charset=utf-8` must beat bare
/// `text/plain`: GTK serves bare `text/plain` as ASCII, every non-ASCII byte
/// escaped (`über` arrives as `\C3\BCber`), so reading it garbled the text
/// and the paste path's restore wrote the garbled copy back. `STRING` is
/// Latin-1 (ICCCM), so it is the last resort.
const TEXT_MIME_TYPES: &[&str] = &[
    "text/plain;charset=utf-8",
    "text/plain",
    "UTF8_STRING",
    "TEXT",
    "STRING",
];

/// Run `wl-paste` with the given extra args (e.g. `--primary`) and return its
/// text, distinguishing a genuinely empty clipboard from one holding
/// non-text content (image, files, ...).
///
/// Does **not** rely on `wl-paste`'s own "inferred type" content negotiation
/// (a bare `wl-paste --no-newline` with no explicit `--type`): that path was
/// observed, live, to sometimes return a non-text selection's raw bytes as
/// if they were text — even immediately after copying an image, with
/// `wl-paste --list-types` correctly reporting only `image/png` on offer.
/// The negotiation wl-paste performs when no type is given is apparently not
/// reliable across all wl-clipboard/compositor/clipboard-manager
/// combinations (reproduced under KDE Plasma 6 / KWin with Klipper active).
///
/// Instead, this always queries `--list-types` first (a plain listing, no
/// negotiation involved) and only issues a real read if a text MIME type is
/// actually listed, with an explicit `--type` naming the most preferred one offered
/// (see [`pick_text_type`]). That type, not a fixed `text/plain`: `--type`
/// with a slash is an exact match, and some offers have no bare `text/plain`
/// (VTE terminals such as Ptyxis offer `text/plain;charset=utf-8`,
/// `UTF8_STRING` and `TARGETS`), so a fixed type failed the read (#210).
///
/// If the list is empty, the clipboard genuinely has no selection — a
/// legitimate empty string. If it's non-empty but contains no text type, the
/// selection holds non-text content and this errors, so a caller restoring a
/// saved clipboard value (see `whisrs`'s paste-injection path) doesn't
/// mistake "can't read this" for "empty" and overwrite it with `""`.
fn run_wl_paste(extra_args: &[&str], command_desc: &str) -> anyhow::Result<String> {
    let list_output = Command::new("wl-paste")
        .arg("--list-types")
        .args(extra_args)
        .output()
        .context("failed to run wl-paste --list-types — is wl-clipboard installed?")?;

    if !list_output.status.success() {
        let stderr = String::from_utf8_lossy(&list_output.stderr);
        if is_empty_clipboard_message(&stderr) {
            return Ok(String::new());
        }
        anyhow::bail!("{command_desc} --list-types failed: {stderr}");
    }

    let types = String::from_utf8_lossy(&list_output.stdout);
    let offered: Vec<&str> = types.lines().map(str::trim).collect();

    if offered.is_empty() {
        // No selection owner at all — genuinely empty clipboard.
        return Ok(String::new());
    }

    let Some(mime) = pick_text_type(&offered) else {
        anyhow::bail!(
            "{command_desc}: clipboard holds non-text content (offered types: {offered:?})"
        );
    };

    let output = Command::new("wl-paste")
        .args(["--no-newline", "--type", mime])
        .args(extra_args)
        .output()
        .context("failed to run wl-paste — is wl-clipboard installed?")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("{command_desc} --type {mime} failed: {stderr}");
    }

    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// The text MIME type to read, given `offered` (as reported by
/// `wl-paste --list-types`): the first [`TEXT_MIME_TYPES`] entry, in that
/// preference order, that is offered. `None` means the offer holds no text.
/// The gate and the read both use this one value, so they cannot disagree.
fn pick_text_type(offered: &[&str]) -> Option<&'static str> {
    TEXT_MIME_TYPES
        .iter()
        .copied()
        .find(|t| offered.contains(t))
}

/// The most a non-text clipboard may weigh to be saved and restored around
/// a paste ([`WaylandClipboard::save`]). Past it the content is left alone.
const SAVED_DATA_MAX_BYTES: usize = 64 * 1024 * 1024;

/// The type to save a non-text offer as, given `offered` (as reported by
/// `wl-paste --list-types`): `image/png` when it is there (what a screenshot
/// or a copied image is pasted as), else the first other image type, else
/// the first type that names content. `None` when only bookkeeping targets
/// are offered (`TARGETS`, `SAVE_TARGETS`, an app's private markers), which
/// carry nothing to put back.
fn pick_data_type<'a>(offered: &[&'a str]) -> Option<&'a str> {
    let is_content = |mime: &&str| {
        mime.contains('/')
            && !mime.starts_with("chromium/")
            && !mime.starts_with("text/_moz_")
            && !mime.starts_with("application/x-kde")
    };
    offered
        .iter()
        .copied()
        .find(|mime| *mime == "image/png")
        .or_else(|| offered.iter().copied().find(|m| m.starts_with("image/")))
        .or_else(|| offered.iter().copied().find(is_content))
}

/// The types the clipboard is offered as (`wl-paste --list-types`). Empty
/// when nothing is copied.
fn offered_types() -> anyhow::Result<Vec<String>> {
    let output = Command::new("wl-paste")
        .arg("--list-types")
        .output()
        .context("failed to run wl-paste --list-types — is wl-clipboard installed?")?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if is_empty_clipboard_message(&stderr) {
            return Ok(Vec::new());
        }
        anyhow::bail!("wl-paste --list-types failed: {stderr}");
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(|line| line.trim().to_string())
        .filter(|line| !line.is_empty())
        .collect())
}

/// Feed `bytes` to `wl-copy` (with `args`), which keeps serving them as the
/// clipboard.
fn run_wl_copy(args: &[&str], bytes: &[u8]) -> anyhow::Result<()> {
    use std::io::Write;

    let mut child = Command::new("wl-copy")
        .args(args)
        .stdin(std::process::Stdio::piped())
        .spawn()
        .context("failed to run wl-copy — is wl-clipboard installed?")?;

    if let Some(ref mut stdin) = child.stdin {
        stdin
            .write_all(bytes)
            .context("failed to write to wl-copy stdin")?;
    }

    let status = child.wait().context("failed to wait for wl-copy")?;
    if !status.success() {
        #[cfg(feature = "logging")]
        log::warn!("wl-copy exited with status {status}");
    }

    Ok(())
}

/// Whether a `wl-paste` stderr message means "the clipboard has no selection
/// at all" (a legitimate empty string). Kept as a defensive fallback in case
/// `--list-types` itself ever errors instead of returning an empty listing.
fn is_empty_clipboard_message(stderr: &str) -> bool {
    stderr.to_lowercase().contains("nothing is copied")
}

/// Clipboard backend that shells out to `wl-paste` (get) and `wl-copy` (set).
pub struct WaylandClipboard;

impl ClipboardBackend for WaylandClipboard {
    fn get_text(&self) -> anyhow::Result<String> {
        run_wl_paste(&[], "wl-paste")
    }

    fn set_text(&self, text: &str) -> anyhow::Result<()> {
        run_wl_copy(&[], text.as_bytes())
    }

    fn get_primary_selection(&self) -> anyhow::Result<String> {
        run_wl_paste(&["--primary"], "wl-paste --primary")
    }

    /// Text as [`get_text`](Self::get_text) reads it. A clipboard that holds
    /// no text (a screenshot, a copied image) is taken as the bytes of one
    /// of its types ([`pick_data_type`]), so a paste can borrow the
    /// clipboard and give the image back.
    fn save(&self) -> anyhow::Result<SavedClipboard> {
        let offered = offered_types()?;
        let offered: Vec<&str> = offered.iter().map(String::as_str).collect();
        if offered.is_empty() || pick_text_type(&offered).is_some() {
            return self.get_text().map(SavedClipboard::Text);
        }
        let Some(mime) = pick_data_type(&offered) else {
            anyhow::bail!("wl-paste: clipboard holds nothing to save (offered types: {offered:?})");
        };
        let output = Command::new("wl-paste")
            .args(["--type", mime])
            .output()
            .context("failed to run wl-paste — is wl-clipboard installed?")?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            anyhow::bail!("wl-paste --type {mime} failed: {stderr}");
        }
        if output.stdout.is_empty() {
            anyhow::bail!("wl-paste --type {mime} returned nothing");
        }
        if output.stdout.len() > SAVED_DATA_MAX_BYTES {
            anyhow::bail!(
                "clipboard holds {} bytes of {mime}, too much to save and restore",
                output.stdout.len()
            );
        }
        Ok(SavedClipboard::Data {
            mime: mime.to_string(),
            bytes: output.stdout,
        })
    }

    fn restore(&self, saved: &SavedClipboard) -> anyhow::Result<()> {
        match saved {
            SavedClipboard::Text(text) => self.set_text(text),
            SavedClipboard::Data { mime, bytes } => run_wl_copy(&["--type", mime], bytes),
        }
    }
}

// ---------------------------------------------------------------------------
// X11: arboard crate (behind "arboard" feature)
// ---------------------------------------------------------------------------

/// The one process-wide `arboard` handle behind every [`X11Clipboard`] call.
///
/// X11 has no clipboard store: the app that copied must keep serving the
/// selection. arboard treats dropping its last handle as the app exiting, so
/// it asks the clipboard manager to take the data (waiting at most 100 ms)
/// and destroys its selection window. The old per-call handle therefore gave
/// the selection away milliseconds after each copy, and the Ctrl+V that
/// followed raced the clipboard manager or compositor, often pasting nothing
/// (#193). This handle is opened on first use and never dropped.
///
/// A failed open is returned, not cached, so a later call retries. The guard
/// must not be held across another clipboard call: the lock is not reentrant.
#[cfg(feature = "arboard")]
fn x11_clipboard() -> anyhow::Result<std::sync::MutexGuard<'static, arboard::Clipboard>> {
    use std::sync::{Mutex, OnceLock, PoisonError};

    static CLIPBOARD: OnceLock<Mutex<arboard::Clipboard>> = OnceLock::new();

    if CLIPBOARD.get().is_none() {
        let clipboard = arboard::Clipboard::new().context("failed to open X11 clipboard")?;
        // A lost race drops this extra handle, which is harmless: the
        // winner's handle still exists, so arboard keeps the selection.
        let _ = CLIPBOARD.set(Mutex::new(clipboard));
    }
    let clipboard = CLIPBOARD.get().expect("X11 clipboard was just initialized");
    // A panic in another clipboard call leaves the handle itself usable.
    Ok(clipboard.lock().unwrap_or_else(PoisonError::into_inner))
}

/// Whether nothing owns the X11 CLIPBOARD selection, i.e. the clipboard is
/// genuinely empty: nothing copied since login, or the app that copied exited
/// and no clipboard manager took over.
///
/// arboard returns the same `ContentNotAvailable` error when nothing owns the
/// selection, when the owner offers no text target, and when the owner times
/// out. Only "no owner" means empty: a caller restoring a saved clipboard
/// reads `Err` as "non-text, do not overwrite it", so `whisrs` typed every
/// paste-mode dictation on an empty X11 clipboard (#79). Asking the X server
/// directly gives [`X11Clipboard`] the same contract as `run_wl_paste` on
/// Wayland. Uses its own short-lived connection, not the arboard handle, and
/// only runs after a read failed.
#[cfg(feature = "arboard")]
fn x11_clipboard_unowned() -> anyhow::Result<bool> {
    use x11rb::protocol::xproto::ConnectionExt as _;

    let (conn, _screen) = x11rb::connect(None).context("failed to connect to X server")?;
    let clipboard = conn.intern_atom(false, b"CLIPBOARD")?.reply()?.atom;
    let owner = conn.get_selection_owner(clipboard)?.reply()?.owner;
    Ok(owner == x11rb::NONE)
}

/// Map an arboard text read to the [`ClipboardBackend::get_text`] contract.
///
/// `ContentNotAvailable` becomes `Ok("")` only when `is_unowned` (in
/// production [`x11_clipboard_unowned`]) confirms nothing owns CLIPBOARD. A
/// negative or failed check keeps the error, so an uncertain clipboard is
/// still protected. The probe runs only for `ContentNotAvailable`.
#[cfg(feature = "arboard")]
fn text_or_empty_if_unowned(
    result: Result<String, arboard::Error>,
    is_unowned: impl FnOnce() -> anyhow::Result<bool>,
) -> anyhow::Result<String> {
    match result {
        Err(arboard::Error::ContentNotAvailable) if matches!(is_unowned(), Ok(true)) => {
            Ok(String::new())
        }
        result => result.context("failed to get text from X11 clipboard"),
    }
}

/// Clipboard backend that uses the `arboard` crate (X11).
/// All instances share one handle that lives as long as the process.
#[cfg(feature = "arboard")]
pub struct X11Clipboard;

#[cfg(feature = "arboard")]
impl ClipboardBackend for X11Clipboard {
    fn get_text(&self) -> anyhow::Result<String> {
        // Bound first so the handle's guard drops before the owner check.
        let result = x11_clipboard()?.get_text();
        text_or_empty_if_unowned(result, x11_clipboard_unowned)
    }

    fn set_text(&self, text: &str) -> anyhow::Result<()> {
        x11_clipboard()?
            .set_text(text)
            .context("failed to set text on X11 clipboard")
    }

    fn get_primary_selection(&self) -> anyhow::Result<String> {
        use arboard::GetExtLinux;
        x11_clipboard()?
            .get()
            .clipboard(arboard::LinuxClipboardKind::Primary)
            .text()
            .context("failed to get text from X11 primary selection")
    }
}

// When arboard is not available, X11Clipboard is not available — callers on
// X11 without the feature will get NoopClipboard from default_clipboard().
#[cfg(not(feature = "arboard"))]
pub struct X11Clipboard;

#[cfg(not(feature = "arboard"))]
impl ClipboardBackend for X11Clipboard {
    fn get_text(&self) -> anyhow::Result<String> {
        anyhow::bail!("X11Clipboard requires the 'arboard' feature");
    }
    fn set_text(&self, _text: &str) -> anyhow::Result<()> {
        anyhow::bail!("X11Clipboard requires the 'arboard' feature");
    }
    fn get_primary_selection(&self) -> anyhow::Result<String> {
        anyhow::bail!("X11Clipboard requires the 'arboard' feature");
    }
}

// ---------------------------------------------------------------------------
// Noop clipboard
// ---------------------------------------------------------------------------

/// Clipboard backend that never succeeds or fails — all operations are no-ops.
pub struct NoopClipboard;

impl ClipboardBackend for NoopClipboard {
    fn get_text(&self) -> anyhow::Result<String> {
        Ok(String::new())
    }

    fn set_text(&self, _text: &str) -> anyhow::Result<()> {
        Ok(())
    }

    fn get_primary_selection(&self) -> anyhow::Result<String> {
        Ok(String::new())
    }
}

// ---------------------------------------------------------------------------
// Auto-detection
// ---------------------------------------------------------------------------

/// Return the appropriate clipboard backend for the current display server.
///
/// Checks `WAYLAND_DISPLAY` to decide: Wayland if set, X11 otherwise.
pub fn default_clipboard() -> Box<dyn ClipboardBackend> {
    if std::env::var("WAYLAND_DISPLAY").is_ok() {
        Box::new(WaylandClipboard)
    } else {
        Box::new(X11Clipboard)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The exact message wl-clipboard emits for a truly empty clipboard (no
    /// selection owner at all), verified against wl-clipboard 2.2.1's
    /// embedded strings.
    #[test]
    fn recognizes_truly_empty_clipboard() {
        assert!(is_empty_clipboard_message("Nothing is copied\n"));
        // Case-insensitive: wording/casing has varied across wl-clipboard
        // versions.
        assert!(is_empty_clipboard_message("nothing is copied\n"));
    }

    /// A non-text offer is saved as its PNG, else another image type, else
    /// the first type with content; bookkeeping targets alone are nothing.
    #[test]
    fn pick_data_type_prefers_the_png_then_an_image_then_any_content() {
        // A screenshot copied in a Chromium app.
        assert_eq!(
            pick_data_type(&[
                "image/png",
                "chromium/x-internal-source-rfh-token",
                "chromium/x-source-url"
            ]),
            Some("image/png")
        );
        // "Copy image" in Firefox: the HTML comes first, the PNG is taken.
        assert_eq!(
            pick_data_type(&["text/html", "text/_moz_htmlinfo", "image/png", "image/bmp"]),
            Some("image/png")
        );
        assert_eq!(
            pick_data_type(&["text/html", "image/jpeg", "image/webp"]),
            Some("image/jpeg")
        );
        assert_eq!(
            pick_data_type(&["text/_moz_htmlinfo", "text/html", "SAVE_TARGETS"]),
            Some("text/html")
        );
        assert_eq!(pick_data_type(&["TARGETS", "SAVE_TARGETS"]), None);
        assert_eq!(pick_data_type(&["chromium/x-source-url"]), None);
    }

    /// On a real Wayland session: an image on the clipboard is saved, survives
    /// a text being written over it, and comes back byte for byte. Ignored by
    /// default because it borrows the session's clipboard (whatever was there
    /// is put back at the end, text or one type of anything else).
    /// Run with `cargo test -p xkb-type -- --ignored image_round_trip`.
    #[test]
    #[ignore = "borrows the clipboard of a running Wayland session"]
    fn image_round_trip_on_a_wayland_session() {
        let clipboard = WaylandClipboard;
        let before = clipboard
            .save()
            .expect("the session's clipboard can be saved");

        // A PNG signature and some bytes: wl-copy does not look inside.
        let png: Vec<u8> = [0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]
            .into_iter()
            .chain((0..=255u8).cycle().take(4096))
            .collect();
        run_wl_copy(&["--type", "image/png"], &png).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(150));

        assert!(clipboard.get_text().is_err(), "an image is not text");
        let saved = clipboard.save().unwrap();
        assert_eq!(
            saved,
            SavedClipboard::Data {
                mime: "image/png".into(),
                bytes: png.clone()
            }
        );

        clipboard.set_text("texto colado\ncom duas linhas").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(150));
        assert_eq!(
            clipboard.get_text().unwrap(),
            "texto colado\ncom duas linhas"
        );

        clipboard.restore(&saved).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(150));
        let again = clipboard.save();

        clipboard.restore(&before).unwrap();
        assert_eq!(again.unwrap(), saved, "the image is back as it was");
    }

    /// A selection that exists but isn't text must NOT be treated as empty —
    /// doing so is exactly the bug this fixes: a caller restoring a saved
    /// clipboard value after a paste would silently overwrite an image or
    /// file selection with `""`.
    #[test]
    fn does_not_treat_non_text_content_as_empty() {
        assert!(!is_empty_clipboard_message(
            "Clipboard content is not available as inferred output type \"text/plain\"\n\
             Use \"wl-paste --list-types\" to view available types."
        ));
        assert!(!is_empty_clipboard_message(
            "Clipboard content is not available as requested type \"text/plain\"\n\
             Use \"wl-paste --list-types\" to view available types."
        ));
    }

    #[test]
    fn does_not_match_stale_no_suitable_type_phrasing() {
        // The old pattern this used to check for; confirm it alone (without
        // "nothing is copied") is correctly treated as an error, not empty.
        assert!(!is_empty_clipboard_message(
            "no suitable type of content copied"
        ));
    }

    /// `pick_text_type` is what `get_text`/`get_primary_selection` now gate
    /// and read on (via `wl-paste --list-types`) instead of trusting
    /// wl-paste's own "inferred type" negotiation — that negotiation was
    /// observed, live, to sometimes return an image selection's raw bytes as
    /// if they were text, even when `--list-types` correctly listed only
    /// `image/png`. Listing types first and only reading with an explicit
    /// `--type` naming a text type that is actually offered avoids depending
    /// on that negotiation at all.
    #[test]
    fn pick_text_type_none_for_empty_list() {
        assert_eq!(pick_text_type(&[]), None);
    }

    #[test]
    fn pick_text_type_none_for_image_only() {
        assert_eq!(pick_text_type(&["image/png"]), None);
        assert_eq!(
            pick_text_type(&["image/png", "application/x-qt-image", "image/bmp"]),
            None
        );
    }

    /// `COMPOUND_TEXT` is an X11 text target, but not one this reads.
    #[test]
    fn pick_text_type_none_for_compound_text_only() {
        assert_eq!(pick_text_type(&["COMPOUND_TEXT"]), None);
    }

    /// VTE terminals (Ptyxis, GNOME Console) offer no bare `text/plain`, so a
    /// fixed `--type text/plain` read failed after the gate passed (#210).
    #[test]
    fn pick_text_type_vte_offer_reads_utf8_plain() {
        assert_eq!(
            pick_text_type(&["text/plain;charset=utf-8", "UTF8_STRING", "TARGETS"]),
            Some("text/plain;charset=utf-8")
        );
    }

    #[test]
    fn pick_text_type_legacy_only_offer_reads_utf8_string() {
        assert_eq!(
            pick_text_type(&["UTF8_STRING", "TARGETS"]),
            Some("UTF8_STRING")
        );
    }

    /// wl-copy's own offer: the pick follows the preference order, not the
    /// listing order. A GTK offer lists both `text/plain` variants and
    /// serves bare `text/plain` as escaped ASCII, so the charset one wins.
    #[test]
    fn pick_text_type_follows_preference_not_listing_order() {
        assert_eq!(
            pick_text_type(&[
                "text/plain",
                "text/plain;charset=utf-8",
                "TEXT",
                "STRING",
                "UTF8_STRING",
            ]),
            Some("text/plain;charset=utf-8")
        );
        assert_eq!(
            pick_text_type(&["image/png", "text/plain"]),
            Some("text/plain")
        );
    }

    /// `STRING` is Latin-1, so it loses to any other text type.
    #[test]
    fn pick_text_type_prefers_text_over_latin1_string() {
        assert_eq!(pick_text_type(&["STRING", "TEXT"]), Some("TEXT"));
    }

    /// Every type the gate accepts is also the type read, so the two can
    /// never disagree.
    #[test]
    fn pick_text_type_reads_every_text_type_offered_alone() {
        for &mime in TEXT_MIME_TYPES {
            assert_eq!(pick_text_type(&[mime]), Some(mime));
        }
    }

    /// `text_or_empty_if_unowned`: only a confirmed unowned CLIPBOARD may
    /// turn `ContentNotAvailable` into `""` (#79).
    #[cfg(feature = "arboard")]
    mod x11 {
        use super::super::text_or_empty_if_unowned;
        use std::cell::Cell;

        /// Run the helper with a probe that records whether it was called.
        fn run(
            result: Result<String, arboard::Error>,
            probe: anyhow::Result<bool>,
        ) -> (anyhow::Result<String>, bool) {
            let called = Cell::new(false);
            let out = text_or_empty_if_unowned(result, || {
                called.set(true);
                probe
            });
            (out, called.get())
        }

        /// Unwrap the error and check it carries the X11 context.
        fn x11_error(out: anyhow::Result<String>) -> anyhow::Error {
            let err = out.expect_err("must stay an error");
            assert!(format!("{err:#}").contains("failed to get text from X11 clipboard"));
            err
        }

        fn is_content_not_available(err: &anyhow::Error) -> bool {
            matches!(
                err.downcast_ref::<arboard::Error>(),
                Some(arboard::Error::ContentNotAvailable)
            )
        }

        #[test]
        fn text_is_returned_without_owner_check() {
            let (out, called) = run(Ok("hello".into()), Ok(true));
            assert_eq!(out.unwrap(), "hello");
            assert!(!called);
        }

        #[test]
        fn content_not_available_with_no_owner_is_empty() {
            let (out, called) = run(Err(arboard::Error::ContentNotAvailable), Ok(true));
            assert_eq!(out.unwrap(), "");
            assert!(called);
        }

        /// An owner without a text target (image, files) must not read as
        /// empty, or a restore would overwrite it with `""`.
        #[test]
        fn content_not_available_with_owner_stays_error() {
            let (out, called) = run(Err(arboard::Error::ContentNotAvailable), Ok(false));
            assert!(is_content_not_available(&x11_error(out)));
            assert!(called);
        }

        /// A failed owner check is uncertain, so it fails safe.
        #[test]
        fn content_not_available_with_failed_owner_check_stays_error() {
            let (out, called) = run(
                Err(arboard::Error::ContentNotAvailable),
                Err(anyhow::anyhow!("no X server")),
            );
            assert!(is_content_not_available(&x11_error(out)));
            assert!(called);
        }

        #[test]
        fn other_error_stays_error_without_owner_check() {
            let (out, called) = run(Err(arboard::Error::ClipboardOccupied), Ok(true));
            let err = x11_error(out);
            assert!(matches!(
                err.downcast_ref::<arboard::Error>(),
                Some(arboard::Error::ClipboardOccupied)
            ));
            assert!(!called);
        }
    }
}
