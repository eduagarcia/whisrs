//! What a dictation's backends answered, kept for the dictation archive
//! (`[general] archive_dir`).
//!
//! The transcription backend and the LLM return only the text. The archive
//! also wants each response as it came (the provider's usage and cost ride
//! in it) and how long each call took. The trait signatures stay as they
//! are: a backend calls [`asr_response`] or [`llm_response`] with the raw
//! body, and whoever runs the dictation wraps it in [`scope`], which hands
//! back what was recorded. The slot is task-local, so dictations that
//! overlap (`[input] queue`) each get their own, and a call made outside
//! any scope (command mode, a recording's summary) records nothing.

use std::cell::RefCell;
use std::future::Future;
use std::time::Duration;

tokio::task_local! {
    static SLOT: RefCell<Captured>;
}

/// One answered request: the body as the server sent it, and the time from
/// the request being sent to the body being read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    pub body: String,
    pub elapsed: Duration,
}

/// What the backends of one dictation answered. The last answer of each
/// kind wins: a retried request records only the one that succeeded.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Captured {
    /// The transcription backend's success body.
    pub asr: Option<Response>,
    /// The LLM's success body (`[general] llm_post_process`).
    pub llm: Option<Response>,
}

/// Run `future` and return its output with what the backends recorded
/// while it ran.
pub async fn scope<F: Future>(future: F) -> (F::Output, Captured) {
    SLOT.scope(RefCell::new(Captured::default()), async move {
        let output = future.await;
        let captured = SLOT.with(|slot| slot.take());
        (output, captured)
    })
    .await
}

/// Record the transcription backend's success body. Nothing outside a
/// [`scope`].
pub fn asr_response(body: &str, elapsed: Duration) {
    let _ = SLOT.try_with(|slot| {
        slot.borrow_mut().asr = Some(Response {
            body: body.to_string(),
            elapsed,
        });
    });
}

/// Record the LLM's success body. Nothing outside a [`scope`].
pub fn llm_response(body: &str, elapsed: Duration) {
    let _ = SLOT.try_with(|slot| {
        slot.borrow_mut().llm = Some(Response {
            body: body.to_string(),
            elapsed,
        });
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_scope_returns_what_was_recorded_inside_it() {
        let (output, captured) = scope(async {
            asr_response("{\"text\":\"first\"}", Duration::from_millis(5));
            asr_response("{\"text\":\"hi\"}", Duration::from_millis(7));
            llm_response("{\"choices\":[]}", Duration::from_millis(9));
            42
        })
        .await;
        assert_eq!(output, 42);
        let asr = captured.asr.unwrap();
        assert_eq!(asr.body, "{\"text\":\"hi\"}");
        assert_eq!(asr.elapsed, Duration::from_millis(7));
        assert_eq!(captured.llm.unwrap().body, "{\"choices\":[]}");
    }

    #[tokio::test]
    async fn outside_a_scope_nothing_is_recorded_and_scopes_do_not_mix() {
        // No scope: a no-op, not a panic.
        asr_response("lost", Duration::ZERO);

        let a = tokio::spawn(scope(async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            asr_response("a", Duration::ZERO);
        }));
        let b = tokio::spawn(scope(async {
            asr_response("b", Duration::ZERO);
        }));
        let ((), a) = a.await.unwrap();
        let ((), b) = b.await.unwrap();
        assert_eq!(a.asr.unwrap().body, "a");
        assert_eq!(b.asr.unwrap().body, "b");
        assert!(a.llm.is_none());
    }
}
