//! What the window shows while it is not showing the dashboard.
//!
//! Every one of these pages is served by the shell itself, from the bundled
//! `ui/` directory, and none of them makes a network request. That is
//! contract C8: `require_loopback_admin` rejects a `file://` origin, so a
//! splash page that tried to `fetch()` the admin API would be refused, and a
//! page that only *reports* what Rust already knows never needs to.
//!
//! The transport is `eval`, not IPC, in one direction only. Rust pushes a
//! state object; the page renders it. The page talks back through exactly one
//! command (`shell_retry`), because a Retry button is the only thing the user
//! can do from here.

use serde::Serialize;

/// One screen. `kind` is the tag the page switches on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum Page {
    /// The first paint, before anything is known.
    Checking,
    /// The server is being started, and `/health` is being polled.
    Starting { message: String },
    /// MCC is not installed and the install script is running.
    ///
    /// It carries a message of its own because the page falls back to
    /// "Checking the server..." when a state has none -- so for the whole of a
    /// first install, which is minutes, this window used to say it was
    /// checking something. A spinner over a stale sentence is the shape of
    /// every "it just hangs" report there has ever been.
    Installing { command: String, message: String },
    /// An update helper is installing, and this window is watching it.
    ///
    /// Deliberately not `Installing`: that page names a command this window is
    /// running and streams its output, and titling this one "Installing My
    /// Claude Code" would say the window is doing the very thing it is
    /// carefully not doing. The distinction is the whole fix -- one installer
    /// at a time -- so the window says which one it is.
    Updating { message: String },
    /// The port is free but starting the server is not this windows job.
    NotOurServer { message: String },
    /// Someone else holds the port. The message is Pythons, verbatim.
    ///
    /// `take_port` is decision Q1: the button is offered only when the holder
    /// was identified, by process, as one of MCC's own. A genuinely foreign
    /// process is never killed without being asked, so the page says so and
    /// keeps re-checking instead.
    PortConflict { message: String, take_port: bool },
    /// The server was healthy and stopped answering; still inside the budget.
    Reconnecting { message: String },
    /// The end of the line. `server_log` is shown when there is one to name.
    Error {
        message: String,
        server_log: Option<String>,
    },
}

/// JavaScript that renders `page`, whether or not the document is ready yet.
///
/// A window that has just been told to navigate back to the local page may
/// still be showing the previous document when this arrives, so the script
/// leaves the state where the page will find it on load rather than assuming
/// a receiver exists.
pub fn render_script(page: &Page) -> String {
    let json = serde_json::to_string(page).unwrap_or_else(|_| "null".to_owned());
    format!(
        "(function(){{var state={json};window.__mccShellPending=state;\
         if(window.__mccShell&&window.__mccShell.render){{\
         window.__mccShell.render(state);}}}})()"
    )
}

/// JavaScript that appends one line of installer output.
pub fn append_output_script(line: &str) -> String {
    let json = serde_json::to_string(line).unwrap_or_else(|_| "\"\"".to_owned());
    format!(
        "(function(){{var line={json};\
         (window.__mccShellOutput=window.__mccShellOutput||[]).push(line);\
         if(window.__mccShell&&window.__mccShell.append){{\
         window.__mccShell.append(line);}}}})()"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_page_is_pushed_as_json_the_document_can_pick_up_late() {
        let script = render_script(&Page::Checking);
        assert!(script.contains("\"kind\":\"checking\""));
        assert!(
            script.contains("__mccShellPending"),
            "a page that arrives before the document must survive the wait"
        );
    }

    #[test]
    fn the_port_conflict_message_reaches_the_page_intact() {
        let script = render_script(&Page::PortConflict {
            message: "nginx (pid 42) holds it".to_owned(),
            take_port: false,
        });
        assert!(script.contains("nginx (pid 42) holds it"));
        assert!(script.contains("\"kind\":\"port-conflict\""));
    }

    #[test]
    fn the_error_page_names_the_log_when_there_is_one() {
        let with_log = render_script(&Page::Error {
            message: "no answer".to_owned(),
            server_log: Some("/var/log/mcc.log".to_owned()),
        });
        assert!(with_log.contains("/var/log/mcc.log"));
        let without = render_script(&Page::Error {
            message: "no answer".to_owned(),
            server_log: None,
        });
        assert!(without.contains("\"server_log\":null"));
    }

    #[test]
    fn the_install_page_shows_the_exact_command() {
        let command = crate::install::install_command("linux");
        let script = render_script(&Page::Installing {
            command: command.display.clone(),
            message: "Installing My Claude Code.".to_owned(),
        });
        assert!(script.contains("curl -fsSL"));
        assert!(script.contains("install.sh"));
        // And it says what it is doing rather than leaving the page on its
        // "Checking the server..." fallback for the length of an install.
        assert!(script.contains("Installing My Claude Code."), "{script}");
    }

    #[test]
    fn a_line_of_output_is_escaped_rather_than_concatenated() {
        // Installer output is untrusted text as far as this page is concerned.
        let script = append_output_script("done\");alert('x');//");
        // The quote that would have closed the string literal is escaped, so
        // the payload stays one JSON string instead of becoming statements.
        assert!(script.contains("\"done\\\");alert('x');//\""), "{script}");
        // And a newline cannot break out of the line either.
        assert!(append_output_script("a\nb").contains("\"a\\nb\""));
    }

    #[test]
    fn every_page_carries_a_kind_the_document_can_switch_on() {
        let pages = [
            Page::Checking,
            Page::Starting {
                message: String::new(),
            },
            Page::Installing {
                command: String::new(),
                message: String::new(),
            },
            Page::Updating {
                message: String::new(),
            },
            Page::NotOurServer {
                message: String::new(),
            },
            Page::PortConflict {
                message: String::new(),
                take_port: false,
            },
            Page::Reconnecting {
                message: String::new(),
            },
            Page::Error {
                message: String::new(),
                server_log: None,
            },
        ];
        for page in pages {
            assert!(render_script(&page).contains("\"kind\":\""), "{page:?}");
        }
    }

    #[test]
    fn every_kind_has_a_heading_in_the_page_that_renders_it() {
        // The page falls back to the product name for a kind it has never
        // heard of, so a new variant does not break anything -- it just wears
        // somebody else's heading, or none. `Updating` was added in 6.58.3 for
        // exactly the reason a fallback is not good enough: it must not say
        // "Installing My Claude Code" while the whole point is that this
        // window is NOT installing anything.
        let document = include_str!("../../ui/index.html");
        for page in [
            Page::Checking,
            Page::Starting {
                message: String::new(),
            },
            Page::Installing {
                command: String::new(),
                message: String::new(),
            },
            Page::Updating {
                message: String::new(),
            },
            Page::NotOurServer {
                message: String::new(),
            },
            Page::PortConflict {
                message: String::new(),
                take_port: false,
            },
            Page::Reconnecting {
                message: String::new(),
            },
            Page::Error {
                message: String::new(),
                server_log: None,
            },
        ] {
            let json = serde_json::to_string(&page).expect("a page serializes");
            let kind = json
                .split("\"kind\":\"")
                .nth(1)
                .and_then(|rest| rest.split('"').next())
                .expect("a kind");
            assert!(
                document.contains(&format!("{kind}:"))
                    || document.contains(&format!("\"{kind}\":")),
                "ui/index.html has no heading for the {kind} page"
            );
        }
    }
}
