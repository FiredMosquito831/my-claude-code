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

/// One stage of an update, as the timeline draws it.
///
/// Every field is already worded here rather than in the page, because the page
/// is a renderer and a renderer that formats durations is a second place where
/// "how long did that take" is decided.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StageLine {
    /// The stage name, exactly as the installer wrote it.
    pub stage: String,
    /// The sentence beside it, when there was one.
    pub message: Option<String>,
    /// `HH:MM:SS` UTC, from the installer's own stamp.
    pub at: Option<String>,
    /// How long this stage took -- or, for the one still running, how long it
    /// has been running. `None` when the receipt carried no elapsed times.
    pub took: Option<String>,
    /// Whether this is the stage the installer is in right now.
    pub current: bool,
}

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
    ///
    /// Everything after `message` is 6.71.0's, and it is the user's ask in
    /// full: "we should see everything happening during an update". The window
    /// is the only process that CAN show it -- a dashboard in a browser tab
    /// cannot read a file on the disk, and during an update there is no server
    /// to ask -- so it shows the stage timeline with the writer's own stamps,
    /// how long the episode has run, the installer's own last lines as they are
    /// written, and where that transcript lives so the user can open it.
    Updating {
        message: String,
        /// The episode so far, oldest first.
        stages: Vec<StageLine>,
        /// Total elapsed, already worded ("1 m 32 s"). `None` before any
        /// writer recorded a start.
        elapsed: Option<String>,
        /// The installer transcript's path, verbatim from the receipt.
        log_path: Option<String>,
        /// Its last lines, as text. Inserted with `textContent`: an installer's
        /// output is untrusted exactly as a server's stderr is.
        log_tail: Vec<String>,
        /// The helper, in one phrase: "installer pid 11372, running".
        helper: Option<String>,
    },
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
    ///
    /// `shell_log` is this window's own transcript -- the installer's two
    /// streams, and every line a server child printed. It exists because
    /// `server_log` is the file the *server* writes, and the states that
    /// most need explaining are the ones where no server ever got far
    /// enough to write one.
    Error {
        message: String,
        server_log: Option<String>,
        shell_log: Option<String>,
    },
    /// The server has been started its budget of times and has not answered.
    ///
    /// Deliberately not `Error`: nothing has ended. The tick is still
    /// starting a server every ten seconds and the page still counts down to
    /// the next attempt -- it has simply stopped being a spinner and says
    /// the exit code, the child's last words and where the logs are.
    /// `detail` is the server's own output and is inserted as text.
    ServerFailed {
        message: String,
        detail: String,
        server_log: Option<String>,
        shell_log: Option<String>,
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

    /// An `Updating` page with nothing in it, for the tests that only care
    /// that every variant carries a kind and a heading.
    fn bare_updating() -> Page {
        Page::Updating {
            message: String::new(),
            stages: Vec::new(),
            elapsed: None,
            log_path: None,
            log_tail: Vec::new(),
            helper: None,
        }
    }

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
            shell_log: Some("/tmp/shell.log".to_owned()),
        });
        assert!(with_log.contains("/var/log/mcc.log"));
        let without = render_script(&Page::Error {
            message: "no answer".to_owned(),
            server_log: None,
            shell_log: None,
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
    fn the_failed_page_escapes_the_servers_own_words() {
        // A server's stderr is untrusted text exactly as installer output is:
        // it can contain a provider's response, a filename a user chose, or a
        // traceback quoting either.
        let script = render_script(&Page::ServerFailed {
            message: "not answering".to_owned(),
            detail: "boom\");alert('x');//".to_owned(),
            server_log: None,
            shell_log: None,
        });
        assert!(script.contains("\"boom\\\");alert('x');//\""), "{script}");
        assert!(script.contains("\"kind\":\"server-failed\""), "{script}");
    }

    #[test]
    fn the_failed_page_names_both_logs_when_it_has_them() {
        let script = render_script(&Page::ServerFailed {
            message: "not answering".to_owned(),
            detail: "mcc-server exited with 1.".to_owned(),
            server_log: Some("/logs/server.log".to_owned()),
            shell_log: Some("/logs/desktop-server-start.log".to_owned()),
        });
        assert!(script.contains("/logs/server.log"), "{script}");
        assert!(
            script.contains("/logs/desktop-server-start.log"),
            "{script}"
        );
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
            bare_updating(),
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
                shell_log: None,
            },
            Page::ServerFailed {
                message: String::new(),
                detail: String::new(),
                server_log: None,
                shell_log: None,
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
            bare_updating(),
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
                shell_log: None,
            },
            Page::ServerFailed {
                message: String::new(),
                detail: String::new(),
                server_log: None,
                shell_log: None,
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
