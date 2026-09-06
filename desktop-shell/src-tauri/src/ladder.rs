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
    /// MCC's own server is on the port and is refusing everything while it
    /// finishes stopping. Not a conflict, not a free port, and above all not
    /// something to start a second server into: wait for it, and say so.
    ///
    /// Before this existed, a window launched during a restart was told the
    /// port was held by "python.exe (pid N), which is not the MCC server" --
    /// about MCC's own process. Since closing and relaunching the app is
    /// exactly what a user does when a restart looks stuck, that page was the
    /// routine outcome of the routine workaround.
    Draining,
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
        "draining" => Decision::Draining,
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

/// The sentence shown while MCC's own server finishes stopping.
pub fn draining_message(status: &Status) -> String {
    format!(
        "The My Claude Code server is shutting down and is refusing new          requests until it has finished. Waiting for it, then reconnecting          for up to {:.0} minutes.",
        status.reconnect_timeout_seconds / 60.0
    )
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

/// Whether a reconnecting window should re-read the whole status document now,
/// rather than only re-probing the health URL again.
///
/// This is the pure half of the "the app hangs until I close and reopen it"
/// fix. A loop that only pings one URL cannot tell "the server is restarting"
/// apart from "the server exited and nothing is going to start another one",
/// and the second is what happens when an update helper fails: the port goes
/// free and stays free, and the window waits out the whole budget doing
/// nothing. Re-reading the document is what notices.
///
/// C9: the cadence is the document's (`reconnect_restatus_seconds`), never
/// this binary's. The 1s floor is not a policy, it is a guard against a
/// document that would turn the loop into a process storm.
pub fn should_restatus(status: &Status, seconds_since_last_restatus: f64) -> bool {
    seconds_since_last_restatus >= status.reconnect_restatus_seconds.max(1.0)
}

/// What a status document read *during* a reconnect licenses the window to do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Respawn {
    /// The port is free and this install owns the server: start it. Once.
    Start,
    /// Start nothing and keep polling.
    Hold,
}

/// Decide whether to start a server from inside a reconnect. Pure.
///
/// Deliberately narrow: only the unambiguous answer -- the ladder itself says
/// `Start`, which means the port is free *and* `server_mode` is `spawn` --
/// gets a spawn, and only if this episode has not already used its one. A
/// server that crash-loops on start would otherwise be restarted every
/// `reconnect_restatus_seconds` for the whole budget, which is a way to turn a
/// broken install into thirty broken installs.
pub fn respawn_verdict(status: &Status, already_respawned: bool) -> Respawn {
    if already_respawned {
        return Respawn::Hold;
    }
    match decide(status) {
        Decision::Start { .. } => Respawn::Start,
        _ => Respawn::Hold,
    }
}

/// A duration a person can read: `45 s`, `3 m 20 s`, `1 h 2 m`.
fn human_duration(seconds: f64) -> String {
    let total = seconds.max(0.0).round() as u64;
    if total < 60 {
        return format!("{total} s");
    }
    let minutes = total / 60;
    if minutes < 60 {
        let rest = total % 60;
        if rest == 0 {
            return format!("{minutes} m");
        }
        return format!("{minutes} m {rest} s");
    }
    let hours = minutes / 60;
    let rest = minutes % 60;
    if rest == 0 {
        format!("{hours} h")
    } else {
        format!("{hours} h {rest} m")
    }
}

