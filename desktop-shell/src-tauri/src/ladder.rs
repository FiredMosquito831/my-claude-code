//! The status ladder, as a pure function.
//!
//! Nothing here starts a process, opens a socket or touches a file. That is
//! the point: the decision that governs what the user sees is a value derived
//! from a status document, so every branch is a unit test rather than a
//! hand-run.
//!
//! The ladder itself is not new logic -- it is `ensure_server()` and
//! `probe_server_presence()` from the Python side, exposed to a second
//! process.

use crate::status::Status;

/// What the window should do about the server, given one status document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// A healthy MCC is already answering: show the dashboard, start nothing.
    Attach { admin_url: String },
    /// The port is free and this install owns the server: start it, then poll.
    Start {
        admin_url: String,
        health_url: String,
    },
    /// The port is free but the server is somebody elses to start.
    NotOurServer { server_mode: String },
    /// A stranger holds the port. The Python message names the holder.
    PortConflict { message: String },
    /// A presence value this build has no branch for. Treated like a schema
    /// mismatch rather than silently mapped onto the nearest neighbour.
    UnknownPresence { presence: String },
}

/// Decide what to do about the server. Pure.
pub fn decide(status: &Status) -> Decision {
    match status.server_presence.as_str() {
        // The URL is used exactly as Python spelled it. Rebuilding it from a
        // host and a port here would be a second source of truth (C1).
        "healthy" => Decision::Attach {
            admin_url: status.admin_url.clone(),
        },
        "foreign" => Decision::PortConflict {
            message: status.port_conflict.clone().unwrap_or_else(|| {
                // Defensive only: Python always carries the message when the
                // presence is foreign. If it ever does not, say the true thing.
                "Something other than My Claude Code is already listening on \
                 the configured port."
                    .to_owned()
            }),
        },
        "free" if status.server_mode == "spawn" => Decision::Start {
            admin_url: status.admin_url.clone(),
            health_url: status.health_url.clone(),
        },
        "free" => Decision::NotOurServer {
            server_mode: status.server_mode.clone(),
        },
        other => Decision::UnknownPresence {
            presence: other.to_owned(),
        },
    }
}

/// The sentence shown when the server is nobody elses to start from here.
pub fn not_our_server_message(server_mode: &str) -> String {
    format!(
        "The server is not running. Server mode is {server_mode}; start \
         mcc-server yourself, or switch to spawn."
    )
}

/// The sentence shown when a start never came up.
pub fn start_timeout_message(status: &Status) -> String {
    format!(
        "The server did not answer within {:.0} seconds. Its log is at {}.",
        status.start_timeout_seconds, status.server_log
    )
}

/// Whether a start that has been running for `elapsed_seconds` may keep going.
pub fn start_may_continue(status: &Status, elapsed_seconds: f64) -> bool {
    elapsed_seconds < status.start_timeout_seconds
}

/// What to do about a server that has stopped answering after it was healthy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reconnect {
    /// Below the debounce threshold: say nothing, change nothing. A single
    /// missed poll during an update must not paint anything over the page.
    Ignore,
    /// Past the threshold, inside the budget: show the reconnect banner.
    Waiting,
    /// Past the budget: the error page, naming the log.
    Failed { server_log: String },
}

