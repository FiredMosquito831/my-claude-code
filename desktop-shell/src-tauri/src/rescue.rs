//! Reading what `mcc-desktop --rescue` printed (7.71.0).
//!
//! The rescue itself is Python's (`cli/rescue.py`): it re-reads the process
//! table, the listening sockets and this configuration folder's server log,
//! refuses if anything holds the port, waits for each old server of THIS port
//! and THIS configuration folder to finish and exit by itself, stops by exact
//! pid what is still there, and waits for the port. This window decides
//! nothing about which process is which (the same rule the tray's "stale
//! servers" item follows): it asks, and reads the one JSON document the
//! command prints.
//!
//! The parse is deliberately forgiving in one direction only. Anything it
//! cannot read is `Failed` -- not proven, nothing started -- never `PortFree`.

use serde::Deserialize;

use crate::controller::{DeadReason, RescueOutcome, RescueResult, StoppedServer};

/// What one run of the command came back as, before parsing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RescueRun {
    /// It ran and printed this.
    Report(String),
    /// It exited 2 with its usage: an `mcc-desktop` from before 7.71.0.
    Unsupported(String),
    /// It did not finish inside its wall, could not be started, or failed.
    Failed(String),
}

/// The request, kept so the outcome can say what it was about even when the
/// command could not.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RescueRequest {
    pub known_pid: Option<i64>,
    pub child_pid: Option<i64>,
    pub reason: DeadReason,
    pub stop_wait_seconds: f64,
}

#[derive(Debug, Deserialize)]
struct Report {
    outcome: String,
    #[serde(default)]
    servers: Vec<ReportServer>,
    #[serde(default)]
    left_alone: Vec<ReportLeft>,
    #[serde(default)]
    detail: Option<String>,
    #[serde(default)]
    stop_wait_seconds: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct ReportServer {
    #[serde(default)]
    pids: Vec<i64>,
    #[serde(default)]
    exited_by_itself: bool,
}

#[derive(Debug, Deserialize)]
struct ReportLeft {
    #[serde(default)]
    pids: Vec<i64>,
}

/// Turn one run into the plain outcome `controller::step` reads.
pub fn outcome_of(request: RescueRequest, run: &RescueRun) -> RescueOutcome {
    let base = RescueOutcome {
        result: RescueResult::Failed,
        reason: request.reason,
        known_pid: request.known_pid,
        child_pid: request.child_pid,
        servers: Vec::new(),
        left_alone: Vec::new(),
        detail: String::new(),
        stop_wait_seconds: request.stop_wait_seconds,
    };
    match run {
        RescueRun::Unsupported(detail) => RescueOutcome {
            result: RescueResult::Unsupported,
            detail: detail.trim().to_owned(),
            ..base
        },
        RescueRun::Failed(detail) => RescueOutcome {
            detail: detail.trim().to_owned(),
            ..base
        },
        RescueRun::Report(raw) => match serde_json::from_str::<Report>(raw.trim()) {
            Err(error) => RescueOutcome {
                detail: format!("the rescue printed something that is not its report: {error}"),
                ..base
            },
            Ok(report) => {
                let result = match report.outcome.as_str() {
                    "port_free" => RescueResult::PortFree,
                    "refused" => RescueResult::Refused,
                    _ => RescueResult::Failed,
                };
                RescueOutcome {
                    result,
                    servers: report
                        .servers
                        .into_iter()
                        .filter(|server| !server.pids.is_empty())
                        .map(|server| StoppedServer {
                            pids: server.pids,
                            exited_by_itself: server.exited_by_itself,
                        })
                        .collect(),
                    left_alone: report
                        .left_alone
                        .into_iter()
                        .flat_map(|left| left.pids)
                        .collect(),
                    detail: report.detail.unwrap_or_default(),
                    stop_wait_seconds: report
                        .stop_wait_seconds
                        .filter(|seconds| *seconds >= 0.0)
                        .unwrap_or(request.stop_wait_seconds),
                    ..base
                }
            }
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> RescueRequest {
        RescueRequest {
            known_pid: Some(58620),
            child_pid: None,
            reason: DeadReason::ListenerLost,
            stop_wait_seconds: 24.0,
        }
    }

    #[test]
    fn a_port_free_report_is_read_in_full() {
        let raw = r#"{"schema": 1, "outcome": "port_free", "reason": "listener-lost",
            "port": 9999, "servers": [{"pids": [42776, 58620], "exited_by_itself": false,
            "why": "lost its listener"}], "left_alone": [{"pids": [9001], "why": "port 8090"}],
            "stop_wait_seconds": 24.0, "detail": ""}"#;
        let outcome = outcome_of(request(), &RescueRun::Report(raw.to_owned()));
        assert_eq!(outcome.result, RescueResult::PortFree);
        assert_eq!(
            outcome.servers,
            vec![StoppedServer {
                pids: vec![42776, 58620],
                exited_by_itself: false
            }]
        );
        assert_eq!(outcome.left_alone, vec![9001]);
        assert_eq!(outcome.known_pid, Some(58620));
    }

    #[test]
    fn a_refusal_is_a_refusal() {
        let raw = r#"{"outcome": "refused", "detail": "port 9999 is held by pid 4242"}"#;
        let outcome = outcome_of(request(), &RescueRun::Report(raw.to_owned()));
        assert_eq!(outcome.result, RescueResult::Refused);
        assert!(outcome.detail.contains("4242"));
    }

    #[test]
    fn anything_unreadable_is_failed_and_never_port_free() {
        for raw in ["", "not json", "{}", r#"{"outcome": "something new"}"#] {
            let outcome = outcome_of(request(), &RescueRun::Report(raw.to_owned()));
            assert_eq!(outcome.result, RescueResult::Failed, "{raw:?}");
        }
        let outcome = outcome_of(request(), &RescueRun::Failed("wall".to_owned()));
        assert_eq!(outcome.result, RescueResult::Failed);
    }

    #[test]
    fn an_old_mcc_desktop_is_unsupported() {
        let outcome = outcome_of(request(), &RescueRun::Unsupported("Usage: ...".to_owned()));
        assert_eq!(outcome.result, RescueResult::Unsupported);
        assert!(outcome.servers.is_empty());
    }
}
