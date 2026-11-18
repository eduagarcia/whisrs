//! What a dictation's backends answered, kept for the dictation archive
//! (`[general] archive_dir`).
//!
//! The transcription backend and the LLM return only the text. The archive
//! also wants each response as it came (the provider's usage and cost ride
//! in it) and how long each call took. The trait signatures stay as they
//! are: a backend calls [`asr_response`] or [`llm_response`] with the raw
//! body, and whoever runs the dictation wraps it in [`scope`], which hands
//! back what was recorded. A request that failed is recorded too
//! ([`asr_failure`], [`llm_failure`]): the status and the body the server
//! refused it with, or the error of a request that got no answer. The slot
//! is task-local, so dictations that overlap (`[input] queue`) each get
//! their own, and a call made outside any scope (command mode, a
//! recording's summary) records nothing.

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

/// One request that failed: refused with an HTTP status, or never answered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    /// The model the request asked for.
    pub model: String,
    /// The HTTP status, or `None` for a request that got no answer.
    pub status: Option<u16>,
    /// The body the server sent with that status, or the error's text.
    pub body: String,
    pub elapsed: Duration,
}

/// What the backends of one dictation answered. The last success of each
/// kind wins; every failure is kept, in the order they happened.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Captured {
    /// The transcription backend's success body.
    pub asr: Option<Response>,
    /// The model that gave [`asr`](Self::asr): not the configured one when
    /// a fallback model answered.
    pub asr_model: Option<String>,
    /// The transcription requests that failed, retries included.
    pub asr_failures: Vec<Failure>,
    /// The LLM's success body (`[general] llm_post_process`).
    pub llm: Option<Response>,
    /// The LLM requests that failed.
    pub llm_failures: Vec<Failure>,
}

impl Captured {
    /// Whether the transcription backend was asked at all: a dictation
    /// with no speech in it never reaches the backend.
    pub fn asked_asr(&self) -> bool {
        self.asr.is_some() || !self.asr_failures.is_empty()
    }
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

/// Record the transcription backend's success body and the `model` that
/// gave it. Nothing outside a [`scope`].
pub fn asr_response(model: &str, body: &str, elapsed: Duration) {
    let _ = SLOT.try_with(|slot| {
        let mut slot = slot.borrow_mut();
        slot.asr = Some(Response {
            body: body.to_string(),
            elapsed,
        });
        slot.asr_model = Some(model.to_string());
    });
}

/// Record a transcription request that failed. Nothing outside a [`scope`].
pub fn asr_failure(model: &str, status: Option<u16>, body: &str, elapsed: Duration) {
    let _ = SLOT.try_with(|slot| {
        slot.borrow_mut()
            .asr_failures
            .push(failure(model, status, body, elapsed));
    });
}

/// Record an LLM request that failed. Nothing outside a [`scope`].
pub fn llm_failure(model: &str, status: Option<u16>, body: &str, elapsed: Duration) {
    let _ = SLOT.try_with(|slot| {
        slot.borrow_mut()
            .llm_failures
            .push(failure(model, status, body, elapsed));
    });
}

fn failure(model: &str, status: Option<u16>, body: &str, elapsed: Duration) -> Failure {
    Failure {
        model: model.to_string(),
        status,
        body: body.to_string(),
        elapsed,
    }
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
            asr_failure(
                "m1",
                Some(429),
                "{\"error\":\"busy\"}",
                Duration::from_millis(3),
            );
            asr_failure("m1", None, "connection reset", Duration::from_millis(4));
            asr_response("m1", "{\"text\":\"first\"}", Duration::from_millis(5));
            asr_response("m2", "{\"text\":\"hi\"}", Duration::from_millis(7));
            llm_failure("llm", Some(500), "oops", Duration::from_millis(8));
            llm_response("{\"choices\":[]}", Duration::from_millis(9));
            42
        })
        .await;
        assert_eq!(output, 42);
        let asr = captured.asr.unwrap();
        assert_eq!(asr.body, "{\"text\":\"hi\"}");
        assert_eq!(asr.elapsed, Duration::from_millis(7));
        assert_eq!(captured.llm.unwrap().body, "{\"choices\":[]}");
        // The model of the last success, and every failure in order.
        assert_eq!(captured.asr_model.as_deref(), Some("m2"));
        let statuses: Vec<_> = captured.asr_failures.iter().map(|f| f.status).collect();
        assert_eq!(statuses, [Some(429), None]);
        assert_eq!(captured.asr_failures[1].body, "connection reset");
        assert_eq!(captured.llm_failures[0].status, Some(500));
    }

    #[test]
    fn asked_asr_is_true_for_an_answer_or_a_failure_only() {
        assert!(!Captured::default().asked_asr());
        let failed = Captured {
            asr_failures: vec![failure("m", Some(429), "", Duration::ZERO)],
            ..Captured::default()
        };
        assert!(failed.asked_asr());
    }

    #[tokio::test]
    async fn outside_a_scope_nothing_is_recorded_and_scopes_do_not_mix() {
        // No scope: a no-op, not a panic.
        asr_response("m", "lost", Duration::ZERO);
        asr_failure("m", Some(429), "lost", Duration::ZERO);

        let a = tokio::spawn(scope(async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            asr_response("m", "a", Duration::ZERO);
        }));
        let b = tokio::spawn(scope(async {
            asr_response("m", "b", Duration::ZERO);
        }));
        let ((), a) = a.await.unwrap();
        let ((), b) = b.await.unwrap();
        assert_eq!(a.asr.unwrap().body, "a");
        assert_eq!(b.asr.unwrap().body, "b");
        assert!(a.llm.is_none());
    }
}