/// The reconnect banner's text, rebuilt on every tick.
///
/// Before 6.50.0 the banner was one static sentence, painted once and never
/// touched again. Over a fourteen-minute update the window showed it for a
/// hundred and seventy consecutive successful loop iterations, which is why
/// the report said the app "doesn't seem to recheck the server status" -- it
/// was rechecking every five seconds and saying nothing about it.
///
/// Everything here is a fact the loop already had: how long it has been
/// trying, how long it may keep trying, when it last checked, what the check
/// said, and -- when a Windows update helper is running -- which stage that
/// helper reported. No new probe, no fetch, no clock the loop does not own.
pub fn reconnect_progress_text(
    status: &Status,
    elapsed_seconds: f64,
    seconds_since_probe: f64,
    last_failure: &str,
    update_stage: Option<&str>,
) -> String {
    let remaining = (status.reconnect_timeout_seconds - elapsed_seconds).max(0.0);
    let mut text = format!(
        "The server stopped answering -- it is probably restarting. Still \
         trying: {} elapsed of {}, {} left. Last checked {} ago ({}).",
        human_duration(elapsed_seconds),
        human_duration(status.reconnect_timeout_seconds),
        human_duration(remaining),
        human_duration(seconds_since_probe),
        last_failure.trim(),
    );
    if let Some(stage) = update_stage {
        let stage = stage.trim();
        if !stage.is_empty() {
            text.push_str(&format!(" Update: {stage}"));
        }
    }
    text
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
    fn an_unknown_presence_is_still_refused_after_draining_is_added() {
        // C3. Adding a presence value must not turn the unknown branch into a
        // guess: a value from a later wheel is still refused loudly, and the
        // refusal still names it.
        let status = status_with(|document| {
            document["server_presence"] = serde_json::json!("quiescing");
        });
        assert_eq!(
            decide(&status),
            Decision::UnknownPresence {
                presence: "quiescing".to_owned()
            }
        );
    }

    #[test]
    fn a_draining_presence_reconnects_rather_than_claiming_a_port_conflict() {
        // The defect this release exists for. `draining` used to arrive as
        // `foreign`, and `foreign` carries a message accusing MCC's own
        // process of not being the MCC server.
        let status = status_with(|document| {
            document["server_presence"] = serde_json::json!("draining");
            // Python does not send a port_conflict for a drain, but even if a
            // stale one arrived it must not be what the window shows.
            document["port_conflict"] =
                serde_json::json!("python.exe (pid 42112) is not the MCC server.");
        });
        assert_eq!(decide(&status), Decision::Draining);
        let message = draining_message(&status);
        assert!(message.contains("shutting down"));
        assert!(message.contains("22 minutes"), "{message}");
        assert!(
            !message.contains("pid"),
            "a drain must never be reported as a port conflict"
        );
    }

    #[test]
    fn reconnect_verdict_asks_for_a_restatus_at_the_documents_cadence() {
        // C9: the cadence is 30s because the document says 30s, not because
        // this binary counts to six.
        let status = status_with(|_| {});
        assert!(!should_restatus(&status, 0.0));
        assert!(!should_restatus(&status, 29.9));
        assert!(should_restatus(&status, 30.0));
        assert!(should_restatus(&status, 120.0));

        let brisk = status_with(|document| {
            document["reconnect_restatus_seconds"] = serde_json::json!(5.0);
        });
        assert!(!should_restatus(&brisk, 4.9));
        assert!(should_restatus(&brisk, 5.0));

        // A document asking for a re-read faster than once a second is asking
        // for a process storm; the floor is a guard, not a policy.
        let absurd = status_with(|document| {
            document["reconnect_restatus_seconds"] = serde_json::json!(0.0);
        });
        assert!(!should_restatus(&absurd, 0.5));
        assert!(should_restatus(&absurd, 1.0));
    }

    #[test]
    fn a_respawn_is_offered_at_most_once_per_reconnect_episode() {
        let free_and_ours = status_with(|document| {
            document["server_presence"] = serde_json::json!("free");
            document["server_mode"] = serde_json::json!("spawn");
        });
        assert_eq!(respawn_verdict(&free_and_ours, false), Respawn::Start);
        // Having used its one, the episode never asks again -- which is what
        // keeps a server that crash-loops on start from being restarted every
        // thirty seconds for seventeen minutes.
        assert_eq!(respawn_verdict(&free_and_ours, true), Respawn::Hold);
    }

    #[test]
    fn only_the_unambiguous_answer_starts_a_server() {
        for (presence, mode) in [
            ("free", "attach"),
            ("free", "off"),
            ("healthy", "spawn"),
            ("foreign", "spawn"),
            ("draining", "spawn"),
            ("quiescing", "spawn"),
        ] {
            let status = status_with(|document| {
                document["server_presence"] = serde_json::json!(presence);
                document["server_mode"] = serde_json::json!(mode);
            });
            assert_eq!(
                respawn_verdict(&status, false),
                Respawn::Hold,
                "{presence}/{mode} must not start a second server"
            );
        }
    }

    #[test]
    fn reconnect_progress_text_names_elapsed_remaining_and_the_last_failure() {
        let status = status_with(|_| {});
        let text = reconnect_progress_text(&status, 200.0, 2.0, "connection refused", None);
        assert!(text.contains("3 m 20 s"), "{text}");
        assert!(text.contains("22 m"), "{text}");
        assert!(text.contains("18 m 40 s"), "{text}");
        assert!(text.contains("2 s ago"), "{text}");
        assert!(text.contains("connection refused"), "{text}");
        assert!(!text.contains("Update:"));

        // The stage the Windows update helper reported, when there is one.
        let with_stage = reconnect_progress_text(
            &status,
            200.0,
            2.0,
            "shutting down",
            Some("Installing the new version."),
        );
        assert!(with_stage.contains("Update: Installing the new version."));
        assert!(with_stage.contains("shutting down"));
    }

    #[test]
    fn the_countdown_only_ever_decreases_and_never_goes_negative() {
        let status = status_with(|_| {});
        let mut previous = f64::MAX;
        for tick in 0..300 {
            let elapsed = f64::from(tick) * 5.0;
            let remaining = (status.reconnect_timeout_seconds - elapsed).max(0.0);
            assert!(remaining <= previous, "remaining time moved backwards");
            assert!(remaining >= 0.0);
            previous = remaining;
            // And the text is rebuilt every tick rather than once.
            let text = reconnect_progress_text(&status, elapsed, 5.0, "no answer", None);
            assert!(text.contains("Last checked"), "{text}");
        }
        assert!((previous - 0.0).abs() < f64::EPSILON);
    }

    #[test]
    fn durations_read_as_a_person_would_say_them() {
        assert_eq!(human_duration(0.0), "0 s");
        assert_eq!(human_duration(45.4), "45 s");
        assert_eq!(human_duration(60.0), "1 m");
        assert_eq!(human_duration(200.0), "3 m 20 s");
        assert_eq!(human_duration(1320.0), "22 m");
        assert_eq!(human_duration(3600.0), "1 h");
        assert_eq!(human_duration(3720.0), "1 h 2 m");
        assert_eq!(human_duration(-5.0), "0 s");
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