/// Decide what a run of failed health checks means. Pure.
///
/// C9: both numbers come from the status document. `health_failure_threshold`
/// is the same debounce `HealthTracker` applies in Python, and
/// `reconnect_timeout_seconds` is the same budget the dashboards own
/// `waitForUpdatedServer` uses -- so a routine update looks the same in this
/// window as it does in a browser tab.
pub fn reconnect_verdict(
    status: &Status,
    consecutive_failures: u32,
    elapsed_seconds: f64,
) -> Reconnect {
    if consecutive_failures < status.health_failure_threshold {
        return Reconnect::Ignore;
    }
    if elapsed_seconds < status.reconnect_timeout_seconds {
        return Reconnect::Waiting;
    }
    Reconnect::Failed {
        server_log: status.server_log.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::status::{parse_status, sample_json};

    fn status_with(mutate: impl FnOnce(&mut serde_json::Value)) -> Status {
        let mut document = sample_json();
        mutate(&mut document);
        parse_status(&document.to_string()).expect("sample parses")
    }

    #[test]
    fn healthy_attaches_without_spawning() {
        let status = status_with(|_| {});
        assert_eq!(
            decide(&status),
            Decision::Attach {
                admin_url: "http://127.0.0.1:9999/admin".to_owned()
            }
        );
    }

    #[test]
    fn the_admin_url_is_taken_verbatim_from_the_status_document() {
        // C1: whatever Python says, including a non-default port and a query
        // this build has never seen, is what gets loaded.
        let status = status_with(|document| {
            document["admin_url"] = serde_json::json!("http://127.0.0.1:41234/admin?x=1");
        });
        match decide(&status) {
            Decision::Attach { admin_url } => {
                assert_eq!(admin_url, "http://127.0.0.1:41234/admin?x=1");
            }
            other => panic!("expected an attach, got {other:?}"),
        }
    }

    #[test]
    fn free_and_spawn_starts_then_polls_health() {
        let status = status_with(|document| {
            document["server_presence"] = serde_json::json!("free");
            document["server_mode"] = serde_json::json!("spawn");
        });
        assert_eq!(
            decide(&status),
            Decision::Start {
                admin_url: "http://127.0.0.1:9999/admin".to_owned(),
                health_url: "http://127.0.0.1:9999/health".to_owned(),
            }
        );
    }

    #[test]
    fn free_and_attach_shows_the_attach_message() {
        let status = status_with(|document| {
            document["server_presence"] = serde_json::json!("free");
            document["server_mode"] = serde_json::json!("attach");
        });
        assert_eq!(
            decide(&status),
            Decision::NotOurServer {
                server_mode: "attach".to_owned()
            }
        );
        assert!(not_our_server_message("attach").contains("switch to spawn"));
    }

    #[test]
    fn free_and_off_starts_nothing_either() {
        let status = status_with(|document| {
            document["server_presence"] = serde_json::json!("free");
            document["server_mode"] = serde_json::json!("off");
        });
        assert_eq!(
            decide(&status),
            Decision::NotOurServer {
                server_mode: "off".to_owned()
            }
        );
    }

    #[test]
    fn foreign_shows_the_port_conflict_verbatim() {
        let status = status_with(|document| {
            document["server_presence"] = serde_json::json!("foreign");
            document["port_conflict"] = serde_json::json!("nginx (pid 4242) is on the port.");
        });
        assert_eq!(
            decide(&status),
            Decision::PortConflict {
                message: "nginx (pid 4242) is on the port.".to_owned()
            }
        );
    }

    #[test]
    fn an_unknown_presence_is_not_guessed_at() {
        let status = status_with(|document| {
            document["server_presence"] = serde_json::json!("draining");
        });
        assert_eq!(
            decide(&status),
            Decision::UnknownPresence {
                presence: "draining".to_owned()
            }
        );
    }

    #[test]
    fn transient_failure_within_budget_does_not_error() {
        let status = status_with(|_| {});
        // Under the threshold: nothing is shown at all.
        assert_eq!(reconnect_verdict(&status, 2, 1.0), Reconnect::Ignore);
        // Over the threshold, well inside the 1320 s budget: a banner, not an
        // error page.
        assert_eq!(reconnect_verdict(&status, 3, 900.0), Reconnect::Waiting);
        assert_eq!(reconnect_verdict(&status, 99, 1319.9), Reconnect::Waiting);
    }

    #[test]
    fn the_budget_comes_from_the_document_and_not_from_this_binary() {
        // C9: shrink both knobs and the verdicts move with them.
        let status = status_with(|document| {
            document["health_failure_threshold"] = serde_json::json!(1);
            document["reconnect_timeout_seconds"] = serde_json::json!(5.0);
        });
        assert_eq!(reconnect_verdict(&status, 1, 1.0), Reconnect::Waiting);
        assert_eq!(
            reconnect_verdict(&status, 1, 5.0),
            Reconnect::Failed {
                server_log: "/home/example/config/logs/server.log".to_owned()
            }
        );
    }

    #[test]
    fn timeout_names_the_server_log() {
        let status = status_with(|_| {});
        assert_eq!(
            reconnect_verdict(&status, 3, 1320.0),
            Reconnect::Failed {
                server_log: "/home/example/config/logs/server.log".to_owned()
            }
        );
        let message = start_timeout_message(&status);
        assert!(message.contains("/home/example/config/logs/server.log"));
        assert!(message.contains("30 seconds"));
        assert!(start_may_continue(&status, 29.9));
        assert!(!start_may_continue(&status, 30.0));
    }
}
